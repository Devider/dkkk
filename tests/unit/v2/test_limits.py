"""S2.5 (FR-9, AR-Q5): turn limits, the last model call without tools and the partial answer."""

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.limits import TurnLimits, limit_hit
from aigw_service.api.v2.middleware.plan import NOT_DONE
from aigw_service.api.v2.prompts.middleware import LIMIT_FALLBACK, LIMIT_PROMPT, LIMIT_REASON
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent.config import AgentSettings
from excel_engine import ExcelAnalysisToolResult

PLAN = [{"content": "Посчитать сценарии", "status": "in_progress"}]
WRITE_PLAN = tool_call("write_todos", "c_plan", todos=PLAN)
EBITDA = 1234.5


def scenarios(call_id: str, low: int = 300) -> AIMessage:
    return tool_call(
        "run_scenarios",
        call_id,
        inputs=[{"id": "IN1", "min": low, "max": low, "step": 1}],
        outputs=["OUT0"],
        year=2026,
    )


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, inline_gate: Any) -> list[dict]:
    """Calls that reached the engine; every scenario gives EBITDA 1234.5."""
    calls: list[dict] = []

    def run(**kw: Any) -> ExcelAnalysisToolResult:
        calls.append(kw)
        frame = pd.DataFrame([{kw["input_names"][0]: kw["ranges"][0][0], "EBITDA": EBITDA}])
        return ExcelAnalysisToolResult(status="OK", result="1 сценарий", content={"result_df": frame})

    monkeypatch.setattr(calc_tools, "analyze_excel_model", run)
    return calls


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Any:
    def set_limits(model: int = 40, tools: int = 30, calcs: int = 10) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "max_model_calls", model)
        monkeypatch.setattr(APP_CONFIG.agent, "max_tool_calls", tools)
        monkeypatch.setattr(APP_CONFIG.agent, "max_calcs", calcs)

    return set_limits


async def build(tmp_path: Path, steps: list, mode: str = "auto", saver: Any = None) -> tuple[Any, ScriptedChatModel]:
    model = ScriptedChatModel(steps=steps)
    store = await uploaded_store("u1", INDEX, str(tmp_path))
    factory = AgentFactory(
        model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode=mode, checkpointer=saver
    )
    return factory.build(), model


async def turn(agent: Any, text: str = "EBITDA 2026 при цене метанола 300?") -> dict:
    return await agent.ainvoke(
        {"messages": [("user", text)]},
        config={"configurable": {"thread_id": "u1:s1"}},
        context=RequestContext(user_id="u1", session_id="s1"),
    )


def tool_codes(out: dict) -> list[tuple[str, str]]:
    return [
        (m.name, json.loads(m.content).get("code") or "ok")
        for m in out["messages"]
        if isinstance(m, ToolMessage) and m.name != "write_todos"
    ]


def quote_numbers(messages: list[BaseMessage]) -> AIMessage:
    """The last call's answer: the numbers the model can see in the tool results."""
    numbers = [
        n["value"]
        for m in messages
        if isinstance(m, ToolMessage) and m.name == "run_scenarios"
        for n in json.loads(m.content).get("numbers", [])
    ]
    return AIMessage(f"Частичный ответ: EBITDA 2026 — {numbers[0]} млн руб.")


class TestSettings:
    """AR-Q5 «Недопустимый лимит», FR-9 «Лимиты из настроек»."""

    def test_defaults(self) -> None:
        settings = AgentSettings()
        assert (settings.max_model_calls, settings.max_tool_calls, settings.max_calcs) == (40, 30, 10)

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_MAX_MODEL_CALLS", "12")
        monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "8")
        monkeypatch.setenv("AGENT_MAX_CALCS", "2")
        settings = AgentSettings()
        assert (settings.max_model_calls, settings.max_tool_calls, settings.max_calcs) == (12, 8, 2)

    @pytest.mark.parametrize("var", ["AGENT_MAX_MODEL_CALLS", "AGENT_MAX_TOOL_CALLS", "AGENT_MAX_CALCS"])
    def test_limit_below_one_is_rejected(self, monkeypatch: pytest.MonkeyPatch, var: str) -> None:
        monkeypatch.setenv(var, "0")
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            AgentSettings()

    def test_factory_takes_limits_from_settings(self, limits: Any) -> None:
        limits(model=7, tools=5, calcs=3)
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog()).middleware()
        turn_limits = next(m for m in stack if isinstance(m, TurnLimits))
        assert (turn_limits.max_model_calls, turn_limits.max_tool_calls, turn_limits.max_calcs) == (7, 5, 3)
        assert turn_limits.engine_tools == default_catalog().engine_tool_names()


class TestPrompts:
    """AR-Q15: the texts the model and the user see are Russian."""

    @pytest.mark.parametrize("text", [LIMIT_PROMPT, LIMIT_FALLBACK, LIMIT_REASON])
    def test_russian(self, text: str) -> None:
        assert re.search("[а-яё]", text, re.IGNORECASE)
        assert not re.search("[a-z]", text, re.IGNORECASE)


