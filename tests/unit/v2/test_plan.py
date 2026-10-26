"""S2.3 (FR-3, FR-4): AGENT_PLAN_MODE, ForcePlanFirst and its place in AgentFactory."""

import json
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.language import LanguageGuard
from aigw_service.api.v2.middleware.plan import NOT_DONE_REASON, ForcePlanFirst, PlanCompletionGuard, has_plan
from aigw_service.api.v2.middleware.todos import PlanStep, PlanTodoList
from aigw_service.api.v2.middleware.turn_context import TurnContext
from aigw_service.api.v2.middleware.turn_summary import TurnSummary
from aigw_service.api.v2.prompts.middleware import TODO_SYSTEM_PROMPT, TODO_TOOL_DESCRIPTION
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools import calc_tools
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent.config import AgentSettings
from excel_engine import ExcelAnalysisToolResult

PLAN = [{"content": "Посчитать сценарии", "status": "in_progress"}]
COMPLETED = [{**PLAN[0], "status": "completed"}]
DONE = tool_call("write_todos", "c_done", todos=COMPLETED)
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
                DONE,
                AIMessage("Готово."),
            ],
            mode,
        )
        assert model.calls[0]["tools"] == ["write_todos"]
        assert {"resolve_terms", "run_scenarios", "write_todos"} <= set(model.calls[1]["tools"])
        assert [name for name, _ in tool_results(out)] == ["write_todos", "run_scenarios", "write_todos"]
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
                DONE,
                AIMessage("Готово."),
            ],
            "always",
        )
        assert [c["tool_choice"] for c in model.calls[:2]] == [None, "write_todos"]
        assert model.calls[1]["tools"] == ["write_todos"]
        assert isinstance(model.calls[1]["messages"][-1], HumanMessage)  # the dropped answer is not in the prompt
        assert all(m.content != "Сразу отвечу без плана." for m in out["messages"])
        assert out["todos"] == COMPLETED
        assert out["messages"][-1].content == "Готово."

    async def test_always_gives_up_after_one_forced_call(self, tmp_path: Path, engine: list) -> None:
        out, model = await run_turn(tmp_path, [AIMessage("Первый."), AIMessage("Второй.")], "always")
        assert len(model.calls) == 2
        assert out["messages"][-1].content == "Второй."

    @pytest.mark.parametrize("mode", ["always", "auto"])
    async def test_blocked_plan_of_previous_turn_is_a_plan(self, tmp_path: Path, mode: str) -> None:
        """TurnContext keeps a plan with blocked steps; next turn the model updates it with every tool visible."""
        from aigw_service.api.v2.tools.catalog import default_catalog  # pylint: disable=import-outside-toplevel

        blocked = [{"content": "Сценарии EBITDA", "status": "blocked", "reason": "нет года расчёта"}]
        model = ScriptedChatModel(
            steps=[tool_call("write_todos", "c1", todos=blocked), AIMessage("За какой год?"), AIMessage("Обновлю.")]
        )
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(
            model,
            default_catalog(),
            store=store,
            upload_dir=str(tmp_path),
            checkpointer=InMemorySaver(),
            plan_mode=mode,
        ).build()
        config = {"configurable": {"thread_id": "u1:s1"}}
        outs = [
            await agent.ainvoke({"messages": [("user", text)]}, config=config, context=RequestContext("u1", "s1"))
            for text in ("Посчитай сценарии EBITDA", "Год 2026")
        ]
        assert outs[0]["messages"][-1].content == "За какой год?"  # blocked is closed: no return
        assert outs[1]["todos"] == blocked
        assert model.calls[0]["tools"] == ["write_todos"]
        assert {"run_scenarios", "write_todos"} <= set(model.calls[2]["tools"])
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
                DONE,
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
        assert [name for name, _ in results] == ["run_scenarios", "write_todos", "run_scenarios", "write_todos"]
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
            middleware=[PlanTodoList(), ForcePlanFirst("auto")],
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
            model, [_calc], middleware=[PlanTodoList(), ForcePlanFirst("always")], context_schema=RequestContext
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
        assert [type(m) for m in stack] == [
            TurnContext,
            LanguageGuard,
            PlanTodoList,
            ForcePlanFirst,
            PlanCompletionGuard,
            TurnSummary,
        ]
        assert stack[3].mode == "always"

    def test_off_installs_nothing(self) -> None:
        from aigw_service.api.v2.tools.catalog import default_catalog  # pylint: disable=import-outside-toplevel

        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), plan_mode="off").middleware()
        assert not any(isinstance(m, (ForcePlanFirst, PlanCompletionGuard)) for m in stack)
        assert any(isinstance(m, PlanTodoList) for m in stack)

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


