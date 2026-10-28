"""Prototype ProgressReporter (spec §8 №5а): writes through ``runtime.stream_writer`` from every hook it needs.

``reporter(label)`` makes a class per label, so two copies can sit at two places of one stack (P4).
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

WRITERS: list[dict[str, Any]] = []
"""What ``stream_writer`` looked like in each hook — kept to tell a live writer from a no-op under ``ainvoke``."""


def _outcome(result: Any) -> str:
    if isinstance(result, ToolMessage):
        try:
            body = json.loads(result.content) if isinstance(result.content, str) else {}
        except json.JSONDecodeError:
            body = {}
        return str(body.get("code") or body.get("status") or result.status)
    return type(result).__name__


def _write(runtime: Any, label: str, hook: str, **data: Any) -> None:
    writer = getattr(runtime, "stream_writer", None)
    WRITERS.append({"label": label, "hook": hook, "writer": repr(writer)[:80]})
    if writer is None:
        return
    writer({"src": label, "hook": hook, **data})


def reporter(label: str) -> AgentMiddleware:
    class _Progress(AgentMiddleware):
        async def abefore_model(self, state: dict[str, Any], runtime: Any) -> None:
            _write(runtime, label, "before_model", messages=len(state.get("messages", [])))

        async def aafter_model(self, state: dict[str, Any], runtime: Any) -> None:
            last = state["messages"][-1]
            calls = [c["name"] for c in getattr(last, "tool_calls", None) or []]
            _write(runtime, label, "after_model", tool_calls=calls, text=bool(last.content and not calls))

        async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
            name = request.tool_call["name"]
            _write(request.runtime, label, "tool_start", tool=name)
            try:
                result = await handler(request)
            except Exception as exc:
                _write(request.runtime, label, "tool_raised", tool=name, error=type(exc).__name__)
                raise
            _write(request.runtime, label, "tool_end", tool=name, outcome=_outcome(result))
            return result

    _Progress.__name__ = f"Progress_{label}"
    return _Progress()