class TestLimits:
    """FR-9 «Лимиты хода»."""

    async def test_calcs_over_limit_do_not_run(self, tmp_path: Path, engine: list, limits: Any) -> None:
        limits(calcs=2)
        steps = [WRITE_PLAN, scenarios("c1", 300), scenarios("c2", 400), scenarios("c3", 500), quote_numbers]
        agent, _model = await build(tmp_path, steps)
        out = await turn(agent)
        assert len(engine) == 2
        assert [code for _, code in tool_codes(out)] == ["ok", "ok", "limit_exceeded"]

    async def test_tool_calls_over_limit_do_not_run(self, tmp_path: Path, engine: list, limits: Any) -> None:
        limits(tools=2)
        steps = [WRITE_PLAN, scenarios("c1", 300), scenarios("c2", 400), quote_numbers]
        agent, _model = await build(tmp_path, steps)
        out = await turn(agent)
        assert len(engine) == 1
        assert tool_codes(out)[-1] == ("run_scenarios", "limit_exceeded")

    async def test_model_calls_never_exceed_limit(self, tmp_path: Path, engine: list, limits: Any) -> None:
        limits(model=3)
        steps = [scenarios(f"c{i}", 300 + i) for i in range(10)]
        agent, model = await build(tmp_path, steps, mode="off")
        out = await turn(agent)
        assert len(model.calls) == 3
        assert model.calls[-1]["tools"] == []
        assert out["messages"][-1].content == LIMIT_FALLBACK  # the tool call of the last answer was dropped

    async def test_last_call_has_no_tools_and_its_tool_call_is_dropped(
        self, tmp_path: Path, engine: list, limits: Any
    ) -> None:
        limits(calcs=1)
        steps = [scenarios("c1", 300), scenarios("c2", 400), scenarios("c3", 500)]
        agent, model = await build(tmp_path, steps, mode="off")
        out = await turn(agent)
        last = model.calls[-1]
        assert last["tools"] == []
        assert last["tool_choice"] is None
        assert last["messages"][-1].content == LIMIT_PROMPT
        assert len(engine) == 1
        final = out["messages"][-1]
        assert isinstance(final, AIMessage)
        assert not final.tool_calls
        assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "c3" for m in out["messages"])

    async def test_limit_prompt_stays_out_of_history(self, tmp_path: Path, engine: list, limits: Any) -> None:
        limits(calcs=1)
        agent, _model = await build(tmp_path, [scenarios("c1"), scenarios("c2", 400), quote_numbers], mode="off")
        out = await turn(agent)
        assert not any(m.content == LIMIT_PROMPT for m in out["messages"])

    async def test_counters_do_not_carry_over(self, tmp_path: Path, engine: list, limits: Any) -> None:
        limits(calcs=1)
        steps = [scenarios("c1", 300), scenarios("c2", 400), quote_numbers, scenarios("c4", 500), AIMessage("Готово.")]
        agent, _model = await build(tmp_path, steps, mode="off", saver=InMemorySaver())
        first = await turn(agent)
        assert limit_hit(first)
        second = await turn(agent, "А при 500?")
        assert not limit_hit(second)
        assert len(engine) == 2  # the second turn ran its calculation with a full limit
        assert second["messages"][-1].content == "Готово."


class TestPartialAnswer:
    """FR-9 «Зацикливание останавливается частичным ответом», FR-8 «Исчерпан лимит хода»."""

    async def test_looping_model_gets_partial_answer_with_numbers(
        self, tmp_path: Path, engine: list, limits: Any
    ) -> None:
        limits(calcs=3)
        loop = [scenarios(f"c{i}", 300) for i in range(6)]  # the same call again and again
        agent, model = await build(tmp_path, [WRITE_PLAN, *loop[:4], quote_numbers])
        out = await turn(agent)
        final = out["messages"][-1].content
        assert str(EBITDA) in final
        assert f"**{NOT_DONE}:**" in final
        assert "Посчитать сценарии" in final
        assert "limit" not in final.lower()
        assert out["todos"][0]["status"] == "skipped"
        assert out["todos"][0]["reason"] == LIMIT_REASON
        assert model.calls[-1]["tools"] == []
        assert len(engine) == 1  # repeats came from CalcCache, yet counted towards the limit

    async def test_always_mode_makes_one_last_call(self, tmp_path: Path, engine: list, limits: Any) -> None:
        limits(model=1)
        agent, model = await build(tmp_path, [AIMessage("Частичный ответ.")], mode="always")
        out = await turn(agent)
        assert len(model.calls) == 1  # ForcePlanFirst does not repeat the last call with tool_choice=write_todos
        assert out["messages"][-1].content == "Частичный ответ."
