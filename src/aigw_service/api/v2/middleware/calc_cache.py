"""CalcCache (FR-17, spec §8 №15, §12 «Кэши» п. 6; design D5): a repeated calculation of the session from the store.

Key: the session (namespace), the workbook hash, the tool and its arguments normalised by the tool's schema — field
order and explicit defaults do not matter. Only successful results are kept. It wraps the engine tools outside
``ToolErrorMiddleware`` and ``ToolRetryMiddleware``, so a hit runs neither the engine nor a retry.

The adapter returns a ``Command`` with the message and its ``step_results`` entry (S1.6 design D7): both are kept, and
a hit gives them back under the new call id with the same ``ref`` — the offloaded file stays in the user's store.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import ValidationError

from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

NAMESPACE = "calc_cache"


def cache_key(file_hash: str, tool: str, args: dict[str, Any]) -> str:
    raw = json.dumps({"file": file_hash, "tool": tool, "args": args}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CalcCache(AgentMiddleware):
    """Session-level cache in the service store, so it does not depend on which worker computed the result."""

    def __init__(self, engine_tools: Iterable[str], store: Any = None) -> None:
        super().__init__()
        self.engine_tools = frozenset(engine_tools)
        self._store = store

    @property
    def store(self) -> Any:
        return self._store if self._store is not None else APP_CTX.agent_memory.store

    def _where(self, request: ToolCallRequest) -> tuple[tuple[str, str], str] | None:
        """Namespace and key of the call, ``None`` when it is not cacheable."""
        name = request.tool_call["name"]
        if name not in self.engine_tools or request.tool is None:
            return None
        file_hash = ((request.state or {}).get("model_ref") or {}).get("file_hash")
        thread_id = getattr(request.runtime.context, "thread_id", None)
        if not file_hash or not thread_id:
            return None
        schema = request.tool.args_schema
        try:
            args = schema.model_validate(request.tool_call.get("args") or {}).model_dump(mode="json")
        except ValidationError:  # ToolNode answers invalid arguments itself
            return None
        return (NAMESPACE, thread_id), cache_key(file_hash, name, args)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        where = self._where(request)
        if where is None:
            return await handler(request)
        namespace, key = where
        cached = await self.store.aget(namespace, key)
        name = request.tool_call["name"]
        if cached is not None:
            logger.info("CalcCache hit {} [{}]", name, namespace[1])
            return _replay(cached.value, request.tool_call["id"], name)
        result = await handler(request)
        entry = _cacheable(result)
        if entry is not None:
            await self.store.aput(namespace, key, {"tool": name, **entry})
        return result


def _replay(value: dict[str, Any], call_id: str, name: str) -> ToolMessage | Command:
    message = ToolMessage(value["content"], tool_call_id=call_id, name=name)
    step = value.get("step")
    if step is None:  # an entry without the step record: just the message
        return message
    return Command(update={"messages": [message], "step_results": [{**step, "call_id": call_id}]})


def _cacheable(result: ToolMessage | Command) -> dict[str, Any] | None:
    """``{content, step}`` of a successful envelope; errors and other state updates (e.g. terms) are not cached."""
    step = None
    if isinstance(result, Command):
        update = result.update if isinstance(result.update, dict) else None
        if not update or set(update) - {"messages", "step_results"}:
            return None
        messages, steps = update.get("messages") or [], update.get("step_results") or []
        if len(messages) != 1 or len(steps) > 1:
            return None
        result, step = messages[0], (steps[0] if steps else None)
    if not isinstance(result, ToolMessage) or result.status == "error" or not isinstance(result.content, str):
        return None
    try:
        ok = json.loads(result.content).get("status") == "ok"
    except (ValueError, AttributeError):
        return None
    return {"content": result.content, "step": step} if ok else None
