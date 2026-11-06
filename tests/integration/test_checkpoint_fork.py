"""Integration tests for forking a real LangGraph graph from ``checkpoint_id`` (HEAD).

Task 3.1/3.2 of the ``thread-runs-isolation`` change: the registry hands the winning
run ``config["configurable"]["checkpoint_id"] = HEAD``. This must be honoured by the real
``MemorySaver`` checkpointer: the run resumes from HEAD, and the whole configurable block
(``thread_id``/``user_id``/``checkpoint_id``) reaches the graph unchanged.

A minimal two-node ``StateGraph`` (``step -> final``, ``messages`` reduced with
``add_messages``, ``MemorySaver`` checkpointer, streamed with
``stream_mode=["values", "checkpoints"]``) mirrors the data path of ``AgentGraph``
without needing an LLM or an Excel model.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph, add_messages

from aigw_service.api.v1.thread_runs import RunStreamFn, ThreadRunRegistry


class _ChatState(TypedDict):
    messages: Annotated[list, add_messages]


class _MiniGraph:
    """Two-node graph: records the turn (``step``) then answers with the full history.

    Checkpoints are created by ``MemorySaver`` on every superstep, mimicking a real
    multi-node agent run.
    """

    def __init__(self) -> None:
        self.checkpointer = MemorySaver()
        workflow = StateGraph(_ChatState)
        workflow.add_node("step", self._step)
        workflow.add_node("final", self._final)
        workflow.add_edge(START, "step")
        workflow.add_edge("step", "final")
        workflow.add_edge("final", END)
        self.graph = workflow.compile(checkpointer=self.checkpointer)

    async def _step(self, state: _ChatState) -> dict:
        return {"messages": [AIMessage(content="processed")]}

    async def _final(self, state: _ChatState) -> dict:
        # The answer reflects the whole history: proves which checkpoint the run forked from.
        history = " | ".join(getattr(m, "content", "") for m in state["messages"])
        return {"messages": [AIMessage(content=f"answer({history})")]}

    def stream_fn(self, message: str) -> RunStreamFn:
        """Return a ``stream_fn`` accepted by ``ThreadRunRegistry.run_latest_wins``."""

        def _stream(config: Mapping[str, Any]) -> AsyncIterator[tuple[str, Mapping[str, Any]]]:
            return self.graph.astream(
                {"messages": [HumanMessage(content=message)]},
                config=config,
                stream_mode=["values", "checkpoints"],
            )

        return _stream

    async def run_and_capture_head(self, registry: ThreadRunRegistry, thread_id: str, config: dict, message: str) -> str:
        """Run one turn through the registry and return the HEAD it commits."""
        await registry.run_latest_wins(thread_id, "u", config, self.stream_fn(message))
        head = registry._head(thread_id)
        assert head is not None
        return head

    def captured_stream_fn(self, message: str, sink: dict[str, Any]) -> RunStreamFn:
        """Like :meth:`stream_fn`, but records the config handed to ``astream``."""
        inner = self.stream_fn(message)

        def _stream(config: Mapping[str, Any]) -> AsyncIterator[tuple[str, Mapping[str, Any]]]:
            sink.update(config)
            return inner(config)

        return _stream

    async def history(self, thread_id: str, checkpoint_id: str | None = None) -> list[str]:
        configurable: dict[str, Any] = {"thread_id": thread_id}
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        snapshot = await self.graph.aget_state({"configurable": configurable})
        return [getattr(m, "content", "") for m in snapshot.values.get("messages", [])]


def _config(thread_id: str, user_id: str = "u") -> dict:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


def _contents(state: Mapping[str, Any]) -> list[str]:
    return [getattr(m, "content", "") for m in state["messages"]]


class TestCheckpointIdFork:
    """``checkpoint_id`` in ``configurable`` selects the base state (task 3.1)."""

    @pytest.mark.asyncio
    async def test_run_with_head_checkpoint_id_forks_from_head_not_latest(self):
        """A run configured with ``checkpoint_id=HEAD`` resumes from HEAD, not the latest checkpoint."""
        mini = _MiniGraph()
        registry = ThreadRunRegistry(None)
        thread_id = "fork:head"

        first_sink: dict[str, Any] = {}
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), mini.captured_stream_fn("turn1", first_sink))
        head1 = registry._head(thread_id)
        assert head1 is not None, "a completed turn must commit HEAD"
        assert "checkpoint_id" not in (first_sink.get("configurable") or {}), "first run has no HEAD yet"

        # A run that forgets HEAD would resume from the thread's latest checkpoint. The
        # next turn proves it does not: the fork base carries checkpoint_id == head1.
        second_sink: dict[str, Any] = {}
        state2 = await registry.run_latest_wins(
            thread_id, "u", _config(thread_id), mini.captured_stream_fn("turn2", second_sink)
        )

        assert second_sink["configurable"]["checkpoint_id"] == head1, "run must fork from HEAD"
        # The fork base is exactly the HEAD state, not anything written later.
        head1_history = await mini.history(thread_id, head1)
        assert head1_history == ["turn1", "processed", "answer(turn1 | processed)"]
        # The next run's answer is built from the HEAD history, proving the fork base.
        assert _contents(state2)[-1] == "answer(turn1 | processed | answer(turn1 | processed) | turn2 | processed)"
        assert await mini.history(thread_id, head1) == head1_history, "forking must not rewrite HEAD"

    @pytest.mark.asyncio
    async def test_first_run_of_thread_has_no_checkpoint_id(self):
        """With an empty registry the run starts fresh: history holds only this turn."""
        mini = _MiniGraph()
        registry = ThreadRunRegistry(None)
        thread_id = "fork:fresh"

        state = await registry.run_latest_wins(thread_id, "u", _config(thread_id), mini.stream_fn("first"))

        contents = _contents(state)
        assert contents[0] == "first"
        assert len(contents) == 3  # human + processed + answer


class TestConfigPropagation:
    """The configurable block survives config forwarding down to the stream (task 3.2)."""

    @pytest.mark.asyncio
    async def test_nested_runnable_sees_checkpoint_id(self):
        """The runnable streamed by the registry sees ``thread_id``, ``user_id`` and ``checkpoint_id``."""
        mini = _MiniGraph()
        registry = ThreadRunRegistry(None)
        thread_id = "fork:propagate"

        await registry.run_latest_wins(thread_id, "u", _config(thread_id), mini.stream_fn("t1"))
        head = registry._head(thread_id)
        assert head is not None

        sink: dict[str, Any] = {}
        await registry.run_latest_wins(thread_id, "u", _config(thread_id), mini.captured_stream_fn("t2", sink))

        assert sink["configurable"]["thread_id"] == thread_id
        assert sink["configurable"]["user_id"] == "u"
        assert sink["configurable"]["checkpoint_id"] == head, "checkpoint_id must be forwarded unchanged"
