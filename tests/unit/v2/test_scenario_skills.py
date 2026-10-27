"""S3.3 (FR-18): skills target-seeking and scenario-analysis — texts, evals.yaml, shipped catalogue, U1 / U3 turns."""

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from tests.unit.v2.fakes import uploaded_store

from aigw_service.api.v2.answer_check import hygiene_issues, is_analytic, structure_issues
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.skills_check import SKILLS_DIR, validate_skills
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.calc_tools import RunScenariosArgs
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.resolve_terms import Pick
from excel_engine import ExcelAnalysisToolResult, ModelInputAnalysisToolResult

SCENARIO_SKILLS = ("target-seeking", "scenario-analysis")
YEARS = (2025, 2026, 2027, 2028)
INDEX = ModelIndex(
    inputs={"IN1": "Метанол"},
    outputs={"OUT0": "EBITDA"},
    input_values={"IN1": dict.fromkeys(YEARS, 400.0)},
    output_years={"OUT0": YEARS},
    output_values={"OUT0": {2026: 950.0, 2027: 1000.0}},
    units={"IN1": "USD/т", "OUT0": "млн USD"},
)
NO_CALC = "Не указан год"


def body(skill: str) -> str:
    return (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def evals(skill: str) -> list[dict]:
    return yaml.safe_load((SKILLS_DIR / skill / "evals.yaml").read_text(encoding="utf-8"))


def tool_names() -> set[str]:
    catalog = default_catalog()
    return {t.name for t in [*catalog.base_tools(), *catalog.skill_tools()]}


# ---------------------------------------------------------------- texts, evals.yaml, shipped catalogue


class TestSkillTexts:
    """FR-18: plan template in §11 order, argument rules, answer sections."""

    @staticmethod
    def in_order(text: str, *parts: str) -> bool:
        positions = [text.index(p) for p in parts]
        return positions == sorted(positions)

    def test_scenario_plan_and_rules(self) -> None:
        text = body("scenario-analysis")
        plan = text[text.index("## Шаблон плана") : text.index("## Шаг 1")]
        assert self.in_order(plan, "Сопоставить", "Базовое значение", "Сетка", "Изменения к базе", "Вывод")
        assert "±20 %" in text
        assert "база × 0,8" in text
        assert "(`max` − `min`) / 4" in text
        assert "**не спрашивай**" in text
        assert "перечисли точки сетки" in text
        assert "Само число шага не пиши" in text
        assert "Нет года — не считай" in text
        assert "`blocked`" in text
        assert "`min` = `max` = 500" in text  # one point instead of point-what-if
        assert "Оговорки" in text

    def test_target_plan_and_rules(self) -> None:
        text = body("target-seeking")
        plan = text[text.index("## Шаблон плана") : text.index("## Шаг 1")]
        assert self.in_order(plan, "Сопоставить", "Базовые значения", "Подбор", "Отклонение", "Вывод")
        assert "всеми четырьмя аргументами" in text  # S3.2: the call without arguments
        assert "Нет года или цели" in text
        assert "`blocked`" in text
        assert "Недостижимо: максимум" in text  # FR-11
        assert "не спрашивай пользователя о выборе показателя" in text  # S3.2: a question on a high match

    @pytest.mark.parametrize("skill", SCENARIO_SKILLS)
    def test_evals_yaml(self, skill: str) -> None:
        known_skills = {s.name for s in validate_skills(SKILLS_DIR, tool_names())}
        cases = evals(skill)
        assert len(cases) >= 4
        assert len({c["id"] for c in cases}) == len(cases)
        for case in cases:
            expect = case["expect"]
            assert case["query"].strip()
            assert expect["skill"] in known_skills
            assert set(expect["tools"]) <= tool_names()
            assert expect["answer"] in {"analytic", "question"}
            assert isinstance(expect["plan_includes"], list)
            assert isinstance(expect["caveat_includes"], list)
        assert {c["expect"]["answer"] for c in cases} == {"analytic", "question"}  # a missing year is covered


class TestShipped:
    """FR-18 «Skills сценариев в поставке»."""

    def test_all_valid_without_extra_tools(self) -> None:
        skills = {s.name: s for s in validate_skills(SKILLS_DIR, tool_names())}
        assert {"economic-answer", *SCENARIO_SKILLS} <= set(skills)
        assert all(not skills[n].include_tools for n in SCENARIO_SKILLS)

    def test_step_rule_in_tool_schema(self) -> None:
        """Design D6: the schema the model sees agrees with the skill; the min / max rules are unchanged."""
        schema = RunScenariosArgs.model_json_schema()["$defs"]["InputRange"]["properties"]
        assert "(конец − начало) / 4" in schema["step"]["description"]
        assert "точки сетки" in schema["step"]["description"]
        assert "не придумывай" not in schema["step"]["description"]
        assert "не придумывай" in schema["min"]["description"]
        assert schema["max"]["description"] == "Конец диапазона; для одной точки равен началу"


# ---------------------------------------------------------------- turns on the scripted model (design D4)


async def _analyzer(_message: str, _index: object) -> list[Pick]:
    return [Pick("input", "цена метанола", "IN1"), Pick("output", "EBITDA", "OUT0")]


def grid_result(year: int, points: dict[float, float]) -> ExcelAnalysisToolResult:
    frame = pd.DataFrame([{"Метанол": x, f"EBITDA_{year}": y} for x, y in points.items()])
    return ExcelAnalysisToolResult(status="OK", result="таблица", content={"result_df": frame})


class Turn:
    """One turn through AgentFactory with the shipped skills, the real catalog and the engine replaced."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.engine_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(rt, "analyze_with_v1", _analyzer)
        self.grid: ExcelAnalysisToolResult | None = None
        self.target: ModelInputAnalysisToolResult | None = None
        monkeypatch.setattr(calc_tools, "analyze_excel_model", self._grid)
        monkeypatch.setattr(calc_tools, "analyze_model_inputs_for_target", self._target)

    def _grid(self, **kwargs: Any) -> ExcelAnalysisToolResult:
        self.engine_calls.append(kwargs)
        assert self.grid is not None
        return self.grid

    def _target(self, **kwargs: Any) -> ModelInputAnalysisToolResult:
        self.engine_calls.append(kwargs)
        assert self.target is not None
        return self.target

    async def run(self, question: str, steps: list, plan_mode: str = "auto") -> tuple[dict, ScriptedChatModel]:
        model = ScriptedChatModel(steps=steps)
        store = await uploaded_store("u1", INDEX, str(self.tmp_path))
        agent = AgentFactory(
            model, default_catalog(), store=store, upload_dir=str(self.tmp_path), plan_mode=plan_mode
        ).build()
        out = await agent.ainvoke(
            {"messages": [("user", question)]}, context=RequestContext(user_id="u1", session_id="s1")
        )
        return out, model


@pytest.fixture
def turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inline_gate: object) -> Turn:
    return Turn(monkeypatch, tmp_path)


def tool_results(out: dict) -> list[tuple[str, dict | str]]:
    results = []
    for m in out["messages"]:
        if isinstance(m, ToolMessage):
            try:
                results.append((m.name, json.loads(m.content)))
            except json.JSONDecodeError:
                results.append((m.name, m.content))
    return results


def plan(*steps: str, status: str = "pending") -> list[dict]:
    return [{"content": s, "status": status} for s in steps]


def read_skill(name: str, call_id: str = "r1") -> AIMessage:
    return tool_call("read_file", call_id, file_path=f"/skills/{name}/SKILL.md")


U3_PLAN = (
    "Сопоставить показатели",
    "Базовое значение цены метанола в 2027",
    "Сетка EBITDA 2027 при цене ±20 % от базы",
    "Вывод",
)
U3_ANSWER = """\
## Вывод
При цене метанола от 320 до 480 EBITDA 2027 меняется от 820,5 до 1 179,5 млн USD.

## Ключевые цифры
| Показатель | Год | Значение | Ед. |
| --- | --- | --- | --- |
| База: Метанол = 400 | 2027 | 1 000 | млн USD |
| EBITDA при Метанол = 320 | 2027 | 820,5 | млн USD |
| EBITDA при Метанол = 480 | 2027 | 1 179,5 | млн USD |

## Анализ
EBITDA растёт вместе с ценой метанола равномерно по всей сетке.

## Где в модели
Входы: «Метанол». Выходы: «EBITDA».

## Оговорки
Диапазон ±20 % от базы 400 → 320–480; шаг не назван — сетка из 5 точек: 320, 360, 400, 440, 480.
"""


class TestRelativeRangeU3:
    """FR-18 «Относительный диапазон (U3)»."""

    async def test_turn(self, turn: Turn) -> None:
        turn.grid = grid_result(2027, {320.0: 820.5, 360.0: 910.25, 400.0: 1000.0, 440.0: 1089.75, 480.0: 1179.5})
        out, model = await turn.run(
            "Посчитай EBITDA 2027 при изменении цены метанола на ±20 % от базы.",
            [
                read_skill("scenario-analysis"),
                tool_call("write_todos", "c1", todos=plan(*U3_PLAN)),
                tool_call(
                    "resolve_terms",
                    "c2",
                    terms=[{"text": "цена метанола", "role": "input"}, {"text": "EBITDA", "role": "output"}],
                ),
                tool_call("get_values", "c3", ids=["IN1", "OUT0"], years=[2027]),
                tool_call(
                    "run_scenarios",
                    "c4",
                    inputs=[{"id": "IN1", "min": 320, "max": 480, "step": 40}],
                    outputs=["OUT0"],
                    year=2027,
                ),
                tool_call("write_todos", "c5", todos=plan(*U3_PLAN, status="completed")),
                AIMessage(U3_ANSWER),
            ],
        )
        results = tool_results(out)
        names = [n for n, _ in results]
        assert names[0] == "read_file"
        assert "Сценарный анализ" in str(results[0][1])
        assert names.index("get_values") < names.index("run_scenarios")  # base before the grid
        assert all(r["status"] == "ok" for n, r in results if n not in {"read_file", "write_todos"})
        assert turn.engine_calls[0]["ranges"] == [[320, 480]]
        assert turn.engine_calls[0]["steps"] == [40]
        answer = out["messages"][-1].text
        assert answer == U3_ANSWER  # AnswerGroundingGuard found every number in the sources (FR-13)
        assert structure_issues(answer) == []
        assert hygiene_issues(answer) == []
        assert any("/skills/scenario-analysis/SKILL.md" in m.text for m in model.calls[0]["messages"][:1])


NO_STEP_ANSWER = """\
## Вывод
При цене метанола от 300 до 500 EBITDA 2026 меняется от 700 до 1 200 млн USD.

## Ключевые цифры
| Показатель | Год | Значение | Ед. |
| --- | --- | --- | --- |
| EBITDA при Метанол = 300 | 2026 | 700 | млн USD |
| EBITDA при Метанол = 500 | 2026 | 1 200 | млн USD |

## Оговорки
Шаг не назван — сетка из 5 точек: 300, 350, 400, 450, 500.
"""


class TestDefaultStep:
    """FR-18 «Шаг не назван»."""

    async def test_turn(self, turn: Turn) -> None:
        turn.grid = grid_result(2026, {300.0: 700.0, 350.0: 825.0, 400.0: 950.0, 450.0: 1075.0, 500.0: 1200.0})
        steps = ("Сопоставить показатели", "Сетка EBITDA 2026 при цене 300–500", "Вывод")
        out, _ = await turn.run(
            "Посчитай EBITDA 2026 при цене метанола от 300 до 500.",
            [
                read_skill("scenario-analysis"),
                tool_call("write_todos", "c1", todos=plan(*steps)),
                tool_call(
                    "resolve_terms",
                    "c2",
                    terms=[{"text": "цена метанола", "role": "input"}, {"text": "EBITDA", "role": "output"}],
                ),
                tool_call(
                    "run_scenarios",
                    "c3",
                    inputs=[{"id": "IN1", "min": 300, "max": 500, "step": 50}],
                    outputs=["OUT0"],
                    year=2026,
                ),
                tool_call("write_todos", "c4", todos=plan(*steps, status="completed")),
                AIMessage(NO_STEP_ANSWER),
            ],
        )
        assert turn.engine_calls[0]["steps"] == [50]
        answer = out["messages"][-1].text
        assert answer == NO_STEP_ANSWER  # the grid points in the caveat are sources (FR-13)
        assert "?" not in answer
        assert is_analytic(answer)
        assert structure_issues(answer) == []
        assert hygiene_issues(answer) == []


class TestMissingYear:
    """FR-18 «Нет года в запросе» (FR-10)."""

    async def test_turn(self, turn: Turn) -> None:
        question = "Уточните, пожалуйста, за какой год посчитать EBITDA?"
        blocked = [
            {"content": "Сопоставить показатели", "status": "completed"},
            {"content": "Сетка EBITDA при цене 300–500", "status": "blocked", "reason": NO_CALC},
        ]
        out, _ = await turn.run(
            "Посчитай EBITDA при цене метанола от 300 до 500 с шагом 100.",
            [
                read_skill("scenario-analysis"),
                tool_call("write_todos", "c1", todos=plan("Сопоставить показатели", "Сетка EBITDA", "Вывод")),
                tool_call(
                    "resolve_terms",
                    "c2",
                    terms=[{"text": "цена метанола", "role": "input"}, {"text": "EBITDA", "role": "output"}],
                ),
                tool_call("write_todos", "c3", todos=blocked),
                AIMessage(question),
            ],
        )
        assert turn.engine_calls == []
        assert [t["status"] for t in out["todos"]] == ["completed", "blocked"]
        answer = out["messages"][-1].text
        assert answer == question
        assert not is_analytic(answer)
        assert hygiene_issues(answer) == []


TARGET_PLAN = (
    "Сопоставить показатели",
    "Базовые значения цены метанола и EBITDA в 2026",
    "Подбор цены метанола для EBITDA 2026 = 1000",
    "Отклонение от базы",
    "Вывод",
)
TARGET_ANSWER = """\
## Вывод
EBITDA 2026 достигает 1 000 млн USD при цене метанола 412,5 USD/т — на 3,125 % выше базы.

## Ключевые цифры
| Показатель | Год | Значение | Ед. |
| --- | --- | --- | --- |
| Метанол, база | 2026 | 400 | USD/т |
| Метанол, найденное значение | 2026 | 412,5 | USD/т |
| EBITDA при найденной цене | 2026 | 1 000,2 | млн USD |
| EBITDA, база | 2026 | 950 | млн USD |

## Где в модели
Входы: «Метанол». Выходы: «EBITDA».

## Оговорки
Цель достигнута в пределах допуска: значение выхода 1 000,2 вместо 1 000.
"""


class TestTargetU1:
    """FR-18 «Подбор под цель по skill (U1)»."""

    async def test_turn(self, turn: Turn) -> None:
        turn.target = ModelInputAnalysisToolResult(
            status="OK",
            result="Найдено 1 сценариев.\n\nподробности",
            content={
                "actual_output_name": "EBITDA",
                "target_value": 1000.0,
                "current_input_values": {"Метанол": 400.0},
                "matching_scenarios": [
                    {"input_values": {"Метанол": 412.5}, "output_value": 1000.2, "deviation_percent": 0.02}
                ],
            },
        )
        out, _ = await turn.run(
            "Какой должна быть цена метанола, чтобы EBITDA в 2026 году была 1000?",
            [
                read_skill("target-seeking"),
                tool_call("write_todos", "c1", todos=plan(*TARGET_PLAN)),
                tool_call(
                    "resolve_terms",
                    "c2",
                    terms=[{"text": "цена метанола", "role": "input"}, {"text": "EBITDA", "role": "output"}],
                ),
                tool_call("get_values", "c3", ids=["IN1", "OUT0"], years=[2026]),
                tool_call("find_input_for_target", "c4", output_id="OUT0", year=2026, target=1000, input_ids=["IN1"]),
                tool_call(
                    "derive",
                    "c5",
                    operation="pct_change",
                    a={"value": 400, "label": "Метанол, база"},
                    b={"value": 412.5, "label": "Метанол, найдено"},
                ),
                tool_call("write_todos", "c6", todos=plan(*TARGET_PLAN, status="completed")),
                AIMessage(TARGET_ANSWER),
            ],
        )
        results = dict(tool_results(out))
        assert "Подбор входа под цель" in str(results["read_file"])
        assert results["find_input_for_target"]["status"] == "ok"
        assert results["derive"]["status"] == "ok"
        call = turn.engine_calls[0]
        assert (call["output_name"], call["output_year"], call["target_value"], call["input_names"]) == (
            "EBITDA",
            2026,
            1000,
            ["Метанол"],
        )
        answer = out["messages"][-1].text
        assert answer == TARGET_ANSWER  # no «н/д»: every number is in the sources (FR-13)
        assert structure_issues(answer) == []
        assert hygiene_issues(answer) == []


class TestCatalogInPrompt:
    """FR-18 «Skills сценариев в поставке»: in the prompt; reading a skill opens no tools."""

    async def test_turn(self, turn: Turn) -> None:
        _, model = await turn.run(
            "Привет",
            [read_skill("scenario-analysis"), read_skill("target-seeking", "r2"), AIMessage("Здравствуйте.")],
            plan_mode="off",  # every tool visible from the first call: only a skill could add one
        )
        system = model.calls[0]["messages"][0]
        assert isinstance(system, SystemMessage)
        for name in ("economic-answer", *SCENARIO_SKILLS):
            assert f"/skills/{name}/SKILL.md" in system.text
        assert "run_scenarios" in model.calls[0]["tools"]
        assert model.calls[0]["tools"] == model.calls[2]["tools"]  # same tools after both skills were read
