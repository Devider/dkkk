"""ScriptedChatModel: deterministic chat model for tests and demos (AR-Q1, AR-Q2) — no network, no LLM.

A step is an ``AIMessage`` or a function of the prompt messages returning one. ``bind_tools`` returns a copy
that shares the script cursor and the call journal, because ``create_agent`` binds tools on every model call.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

Step = AIMessage | Callable[[list[BaseMessage]], AIMessage]


class ScriptExhaustedError(RuntimeError):
    """The model was called more times than the script has steps."""


class ScriptedChatModel(BaseChatModel):
    """Replays ``steps`` one per model call and records what each call saw."""

    steps: list[Any]
    _shared: "ScriptedChatModel | None" = PrivateAttr(default=None)
    _cursor: int = PrivateAttr(default=0)
    _calls: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _tools: list[str] = PrivateAttr(default_factory=list)
    _tool_choice: Any = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Per model call: tool names bound, tool_choice and the prompt messages."""
        return self._root._calls  # pylint: disable=protected-access

    @property
    def _root(self) -> "ScriptedChatModel":
        return self._shared or self

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **_kwargs: Any) -> "ScriptedChatModel":
        bound = self.model_copy()
        bound._shared = self._root  # pylint: disable=protected-access
        bound._tools = [getattr(t, "name", None) or t.get("name") for t in tools]  # pylint: disable=protected-access
        bound._tool_choice = tool_choice  # pylint: disable=protected-access
        return bound

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        root = self._root
        if root._cursor >= len(root.steps):  # pylint: disable=protected-access
            raise ScriptExhaustedError(f"script has {len(root.steps)} steps, model called again")
        step = root.steps[root._cursor]  # pylint: disable=protected-access
        root._cursor += 1  # pylint: disable=protected-access
        root._calls.append({"tools": list(self._tools), "tool_choice": self._tool_choice, "messages": list(messages)})  # pylint: disable=protected-access
        message = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=message)])

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedChatModel":
        """JSON list of steps: ``{"content": "текст"}`` or ``{"tool": "name", "args": {...}}``."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        steps = []
        for i, step in enumerate(raw):
            if "tool" in step:
                steps.append(tool_call(step["tool"], f"call_{i}", **step.get("args", {})))
            else:
                steps.append(AIMessage(step["content"]))
        return cls(steps=steps)


def tool_call(name: str, call_id: str = "call_1", **args: Any) -> AIMessage:
    """An ``AIMessage`` with exactly one tool call (GigaChat makes one call per response, spike S0.2)."""
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])
