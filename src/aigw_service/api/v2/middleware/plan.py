"""ForcePlanFirst (FR-3, FR-4, ADR-3, spec §8 правила 2 и 9, №11): no tool work before the turn has a plan.

Until ``todos`` is non-empty the model sees only ``write_todos`` and ``read_file`` with ``tool_choice=auto``
(GigaChat cannot force "any of the set" — spike S0.2). In ``always`` an answer without a tool call is dropped and
the call is repeated once with ``tool_choice="write_todos"``; in ``auto`` it is the answer of a turn without a
plan. Any other tool call without a plan gets ``plan_required`` instead of running.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from aigw_service.api.v2.tools.envelope import ErrorCode, failure, to_json

PlanMode = Literal["always", "auto"]

WRITE_TODOS = "write_todos"
READ_FILE = "read_file"
SKILLS_ROOT = "/skills/"


def has_plan(state: Any) -> bool:
    """A plan of this turn, or of the previous one kept by TurnContext because it has ``blocked`` steps."""
    return bool((state or {}).get("todos"))


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        return tool.get("name") or (tool.get("function") or {}).get("name")
    return getattr(tool, "name", None)


def _allowed_without_plan(tool_call: dict[str, Any]) -> bool:
    if tool_call["name"] == WRITE_TODOS:
        return True
    if tool_call["name"] != READ_FILE:
        return False
    args = tool_call.get("args") or {}
    path = str(args.get("file_path") or args.get("path") or "")
    return path.startswith(SKILLS_ROOT) and ".." not in path


def _has_tool_calls(response: ModelResponse | AIMessage) -> bool:
    messages = [response] if isinstance(response, AIMessage) else response.result
    return any(isinstance(m, AIMessage) and m.tool_calls for m in messages)


def _plan_required(tool_call: dict[str, Any]) -> ToolMessage:
    return ToolMessage(
        to_json(failure(ErrorCode.PLAN_REQUIRED)),
        tool_call_id=tool_call["id"],
        name=tool_call["name"],
        status="error",
    )


class ForcePlanFirst(AgentMiddleware):
    """Placed right after ``TodoListMiddleware``; mode ``off`` means the middleware is not installed at all."""

    def __init__(self, mode: str) -> None:
        super().__init__()
        if mode not in ("always", "auto"):
            raise ValueError(f"ForcePlanFirst mode must be 'always' or 'auto', got {mode!r}")
        self.mode: PlanMode = mode  # type: ignore[assignment]

    def _narrow(self, request: ModelRequest) -> ModelRequest:
        tools = [t for t in request.tools if _tool_name(t) in (WRITE_TODOS, READ_FILE)]
        return request.override(tools=tools)

    def _needs_forcing(self, response: ModelResponse | AIMessage) -> bool:
        return self.mode == "always" and not _has_tool_calls(response)

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse | AIMessage:
        if has_plan(request.state):
            return handler(request)
        narrowed = self._narrow(request)
        response = handler(narrowed)
        if self._needs_forcing(response):  # the dropped answer never reaches the state (spike S0.2)
            response = handler(narrowed.override(tool_choice=WRITE_TODOS))
        return response

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse | AIMessage:
        if has_plan(request.state):
            return await handler(request)
        narrowed = self._narrow(request)
        response = await handler(narrowed)
        if self._needs_forcing(response):
            response = await handler(narrowed.override(tool_choice=WRITE_TODOS))
        return response

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        if has_plan(request.state) or _allowed_without_plan(request.tool_call):
            return handler(request)
        return _plan_required(request.tool_call)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        if has_plan(request.state) or _allowed_without_plan(request.tool_call):
            return await handler(request)
        return _plan_required(request.tool_call)
