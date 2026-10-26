"""S2.6 (FR-10, FR-16): checkpointer, TurnSummary, dialog memory; scenarios U5 and U8 on the scripted model."""

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2 import factory
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import turn_summary
from aigw_service.api.v2.middleware.prompt import append_block
from aigw_service.api.v2.middleware.turn_summary import TurnSummary, build_summary, memory_text
from aigw_service.api.v2.prompts.middleware import TODO_SYSTEM_PROMPT
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.resolve_terms import Pick
from aigw_service.context import APP_CTX
from excel_engine import ExcelAnalysisToolResult

TERMS = [{"text": "метанол", "role": "input"}, {"text": "EBITDA", "role": "output"}]
GRID = [{"id": "IN1", "min": 300, "max": 500, "step": 100}]
STEPS = [
    {"content": "Сопоставить термины", "status": "completed"},
    {"content": "Сценарии EBITDA", "status": "in_progress"},
]


def plan(status: str, reason: str | None = None) -> list[dict]:
    step = {"content": "Сценарии EBITDA", "status": status} | ({"reason": reason} if reason else {})
    return [STEPS[0], step]


@pytest.fixture
def domain(monkeypatch: pytest.MonkeyPatch, inline_gate: Any) -> list[dict]:
    """resolve_terms and the engine without LLM and LibreOffice; returns the engine calls."""
    calls: list[dict] = []

    async def analyzer(_message: str, _index: Any) -> list[Pick]:
        return [Pick("input", "метанол", "IN1"), Pick("output", "EBITDA", "OUT0")]

    def engine(**kw: Any) -> ExcelAnalysisToolResult:
        calls.append(kw)
        frame = pd.DataFrame({"Метанол": [300, 400, 500], "EBITDA": [1200.0, 1350.5, 1501.0]})
        return ExcelAnalysisToolResult(status="OK", result="", content={"result_df": frame})

    monkeypatch.setattr(rt, "analyze_with_v1", analyzer)
    monkeypatch.setattr(calc_tools, "analyze_excel_model", engine)
    return calls


class Session:
    """One user session over a checkpointer: a turn per ``ask``."""

    def __init__(self, model: ScriptedChatModel, store: Any, upload_dir: Path, saver: Any, session: str = "s1"):
        self.model = model
        self.agent = AgentFactory(
            model, default_catalog(), store=store, upload_dir=str(upload_dir), checkpointer=saver
        ).build()
        self.session = session

    async def ask(self, text: str) -> dict:
        return await self.agent.ainvoke(
            {"messages": [HumanMessage(text)]},
            config={"configurable": {"thread_id": f"u1:{self.session}"}},
            context=RequestContext(user_id="u1", session_id=self.session),
        )


def tools_called(out: dict, since: str) -> list[str]:
    """Tool names of the turn that started with the message ``since``."""
    messages = out["messages"]
    start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and m.content == since)
    return [m.name for m in messages[start:] if isinstance(m, ToolMessage)]


def system_of(model: ScriptedChatModel, call: int) -> str:
    system = model.calls[call]["messages"][0]
    assert isinstance(system, SystemMessage)
    return system.text


def calc_turn(year: int = 2026) -> list:
    """A whole turn: plan, terms, scenarios, closed plan, answer."""
    return [
        tool_call("write_todos", "p1", todos=STEPS),
        tool_call("resolve_terms", "r1", terms=TERMS),
        tool_call("run_scenarios", "c1", inputs=GRID, outputs=["OUT0"], year=year),
        tool_call("write_todos", "p2", todos=plan("completed")),
        AIMessage(f"EBITDA {year} растёт с ценой метанола: от 1200 до 1501."),
    ]


