"""ProgressReporter (ADR-14, FR-22, spec §8 №17а; S6.4 design D1–D3): progress the node updates do not carry.

Writes only ``custom`` parts in the format of S6.2 design D4 through ``runtime.stream_writer``:

- ``before_model`` — the phase of the turn (``planning`` / ``executing`` / ``answering``); ``checking`` comes from
  ``StreamTranslator`` itself, and repeats of a phase are dropped there too;
- ``wrap_tool_call`` — outcome ``retry`` when a calculation attempt failed and ``ToolRetryMiddleware`` will repeat it.

It sits right inside ``ToolRetryMiddleware`` (spike S6.1, P4): only there an engine retry is visible. It never
changes the turn, and under ``ainvoke`` ``stream_writer`` has no consumer, so ``/invoke-agent`` is unaffected.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from aigw_service.api.v2.middleware.limits import TOOL_CALLS, limit_hit
from aigw_service.api.v2.middleware.resilience import ENGINE_RETRIES, is_transient_tool_error
from aigw_service.api.v2.middleware.todos import OPEN

PROGRESS_KEY = "progress"
"""The key ``StreamTranslator`` reads ``custom`` parts by (S6.2 design D4)."""


def phase_of(state: Mapping[str, Any]) -> str:
    """Design D2: the first matching rule gives the phase before a model call."""
    if limit_hit(state):  # the next model call is the last one, without tools
        return "answering"
    todos = [t for t in state.get("todos") or [] if isinstance(t, Mapping)]
    if any(t.get("status") in OPEN for t in todos):
        return "executing"
    tool_calls = (state.get("guard_counters") or {}).get(TOOL_CALLS, 0)
    if todos and tool_calls:
        return "answering"
    if not tool_calls:  # also a closed plan kept from the previous turn
        return "planning"
    return "executing"  # tools were called without a plan (plan mode off)


def _tool_name(request: ToolCallRequest) -> str:
    return request.tool.name if request.tool is not None else request.tool_call["name"]


def _thread_id(request: ToolCallRequest) -> str:
    return str(getattr(request.runtime.context, "thread_id", ""))


class ProgressReporter(AgentMiddleware):
    """Placed right after ``engine_retry``: inside the engine retries, outside the tool adapter."""

    def __init__(self, engine_tools: Iterable[str], max_retries: int = ENGINE_RETRIES) -> None:
        super().__init__()
        self.engine_tools = frozenset(engine_tools)
        self.max_retries = max_retries
        self._attempts: dict[tuple[str, str], int] = {}
        """Failed attempts per ``(thread_id, tool_call_id)``: scripted call IDs repeat across sessions."""

    # ---------------------------------------------------------------- phase

    def before_model(self, state: dict[str, Any], runtime: Any) -> None:
        runtime.stream_writer({PROGRESS_KEY: "phase", "phase": phase_of(state)})

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> None:
        self.before_model(state, runtime)

    # ---------------------------------------------------------------- engine retry

    def _failed(self, key: tuple[str, str], request: ToolCallRequest, exc: Exception) -> None:
        """Writes ``retry`` when ``ToolRetryMiddleware`` will repeat the attempt: same predicate, attempts left."""
        attempt = self._attempts[key] = self._attempts.get(key, 0) + 1
        if is_transient_tool_error(exc) and attempt <= self.max_retries:
            code = getattr(exc, "code", None)
            part = {PROGRESS_KEY: "retry", "tool_call_id": request.tool_call["id"], "code": str(code)}
            request.runtime.stream_writer(part)
        else:  # the last or a non-retried failure: its outcome comes from the error envelope
            self._attempts.pop(key, None)

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        if _tool_name(request) not in self.engine_tools:
            return handler(request)
        key = (_thread_id(request), str(request.tool_call["id"]))
        try:
            result = handler(request)
        except Exception as exc:
            self._failed(key, request, exc)
            raise
        self._attempts.pop(key, None)
        return result

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        if _tool_name(request) not in self.engine_tools:
            return await handler(request)
        key = (_thread_id(request), str(request.tool_call["id"]))
        try:
            result = await handler(request)
        except Exception as exc:
            self._failed(key, request, exc)
            raise
        self._attempts.pop(key, None)
        return result
