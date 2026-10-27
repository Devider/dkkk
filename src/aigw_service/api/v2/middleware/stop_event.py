"""StopEventGuard (AR-Q11, FR-15, spec §8 №4): GigaChat StopEvent → ``StopEventError`` → HTTP 503 at once.

GigaChat answers HTTP 403 with a "temporarily unavailable" body (ТН08). The model of v2 is not wrapped like v1's
``_wrap_llm_with_stop_event`` — no method patching is needed (spike S0.2): this middleware sees the error of the
agent's model call and of a tool call (``resolve_terms`` lets the semantic layer's error through, S1.3). Placed
right after LanguageGuard, outside any retrying middleware, so a StopEvent is never retried.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from gigachat.exceptions import ForbiddenError
from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langgraph.prebuilt.tool_node import ToolCallRequest

from aigw_service.context import _STOP_EVENT_MARKER, APP_CTX  # one marker for v1 and v2
from aigw_service.exceptions import StopEventError

logger = APP_CTX.get_logger()

STOP_EVENT_MESSAGE = "Выбранная модель GigaChat временно недоступна. Попробуйте позже."


def stop_event(exc: BaseException) -> StopEventError | None:
    """The StopEventError for a GigaChat StopEvent, ``None`` for any other error."""
    if isinstance(exc, StopEventError):
        return exc
    if not isinstance(exc, ForbiddenError) or _STOP_EVENT_MARKER not in (exc.content or b"").lower():
        return None
    logger.error("StopEvent detected: HTTP 403 from {}", str(exc.url))
    return StopEventError(user_message=STOP_EVENT_MESSAGE, url=str(exc.url), status_code=403, reason=str(exc.content))


class StopEventGuard(AgentMiddleware):
    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        try:
            return handler(request)
        except Exception as exc:
            if (error := stop_event(exc)) is not None:
                raise error from exc
            raise

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        try:
            return await handler(request)
        except Exception as exc:
            if (error := stop_event(exc)) is not None:
                raise error from exc
            raise

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        try:
            return handler(request)
        except Exception as exc:
            if (error := stop_event(exc)) is not None:
                raise error from exc
            raise

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> Any:
        try:
            return await handler(request)
        except Exception as exc:
            if (error := stop_event(exc)) is not None:
                raise error from exc
            raise
