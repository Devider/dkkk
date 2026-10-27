"""Retries and tool errors of agent v2 (AR-Q11, FR-7, spec §8 №14, №16, №17; design D3, D4).

The middleware are langchain's; only the predicates and the error content are ours:

- ``model_retry`` repeats the agent's model call on network errors, 5xx and 429 (GigaChat does not retry 429
  itself — spike S0.3); 403 and StopEvent are never retried, and after the last attempt the exception propagates
  instead of becoming the answer text (``on_failure="error"`` — spike S0.2).
- ``tool_errors`` turns any tool exception into the error envelope; StopEvent passes through to ``StopEventGuard``.
- ``engine_retry`` sits inside ``tool_errors`` and repeats an engine tool once on ``engine_unavailable``.
"""

from collections.abc import Iterable

import httpx
from gigachat.exceptions import ForbiddenError, RateLimitError, ServerError
from langchain.agents.middleware import ModelRetryMiddleware, ToolErrorMiddleware, ToolRetryMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest

from aigw_service.api.v2.middleware.stop_event import is_stop_event
from aigw_service.api.v2.tools.envelope import TRANSIENT_CODES, ToolError, failure_from_exception, to_json
from aigw_service.exceptions import StopEventError

ENGINE_RETRIES = 1
"""AR-Q11: a LibreOffice failure is repeated once; the engine gate has already recreated its pool."""
ENGINE_RETRY_DELAY = 1.0
BACKOFF = 2.0
"""The pause doubles with every model retry: 2, 4, 8 s by default."""

_TRANSIENT_LLM = (ServerError, RateLimitError, httpx.TransportError, ConnectionError, TimeoutError)


def is_transient_llm_error(exc: BaseException) -> bool:
    """Network error, 5xx or 429; never 403 / StopEvent, whatever the client's exception hierarchy becomes."""
    if isinstance(exc, (ForbiddenError, StopEventError)):
        return False
    return isinstance(exc, _TRANSIENT_LLM)


def is_transient_tool_error(exc: BaseException) -> bool:
    return isinstance(exc, ToolError) and exc.code in TRANSIENT_CODES


def tool_error_content(exc: Exception, _request: ToolCallRequest) -> str | None:
    """The envelope for the model; ``None`` lets a StopEvent propagate to HTTP 503 (AR-Q11)."""
    if is_stop_event(exc):
        return None
    return to_json(failure_from_exception(exc))


async def atool_error_content(exc: Exception, request: ToolCallRequest) -> str | None:
    return tool_error_content(exc, request)


def model_retry(attempts: int, wait: float) -> ModelRetryMiddleware:
    return ModelRetryMiddleware(
        max_retries=attempts,
        retry_on=is_transient_llm_error,
        on_failure="error",
        initial_delay=wait,
        backoff_factor=BACKOFF,
        jitter=False,
    )


def tool_errors() -> ToolErrorMiddleware:
    return ToolErrorMiddleware(tool_error_content, aon_error=atool_error_content)


def engine_retry(engine_tools: Iterable[str]) -> ToolRetryMiddleware:
    return ToolRetryMiddleware(
        max_retries=ENGINE_RETRIES,
        tools=list(engine_tools),
        retry_on=is_transient_tool_error,
        on_failure="error",
        initial_delay=ENGINE_RETRY_DELAY,
        jitter=False,
    )
