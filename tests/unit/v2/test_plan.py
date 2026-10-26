"""S2.3 (FR-3, FR-4): AGENT_PLAN_MODE, ForcePlanFirst and its place in AgentFactory."""

import json
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.language import LanguageGuard
from aigw_service.api.v2.middleware.plan import ForcePlanFirst, has_plan
from aigw_service.api.v2.middleware.turn_context import TurnContext
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools import calc_tools
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent.config import AgentSettings
from excel_engine import ExcelAnalysisToolResult

PLAN = [{"content": "Посчитать сценарии", "status": "in_progress"}]
SCENARIOS = {"inputs": [{"id": "IN1", "min": 300, "max": 500, "step": 100}], "outputs": ["OUT0"], "year": 2026}


class TestSettings:
    def test_default_is_auto(self) -> None:
        assert AgentSettings().plan_mode == "auto"

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_PLAN_MODE", "always")
        assert AgentSettings().plan_mode == "always"

    def test_invalid_value_names_allowed_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_PLAN_MODE", "sometimes")
        with pytest.raises(ValidationError, match="'always', 'auto' or 'off'"):
            AgentSettings()


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, inline_gate: Any) -> list[dict]:
    """Calls that reached the engine."""
    calls: list[dict] = []

    def run(**kw: Any) -> ExcelAnalysisToolResult:
        calls.append(kw)
        return ExcelAnalysisToolResult(status="OK", result="3 сценария", content={})

    monkeypatch.setattr(calc_tools, "analyze_excel_model", run)
    return calls


async def run_turn(
    tmp_path: Path, steps: list, mode: str = "auto", text: str = "EBITDA 2026 при цене метанола 300–500?"
) -> tuple[dict, ScriptedChatModel]:
    from aigw_service.api.v2.tools.catalog import default_catalog  # pylint: disable=import-outside-toplevel

    model = ScriptedChatModel(steps=steps)
    store = await uploaded_store("u1", INDEX, str(tmp_path))
    agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode=mode).build()
    out = await agent.ainvoke({"messages": [("user", text)]}, context=RequestContext(user_id="u1", session_id="s1"))
    return out, model


def tool_results(out: dict) -> list[tuple[str, dict]]:
    return [
        (m.name, {} if m.name == "write_todos" else json.loads(m.content))
        for m in out["messages"]
        if isinstance(m, ToolMessage)
    ]


class TestModelCall:
    """FR-3: until the plan exists the model sees write_todos (and read_file) only."""

    @pytest.mark.parametrize("mode", ["always", "auto"])
    async def test_first_tool_call_is_the_plan(self, tmp_path: Path, engine: list, mode: str) -> None:
        out, model = await run_turn(
            tmp_path,
            [
                tool_call("write_todos", "c1", todos=PLAN),
                tool_call("run_scenarios", "c2", **SCENARIOS),
                AIMessage("Готово."),
            ],
            mode,
        )
        assert model.calls[0]["tools"] == ["write_todos"]
        assert {"resolve_terms", "run_scenarios", "write_todos"} <= set(model.calls[1]["tools"])
        assert [name for name, _ in tool_results(out)] == ["write_todos", "run_scenarios"]
        assert len(engine) == 1

    async def test_auto_answer_without_plan(self, tmp_path: Path, engine: list) -> None:
        """FR-4: «почему DSCR просел?» in auto — no plan, no calculation, one model call."""
        out, model = await run_turn(
            tmp_path, [AIMessage("DSCR просел из-за роста долга.")], "auto", text="Почему DSCR просел?"
        )
        assert out["messages"][-1].content == "DSCR просел из-за роста долга."
        assert len(model.calls) == 1
        assert not out.get("todos")
        assert not engine

    async def test_always_forces_the_plan(self, tmp_path: Path, engine: list) -> None:
        out, model = await run_turn(
            tmp_path,
            [
                AIMessage("Сразу отвечу без плана."),
                tool_call("write_todos", "c1", todos=PLAN),
                tool_call("run_scenarios", "c2", **SCENARIOS),
                AIMessage("Готово."),
            ],
            "always",
        )
        assert [c["tool_choice"] for c in model.calls[:2]] == [None, "write_todos"]
        assert model.calls[1]["tools"] == ["write_todos"]
        assert isinstance(model.calls[1]["messages"][-1], HumanMessage)  # the dropped answer is not in the prompt
        assert all(m.content != "Сразу отвечу без плана." for m in out["messages"])
        assert out["todos"] == PLAN
        assert out["messages"][-1].content == "Готово."

    async def test_always_gives_up_after_one_forced_call(self, tmp_path: Path, engine: list) -> None:
        out, model = await run_turn(tmp_path, [AIMessage("Первый."), AIMessage("Второй.")], "always")
        assert len(model.calls) == 2
        assert out["messages"][-1].content == "Второй."

    @pytest.mark.parametrize("mode", ["always", "auto"])
    async def test_plan_kept_from_previous_turn_is_a_plan(self, mode: str) -> None:
        """A plan left in the checkpoint (TurnContext keeps one with blocked steps): every tool is visible."""
        model = ScriptedChatModel(
            steps=[tool_call("write_todos", "c1", todos=PLAN), AIMessage("Нужен год расчёта."), AIMessage("Обновлю.")]
        )
        agent = create_agent(
            model,
            [_calc],
            middleware=[TodoListMiddleware(), ForcePlanFirst(mode)],
            context_schema=RequestContext,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "t1"}}
        for text in ("Посчитай сценарии", "Год 2026"):
            await agent.ainvoke({"messages": [("user", text)]}, config=config, context=RequestContext("u1", "s1"))
        assert model.calls[0]["tools"] == ["write_todos"]
        assert set(model.calls[2]["tools"]) == {"write_todos", "_calc"}
        assert len(model.calls) == 3

    def test_has_plan(self) -> None:
        assert not any(has_plan(state) for state in ({}, None, {"todos": []}))
        assert has_plan({"todos": PLAN})


