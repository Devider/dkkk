"""Test doubles for agent v2 unit tests: a minimal turn context and an in-process engine gate (no network)."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools.context import ModelIndex

__all__ = ["FakeContext", "InlineGate", "ScriptedChatModel", "tool_call"]


@dataclass
class FakeContext:
    """Implements ``ToolContext`` for tests."""

    user_id: str = "u1"
    thread_id: str = "u1:s1"
    file_path: str = "/tmp/user_file_u1.xlsx"
    model_index: ModelIndex = field(default_factory=lambda: ModelIndex({"IN1": "Метанол"}, {"OUT0": "EBITDA"}))


class InlineGate:
    """EngineGate stand-in for unit tests: runs the domain function in-process and records session keys."""

    def __init__(self) -> None:
        self.sessions: list[str] = []
        self.error: Exception | None = None

    async def run(self, session_key: str, func: Callable[..., Any], /, **kwargs: Any) -> Any:
        self.sessions.append(session_key)
        if self.error is not None:
            raise self.error
        return func(**kwargs)


async def uploaded_store(user_id: str, index: ModelIndex, upload_dir: str, file_name: str = "model.xlsx") -> Any:
    """Agent store as after ``/upload`` at AGENT_ENGINE=v2: the user's file, its hash and a registry record."""
    from pathlib import Path

    from langgraph.store.memory import InMemoryStore

    from aigw_service.api.v2.turn.card import build_card
    from aigw_service.api.v2.turn.registry import NAMESPACE, _dump

    stored = f"user_file_{user_id}.xlsx"
    (Path(upload_dir) / stored).write_bytes(b"workbook bytes of " + user_id.encode())
    digest = f"hash-{user_id}"
    store = InMemoryStore()
    await store.aput(("memories", user_id), user_id, {"filename": stored, "file_hash": digest})
    await store.aput(NAMESPACE, digest, {"index": _dump(index), "card": build_card(file_name, index)})
    return store
