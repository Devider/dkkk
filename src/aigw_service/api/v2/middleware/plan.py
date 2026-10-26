"""ForcePlanFirst and PlanCompletionGuard (FR-3, FR-4, FR-8, ADR-3, spec §8 правила 2, 7 и 9, №11 и №19).

ForcePlanFirst: no tool work before the turn has a plan.

Until ``todos`` is non-empty the model sees only ``write_todos`` and ``read_file`` with ``tool_choice=auto``
(GigaChat cannot force "any of the set" — spike S0.2). In ``always`` an answer without a tool call is dropped and
the call is repeated once with ``tool_choice="write_todos"``; in ``auto`` it is the answer of a turn without a
plan. Any other tool call without a plan gets ``plan_required`` instead of running.

PlanCompletionGuard: an answer while steps are ``pending`` / ``in_progress`` goes back to the model with a reminder
as a user message (the model's answer must not stay last — spike S0.2 C7), at most N times per turn; then the answer
is accepted with a «Не выполнено» section and the open steps are closed as ``skipped``.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NotRequired

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from aigw_service.api.v2.middleware.todos import OPEN, PlanState, plan_text
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


NOT_DONE_REASON = "Не выполнено к концу хода"
REMINDER = (
    "В плане остались незакрытые шаги:\n{steps}\n\nДоведи их: выполни, либо отметь `skipped` с причиной, "
    "либо `blocked` с причиной и вопросом пользователю. Затем дай финальный ответ."
)
NOT_DONE = "Не выполнено"
"""Russian whatever the question's language (FR-14)."""


class GuardState(PlanState):
    """The counter lives in the state, so it holds across model calls of the turn (TurnContext resets it)."""

    guard_counters: NotRequired[dict[str, int]]


class PlanCompletionGuard(AgentMiddleware):
    """Last in the stack, so its ``after_model`` checks the answer first (spec §8 №19)."""

    state_schema = GuardState

    def __init__(self, max_returns: int) -> None:
        super().__init__()
        self.max_returns = max_returns

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        answer = messages[-1] if messages else None
        if not isinstance(answer, AIMessage) or answer.tool_calls:
            return None
        todos = state.get("todos") or []
        open_steps = [t for t in todos if t.get("status") in OPEN]
        if not open_steps:
            return None
        counters = state.get("guard_counters") or {}
        returns = counters.get("plan_returns", 0)
        if returns < self.max_returns:
            return {
                "messages": [HumanMessage(REMINDER.format(steps=plan_text(open_steps)))],
                "guard_counters": {**counters, "plan_returns": returns + 1},
                "jump_to": "model",
            }
        listed = "\n".join(f"- {t['content']}" for t in open_steps)
        closed = [
            {**t, "status": "skipped", "reason": NOT_DONE_REASON} if t.get("status") in OPEN else t for t in todos
        ]
        final = AIMessage(f"{answer.text.rstrip()}\n\n**{NOT_DONE}:**\n{listed}", id=answer.id)
        return {"messages": [final], "todos": closed}

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
