"""S2.7 (AR-Q6, AR-Q5, FR-16): history compression on the scripted model — budget, tail, storage, what survives."""

import re
from pathlib import Path
from typing import Any

import pytest
from gigachat.exceptions import ForbiddenError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr, ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_memory import STEPS, TERMS, plan
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_skills import catalog_with_skill_tool, skill_md, write_skill

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.summarization import (
    CHARS_PER_TOKEN,
    SUMMARY_SOURCE,
    ContextBudget,
    context_tokens,
)
from aigw_service.api.v2.prompts.middleware import (
    GROUNDING_CAVEAT,
    LIMIT_FALLBACK,
    SUMMARY_MESSAGE,
    SUMMARY_MESSAGE_NO_PATH,
    SUMMARY_PROMPT,
)
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent.config import AgentSettings
from aigw_service.exceptions import StopEventError

BUDGET = ContextBudget(max_prompt_tokens=12000, trigger=0.7, keep=0.3)
"""Small enough to compress after a few turns; the fixed part (prompt, skill, tool schemas) is ~5.6k tokens.

Below the 20k minimum of AGENT_MAX_PROMPT_TOKENS on purpose: the factory takes it directly, settings are not involved.
"""
SUMMARY_MARK = "Сообщения для сжатия:"
SUMMARY_TEXT = (
    "1. Запросы: EBITDA при цене метанола. 2. Результаты: EBITDA при Метанол = 400 — 1350.5. 3. Открытых нет."
)
STOP_BODY = b'{"status": 403, "message": "Model is temporarily unavailable due to technical reasons"}'
GRID = [{"id": "IN1", "min": 300, "max": 500, "step": 100}]
PADDING = "Учти, что модель кредитная, а показатели нужны для решения комитета. " * 40  # ~2.8k chars


class SummarizingModel(ScriptedChatModel):
    """Answers the summary prompt itself, so the script holds only the agent's own steps."""

    stop_on_summary: bool = False
    _summaries: list[str] = PrivateAttr(default_factory=list)

    @property
    def summaries(self) -> list[str]:
        return self._root._summaries  # pylint: disable=protected-access

    def _generate(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if messages and SUMMARY_MARK in messages[-1].text:
            if self.stop_on_summary:
                raise ForbiddenError("https://gigachat/api/v1/chat/completions", 403, STOP_BODY, None)
            self.summaries.append(messages[-1].text)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(SUMMARY_TEXT))])
        return super()._generate(messages, stop, run_manager, **kwargs)


def calc_turn(year: int, ids: str) -> list:
    return [
        tool_call("write_todos", f"p{ids}", todos=STEPS),
        tool_call("resolve_terms", f"r{ids}", terms=TERMS),
        tool_call("run_scenarios", f"c{ids}", inputs=GRID, outputs=["OUT0"], year=year),
        tool_call("write_todos", f"q{ids}", todos=plan("completed")),
        AIMessage(f"EBITDA {year} растёт с ценой метанола: от 1200 до 1501."),
    ]


class Session:
    def __init__(self, model: ScriptedChatModel, store: Any, upload_dir: Path, user: str = "u1", **kw: Any) -> None:
        self.agent = AgentFactory(
            model,
            kw.pop("catalog", None) or default_catalog(),
            store=store,
            upload_dir=str(upload_dir),
            checkpointer=kw.pop("saver", None) or InMemorySaver(),
            prompt_budget=kw.pop("budget", BUDGET),
            **kw,
        ).build()
        self.user = user

    async def ask(self, text: str) -> dict:
        return await self.agent.ainvoke(
            {"messages": [HumanMessage(text)]},
            config={"configurable": {"thread_id": f"{self.user}:s1"}},
            context=RequestContext(user_id=self.user, session_id="s1"),
        )


def is_summary(message: Any) -> bool:
    return isinstance(message, HumanMessage) and message.additional_kwargs.get("lc_source") == SUMMARY_SOURCE


def pinned(messages: list) -> list:
    return [m for m in messages if m.additional_kwargs.get("lc_source") == "pinned_skill"]


def compressed_calls(model: ScriptedChatModel) -> list[int]:
    return [i for i, c in enumerate(model.calls) if any(is_summary(m) for m in c["messages"])]


async def long_session(tmp_path: Path, turns: int, **kw: Any) -> tuple[SummarizingModel, list[dict], Session]:
    steps = [s for t in range(turns) for s in calc_turn(2020 + t, str(t))]
    model = SummarizingModel(steps=steps)
    session = Session(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, **kw)
    outs = [
        await session.ask(f"Вопрос {t}: EBITDA {2020 + t} при цене метанола 300–500? {PADDING}") for t in range(turns)
    ]
    return model, outs, session


