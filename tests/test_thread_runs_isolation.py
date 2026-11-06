"""Isolation of concurrent runs of the same agent thread (ThreadRunRegistry).

Verifies the **run-latest-wins** strategy end to end at the registry level:

- A new request for a ``thread_id`` supersedes (cancels) an in-flight run.
- Each run forks from the last successfully completed checkpoint (HEAD), so the
  intermediate checkpoints of a cancelled run are never inherited.
- HEAD is updated only on successful completion; ``CancelledError`` leaves it intact.
- Multi-turn history stays consistent: a cancelled run leaves no artifacts in the
  history that the next successful run continues.

A fake graph (instead of the real ``MemorySaver``+LangGraph stack) produces
``asyncio``-streams of checkpoints/values events, mirroring the contract of
``graph.astream(stream_mode=["values", "checkpoints"])`` in v1.x.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aigw_service.api.v1.thread_runs import (
    ThreadRunRegistry,
    get_thread_run_registry,
)


class FakeGraph:
    """Emits checkpoint/values events like ``agent.graph.astream`` with MemorySaver.

    Parameters
    ----------
    checkpoint_ids
        Order in which the run creates top-level checkpoints (empty ``checkpoint_ns``).
    final_message
        The final ``values``-state message (the answer the run would produce).
    gate
        Optional ``asyncio.Event`` awaited after the first checkpoint, used to hold a
        run in-flight so a newer request can supersede it.
    """

    def __init__(
        self,
        checkpoint_ids: list[str],
        final_message: str,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.checkpoint_ids = checkpoint_ids
        self.final_message = final_message
        self.gate = gate
        self.started = asyncio.Event()
        self.received_config: dict[str, Any] | None = None

    def stream_fn(self) -> "FakeGraph._stream":
        """Factory accepted by ``ThreadRunRegistry.run_latest_wins``."""
        return self._stream

    async def _stream(self, config: dict[str, Any]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.received_config = dict(config)

        # Mirror LangGraph: each checkpoint appears once with its pending "next" nodes and
        # again as a closing event with an empty "next" once the graph stops there.
        def _event(
            checkpoint_id: str, next_nodes: list[str], values: dict[str, Any] | None = None
        ) -> tuple[str, dict[str, Any]]:
            return (
                "checkpoints",
                {
                    "config": {"configurable": {"checkpoint_id": checkpoint_id, "checkpoint_ns": ""}},
                    "next": next_nodes,
                    "values": values or {"messages": []},
                },
            )

        yield _event("turn-input", [])
        last_index = len(self.checkpoint_ids) - 1
        for index, checkpoint_id in enumerate(self.checkpoint_ids):
            pending_next = [] if index == last_index else ["orchestrator"]
            yield _event(checkpoint_id, pending_next)
            yield ("values", {"messages": [f"{checkpoint_id}:step"]})
            if self.gate is not None and index == 0:
                self.started.set()
                await self.gate.wait()
        if self.checkpoint_ids:
            yield _event(self.checkpoint_ids[-1], [], {"messages": ["processed"]})
        yield ("values", {"messages": [self.final_message]})

    def received_checkpoint_id(self) -> str | None:
        if self.received_config is None:
            return None
        configurable = self.received_config.get("configurable") or {}
        return configurable.get("checkpoint_id")


def _checkpoint(
    checkpoint_id: str, checkpoint_ns: str = "", pending: list[str] | None = None
) -> tuple[str, dict[str, Any]]:
    return (
        "checkpoints",
        {
            "config": {"configurable": {"checkpoint_id": checkpoint_id, "checkpoint_ns": checkpoint_ns}},
            "next": pending or [],
        },
    )


def _values(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return ("values", state)


def _config(thread_id: str, user_id: str = "u") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


class TestThreadRunIsolation:
    """run-latest-wins: supersession, HEAD update, fork isolation (task 4.x)."""

    @pytest.mark.asyncio
    async def test_latest_win_supersedes_active_run_and_updates_head(self):
        """Request B cancels in-flight A; HEAD advances only on B's success.

        Scenario (spec, 4.1): A creates checkpoints, B cancels A, HEAD stays at the last
        successfully completed value, then a successful run advances it. The cancelled
        run is aborted with ``asyncio.CancelledError`` so it produces no HTTP response
        (a superseded request must not yield any reply to the client).
        """
        thread_id = "iso:a-b"
        registry = ThreadRunRegistry(None)

        gate_a = asyncio.Event()
        fake_a = FakeGraph(["c1", "c2", "c3"], "A-FINAL", gate=gate_a)
        task_a = asyncio.create_task(registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_a.stream_fn()))
        await fake_a.started.wait()  # A is mid-flight, having created c1
        assert registry._head(thread_id) is None, "in-flight run must not have advanced HEAD"

        fake_b = FakeGraph(["b1", "b2"], "B-ANSWER")
        state_b = await registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_b.stream_fn())

        assert state_b == {"messages": ["B-ANSWER"]}
        assert registry._head(thread_id) == "b2", "successful run must advance HEAD to its last checkpoint_elem"

        # A was superseded: it must be cancelled (thus producing no HTTP response),
        # not complete successfully.
        with pytest.raises(asyncio.CancelledError):
            await task_a
        assert registry._head(thread_id) == "b2", "cancelled run must not change HEAD"

    @pytest.mark.asyncio
    async def test_next_run_forks_from_head_not_from_cancelled_checkpoints(self):
        """Run C after a cancelled A forks from HEAD, not from A's c1/c2/c3 (spec 4.1)."""
        thread_id = "iso:c"
        registry = ThreadRunRegistry(None)

        gate_a = asyncio.Event()
        fake_a = FakeGraph(["c1", "c2", "c3"], "A-FINAL", gate=gate_a)
        task_a = asyncio.create_task(registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_a.stream_fn()))
        await fake_a.started.wait()

        # B wins and establishes HEAD = b2.
        fake_b = FakeGraph(["b1", "b2"], "B-ANSWER")
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_b.stream_fn())
        with pytest.raises(asyncio.CancelledError):
            await task_a

        # C must fork from the last successfully completed state ("b2"), never c1.
        fake_c = FakeGraph(["c9"], "C-ANSWER")
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_c.stream_fn())

        assert fake_c.received_checkpoint_id() == "b2", "C must start from HEAD, not A's checkpoints"

    @pytest.mark.asyncio
    async def test_success_updates_head_to_final_checkpoint(self):
        """Successful run records the final checkpoint_id as HEAD (spec HEAD-update)."""
        thread_id = "iso:success"
        registry = ThreadRunRegistry(None)

        state = await registry.run_latest_wins(
            thread_id, "u", _config(thread_id), FakeGraph(["x1", "x2", "x3"], "OK").stream_fn()
        )

        assert state == {"messages": ["OK"]}
        assert registry._head(thread_id) == "x3", "HEAD must be the final checkpoint_id of a successful run"

    @pytest.mark.asyncio
    async def test_cancellation_does_not_change_head(self):
        """CancelledError must not update HEAD (spec HEAD-update, cancellation scenario).

        A cancelled run's own intermediate checkpoint must never become HEAD: after it
        is superseded, HEAD reflects the winning run's last successful checkpoint, not
        the cancelled run's ``partial`` one.
        """
        thread_id = "iso:no-head-change"
        registry = ThreadRunRegistry(None)

        # Start a slow run A that creates an intermediate checkpoint, then gets superseded.
        gate_a = asyncio.Event()
        fake_a = FakeGraph(["partial"], "NEVER", gate=gate_a)
        task_a = asyncio.create_task(registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_a.stream_fn()))
        await fake_a.started.wait()

        # Winning run B supersedes A and completes successfully.
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), FakeGraph(["winner1"], "WIN").stream_fn())
        with pytest.raises(asyncio.CancelledError):
            await task_a

        assert registry._head(thread_id) == "winner1", (
            "HEAD must come from the winning run, never from the cancelled run's 'partial'"
        )

    @pytest.mark.asyncio
    async def test_cancelled_run_leaves_no_artifacts_and_history_is_consistent(self):
        """Multi-turn continuity: cancelled A leaves no artifacts (spec multi-turn).

        When A is cancelled mid-turn, its checkpoints are neither used as HEAD nor as a
        fork base for the next successful run B; B's history starts from the state before A.
        """
        thread_id = "iso:history"
        registry = ThreadRunRegistry(None)

        # Turn 1 completes successfully: history ["user"] is committed as HEAD h1.
        history1 = FakeGraph(["h1"], "turn1-ok")
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), history1.stream_fn())
        assert registry._head(thread_id) == "h1"

        # Turn 2 (A) is cancelled after creating an intermediate checkpoint.
        gate_a = asyncio.Event()
        fake_a = FakeGraph(["a-intermediate"], "turn2-A-never", gate=gate_a)
        task_a = asyncio.create_task(registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_a.stream_fn()))
        await fake_a.started.wait()

        # Turn 3 (B) wins and completes; it must fork from h1 (turn 1), not a-intermediate.
        fake_b = FakeGraph(["h3"], "turn3-ok")
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_b.stream_fn())
        with pytest.raises(asyncio.CancelledError):
            await task_a

        assert fake_b.received_checkpoint_id() == "h1", (
            "B must continue from the last successfully completed run, ignoring A's artifacts"
        )
        assert registry._head(thread_id) == "h3"

    @pytest.mark.asyncio
    async def test_second_run_after_first_cancelled_forks_from_clean_state(self):
        """First run is cancelled before completing — HEAD stays None at cancellation time.

        In production (with real checkpointer) a UUID clean slate checkpoint is created
        and B forks from it. In the test environment (checkpointer=None) the fallback
        path is used: no checkpoint_id is passed, so the test only verifies that B does
        NOT fork from A's partial checkpoint.
        """
        thread_id = "iso:clean-slate"
        registry = ThreadRunRegistry(None)

        # --- first run (A) starts but gets superseded before completing ---
        gate_a = asyncio.Event()
        fake_a = FakeGraph(["a-partial"], "A-NEVER", gate=gate_a)
        task_a = asyncio.create_task(registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_a.stream_fn()))
        await fake_a.started.wait()  # A has created a-partial

        # HEAD is still None — no successful completion yet.
        assert registry._head(thread_id) is None, "HEAD must stay None before any successful completion"

        # --- second run (B) supersedes A ---
        fake_b = FakeGraph(["b1"], "B-ANSWER")
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_b.stream_fn())

        with pytest.raises(asyncio.CancelledError):
            await task_a

        # In test environment (checkpointer=None) no checkpoint_id is passed.
        # Key assertion: B must NOT receive "a-partial" (the partial checkpoint from A).
        received_cp = fake_b.received_checkpoint_id()
        assert received_cp != "a-partial", (
            f"Second run must NOT fork from A's partial checkpoint, got {received_cp!r}"
        )

        # HEAD is now updated to B's last checkpoint — correct multi-turn behavior.
        assert registry._head(thread_id) == "b1"

        # --- third run (C) must fork from B's checkpoint (HEAD=b1) ---
        fake_c = FakeGraph(["c1"], "C-ANSWER")
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), fake_c.stream_fn())
        assert fake_c.received_checkpoint_id() == "b1", "C must fork from HEAD (b1), not from A's partial checkpoint"