class TestPlanSchema:
    """FR-5 «Статусы шагов плана»."""

    @staticmethod
    def agent(steps: list) -> tuple[Any, ScriptedChatModel]:
        model = ScriptedChatModel(steps=steps)
        return create_agent(model, [_calc], middleware=[PlanTodoList()], context_schema=RequestContext), model

    async def test_skipped_and_blocked_keep_reason(self) -> None:
        plan = [
            {"content": "Базовое значение", "status": "completed"},
            {"content": "Сценарии", "status": "skipped", "reason": "движок недоступен"},
            {"content": "Цель DSCR", "status": "blocked", "reason": "нет целевого значения"},
        ]
        agent, _ = self.agent([tool_call("write_todos", "c1", todos=plan), AIMessage("ок")])
        out = await agent.ainvoke({"messages": [("user", "x")]}, context=RequestContext("u1", "s1"))
        assert out["todos"] == plan
        message = next(m for m in out["messages"] if isinstance(m, ToolMessage))
        assert message.content.startswith("План обновлён:")
        assert "3. [blocked] Цель DSCR — нет целевого значения" in message.content

    @pytest.mark.parametrize("status", ["skipped", "blocked"])
    @pytest.mark.parametrize("reason", [None, " "])
    async def test_closed_without_reason_is_rejected(self, status: str, reason: str | None) -> None:
        step = {"content": "Сценарии", "status": status} | ({"reason": reason} if reason is not None else {})
        agent, _ = self.agent(
            [
                tool_call("write_todos", "c1", todos=PLAN),
                tool_call("write_todos", "c2", todos=[step]),
                AIMessage("ок"),
            ]
        )
        out = await agent.ainvoke({"messages": [("user", "x")]}, context=RequestContext("u1", "s1"))
        rejected = [m for m in out["messages"] if isinstance(m, ToolMessage)][1]
        assert rejected.status == "error"
        assert "reason" in rejected.content
        assert out["todos"] == PLAN

    def test_unknown_status_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanStep(content="x", status="done")

    def test_prompts_name_statuses(self) -> None:
        for text in (TODO_SYSTEM_PROMPT, TODO_TOOL_DESCRIPTION):
            assert all(word in text for word in ("skipped", "blocked", "reason"))
        assert "`blocked`" in TODO_SYSTEM_PROMPT