class TestSettings:
    """AR-Q5 «Недопустимый бюджет контекста», «Недопустимые доли сжатия»."""

    def test_defaults(self) -> None:
        settings = AgentSettings()
        assert (settings.max_prompt_tokens, settings.summary_trigger, settings.summary_keep) == (30000, 0.7, 0.3)
        assert ContextBudget.from_settings() == ContextBudget(30000, 0.7, 0.3)
        assert (ContextBudget(30000, 0.7, 0.3).trigger_tokens, ContextBudget(30000, 0.7, 0.3).keep_tokens) == (
            21000,
            9000,
        )

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_MAX_PROMPT_TOKENS", "20000")
        monkeypatch.setenv("AGENT_SUMMARY_TRIGGER", "0.8")
        monkeypatch.setenv("AGENT_SUMMARY_KEEP", "0.2")
        settings = AgentSettings()
        assert (settings.max_prompt_tokens, settings.summary_trigger, settings.summary_keep) == (20000, 0.8, 0.2)

    @pytest.mark.parametrize(
        ("variable", "value"),
        [
            ("AGENT_MAX_PROMPT_TOKENS", "19999"),
            ("AGENT_SUMMARY_TRIGGER", "0"),
            ("AGENT_SUMMARY_TRIGGER", "1"),
            ("AGENT_SUMMARY_KEEP", "0"),
            ("AGENT_SUMMARY_KEEP", "1"),
        ],
    )
    def test_invalid_is_rejected(self, monkeypatch: pytest.MonkeyPatch, variable: str, value: str) -> None:
        monkeypatch.setenv(variable, value)
        with pytest.raises(ValidationError):
            AgentSettings()

    def test_tail_not_below_trigger_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_SUMMARY_TRIGGER", "0.5")
        monkeypatch.setenv("AGENT_SUMMARY_KEEP", "0.5")
        with pytest.raises(ValidationError, match="AGENT_SUMMARY_KEEP must be less than AGENT_SUMMARY_TRIGGER"):
            AgentSettings()


class TestTokens:
    """Design D3: one approximate counter, ~3 characters of Cyrillic per token."""

    def test_cyrillic(self) -> None:
        tokens = context_tokens([HumanMessage("ы" * 300)])
        assert 300 / CHARS_PER_TOKEN <= tokens <= 300 / CHARS_PER_TOKEN + 10

    def test_tools_count(self) -> None:
        @tool
        def demo(x: int) -> int:
            """Демо-инструмент с описанием."""
            return x

        messages = [HumanMessage("Привет")]
        assert context_tokens(messages, [demo]) > context_tokens(messages)


class TestPrompts:
    """AR-Q15: Russian texts; Latin only in placeholders and names the model must keep."""

    @pytest.mark.parametrize("text", [SUMMARY_PROMPT, SUMMARY_MESSAGE, SUMMARY_MESSAGE_NO_PATH])
    def test_russian(self, text: str) -> None:
        assert re.search("[а-яё]", text)
        latin = set(re.findall("[A-Za-z_]+", re.sub(r"`[^`]*`|\{\w+\}|</?\w+>", "", text)))
        assert latin <= {"IN", "OUT", "ID", "ref"}

    def test_prompt_asks_to_keep_numbers_ids_refs(self) -> None:
        for fragment in ("число", "ID показателей", "ref", "/results/", "Открытые вопросы", "Не добавляй чисел"):
            assert fragment in SUMMARY_PROMPT


