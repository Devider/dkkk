"""Isolation of concurrent runs of the same agent thread.

LangGraph's ``MemorySaver`` checkpointer partitions conversation history purely by
``thread_id``. If two requests for the same ``thread_id`` run in parallel they would
resume from (and write to) the same shared checkpoint, mixing their message histories.
``ThreadRunRegistry`` implements the **run-latest-wins** strategy: only the newest
request for a thread is allowed to run; a superseded run is cancelled quietly, and a
new run always forks from the checkpoint of the last successfully completed run
(HEAD) of that thread.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass
class RunInfo:
    """Per-thread bookkeeping: last successful checkpoint + the in-flight task."""

    HEAD: str | None = None
    task: asyncio.Task | None = None


# A stream of ``(mode, payload)`` events as produced by ``graph.astream(...)`` with
# ``stream_mode=["values", "checkpoints"]``. ``values`` payload is a state mapping;
# ``checkpoints`` payload carries ``config`` with ``checkpoint_id``/``checkpoint_ns``.
RunStreamFn = Callable[[Mapping[str, Any]], AsyncIterator[tuple[str, Mapping[str, Any]]]]


def _extract_checkpoint_info(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(checkpoint_id, checkpoint_ns)`` from a checkpoints-mode event payload.

    ``checkpoint_ns`` is empty for the top-level checkpoint of a thread; nested
    (sub-agent) checkpoints carry a non-empty namespace and are ignored when choosing
    the thread's HEAD.
    """
    config = payload.get("config") or {}
    configurable = config.get("configurable") or {}
    return configurable.get("checkpoint_id"), configurable.get("checkpoint_ns")


class _HeadTracker:
    """Picks the checkpoint a completed turn ends on from ``checkpoints`` events.

    LangGraph's checkpoint event is offset: the event for checkpoint ``C`` reports ``C``'s
    ``config`` (id, ``next``) but the state *before* ``C``'s tasks run. A commit point is a
    checkpoint the graph stops on with no pending nodes — a paused or finished run:

    * if the run wrote messages after the last pending checkpoint, that closing checkpoint
      comes last and is HEAD (HEAD reads its own, final, state);
    * otherwise (as with the agent graph, whose last node does not write messages) the run
      stops on the last mid-turn checkpoint; HEAD is that id, because forking there resumes
      exactly the task that was pending and produces the completed turn's state.

    Mid-run checkpoints of a run that is *cancelled* never become HEAD: a cancelled run
    stops in a checkpoint with pending nodes that no event ever shows being finished.
    """

    def __init__(self) -> None:
        self._last_pending: str | None = None
        self._closing: str | None = None
        self._values: Mapping[str, Any] | None = None

    def add(self, payload: Mapping[str, Any]) -> None:
        checkpoint_id, checkpoint_ns = _extract_checkpoint_info(payload)
        if checkpoint_id is None or checkpoint_ns != "":
            return
        if payload.get("next"):
            self._last_pending = checkpoint_id
            return
        # The state carried by this event is the completed turn's state.
        self._closing = checkpoint_id
        self._values = payload.get("values") or {}

    @property
    def last_completed(self) -> str | None:
        """Checkpoint id a completed turn ends on."""
        if self._closing is not None and self._values is not None and self._values.get("messages"):
            return self._closing
        return self._last_pending or self._closing

    @property
    def values(self) -> Mapping[str, Any] | None:
        """State committed with :attr:`last_completed`."""
        return self._values