class TestCheckpointFiltering:
    """Empty-``checkpoint_ns`` filtering used to compute the thread HEAD (task 1.2/1.4)."""

    @pytest.mark.asyncio
    async def test_only_empty_ns_checkpoints_advance_head(self):
        """Nested (non-empty checkpoint_ns) checkpoints must not become HEAD."""
        registry = ThreadRunRegistry(None)
        thread_id = "iso:nested"

        def stream_fn(_config: dict[str, Any]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
            async def _stream() -> AsyncIterator[tuple[str, dict[str, Any]]]:
                # Top-level checkpoints (empty ns) interleaved with nested subgraph ones.
                yield _checkpoint("top-1")
                yield _checkpoint("nested-1", checkpoint_ns="__inner__")
                yield _checkpoint("nested-2", checkpoint_ns="__inner__")
                yield _checkpoint("top-2")
                yield _values({"messages": ["done"]})

            return _stream()

        await registry.run_latest_wins(thread_id, "u", _config(thread_id), stream_fn)
        assert registry._head(thread_id) == "top-2", "nested checkpoints must be ignored for HEAD"

    @pytest.mark.asyncio
    async def test_mid_turn_checkpoint_only_commits_when_graph_stops_there(self):
        """A run that stops on a checkpoint with pending nodes makes that checkpoint HEAD.

        Forking there resumes exactly the pending task, which is how the agent graph's
        final (non-message-writing) run is represented — the state it presents to the
        client lives on the next checkpoint. A run *cancelled* mid-turn never gets here:
        its pending checkpoint is never closed by a stopping event.
        """
        registry = ThreadRunRegistry(None)
        thread_id = "iso:mid-turn"

        def stream_fn(_config: dict[str, Any]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
            async def _stream() -> AsyncIterator[tuple[str, dict[str, Any]]]:
                yield _values({"messages": ["start"]})
                yield _checkpoint("done-1")
                yield _checkpoint("pending-2", pending=["tool_executor"])
                yield _checkpoint("pending-2")
                yield _values({"messages": ["mid"]})

            return _stream()

        await registry.run_latest_wins(thread_id, "u", _config(thread_id), stream_fn)
        assert registry._head(thread_id) == "pending-2", "the run stopped on the pending checkpoint"


class TestRegistrySingleton:
    """Process-wide singleton registry (task 1.5)."""

    def test_singleton_returns_same_instance(self):
        first = get_thread_run_registry()
        second = get_thread_run_registry()
        assert first is second, "registry must be a process-wide singleton"

    @pytest.mark.asyncio
    async def test_head_persists_between_calls_on_same_instance(self):
        """HEAD tracked by the singleton must persist between run_latest_wins calls."""
        registry = get_thread_run_registry()
        thread_id = "iso:singleton"

        await registry.run_latest_wins(thread_id, "u", _config(thread_id), FakeGraph(["s1"], "ok1").stream_fn())
        assert registry._head(thread_id) == "s1"

        await registry.run_latest_wins(thread_id, "u", _config(thread_id), FakeGraph(["s2"], "ok2").stream_fn())
        assert registry._head(thread_id) == "s2"

        # Clean up so other tests see a pristine registry for this thread.
        registry._runs.pop(thread_id, None)
