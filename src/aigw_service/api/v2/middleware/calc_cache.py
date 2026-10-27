"""CalcCache (FR-17, spec §8 №15, §12 «Кэши» п. 6; design D5): a repeated calculation of the session from the store.

Key: the session (namespace), the workbook hash, the tool and its arguments normalised by the tool's schema — field
order and explicit defaults do not matter. Only successful results are kept. It wraps the engine tools outside
``ToolErrorMiddleware`` and ``ToolRetryMiddleware``, so a hit runs neither the engine nor a retry.
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
            return ToolMessage(cached.value["content"], tool_call_id=request.tool_call["id"], name=name)
        result = await handler(request)
        if _successful(result):
            await self.store.aput(namespace, key, {"tool": name, "content": result.content})
        return result


def _successful(result: ToolMessage | Command) -> bool:
    """Only a plain successful envelope; errors and results with state updates are not cached."""
    if not isinstance(result, ToolMessage) or result.status == "error" or not isinstance(result.content, str):
        return False
    try:
        return json.loads(result.content).get("status") == "ok"
    except (ValueError, AttributeError):
        return False