class TestScenarios:
    async def test_u5_missing_year_then_answer_continues_the_plan(self, tmp_path: Path, domain: list) -> None:
        """U5 / FR-10: no year → blocked and a question; «2026» finishes the same plan without resolve_terms."""
        model = ScriptedChatModel(
            steps=[
                tool_call("write_todos", "p1", todos=STEPS),
                tool_call("resolve_terms", "r1", terms=TERMS),
                tool_call("run_scenarios", "c1", inputs=GRID, outputs=["OUT0"]),
                tool_call("write_todos", "p2", todos=plan("blocked", "нет года расчёта")),
                AIMessage("За какой год посчитать сценарии?"),
                # turn 2
                tool_call("write_todos", "p3", todos=plan("in_progress")),
                tool_call("run_scenarios", "c2", inputs=GRID, outputs=["OUT0"], year=2026),
                tool_call("write_todos", "p4", todos=plan("completed")),
                AIMessage("EBITDA 2026: от 1200 до 1501."),
            ]
        )
        session = Session(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, InMemorySaver())

        first = await session.ask("Сценарии EBITDA при цене метанола 300–500")
        missing = json.loads([m for m in first["messages"] if isinstance(m, ToolMessage)][2].content)
        assert missing["code"] == "missing_input"
        assert first["messages"][-1].content == "За какой год посчитать сценарии?"
        assert first["todos"][1]["status"] == "blocked"
        assert not domain

        second = await session.ask("2026")
        turn2 = model.calls[5]
        assert {"run_scenarios", "resolve_terms", "write_todos"} <= set(turn2["tools"])  # plan kept: no narrowing
        memory = system_of(model, 5)
        assert "«метанол» → IN1 (Метанол)" in memory
        assert "- Сценарии EBITDA — нет года расчёта" in memory
        assert tools_called(second, "2026") == ["write_todos", "run_scenarios", "write_todos"]
        assert [t["status"] for t in second["todos"]] == ["completed", "completed"]
        assert second["messages"][-1].content == "EBITDA 2026: от 1200 до 1501."
        assert len(domain) == 1
        assert [s["question"] for s in second["turn_summaries"]] == [
            "Сценарии EBITDA при цене метанола 300–500",
            "2026",
        ]

    async def test_u8_question_about_last_answer(self, tmp_path: Path, domain: list) -> None:
        """U8 / FR-16: «почему DSCR так просел?» — answered from the summary, no tools, no recalculation."""
        model = ScriptedChatModel(steps=[*calc_turn(), AIMessage("Из-за снижения EBITDA при цене 300.")])
        session = Session(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, InMemorySaver())
        await session.ask("EBITDA 2026 при цене метанола 300–500?")
        out = await session.ask("Почему DSCR так просел?")

        assert len(model.calls) == 6
        memory = system_of(model, 5)
        assert "Прошлые ходы:\n1. «EBITDA 2026 при цене метанола 300–500?» План: 2 из 2 выполнено." in memory
        assert "EBITDA при Метанол = 300: 1200" in memory
        assert "EBITDA при Метанол = 400: 1350.5" in memory
        assert tools_called(out, "Почему DSCR так просел?") == []
        assert len(domain) == 1
        assert len(out["turn_summaries"]) == 1  # a turn without plan and tools adds no summary

    async def test_same_for_2027_without_resolve_terms(self, tmp_path: Path, domain: list) -> None:
        """FR-16: the follow-up reuses term IDs from memory."""
        follow_up = [
            tool_call("write_todos", "p5", todos=[{"content": "Сценарии EBITDA 2027", "status": "in_progress"}]),
            tool_call("run_scenarios", "c5", inputs=GRID, outputs=["OUT0"], year=2027),
            tool_call("write_todos", "p6", todos=[{"content": "Сценарии EBITDA 2027", "status": "completed"}]),
            AIMessage("EBITDA 2027: от 1200 до 1501."),
        ]
        model = ScriptedChatModel(steps=[*calc_turn(), *follow_up])
        session = Session(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, InMemorySaver())
        await session.ask("EBITDA 2026 при цене метанола 300–500?")
        out = await session.ask("А если то же на 2027?")

        assert "«EBITDA» → OUT0 (EBITDA)" in system_of(model, 5)
        assert tools_called(out, "А если то же на 2027?") == ["write_todos", "run_scenarios", "write_todos"]
        assert [c["output_years"] for c in domain] == [[2026], [2027]]

    async def test_sessions_are_isolated(self, tmp_path: Path, domain: list) -> None:
        model = ScriptedChatModel(steps=[*calc_turn(), AIMessage("Привет!")])
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        saver = InMemorySaver()
        await Session(model, store, tmp_path, saver).ask("EBITDA 2026 при цене метанола 300–500?")
        out = await Session(model, store, tmp_path, saver, session="s2").ask("Привет")

        assert "Память диалога" not in system_of(model, 5)
        assert not out.get("turn_summaries")
        assert not out.get("resolved_terms")
        assert [m.content for m in out["messages"]] == ["Привет", "Привет!"]


def state_with(messages: list, **fields: Any) -> dict[str, Any]:
    return {"messages": messages, "turn_message_id": messages[0].id, "turn_id": "t1"} | fields