class ThreadRunRegistry:
    """Process-wide registry of active runs keyed by ``thread_id``.

    One instance per process (see :func:`get_thread_run_registry`) so that the registry
    shares the same lifecycle as the process-local checkpointer.
    """

    def __init__(self, logger: Any | None = None) -> None:
        self._runs: dict[str, RunInfo] = {}
        self._logger = logger

    def _head(self, thread_id: str) -> str | None:
        info = self._runs.get(thread_id)
        return info.HEAD if info is not None else None

    def _log_info(self, message: str, *args: Any) -> None:
        if self._logger is not None:
            self._logger.info(message, *args)

    def _log_warning(self, message: str, *args: Any) -> None:
        if self._logger is not None:
            self._logger.warning(message, *args)

    async def run_latest_wins(
        self,
        thread_id: str,
        user_id: str,
        config: Mapping[str, Any],
        stream_fn: RunStreamFn,
    ) -> dict[str, Any]:
        """Run ``stream_fn`` for ``thread_id``, superseding any in-flight run.

        Steps:
        1. Cancel the previously active run's stream task (logged, never surfaced to a
           client). The cancelled run re-raises ``CancelledError``, which aborts its HTTP
           request without producing a response — a superseded request must not yield an
           HTTP reply, so the client receives only the winning run's answer.
        2. Fork from the last successfully completed checkpoint (HEAD) if present, by
           putting ``checkpoint_id`` into ``config["configurable"]``.
        3. Run the stream inside a shielded task so an outer cancellation cannot
           pre-maturely kill the graph; store the stream task in the registry.
        4. On successful completion update HEAD; on ``CancelledError`` leave HEAD intact
           and re-raise so the superseded request is aborted without an HTTP response.

        Returns the final ``values``-mode state for the caller to build a response from.
        """
        previous = self._runs.get(thread_id)
        if previous is not None and previous.task is not None and not previous.task.done():
            self._log_warning("Superseding active run for thread {} (user={})", thread_id, user_id)
            previous.task.cancel()

        current_head = self._head(thread_id)
        run_config: dict[str, Any] = dict(config)
        run_configurable = dict(run_config.get("configurable") or {})
        if current_head is not None:
            # Fork from last successfully completed checkpoint
            run_configurable["checkpoint_id"] = current_head
        else:
            # HEAD is None: generate a UUID to use as fork point.
            # This avoids loading partial checkpoints from cancelled runs.
            run_configurable["checkpoint_id"] = str(uuid.uuid4())
        run_config["configurable"] = run_configurable

        run_task = asyncio.create_task(self._consume(stream_fn(run_config), thread_id, user_id))
        self._runs[thread_id] = RunInfo(HEAD=current_head, task=run_task)

        try:
            return await asyncio.shield(run_task)
        except asyncio.CancelledError:
            # This run was superseded (a newer request cancelled our stream) or the
            # request was aborted. Cancel the graph stream so no stray computation is
            # left running, keep HEAD intact, and re-raise ``CancelledError``: it is a
            # BaseException so it escapes the router's ``except Exception`` handler and
            # aborts the HTTP request without producing any response. A superseded
            # request must not yield an HTTP reply, else the client might take it for
            # the winning run's answer.
            self._log_info("Run for thread {} cancelled (user={}); aborting without response", thread_id, user_id)
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
            raise

    async def _consume(
        self,
        stream: AsyncIterator[tuple[str, Mapping[str, Any]]],
        thread_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Consume astream events, tracking the final state and the thread's HEAD.

        HEAD is updated to the last ``checkpoint_id`` that belongs to a fully completed
        turn (see :class:`_HeadTracker`), and only when the stream completes without
        cancellation.
        """
        final_state: dict[str, Any] = {}
        tracker = _HeadTracker()
        try:
            async for mode, payload in stream:
                if mode == "values":
                    final_state = dict(payload)
                elif mode == "checkpoints":
                    tracker.add(payload)
        except asyncio.CancelledError:
            # Superseded: do not advance HEAD.
            self._log_info("Stream for thread {} cancelled before completion (user={})", thread_id, user_id)
            raise
        except Exception as e:
            self._log_info("Stream for thread {} failed: {}", thread_id, str(e))
            raise
        last_checkpoint_id = tracker.last_completed
        if last_checkpoint_id is not None:
            info = self._runs.get(thread_id)
            if info is not None:
                info.HEAD = last_checkpoint_id
            self._log_info("Advanced HEAD for thread {} to {} (user={})", thread_id, last_checkpoint_id, user_id)
        # The checkpoint event carries the state just *before* the committed (offset) state,
        # so the closing ``values`` event — emitted after the graph stops — is authoritative.
        # Falling back to the tracker keeps the registry usable with graphs that only emit
        # checkpoints.
        return final_state or dict(tracker.values or {})


@lru_cache(maxsize=1)
def get_thread_run_registry() -> ThreadRunRegistry:
    """Return the process-wide :class:`ThreadRunRegistry` singleton.

    Mirrors the ``@lru_cache`` on ``get_agent`` in ``router.py`` so the router and the
    tests share one registry instance (and therefore a single HEAD per thread).
    """

    return ThreadRunRegistry()
