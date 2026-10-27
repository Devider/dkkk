"""S4.1 (FR-13, ADR-9): numbers of the answer, AnswerGroundingGuard and its place in the agent."""

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_derive import AGENT_INDEX

from aigw_service.api.v2.answer_check import hygiene_issues, sections, unconfirmed_numbers
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.grounding import AnswerGroundingGuard, remove_numbers
from aigw_service.api.v2.middleware.limits import LIMIT_HIT, TURN_TIMEOUT
from aigw_service.api.v2.middleware.plan import PlanCompletionGuard
from aigw_service.api.v2.prompts.middleware import GROUNDING_CAVEAT
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.grounding import Source, answer_numbers, confirm
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent.config import AgentSettings


def written(text: str) -> list[str]:
    return [m.written for m in answer_numbers(text)]


def one(text: str) -> Any:
    (mention,) = answer_numbers(text)
    return mention


class TestAnswerNumbers:
    """Design D2: what is checked and how it is read."""

    @pytest.mark.parametrize(
        "text",
        [
            "в 2027 году",
            "1. Сценарии\n2) Цель",
            "ячейка P143, квартал Q4, выбросы CO2, FY2025, шаг S1.7",
            "на 31.12.2025",
            "за 3 года в 2 сценариях, 10 лет",
        ],
        ids=["year", "list", "identifier", "date", "small"],
    )
    def test_exempt(self, text: str) -> None:
        assert not answer_numbers(text)

    def test_parsed_as_written(self) -> None:
        m = one("итого 2,50 USD млн")
        assert (m.value, m.decimals, m.percent) == (2.5, 2, False)
        assert one("итого 1 200").value == 1200
        assert one("снизится на −12,5").value == -12.5
        m = one("маржа 12,5 %")
        assert (m.value, m.percent, m.written) == (12.5, True, "12,5 %")
        assert one("рост 3.5x").value == 3.5

    def test_checked_besides_exemptions(self) -> None:
        text = "1. В 2026 году EBITDA 412,3 при цене 300, 15 %, -3, 11 и 2100,5\n| EBITDA | 2026 | 57,4 |"
        assert written(text) == ["412,3", "300", "15 %", "-3", "11", "2100,5", "57,4"]

    def test_list_marker_only_at_line_start(self) -> None:
        assert written("шаг 15. Далее") == ["15"]


SOURCES = [Source(412.31, "a"), Source(0.125, "b"), Source(-30, "c"), Source(12.6, "d"), Source(0.0, "e")]


class TestConfirm:
    """Design D3: tolerance by written digits, sign ignored, percent ↔ share."""

    @pytest.mark.parametrize(
        ("text", "label"),
        [("412,3", "a"), ("12,5 %", "b"), ("30", "c"), ("−30", "c"), ("0,0", "e"), ("12,6", "d")],
    )
    def test_confirmed(self, text: str, label: str) -> None:
        source = confirm(one(text), SOURCES)
        assert source is not None
        assert source.label == label

    @pytest.mark.parametrize("text", ["13", "12,5", "412,0", "0,01", "57,4"])
    def test_unconfirmed(self, text: str) -> None:
        assert confirm(one(text), SOURCES) is None

    def test_nearest_source_wins(self) -> None:
        source = confirm(one("100,0"), [Source(100.04, "far"), Source(100.01, "near")])
        assert source is not None
        assert source.label == "near"


class TestUnconfirmedNumbers:
    """Design D4: the detector eval and the guard share."""

    def test_exempt_only_answer_passes(self) -> None:
        text = "1. В 2027 году за 3 года ячейка P143 не меняется."
        assert unconfirmed_numbers(text, []) == []

    def test_invented_number_found_once_per_mention(self) -> None:
        found = unconfirmed_numbers("EBITDA 412,3, маржа 57,4.", [Source(412.31, "EBITDA")])
        assert [m.written for m in found] == ["57,4"]


ANALYTIC = "## Вывод\n\nEBITDA 57,4.\n\n## Ключевые цифры\n\n| A | 2026 | 57,4 % |\n| --- | --- | --- |"