@tool
def _calc(x: int) -> str:
    """Calculation stand-in."""
    return f"calc {x}"


@tool
def read_file(file_path: str) -> str:
    """read_file stand-in (deepagents FilesystemMiddleware, S3.1)."""
    return f"content of {file_path}"


class TestToolCall:
    """FR-3 «Расчёт без плана отклоняется»."""

    async def test_calculation_without_plan_is_rejected_then_runs(self, tmp_path: Path, engine: list) -> None:
        out, _ = await run_turn(
            tmp_path,
            [
                tool_call("run_scenarios", "c1", **SCENARIOS),
                tool_call("write_todos", "c2", todos=PLAN),
                tool_call("run_scenarios", "c3", **SCENARIOS),
                AIMessage("Готово."),
            ],
        )
        results = tool_results(out)
        assert results[0] == (
            "run_scenarios",
            {
                "status": "error",
                "error": "Расчёт без плана не выполняется",
                "code": "plan_required",
                "hint": "Сначала составь план через write_todos.",
            },
        )
        assert [name for name, _ in results] == ["run_scenarios", "write_todos", "run_scenarios"]
        assert results[2][1]["status"] == "ok"
        assert len(engine) == 1

    @pytest.mark.parametrize(
        ("path", "allowed"),
        [("/skills/dscr/SKILL.md", True), ("/results/r1.json", False), ("/skills/../results/r1.json", False)],
    )
    async def test_read_file_only_in_skills(self, path: str, allowed: bool) -> None:
        model = ScriptedChatModel(steps=[tool_call("read_file", "c1", file_path=path), AIMessage("ок")])
        agent = create_agent(
            model,
            [read_file, _calc],
            middleware=[TodoListMiddleware(), ForcePlanFirst("auto")],
            context_schema=RequestContext,
        )
        out = await agent.ainvoke({"messages": [("user", "x")]}, context=RequestContext("u1", "s1"))
        message = next(m for m in out["messages"] if isinstance(m, ToolMessage))
        assert set(model.calls[0]["tools"]) == {"write_todos", "read_file"}
        assert (message.content == f"content of {path}") is allowed
        assert ("plan_required" in message.content) is not allowed

    def test_sync_paths(self) -> None:
        model = ScriptedChatModel(
            steps=[
                tool_call("_calc", "c1", x=1),
                AIMessage("без плана"),
                tool_call("write_todos", "c2", todos=PLAN),
                tool_call("_calc", "c3", x=2),
                AIMessage("ок"),
            ]
        )
        agent = create_agent(
            model, [_calc], middleware=[TodoListMiddleware(), ForcePlanFirst("always")], context_schema=RequestContext
        )
        out = agent.invoke({"messages": [("user", "x")]}, context=RequestContext("u1", "s1"))
        contents = [m.content for m in out["messages"] if isinstance(m, ToolMessage) and m.name == "_calc"]
        assert "plan_required" in contents[0]
        assert contents[1] == "calc 2"
        assert model.calls[2]["tool_choice"] == "write_todos"


class TestFactory:
    """FR-4 «Режимы плана»: the place of ForcePlanFirst and mode off."""

    def test_order_and_mode_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aigw_service.api.v2.tools.catalog import default_catalog  # pylint: disable=import-outside-toplevel

        monkeypatch.setattr(APP_CONFIG.agent, "plan_mode", "always")
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog()).middleware()
        assert [type(m) for m in stack] == [TurnContext, LanguageGuard, TodoListMiddleware, ForcePlanFirst]
        assert stack[3].mode == "always"

    def test_off_installs_nothing(self) -> None:
        from aigw_service.api.v2.tools.catalog import default_catalog  # pylint: disable=import-outside-toplevel

        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), plan_mode="off").middleware()
        assert not any(isinstance(m, ForcePlanFirst) for m in stack)

    async def test_off_runs_calculation_without_plan(self, tmp_path: Path, engine: list) -> None:
        out, model = await run_turn(
            tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("Готово.")], "off"
        )
        assert {"resolve_terms", "run_scenarios", "write_todos"} <= set(model.calls[0]["tools"])
        assert tool_results(out)[0][1]["status"] == "ok"
        assert len(engine) == 1

    def test_middleware_rejects_off(self) -> None:
        with pytest.raises(ValueError, match="'always' or 'auto'"):
            ForcePlanFirst("off")
