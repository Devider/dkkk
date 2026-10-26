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
