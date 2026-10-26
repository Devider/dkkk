"""TurnContext (FR-1, spec §8 №1): workbook, model index and card for the turn, reset of per-turn state.

``before_agent`` puts the user's workbook path and the registry's index into ``RequestContext`` (the object the
tools read) or ends the turn without an LLM call when nothing is uploaded; ``wrap_model_call`` appends the model
card to the system prompt of every model call.
"""

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, ModelRequest, hook_config
from langchain_core.messages import AIMessage, SystemMessage

from aigw_service.api.v2.state import CopilotAgentState
from aigw_service.api.v2.tools.state import RESET
from aigw_service.api.v2.turn.card import card_text
from aigw_service.api.v2.turn.registry import ModelRegistry, file_hash
from aigw_service.context import APP_CTX

NO_MODEL_ANSWER = "Чтобы ответить, мне нужна ваша финансовая модель. Загрузите файл Excel и повторите вопрос."
BLOCKED = "blocked"


def memory_key(user_id: str) -> tuple[tuple[str, str], str]:
    """Where /upload records the user's workbook (shared with v1)."""
    return ("memories", user_id), user_id


class TurnContext(AgentMiddleware):
    """Must be the first middleware: every other one may rely on the workbook and the index being in context."""

    state_schema = CopilotAgentState

    def __init__(self, store: Any = None, upload_dir: str | Path | None = None) -> None:
        super().__init__()
        self._store = store
        self.upload_dir = Path(upload_dir or tempfile.gettempdir())

    @property
    def store(self) -> Any:
        return self._store if self._store is not None else APP_CTX.agent_memory.store

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        ctx = runtime.context
        namespace, key = memory_key(ctx.user_id)
        record = await self.store.aget(namespace, key)
        filename = (record.value or {}).get("filename") if record else None
        path = self.upload_dir / filename if filename else None
        if path is None or not path.exists():
            return {"messages": [AIMessage(NO_MODEL_ANSWER)], "jump_to": "end"}

        digest = record.value.get("file_hash")
        if not digest:  # uploaded before v2: hash once and remember it
            digest = await asyncio.to_thread(file_hash, path)
            await self.store.aput(namespace, key, {**record.value, "file_hash": digest})
        registry = ModelRegistry(self.store)
        found = await registry.get(digest)
        if found is None:  # the only path that opens the workbook
            await registry.register(path, file_name=filename)
            found = await registry.get(digest)
        index, card = found
        ctx.file_path, ctx.model_index, ctx.card = str(path), index, card
        return self._turn_update(state, digest, card)

    @staticmethod
    def _turn_update(state: dict[str, Any], digest: str, card: dict[str, Any]) -> dict[str, Any]:
        """Spec §9 правила 1–2: per-turn fields reset; a new workbook drops terms and plan of the old one."""
        turn_id = uuid4().hex
        update: dict[str, Any] = {"turn_id": turn_id, "step_results": [], "guard_counters": {}}
        if not any(t.get("status") == BLOCKED for t in state.get("todos") or []):
            update["todos"] = []
        previous = (state.get("model_ref") or {}).get("file_hash")
        replaced = bool(previous) and previous != digest
        if replaced:
            update["resolved_terms"] = {RESET: True}
            update["todos"] = []
        update["model_ref"] = {
            "file_name": card["file_name"],
            "file_hash": digest,
            "replaced_in_turn": turn_id if replaced else None,
        }
        return update

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        card = getattr(request.runtime.context, "card", None)
        if card:
            model_ref = request.state.get("model_ref") or {}
            replaced = model_ref.get("replaced_in_turn") == request.state.get("turn_id")
            request = request.override(system_message=_append(request.system_message, card_text(card, replaced)))
        return await handler(request)


def _append(system: SystemMessage | None, text: str) -> SystemMessage:
    """One system message only (GigaChat, spike S0.2): the card is a block of the existing one."""
    if system is None:
        return SystemMessage(text)
    blocks = system.content if isinstance(system.content, list) else [{"type": "text", "text": system.content}]
    return SystemMessage(content=[*blocks, {"type": "text", "text": "\n\n" + text}])
