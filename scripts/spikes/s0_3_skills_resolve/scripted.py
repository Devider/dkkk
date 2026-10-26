"""Prototype ScriptedChatModel: deterministic chat model for create_agent tests (no network)."""

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

# A step gets the prompt messages and the names of the tools bound for this call
Step = AIMessage | Callable[[list[BaseMessage], list[str]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    """Replays `steps` one per model call; records the tools and tool_choice each call saw."""

    steps: list[Any]
    _cursor: int = PrivateAttr(default=0)
    _tools: list[str] = PrivateAttr(default_factory=list)
    _tool_choice: Any = PrivateAttr(default=None)
    _shared: Any = PrivateAttr(default=None)
    _calls: list[dict] = PrivateAttr(default_factory=list)

    @property
    def calls(self) -> list[dict]:
        """Per model call: tools seen, tool_choice, prompt size."""
        return self._calls

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **_kwargs: Any) -> "ScriptedChatModel":
        bound = self.model_copy()
        bound._cursor = self._cursor  # pylint: disable=protected-access
        bound._tools = [_name(t) for t in tools]  # pylint: disable=protected-access
        bound._tool_choice = tool_choice  # pylint: disable=protected-access
        bound._shared = self._shared or self  # pylint: disable=protected-access
        return bound

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        owner = self._shared or self
        if owner._cursor >= len(owner.steps):  # pylint: disable=protected-access
            raise RuntimeError("ScriptedChatModel: script exhausted")
        step = owner.steps[owner._cursor]  # pylint: disable=protected-access
        owner._cursor += 1  # pylint: disable=protected-access
        owner._calls.append(  # pylint: disable=protected-access
            {"tools": list(self._tools), "tool_choice": self._tool_choice, "n_messages": len(messages)}
        )
        message = step(messages, self._tools) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=message)])


def _name(tool: Any) -> str:
    return getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else str(tool))


_counter = {"n": 0}


def call(name: str, **args: Any) -> AIMessage:
    """AIMessage with one tool call (GigaChat makes one call per response, see S0.2)."""
    _counter["n"] += 1
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{_counter['n']}"}])


def last_tool_json(messages: list[BaseMessage]) -> dict:
    """Parsed content of the latest ToolMessage."""
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            return json.loads(m.content)
    raise ValueError("no ToolMessage in history")
