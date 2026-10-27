"""Telemetry (AR-Q13, spec §8 №5, S4.3 design D1): one log line per model call and per tool call.

``LLM CALL`` — time, tokens, the tools the model called and an error code; ``TOOL CALL`` — time, status and the
envelope's error code. Exception texts never go into these lines, only a code (FR-14). Placed right inside
``StopEventGuard``: one line per logical call, retries and history compression included. Layer sizes stay in
``CONTEXT LAYERS`` (ContextMeter, the innermost wrapper); the two lines join on ``turn_id`` + ``call``.
"""

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from aigw_service.api.v2.middleware.context_meter import call_number
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

TOOL_ERROR = "tool_error"
"""Code of a failed tool answer that is not an envelope (e.g. invalid arguments answered by the tool node)."""


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 3)


def _base(request: ModelRequest | ToolCallRequest) -> dict[str, Any]:
    context = getattr(request.runtime, "context", None)
    state = request.state or {}
    return {"trace_id": getattr(context, "trace_id", ""), "turn_id": state.get("turn_id")}


def _answer(response: Any) -> AIMessage | None:
    """The model's message from any ``wrap_model_call`` result shape."""
    if isinstance(response, AIMessage):
        return response
    inner = getattr(response, "model_response", response)  # ExtendedModelResponse wraps a ModelResponse
    messages: list[BaseMessage] = list(getattr(inner, "result", None) or [])
    return next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)


def token_usage(message: AIMessage | None) -> dict[str, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    keys = ("input_tokens", "output_tokens", "total_tokens")
    return {key: int(usage[key]) for key in keys if key in usage}


def model_record(request: ModelRequest, started: float, response: Any = None, error: str | None = None) -> str:
    message = _answer(response)
    record = {
        **_base(request),
        "node": "model",
        "call": call_number(request.state or {}),
        "elapsed_s": _elapsed(started),
        "token_usage": token_usage(message),
        "tool_calls": [call["name"] for call in (message.tool_calls if message else [])],
        "error": error,
    }
    return json.dumps(record, ensure_ascii=False)


def _tool_message(result: Any) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    if isinstance(result, Command) and isinstance(result.update, dict):
        messages = result.update.get("messages") or []
        return next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
    return None


def tool_outcome(result: Any) -> tuple[str, str | None]:
    """``(status, code)`` of a tool answer: the envelope's fields, else the tool message status."""
    message = _tool_message(result)
    if message is None:
        return "ok", None
    try:
        body = json.loads(message.content) if isinstance(message.content, str) else None
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("status") == "error":
        return "error", str(body.get("code") or TOOL_ERROR)
    if message.status == "error":
        return "error", TOOL_ERROR
    return "ok", None


def tool_record(request: ToolCallRequest, started: float, status: str, code: str | None) -> str:
    record = {
        **_base(request),
        "tool": request.tool_call.get("name"),
        "elapsed_s": _elapsed(started),
        "status": status,
        "code": code,
    }
    return json.dumps(record, ensure_ascii=False)


class Telemetry(AgentMiddleware):
    """Logs and re-raises: it never changes a call, its result or an exception."""

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        started = time.perf_counter()
        try:
            response = handler(request)
        except BaseException as exc:
            logger.info("LLM CALL {}", model_record(request, started, error=type(exc).__name__))
            raise
        logger.info("LLM CALL {}", model_record(request, started, response))
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        started = time.perf_counter()
        try:
            response = await handler(request)
        except BaseException as exc:
            logger.info("LLM CALL {}", model_record(request, started, error=type(exc).__name__))
            raise
        logger.info("LLM CALL {}", model_record(request, started, response))
        return response

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
        started = time.perf_counter()
        try:
            result = handler(request)
        except BaseException as exc:
            logger.info("TOOL CALL {}", tool_record(request, started, "error", type(exc).__name__))
            raise
        logger.info("TOOL CALL {}", tool_record(request, started, *tool_outcome(result)))
        return result

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> Any:
        started = time.perf_counter()
        try:
            result = await handler(request)
        except BaseException as exc:
            logger.info("TOOL CALL {}", tool_record(request, started, "error", type(exc).__name__))
            raise
        logger.info("TOOL CALL {}", tool_record(request, started, *tool_outcome(result)))
        return result
