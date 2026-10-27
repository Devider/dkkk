"""Cancellation (AR-Q10, AR-Q12, spec §8 №3, §10 «Контракт» п. 8; design D3): turn deadline and client disconnect.

Checks run only between calls, never inside one, so the checkpoint stays consistent: a model call or a
calculation that already started is finished. After the deadline no new tool runs (``cancelled``) and the next
model call is the last one, without tools — the same last call as ``TurnLimits`` (S2.5). After a disconnect no
new tool runs either, and the turn ends before the next model call with a fixed message: nobody reads it.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from aigw_service.api.v2.middleware.limits import LIMIT_HIT, TURN_TIMEOUT, LimitsState, limit_hit
from aigw_service.api.v2.prompts.middleware import CANCELLED_ANSWER
from aigw_service.api.v2.tools.envelope import ErrorCode, failure, to_json
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()


def _now() -> float:
    """Monotonic clock; unit tests move it instead of waiting."""
    return time.monotonic()


def _expired(context: Any) -> bool:
    deadline = getattr(context, "deadline", None)
    return deadline is not None and _now() >= deadline


def _cancelled(context: Any) -> bool:
    event = getattr(context, "cancel", None)
    return event is not None and event.is_set()


class Cancellation(AgentMiddleware):
    """Placed right after ``LanguageGuard``: outside every tool wrapper, so a stopped call runs nothing."""

    state_schema = LimitsState

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        context = runtime.context
        if _cancelled(context):
            logger.warning("Turn cancelled: the client disconnected, ending without a model call")
            return {"messages": [AIMessage(CANCELLED_ANSWER)], "jump_to": "end"}
        if _expired(context) and not limit_hit(state):
            logger.warning("Turn deadline passed: the next model call is the last one")
            counters = dict(state.get("guard_counters") or {})
            counters[LIMIT_HIT] = counters[TURN_TIMEOUT] = 1
            return {"guard_counters": counters}
        return None

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        context = request.runtime.context
        if _cancelled(context) or _expired(context):
            call = request.tool_call
            logger.warning("Tool call {} not run: the turn is stopped", call["name"])
            return ToolMessage(
                to_json(failure(ErrorCode.CANCELLED)), tool_call_id=call["id"], name=call["name"], status="error"
            )
        return await handler(request)