class TestRemove:
    """Design D6: «н/д» and the caveat."""

    def test_number_with_percent_becomes_placeholder(self) -> None:
        text = remove_numbers(ANALYTIC, unconfirmed_numbers(ANALYTIC, []))
        assert "57,4" not in text
        assert "EBITDA н/д." in text
        assert "| A | 2026 | н/д |" in text
        assert text.endswith(f"## Оговорки\n\n- {GROUNDING_CAVEAT}")
        assert [s[0] for s in sections(text)] == ["Вывод", "Ключевые цифры", "Оговорки"]

    def test_caveat_appended_to_existing_section(self) -> None:
        answer = "## Вывод\n\nEBITDA 57,4.\n\n## Оговорки\n\n- Цены 2026 года.\n"
        text = remove_numbers(answer, unconfirmed_numbers(answer, []))
        assert text.endswith(f"## Оговорки\n\n- Цены 2026 года.\n- {GROUNDING_CAVEAT}")
        assert text.count("Оговорки") == 1

    def test_caveat_before_a_following_section(self) -> None:
        answer = "## Вывод\n\nEBITDA 57,4.\n\n## Оговорки\n\n- Допущение.\n\n## Где в модели\n\nEBITDA."
        text = remove_numbers(answer, unconfirmed_numbers(answer, []))
        assert f"- Допущение.\n- {GROUNDING_CAVEAT}\n\n## Где в модели" in text

    def test_plain_answer_gets_last_paragraph(self) -> None:
        answer = "EBITDA составит 57,4 млн."
        text = remove_numbers(answer, unconfirmed_numbers(answer, []))
        assert text == f"EBITDA составит н/д млн.\n\n{GROUNDING_CAVEAT}"
        assert hygiene_issues(text) == []


def state_of(answer: AIMessage, *numbers: float, user: str = "Посчитай", counters: dict | None = None) -> dict:
    step = {"tool": "get_values", "numbers": [{"value": v, "label": f"n{i}"} for i, v in enumerate(numbers)]}
    return {
        "messages": [HumanMessage(user), answer],
        "step_results": [step],
        "guard_counters": counters or {},
    }


class TestGuardOnState:
    """FR-13 scenarios on the hook itself."""

    def test_grounded_answer_is_untouched(self) -> None:
        assert AnswerGroundingGuard(1).after_model(state_of(AIMessage("EBITDA 412,3."), 412.31), None) is None

    def test_tool_call_and_no_numbers_skipped(self) -> None:
        guard = AnswerGroundingGuard(1)
        assert guard.after_model(state_of(tool_call("get_values", "c1", ids=["OUT0"])), None) is None
        assert guard.after_model(state_of(AIMessage("Готово.")), None) is None
        assert guard.after_model({"messages": []}, None) is None

    def test_return_lists_unique_numbers_in_order(self) -> None:
        out = AnswerGroundingGuard(1).after_model(state_of(AIMessage("57,4 и 12 %, снова 57,4."), 1.0), None)
        assert out is not None
        assert out["jump_to"] == "model"
        (reminder,) = out["messages"]
        assert isinstance(reminder, HumanMessage)
        assert ": 57,4, 12 %." in reminder.content
        assert "`derive`" in reminder.content
        assert out["guard_counters"] == {"grounding_returns": 1}

    def test_after_return_numbers_are_removed(self) -> None:
        answer = AIMessage("EBITDA 57,4.", id="a1")
        out = AnswerGroundingGuard(1).after_model(state_of(answer, counters={"grounding_returns": 1}), None)
        assert out is not None
        assert "jump_to" not in out
        (final,) = out["messages"]
        assert final.id == "a1"
        assert final.content == f"EBITDA н/д.\n\n{GROUNDING_CAVEAT}"
        assert out["guard_counters"] == {"grounding_returns": 1, "grounding_removed": 1}

    def test_zero_returns_removes_at_once(self) -> None:
        out = AnswerGroundingGuard(0).after_model(state_of(AIMessage("EBITDA 57,4.")), None)
        assert out is not None
        assert "jump_to" not in out
        assert "н/д" in out["messages"][0].content

    @pytest.mark.parametrize("counters", [{LIMIT_HIT: 1}, {LIMIT_HIT: 1, TURN_TIMEOUT: 1}], ids=["limit", "timeout"])
    def test_exhausted_turn_removes_without_return(self, counters: dict) -> None:
        out = AnswerGroundingGuard(1).after_model(state_of(AIMessage("EBITDA 57,4."), counters=counters), None)
        assert out is not None
        assert "jump_to" not in out
        assert "н/д" in out["messages"][0].content

    def test_user_message_and_past_turn_are_sources(self) -> None:
        state = state_of(AIMessage("При пороге 3,5x значение 2,81."), user="Порог Долг/EBITDA 3,5x?")
        state["step_results"] = []
        state["turn_summaries"] = [{"results": [{"numbers": [{"label": "Долг/EBITDA", "value": 2.8134}]}]}]
        assert AnswerGroundingGuard(1).after_model(state, None) is None

    async def test_async_hook(self) -> None:
        out = await AnswerGroundingGuard(1).aafter_model(state_of(AIMessage("EBITDA 57,4.")), None)
        assert out is not None
        assert out["jump_to"] == "model"


