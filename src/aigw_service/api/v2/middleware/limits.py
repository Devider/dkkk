"""TurnLimits (FR-9, spec §8 правило 8, №12–13): model, tool and calculation limits of one turn.

Deviation from §8 (design D1): not langchain's ``ModelCallLimitMiddleware`` / ``ToolCallLimitMiddleware`` —
they either end the turn with a fixed English message and no model call or keep the tools for the model.
Here, once any limit is hit, the model is called exactly once more without tools for a partial answer.

The counters live in ``guard_counters``, which ``TurnContext`` resets at the start of every turn, so they never
carry over to the next turn; the ``limit_hit`` flag there is also read by the plan middleware (design D2).
"""

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from aigw_service.api.v2.prompts.middleware import LIMIT_FALLBACK, LIMIT_PROMPT
from aigw_service.api.v2.tools.envelope import ErrorCode, failure, to_json
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

LIMIT_HIT = "limit_hit"
MODEL_CALLS, TOOL_CALLS, CALC_CALLS = "model_calls", "tool_calls", "calc_calls"


def limit_hit(state: Any) -> bool:
    """A limit of this turn is exhausted: the next model call is the last one, without tools."""
    return bool(((state or {}).get("guard_counters") or {}).get(LIMIT_HIT))


class LimitsState(AgentState):
    guard_counters: NotRequired[dict[str, int]]


class TurnLimits(AgentMiddleware):
    """Placed right after ``ForcePlanFirst``: inside it, so the tools it narrows to are dropped as well."""

    state_schema = LimitsState

    def __init__(self, max_model_calls: int, max_tool_calls: int, max_calcs: int, engine_tools: Iterable[str]) -> None:
        super().__init__()
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.max_calcs = max_calcs
        self.engine_tools = frozenset(engine_tools)

    # ---------------------------------------------------------------- counting

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        counters = dict(state.get("guard_counters") or {})
        counters[MODEL_CALLS] = counters.get(MODEL_CALLS, 0) + 1
        if counters[MODEL_CALLS] >= self.max_model_calls and not counters.get(LIMIT_HIT):
            logger.warning(
                "Turn limit: model call {} of {} is the last one", counters[MODEL_CALLS], self.max_model_calls
            )
            counters[LIMIT_HIT] = 1
        return {"guard_counters": counters}

    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        answer = messages[-1] if messages else None
        if not isinstance(answer, AIMessage) or not answer.tool_calls:
            return None
        counters = dict(state.get("guard_counters") or {})
        blocked: list[ToolMessage] = []
        for call in answer.tool_calls:
            calc = call["name"] in self.engine_tools
            over_tools = counters.get(TOOL_CALLS, 0) + 1 > self.max_tool_calls
            over_calcs = calc and counters.get(CALC_CALLS, 0) + 1 > self.max_calcs
            if counters.get(LIMIT_HIT) or over_tools or over_calcs:
                blocked.append(_limit_exceeded(call))
                counters[LIMIT_HIT] = 1
                continue
            counters[TOOL_CALLS] = counters.get(TOOL_CALLS, 0) + 1
            if calc:
                counters[CALC_CALLS] = counters.get(CALC_CALLS, 0) + 1
        update: dict[str, Any] = {"guard_counters": counters}
        if blocked:  # create_agent does not run a call that already has a ToolMessage
            logger.warning("Turn limit: {} tool call(s) blocked", len(blocked))
            update["messages"] = blocked
        return update

    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)

    # ---------------------------------------------------------------- the last call

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse | AIMessage:
        if not limit_hit(request.state):
            return handler(request)
        return _final(handler(_last_call(request)))

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse | AIMessage:
        if not limit_hit(request.state):
            return await handler(request)
        return _final(await handler(_last_call(request)))


def _limit_exceeded(call: dict[str, Any]) -> ToolMessage:
    return ToolMessage(
        to_json(failure(ErrorCode.LIMIT_EXCEEDED)), tool_call_id=call["id"], name=call["name"], status="error"
    )


def _last_call(request: ModelRequest) -> ModelRequest:
    """No tools, and the instruction only in this request — the history keeps the dialog as it was."""
    return request.override(tools=[], tool_choice=None, messages=[*request.messages, HumanMessage(LIMIT_PROMPT)])


def _final(response: ModelResponse | AIMessage) -> ModelResponse | AIMessage:
    """The answer ends the turn: a tool call it may still carry is dropped, an empty text gets a fixed one."""
    if isinstance(response, AIMessage):
        return _without_calls(response)
    return ModelResponse(
        result=[_without_calls(m) if isinstance(m, AIMessage) else m for m in response.result],
        structured_response=response.structured_response,
    )


def _without_calls(message: AIMessage) -> AIMessage:
    if not message.tool_calls and message.text.strip():
        return message
    text = message.text.strip() or LIMIT_FALLBACK
    return AIMessage(text, id=message.id, response_metadata=message.response_metadata)
