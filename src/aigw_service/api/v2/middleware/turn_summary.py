"""TurnSummary (FR-16, ADR-8, spec §8 №20, §9 правила 4–5): the turn's digest without an LLM, and the dialog memory.

``after_agent`` turns this turn's plan and tool results into a compact summary (last 5 kept in ``turn_summaries``,
each one logged); ``wrap_model_call`` shows the model resolved term IDs, past summaries and steps waiting for the
user as a block of the system prompt — they live outside ``messages``, so history compression cannot drop them.

Source of results: the turn's ``ToolMessage`` envelopes. An offloaded result keeps its ``ref``, so a later turn can
read the full result with ``read_file`` instead of recalculating (S1.6 design D8).
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from aigw_service.api.v2.middleware.prompt import append_block
from aigw_service.api.v2.state import CopilotAgentState
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

KEEP = 5
QUESTION_CHARS = 200
SUMMARY_CHARS = 300
NUMBERS_PER_RESULT = 8
NUMBERS_PER_TURN = 16
PLAN_TOOL = "write_todos"


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _turn_messages(state: dict[str, Any]) -> tuple[HumanMessage | None, list[BaseMessage]]:
    """The user message the turn started with (TurnContext) and every message after it."""
    messages = state.get("messages") or []
    start = state.get("turn_message_id")
    index = next((i for i, m in enumerate(messages) if start and m.id == start), None)
    if index is None:
        return None, []
    asked = messages[index]
    return (asked if isinstance(asked, HumanMessage) else None), messages[index + 1 :]


def _result(message: ToolMessage, budget: int) -> dict[str, Any]:
    try:
        envelope = json.loads(message.content) if isinstance(message.content, str) else None
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or "status" not in envelope:
        return {"tool": message.name, "status": "ok", "summary": _cut(str(message.content), SUMMARY_CHARS)}
    result: dict[str, Any] = {"tool": message.name, "status": envelope["status"]}
    if envelope.get("code"):
        result["code"] = envelope["code"]
    result["summary"] = _cut(envelope.get("result") or envelope.get("error") or "", SUMMARY_CHARS)
    numbers = [
        {"label": n["label"], "value": n["value"]}
        for n in (envelope.get("numbers") or [])[: min(NUMBERS_PER_RESULT, budget)]
    ]
    if numbers:
        result["numbers"] = numbers
    if envelope.get("ref"):
        result["ref"] = envelope["ref"]
    return result


def build_summary(state: dict[str, Any]) -> dict[str, Any] | None:
    """``{turn_id, question, plan, results}``, or ``None`` for a turn with neither a plan nor tool results."""
    asked, messages = _turn_messages(state)
    plan = [dict(t) for t in state.get("todos") or []]
    results: list[dict[str, Any]] = []
    budget = NUMBERS_PER_TURN
    for message in messages:
        if isinstance(message, ToolMessage) and message.name != PLAN_TOOL:
            result = _result(message, budget)
            budget -= len(result.get("numbers", []))
            results.append(result)
    if asked is None or not (plan or results):
        return None
    return {
        "turn_id": state.get("turn_id"),
        "question": _cut(asked.text, QUESTION_CHARS),
        "plan": plan,
        "results": results,
    }


def _number(value: Any) -> str:
    """As in the result, without float noise: the answer must quote the same number (FR-13)."""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(round(value, 4))
    return str(value)


def _summary_line(i: int, summary: dict[str, Any]) -> str:
    parts = [f"{i}. «{summary['question']}»"]
    plan = summary.get("plan") or []
    if plan:
        done = sum(t.get("status") == "completed" for t in plan)
        parts.append(f"План: {done} из {len(plan)} выполнено.")
    for r in summary.get("results") or []:
        code = f" [{r['code']}]" if r.get("code") else ""
        parts.append(f"{r['tool']}{code} — {r['summary']}")
        if r.get("numbers"):
            parts.append("Числа: " + "; ".join(f"{n['label']}: {_number(n['value'])}" for n in r["numbers"]) + ".")
        if r.get("ref"):
            parts.append(f"Полный результат: {r['ref']}.")
    return " ".join(parts)


def memory_text(state: dict[str, Any]) -> str | None:
    """The «Память диалога» block, or ``None`` when there is nothing to remember."""
    sections = []
    terms = state.get("resolved_terms") or {}
    if terms:
        lines = [f"- «{text}» → {t['id']} ({t.get('name', '')})" for text, t in terms.items() if t.get("id")]
        if lines:
            sections.append("Сопоставленные термины (ID используй без повторного resolve_terms):\n" + "\n".join(lines))
    summaries = state.get("turn_summaries") or []
    if summaries:
        sections.append("Прошлые ходы:\n" + "\n".join(_summary_line(i, s) for i, s in enumerate(summaries, 1)))
    blocked = [t for t in state.get("todos") or [] if t.get("status") == "blocked"]
    if blocked:
        lines = [f"- {t['content']} — {t.get('reason', '')}" for t in blocked]
        sections.append(
            "Ждут ответа пользователя (обнови эти шаги плана по его ответу, новый план не строй):\n" + "\n".join(lines)
        )
    return "## Память диалога\n\n" + "\n\n".join(sections) if sections else None


class TurnSummary(AgentMiddleware):
    """Last in the stack (spec §8 №20); independent of the plan mode."""

    state_schema = CopilotAgentState

    def after_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        summaries = list(state.get("turn_summaries") or [])
        if summaries and summaries[-1].get("turn_id") == state.get("turn_id"):
            return None  # the turn ended before TurnContext started a new one, or is already summarized
        summary = build_summary(state)
        if summary is None:
            return None
        thread_id = getattr(getattr(runtime, "context", None), "thread_id", "")
        logger.info("Turn summary {}: {}", thread_id, json.dumps(summary, ensure_ascii=False))
        return {"turn_summaries": [*summaries, summary][-KEEP:]}

    async def aafter_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self.after_agent(state, runtime)

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        return handler(self._with_memory(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        return await handler(self._with_memory(request))

    @staticmethod
    def _with_memory(request: ModelRequest) -> ModelRequest:
        text = memory_text(request.state)
        if text is None:
            return request
        return request.override(system_message=append_block(request.system_message, text))