class TestCompression:
    """AR-Q6 «Сжатие истории сессии»."""

    async def test_short_session_is_not_compressed(self, tmp_path: Path, domain: list) -> None:
        model, outs, _ = await long_session(tmp_path, 1)
        assert not model.summaries
        assert len(model.calls) == 5  # no extra model call
        assert not compressed_calls(model)
        assert outs[0]["messages"][-1].content.startswith("EBITDA 2020")

    async def test_long_session_stays_under_budget(self, tmp_path: Path, domain: list, layer_logs: list) -> None:
        """«Длинная сессия не превышает порог»."""
        model, outs, _ = await long_session(tmp_path, 8)
        assert model.summaries
        assert [o["messages"][-1].content for o in outs] == [
            f"EBITDA {2020 + t} растёт с ценой метанола: от 1200 до 1501." for t in range(8)
        ]
        assert len(layer_logs) == len(model.calls)
        assert max(r["layers"]["total"] for r in layer_logs) < BUDGET.max_prompt_tokens
        uncompressed = sum(context_tokens([m]) for m in outs[-1]["messages"])
        assert uncompressed > BUDGET.max_prompt_tokens  # without compression the last turn would not fit

    async def test_tail_is_verbatim_and_pairs_kept(self, tmp_path: Path, domain: list) -> None:
        """«Свежий хвост дословно», «Повторное сжатие»."""
        model, outs, _ = await long_session(tmp_path, 8)
        calls = compressed_calls(model)
        assert len(model.summaries) >= 2
        history = outs[-1]["messages"]
        for index in calls:
            messages = [m for m in model.calls[index]["messages"] if not isinstance(m, SystemMessage)]
            assert is_summary(messages[0])
            assert sum(is_summary(m) for m in messages) == 1
            assert messages[0].text.startswith("Начало диалога сжато.")
            assert SUMMARY_TEXT in messages[0].text
            tail = [m for m in messages[1:] if m.additional_kwargs.get("lc_source") != "pinned_skill"]
            stored = [m for m in history if m.id in {t.id for t in tail}]
            assert [m.content for m in stored] == [m.content for m in tail]  # verbatim
            answered = {m.tool_call_id for m in tail if isinstance(m, ToolMessage)}
            called = {c["id"] for m in tail if isinstance(m, AIMessage) for c in m.tool_calls}
            assert called <= answered <= called

    async def test_summary_prompt_gets_compressed_messages(self, tmp_path: Path, domain: list) -> None:
        model, _, _ = await long_session(tmp_path, 6)
        assert "Вопрос 0: EBITDA 2020" in model.summaries[0]
        assert model.summaries[0].startswith("Ты сжимаешь начало диалога")

    async def test_full_history_by_reference(self, tmp_path: Path, domain: list) -> None:
        """«Полная история по ссылке»: readable by the same user only."""
        model, outs, session = await long_session(tmp_path, 6)
        summary = next(m for i in compressed_calls(model) for m in model.calls[i]["messages"] if is_summary(m))
        path = re.search(r"`(/results/conversation_history/[^`]+)`", summary.text).group(1)

        model.steps.extend(
            [
                tool_call("write_todos", "h0", todos=plan("completed")),  # auto mode: a /results/ read needs a plan
                tool_call("read_file", "h1", file_path=path, limit=400),
                AIMessage("Прочитал."),
            ]
        )
        out = await session.ask("Что я спрашивал в начале?")
        own = next(m for m in out["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "h1")
        assert "Вопрос 0: EBITDA 2020" in own.text
        assert outs  # the session went on after the read

        store = session.agent.store
        record = (await store.aget(("memories", "u1"), "u1")).value
        await store.aput(("memories", "u2"), "u2", record)  # u2 has the same workbook, same x-session-id
        other_model = SummarizingModel(
            steps=[
                tool_call("write_todos", "x1", todos=plan("completed")),
                tool_call("read_file", "h2", file_path=path),
                AIMessage("Нет."),
            ]
        )
        other = await Session(other_model, store, tmp_path, user="u2").ask("Прочитай историю")
        theirs = next(m for m in other["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "h2")
        assert "Вопрос 0" not in theirs.text
        assert "not found" in theirs.text.lower() or "не найден" in theirs.text.lower()

    async def test_stop_event_on_summary(self, tmp_path: Path, domain: list) -> None:
        """«StopEvent при вызове сводки»: no retry, StopEventError → HTTP 503."""
        steps = [s for t in range(8) for s in calc_turn(2020 + t, str(t))]
        model = SummarizingModel(steps=steps, stop_on_summary=True)
        session = Session(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path)
        for t in range(8):
            try:
                await session.ask(f"Вопрос {t}: EBITDA {2020 + t}? {PADDING}")
            except StopEventError:
                break
        else:
            pytest.fail("the summary call never happened")
        assert not compressed_calls(model)  # the StopEvent came from the summary call, nothing was sent after it
        with pytest.raises(StopEventError):
            await session.ask("Ещё вопрос")  # the next turn needs a summary again and stops again

    async def test_summary_call_is_outside_turn_limits(
        self, tmp_path: Path, domain: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "max_model_calls", 5)  # exactly one calc turn
        model, outs, _ = await long_session(tmp_path, 8)
        assert model.summaries
        assert all(LIMIT_FALLBACK not in o["messages"][-1].text for o in outs)
        assert all(o["messages"][-1].content.startswith("EBITDA") for o in outs)


def padding_reads(count: int, start: int = 0) -> list:
    return [
        tool_call("read_file", f"pad{i}", file_path="/skills/economic-answer/SKILL.md", limit=400)
        for i in range(start, start + count)
    ]


class TestSurvives:
    """AR-Q6 «Что переживает сжатие истории», FR-16 «Память после сжатия истории»."""

    async def test_mid_turn_compression(self, tmp_path: Path, domain: list) -> None:
        """Plan, card, memory and turn summaries survive; the pinned skill returns; numbers stay grounded."""
        answer = "EBITDA 2026 при цене метанола 400 — 1350.5."
        model = SummarizingModel(
            steps=[
                *calc_turn(2025, "a"),  # turn 1: leaves a summary and resolved terms
                tool_call("write_todos", "p1", todos=STEPS),
                tool_call("run_scenarios", "c1", inputs=GRID, outputs=["OUT0"], year=2026),
                *padding_reads(4),
                tool_call("write_todos", "p2", todos=plan("completed")),
                AIMessage(answer),
            ]
        )
        session = Session(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path)
        await session.ask("EBITDA 2025 при цене метанола 300–500?")
        out = await session.ask(f"А на 2026? {PADDING}")

        calls = compressed_calls(model)
        assert calls
        assert calls[0] > 5  # compressed inside turn 2
        first = calls[0]
        before, after = model.calls[first - 1]["messages"][0], model.calls[first]["messages"][0]
        assert before.text == after.text  # card, skills, plan rules and dialog memory are the same
        assert "## Загруженная модель" in after.text
        assert "## Память диалога" in after.text
        assert "«метанол» → IN1 (Метанол)" in after.text

        # the calculation went into the summary, the answer's number is still grounded by step_results
        assert not any(isinstance(m, ToolMessage) and m.name == "run_scenarios" for m in model.calls[-1]["messages"])
        assert out["messages"][-1].content == answer
        assert GROUNDING_CAVEAT not in out["messages"][-1].content
        assert any(r.get("tool") == "run_scenarios" for r in out["step_results"])

        # the pinned skill went into the summary and is pinned again before the next call, one copy at a time
        assert all(len(pinned(c["messages"])) <= 1 for c in model.calls)
        assert any(len(pinned(model.calls[i]["messages"])) == 1 for i in range(first + 1, len(model.calls)))

        # state outside messages is intact; the turn's summary is built from the whole turn
        assert out["resolved_terms"]["метанол"]["id"] == "IN1"
        assert [t["status"] for t in out["todos"]] == ["completed", "completed"]
        summaries = out["turn_summaries"]
        assert len(summaries) == 2
        assert summaries[-1]["turn_id"] == out["turn_id"]
        assert summaries[-1]["question"].startswith("А на 2026?")  # turn_message_id still found after compression
        assert [r["tool"] for r in summaries[-1]["results"] if r["tool"] == "run_scenarios"] == ["run_scenarios"]

    async def test_skill_tools_hidden_after_compression(self, tmp_path: Path) -> None:
        """Instructions of an unpinned skill went into the summary: its tools are hidden until it is read again."""
        skills = write_skill(tmp_path / "skills", text=skill_md(tools="demo_calc"))
        write_skill(skills, "pad-skill", skill_md("pad-skill", "Справочник. Reference.", body=PADDING * 2))
        steps = [
            tool_call("read_file", "s1", file_path="/skills/demo-skill/SKILL.md"),
            *[tool_call("read_file", f"x{i}", file_path="/skills/pad-skill/SKILL.md", limit=400) for i in range(6)],
            tool_call("read_file", "s2", file_path="/skills/demo-skill/SKILL.md"),
            tool_call("demo_calc", "d1", x=2),
            AIMessage("Готово: demo 2."),
        ]
        model = SummarizingModel(steps=steps)
        session = Session(
            model,
            await uploaded_store("u1", INDEX, str(tmp_path)),
            tmp_path,
            catalog=catalog_with_skill_tool(),
            plan_mode="off",
            skills_dir=skills,
            pinned_skills=(),
        )
        out = await session.ask("Посчитай демо")
        first = compressed_calls(model)[0]
        assert "demo_calc" in model.calls[1]["tools"]  # opened by the first read
        assert "demo_calc" not in model.calls[first]["tools"]  # the read went into the summary
        assert "demo_calc" in model.calls[-2]["tools"]  # read again → opened again
        assert out["messages"][-1].content == "Готово: demo 2."
