"""StreamTranslator (ADR-14, S6.2 design D3–D5): parts of ``agent.astream`` → drafts of progress events.

A pure class with no I/O: parts of ``astream(stream_mode=["updates", "custom"], version="v2")`` go in
(``{"type", "ns", "data"}``, spike S6.1 P2), drafts go out (``events.Draft``). Mapping — S6.1 design «Results»:
a tool call starts with a ``model`` update carrying ``tool_calls`` and ends with a ``tools`` update carrying its
``ToolMessage``; the plan comes as ``todos``; a text answer is only a candidate until the guards let it pass
(a guard update with ``jump_to`` rejects it). Service nodes of middleware give nothing (FR-24).
"""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from aigw_service.api.v2.middleware.limits import LIMIT_HIT, TURN_TIMEOUT
from aigw_service.api.v2.stream.events import Draft
from aigw_service.api.v2.stream.labels import PHASE_TEXT, PLAN_TOOL, failure_view, retry_text, scrub, tool_label
from aigw_service.api.v2.tools.context import ModelIndex

REFUSAL_NODES = frozenset({"TurnContext.before_agent", "LanguageGuard.before_agent"})
"""Nodes that end the turn before any model call with a fixed answer (FR-1, FR-2)."""
CANCEL_NODE_PREFIX = "Cancellation."
PROGRESS_KEY = "progress"
"""Key of a ``custom`` part written through ``runtime.stream_writer`` (design D4; ProgressReporter, S6.4)."""


@dataclass
class _OpenCall:
    call: int
    label: str
    started: float


def _text(message: BaseMessage) -> str:
    """The same text ``AgentV2.answer`` returns for a message."""
    return message.content if isinstance(message.content, str) else str(message.content)


def _envelope(message: ToolMessage) -> dict[str, Any] | None:
    try:
        envelope = json.loads(message.content) if isinstance(message.content, str) else None
    except ValueError:
        return None
    return envelope if isinstance(envelope, dict) and "status" in envelope else None