def envelope(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


class TestSummary:
    def test_build(self) -> None:
        numbers = [{"label": f"EBITDA {i}", "value": float(i)} for i in range(12)]
        messages = [
            HumanMessage("Посчитай " + "очень " * 60, id="h1"),
            ToolMessage("План обновлён", tool_call_id="a", name="write_todos"),
            ToolMessage(envelope(status="ok", result="Сценариев: 12.", numbers=numbers), tool_call_id="b", name="run"),
            ToolMessage(
                envelope(status="error", error="Не указан год", code="missing_input", hint="Спроси год."),
                tool_call_id="c",
                name="run",
            ),
            ToolMessage(envelope(status="ok", result="x", numbers=numbers), tool_call_id="d", name="run"),
            ToolMessage("не JSON", tool_call_id="e", name="other"),
            HumanMessage("напоминание guard"),
        ]
        summary = build_summary(state_with(messages, todos=plan("blocked", "нет года")))
        assert summary["turn_id"] == "t1"
        assert len(summary["question"]) == 200
        assert summary["question"].endswith("…")
        assert summary["plan"] == plan("blocked", "нет года")
        assert [r["tool"] for r in summary["results"]] == ["run", "run", "run", "other"]
        assert len(summary["results"][0]["numbers"]) == 8
        assert summary["results"][1] == {
            "tool": "run",
            "status": "error",
            "code": "missing_input",
            "summary": "Не указан год",
        }
        assert len(summary["results"][2]["numbers"]) == 8  # 16 per turn
        assert summary["results"][3] == {"tool": "other", "status": "ok", "summary": "не JSON"}

    def test_nothing_to_remember(self) -> None:
        assert build_summary(state_with([HumanMessage("Привет", id="h1"), AIMessage("Привет!")])) is None
        assert (
            build_summary({"messages": [HumanMessage("x", id="h1")], "turn_message_id": "gone", "todos": STEPS})
            is None
        )

    def test_keeps_last_five_and_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logged: list = []
        monkeypatch.setattr(turn_summary.logger, "info", lambda *args: logged.append(args))
        old = [{"turn_id": f"t{i}", "question": str(i), "plan": [], "results": []} for i in range(5)]
        state = state_with([HumanMessage("Новый", id="h1")], todos=STEPS, turn_id="t9", turn_summaries=old)
        runtime = type("R", (), {"context": RequestContext(user_id="u1", session_id="s1")})()
        update = TurnSummary().after_agent(state, runtime)
        assert [s["turn_id"] for s in update["turn_summaries"]] == ["t1", "t2", "t3", "t4", "t9"]
        assert logged[0][1] == "u1:s1"
        assert json.loads(logged[0][2])["question"] == "Новый"

    def test_turn_already_summarized(self) -> None:
        """A turn TurnContext ended in before_agent keeps the old turn_id: no second summary."""
        state = state_with([HumanMessage("x", id="h1")], todos=STEPS, turn_summaries=[{"turn_id": "t1"}])
        assert TurnSummary().after_agent(state, None) is None


class TestMemoryBlock:
    def test_text(self) -> None:
        state = {
            "resolved_terms": {"метанол": {"id": "IN1", "name": "Метанол"}, "что-то": {"id": None}},
            "turn_summaries": [
                {
                    "question": "EBITDA 2026?",
                    "plan": plan("completed"),
                    "results": [
                        {"tool": "run", "status": "error", "code": "missing_input", "summary": "Не указан год"},
                        {
                            "tool": "run",
                            "status": "ok",
                            "summary": "ok",
                            "numbers": [{"label": "EBITDA", "value": 1.23456}],
                        },
                    ],
                }
            ],
            "todos": plan("blocked", "нет года"),
        }
        text = memory_text(state)
        assert text.startswith("## Память диалога")
        assert "- «метанол» → IN1 (Метанол)" in text
        assert "что-то" not in text
        assert (
            "1. «EBITDA 2026?» План: 2 из 2 выполнено. run [missing_input] — Не указан год run — ok Числа: EBITDA: 1.2346."
            in text
        )
        assert "Ждут ответа пользователя" in text

    def test_empty(self) -> None:
        assert memory_text({}) is None

    def test_one_system_message(self) -> None:
        merged = append_block(SystemMessage("Инструкции"), "## Память")
        assert [b["text"] for b in merged.content] == ["Инструкции", "\n\n## Память"]
        assert append_block(None, "## Память").content == "## Память"

    def test_plan_prompt_continues_blocked(self) -> None:
        assert "`blocked` прошлого хода" in TODO_SYSTEM_PROMPT

    def test_sync_wrap(self) -> None:
        seen: list = []
        request = type("Q", (), {})()
        request.state = {"resolved_terms": {"метанол": {"id": "IN1", "name": "Метанол"}}}
        request.system_message = SystemMessage("Инструкции")
        request.override = lambda **kw: seen.append(kw["system_message"]) or "overridden"
        assert TurnSummary().wrap_model_call(request, lambda r: r) == "overridden"
        assert "IN1" in seen[0].text


class TestWiring:
    async def test_turn_message_id(self, tmp_path: Path) -> None:
        model = ScriptedChatModel(steps=[AIMessage("ок")])
        session = Session(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, InMemorySaver())
        out = await session.ask("Привет")
        assert out["turn_message_id"] == out["messages"][0].id

    def test_service_agent_uses_service_checkpointer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}
        monkeypatch.setattr(factory, "build_model", lambda: ScriptedChatModel(steps=[]))
        monkeypatch.setattr(factory.AgentFactory, "build", lambda self: seen.setdefault("saver", self.checkpointer))
        factory.get_agent_v2.cache_clear()
        try:
            assert factory.get_agent_v2() is APP_CTX.agent_memory.checkpointer
        finally:
            factory.get_agent_v2.cache_clear()

    def test_summary_also_in_mode_off(self) -> None:
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), plan_mode="off").middleware()
        assert isinstance(stack[-1], TurnSummary)