class TestCompletionGuard:
    """FR-8 «Завершение плана»."""

    @staticmethod
    def agent(steps: list, max_returns: int = 3) -> tuple[Any, ScriptedChatModel]:
        model = ScriptedChatModel(steps=steps)
        middleware = [PlanTodoList(), PlanCompletionGuard(max_returns)]
        return create_agent(model, [_calc], middleware=middleware, context_schema=RequestContext), model

    async def test_early_answer_goes_back_with_reminder(self) -> None:
        agent, model = self.agent(
            [
                tool_call("write_todos", "c1", todos=PLAN),
                AIMessage("Рано."),
                tool_call("_calc", "c2", x=1),
                tool_call("write_todos", "c3", todos=COMPLETED),
                AIMessage("Готово."),
            ]
        )
        out = await agent.ainvoke({"messages": [("user", "Посчитай")]}, context=RequestContext("u1", "s1"))
        reminder = model.calls[2]["messages"][-1]
        assert isinstance(reminder, HumanMessage)
        assert "1. [in_progress] Посчитать сценарии" in reminder.content
        assert out["messages"][-1].content == "Готово."
        assert out["guard_counters"] == {"plan_returns": 1}
        assert out["todos"] == COMPLETED

    async def test_returns_are_limited_then_not_done_section(self) -> None:
        plan = [*COMPLETED, {"content": "Цель DSCR", "status": "pending"}]
        agent, model = self.agent(
            [tool_call("write_todos", "c1", todos=plan)] + [AIMessage("Рано.") for _ in range(4)]
        )
        out = await agent.ainvoke({"messages": [("user", "Посчитай")]}, context=RequestContext("u1", "s1"))
        assert len(model.calls) == 5  # plan + first answer + 3 returns
        assert out["guard_counters"] == {"plan_returns": 3}
        assert out["messages"][-1].content == "Рано.\n\n**Не выполнено:**\n- Цель DSCR"
        assert out["todos"] == [*COMPLETED, {"content": "Цель DSCR", "status": "skipped", "reason": NOT_DONE_REASON}]
        assert sum(isinstance(m, HumanMessage) for m in out["messages"]) == 4

    async def test_closed_plan_is_accepted(self) -> None:
        plan = [
            *COMPLETED,
            {"content": "Сценарии", "status": "skipped", "reason": "движок недоступен"},
            {"content": "Цель", "status": "blocked", "reason": "нет года"},
        ]
        agent, model = self.agent([tool_call("write_todos", "c1", todos=plan), AIMessage("За какой год?")])
        out = await agent.ainvoke({"messages": [("user", "x")]}, context=RequestContext("u1", "s1"))
        assert out["messages"][-1].content == "За какой год?"
        assert len(model.calls) == 2

    async def test_no_plan_or_tool_call_is_not_checked(self) -> None:
        agent, model = self.agent([AIMessage("Привет!")])
        out = await agent.ainvoke({"messages": [("user", "Привет")]}, context=RequestContext("u1", "s1"))
        assert out["messages"][-1].content == "Привет!"
        assert len(model.calls) == 1

    async def test_english_request(self) -> None:
        agent, _ = self.agent([tool_call("write_todos", "c1", todos=PLAN), AIMessage("Early.")], max_returns=0)
        out = await agent.ainvoke(
            {"messages": [("user", "What is the EBITDA for 2026?")]}, context=RequestContext("u1", "s1")
        )
        assert out["messages"][-1].content == "Early.\n\n**Not done:**\n- Посчитать сценарии"

    def test_sync_path(self) -> None:
        agent, model = self.agent(
            [tool_call("write_todos", "c1", todos=PLAN), AIMessage("Рано."), AIMessage("Рано.")], 1
        )
        out = agent.invoke({"messages": [("user", "Посчитай")]}, context=RequestContext("u1", "s1"))
        assert len(model.calls) == 3
        assert "**Не выполнено:**" in out["messages"][-1].content

    async def test_off_accepts_open_plan(self, tmp_path: Path, engine: list) -> None:
        out, model = await run_turn(tmp_path, [tool_call("write_todos", "c1", todos=PLAN), AIMessage("Рано.")], "off")
        assert out["messages"][-1].content == "Рано."
        assert len(model.calls) == 2


class TestMaxReturnsSetting:
    def test_default(self) -> None:
        assert AgentSettings().plan_max_returns == 3

    def test_negative_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_PLAN_MAX_RETURNS", "-1")
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            AgentSettings()

    def test_factory_passes_the_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aigw_service.api.v2.tools.catalog import default_catalog  # pylint: disable=import-outside-toplevel

        monkeypatch.setattr(APP_CONFIG.agent, "plan_max_returns", 5)
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), plan_mode="auto").middleware()
        assert next(m for m in stack if isinstance(m, PlanCompletionGuard)).max_returns == 5
