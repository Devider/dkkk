"""Test doubles for agent v2 unit tests: a scripted chat model and a minimal turn context (no network)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from aigw_service.api.v2.tools.context import ModelIndex


class ScriptedChatModel(BaseChatModel):
    """Replays ``steps`` one per model call; a step is an ``AIMessage`` or ``f(messages) -> AIMessage``."""

    steps: list[Any]
    _cursor: list[int] = PrivateAttr(default_factory=lambda: [0])

    @property
    def _llm_type(self) -> str:
        return "scripted-test"

    def bind_tools(self, tools: Sequence[Any], **_kwargs: Any) -> "ScriptedChatModel":
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        step = self.steps[self._cursor[0]]
        self._cursor[0] += 1
        message = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_call(name: str, call_id: str = "call_1", **args: Any) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])


@dataclass
class FakeContext:
    """Implements ``ToolContext`` for tests."""

    user_id: str = "u1"
    thread_id: str = "u1:s1"
    file_path: str = "/tmp/user_file_u1.xlsx"
    model_index: ModelIndex = field(default_factory=lambda: ModelIndex({"IN1": "Метанол"}, {"OUT0": "EBITDA"}))


Step = AIMessage | Callable[[list[BaseMessage]], AIMessage]


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