class StreamTranslator:  # pylint: disable=too-many-instance-attributes  # translation state of one turn
    """One turn; ``index_provider`` returns ``RequestContext.model_index``, filled once the turn has started."""

    def __init__(
        self,
        index_provider: Callable[[], ModelIndex | None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._index = index_provider
        self._clock = clock
        self._open: dict[str, _OpenCall] = {}
        self._calls = 0
        self._steps: list[dict[str, Any]] = []
        self._phase: str | None = None
        self._candidate: AIMessage | None = None
        self._refusal = False
        self._cancelled = False
        self._counters: dict[str, Any] = {}

    # ---------------------------------------------------------------- input

    def feed(self, part: Mapping[str, Any]) -> list[Draft]:
        kind, data = part.get("type"), part.get("data")
        if kind == "updates" and isinstance(data, Mapping):
            out: list[Draft] = []
            for node, update in data.items():
                if isinstance(update, Mapping):
                    out += self._update(str(node), update)
            return out
        if kind == "custom" and isinstance(data, Mapping) and PROGRESS_KEY in data:
            return self._custom(data)
        return []

    def _update(self, node: str, update: Mapping[str, Any]) -> list[Draft]:
        out: list[Draft] = []
        if isinstance(update.get("guard_counters"), Mapping):
            self._counters = dict(update["guard_counters"])
        messages = update.get("messages") or []
        if not isinstance(messages, list):
            messages = [messages]
        for message in messages:
            if isinstance(message, AIMessage):
                out += self._ai_message(node, message, ended=update.get("jump_to") == "end")
            elif isinstance(message, ToolMessage):
                out += self._tool_message(message)
        if "todos" in update:
            out += self._plan(update.get("todos") or [])
        if update.get("jump_to") == "model":  # a guard sent the candidate back to the model
            self._candidate = None
        return out

    def _ai_message(self, node: str, message: AIMessage, *, ended: bool) -> list[Draft]:
        if message.tool_calls:
            self._candidate = None
            return [d for c in message.tool_calls if (d := self._start(c)) is not None]
        same = self._candidate is not None and message.id is not None and message.id == self._candidate.id
        if node == "model" or same or ended:
            self._candidate = message
            self._refusal = ended and node in REFUSAL_NODES
            self._cancelled = self._cancelled or (ended and node.startswith(CANCEL_NODE_PREFIX))
        if node == "model":
            return self._set_phase("checking")
        return []

    def _start(self, tool_call: Mapping[str, Any]) -> Draft | None:
        name = tool_call.get("name") or ""
        if name == PLAN_TOOL:  # the plan arrives as ``todos`` in the tools update
            return None
        self._calls += 1
        label = tool_label(name, tool_call.get("args"), self._index())
        self._open[str(tool_call.get("id"))] = _OpenCall(self._calls, label, self._clock())
        return {"event": "tool.started", "call": self._calls, "step": self._active_step(), "label": label}

    def _tool_message(self, message: ToolMessage) -> list[Draft]:
        opened = self._open.pop(str(message.tool_call_id), None)
        if opened is None:  # write_todos, or a call this translator never saw start
            return []
        envelope = _envelope(message)
        if envelope is None or envelope.get("status") != "error":
            return [self._finished(opened, "ok")]
        outcome, text = failure_view(envelope.get("code"))
        return [self._finished(opened, outcome, text)]

    def _finished(self, opened: _OpenCall, outcome: str, text: str | None = None) -> Draft:
        return {
            "event": "tool.finished",
            "call": opened.call,
            "label": opened.label,
            "outcome": outcome,
            "elapsed_s": round(max(self._clock() - opened.started, 0.0), 1),
            "text": text,
        }

    def _plan(self, todos: list[Any]) -> list[Draft]:
        steps = []
        for n, todo in enumerate(t for t in todos if isinstance(t, Mapping)):
            reason = scrub(todo.get("reason") or "") or None
            title = scrub(todo.get("content") or "") or f"Шаг {n + 1}"
            steps.append({"n": n + 1, "title": title, "status": todo.get("status"), "reason": reason})
        if steps == self._steps:
            return []
        self._steps = steps
        return [{"event": "plan", "steps": steps}]

    def _active_step(self) -> int | None:
        return next((s["n"] for s in self._steps if s["status"] == "in_progress"), None)

    def _set_phase(self, phase: str) -> list[Draft]:
        if phase == self._phase or phase not in PHASE_TEXT:
            return []
        self._phase = phase
        return [{"event": "phase", "phase": phase, "text": PHASE_TEXT[phase]}]  # type: ignore[index]

    def _custom(self, data: Mapping[str, Any]) -> list[Draft]:
        if data[PROGRESS_KEY] == "phase":
            return self._set_phase(str(data.get("phase")))
        if data[PROGRESS_KEY] == "retry":
            key = str(data.get("tool_call_id"))
            opened = self._open.get(key)
            if opened is None:
                return []
            finished = self._finished(opened, "retry", retry_text(data.get("code")))
            self._open[key] = _OpenCall(opened.call, opened.label, self._clock())
            started = {
                "event": "tool.started",
                "call": opened.call,
                "step": self._active_step(),
                "label": opened.label,
            }
            return [finished, started]
        return []

    # ---------------------------------------------------------------- end of the turn

    def finish(self) -> Draft:
        """The terminal ``answer`` once ``astream`` is over (design D5); no candidate — a wiring error."""
        if self._candidate is None:
            raise RuntimeError("turn ended without an answer")
        return {"event": "answer", "text": _text(self._candidate), "outcome": self.outcome()}

    def outcome(self) -> str:
        if self._refusal:
            return "refusal"
        if self._cancelled or self._counters.get(LIMIT_HIT) or self._counters.get(TURN_TIMEOUT):
            return "partial"
        if any(s["status"] == "blocked" for s in self._steps):
            return "clarify"
        return "full"