class TestSetting:
    def test_default(self) -> None:
        assert AgentSettings().grounding_max_returns == 1

    def test_negative_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_GROUNDING_MAX_RETURNS", "-1")
        with pytest.raises(ValidationError):
            AgentSettings()

    def test_factory_passes_the_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "grounding_max_returns", 4)
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog()).middleware()
        guard = next(m for m in stack if isinstance(m, AnswerGroundingGuard))
        assert guard.max_returns == 4
        assert isinstance(stack[stack.index(guard) + 1], PlanCompletionGuard)

    def test_installed_when_plan_is_off(self) -> None:
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), plan_mode="off").middleware()
        assert any(isinstance(m, AnswerGroundingGuard) for m in stack)
        assert not any(isinstance(m, PlanCompletionGuard) for m in stack)


async def turn(tmp_path: Path, steps: list, question: str, mode: str = "off") -> tuple[dict, ScriptedChatModel]:
    model = ScriptedChatModel(steps=steps)
    store = await uploaded_store("u1", AGENT_INDEX, str(tmp_path))
    agent = AgentFactory(
        model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode=mode, pinned_skills=()
    ).build()
    out = await agent.ainvoke(
        {"messages": [HumanMessage(question)]}, context=RequestContext(user_id="u1", session_id="s1")
    )
    return out, model


def last_prompt(model: ScriptedChatModel, call: int) -> Any:
    return model.calls[call]["messages"][-1]


class TestInAgent:
    """ADR-9: the assembled agent."""

    async def test_invented_number_is_fixed_after_one_return(self, tmp_path: Path) -> None:
        steps = [
            tool_call("get_values", "c1", ids=["OUT0"], years=[2026]),
            AIMessage("Долг/EBITDA в 2026 году — 57,4."),
            AIMessage("Долг/EBITDA в 2026 году — 2,81."),
        ]
        out, model = await turn(tmp_path, steps, "Какой Долг/EBITDA в 2026?")
        reminder = last_prompt(model, 2)
        assert isinstance(reminder, HumanMessage)
        assert "57,4" in reminder.content
        assert out["messages"][-1].content == "Долг/EBITDA в 2026 году — 2,81."
        assert out["guard_counters"]["grounding_returns"] == 1
        assert "grounding_removed" not in out["guard_counters"]

    async def test_repeated_number_is_removed(self, tmp_path: Path) -> None:
        steps = [AIMessage("EBITDA вырастет до 57,4."), AIMessage("EBITDA вырастет до 57,4.")]
        out, model = await turn(tmp_path, steps, "Какая будет EBITDA?")
        assert len(model.calls) == 2
        assert out["messages"][-1].content == f"EBITDA вырастет до н/д.\n\n{GROUNDING_CAVEAT}"
        assert out["guard_counters"]["grounding_removed"] == 1

    async def test_derived_number_is_accepted(self, tmp_path: Path) -> None:
        steps = [
            tool_call("get_values", "c1", ids=["OUT0"], years=[2026]),
            tool_call("derive", "c2", operation="headroom", a={"value": 2.81}, threshold=3.5, direction="max"),
            AIMessage("Долг/EBITDA 2,81 при ковенанте 3,5x: запас 0,69."),
        ]
        out, model = await turn(tmp_path, steps, "Какой запас до ковенанта Долг/EBITDA 3,5x в 2026?")
        assert len(model.calls) == 3
        assert "grounding_returns" not in out["guard_counters"]

    async def test_plan_is_checked_before_numbers(self, tmp_path: Path) -> None:
        plan = [{"content": "Значение Долг/EBITDA", "status": "in_progress"}]
        steps = [
            tool_call("write_todos", "c1", todos=plan),
            AIMessage("Долг/EBITDA — 57,4."),
            tool_call("write_todos", "c2", todos=[{**plan[0], "status": "completed"}]),
            AIMessage("Долг/EBITDA — 57,4."),
            AIMessage("Значение не получено."),
        ]
        out, model = await turn(tmp_path, steps, "Какой Долг/EBITDA?", mode="auto")
        plan_reminder = last_prompt(model, 2)
        assert "незакрытые шаги" in plan_reminder.content
        assert "57,4" not in plan_reminder.content
        assert "57,4" in last_prompt(model, 4).content
        assert out["messages"][-1].content == "Значение не получено."
        assert out["guard_counters"]["plan_returns"] == 1
        assert out["guard_counters"]["grounding_returns"] == 1
