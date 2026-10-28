"""S3.4 (FR-18): skills covenant-headroom and sensitivity-drivers — texts, evals.yaml, shipped catalogue, turns.

Turns run through the real ``AgentFactory`` and catalog with the shipped skills on a scripted model (design D7);
the engine is replaced, ``trace_dependencies`` walks a small workbook written into the upload folder.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any

import openpyxl
import pandas as pd
import pytest
from langchain_core.messages import AIMessage, SystemMessage
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_scenario_skills import evals, plan, read_skill, tool_names, tool_results
from tests.unit.v2.test_summarization import BUDGET, PADDING, Session, SummarizingModel, compressed_calls

from aigw_service.api.v2.answer_check import hygiene_issues, is_analytic, structure_issues
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.skills_check import SKILLS_DIR, validate_skills
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.resolve_terms import Pick
from excel_engine import ExcelAnalysisToolResult

NEW_SKILLS = ("covenant-headroom", "sensitivity-drivers")
ALL_SKILLS = ("economic-answer", "target-seeking", "scenario-analysis", *NEW_SKILLS)
YEARS = (2026, 2027)
INDEX = ModelIndex(
    inputs={"IN0": "Метанол", "IN1": "Курс USD", "IN2": "Капекс"},
    outputs={"OUT0": "Выручка", "OUT1": "EBITDA", "OUT2": "DSCR"},
    input_values={"IN0": {2026: 400.0, 2027: 380.0}, "IN1": {2026: 90.0, 2027: 92.0}, "IN2": {2026: 50.0, 2027: 50.0}},
    output_years={"OUT0": YEARS, "OUT1": YEARS, "OUT2": YEARS},
    output_values={
        "OUT0": {2026: 36000.0, 2027: 34960.0},
        "OUT1": {2026: 950.0, 2027: 900.0},
        "OUT2": {2026: 1.45, 2027: 1.3},
    },
    units={"IN0": "USD/т", "IN1": "руб./USD", "OUT1": "млн USD"},
)


def body(skill: str, path: str = "SKILL.md") -> str:
    return (SKILLS_DIR / skill / path).read_text(encoding="utf-8")


def write_workbook(path: Path) -> None:
    """Rows in the order of INDEX: EBITDA ← Выручка ← Метанол × Курс; DSCR ← EBITDA / Капекс."""
    wb = openpyxl.Workbook()
    inputs = wb.active
    inputs.title = "Inputs"
    calc, outputs = wb.create_sheet("Calc"), wb.create_sheet("Outputs")
    for sheet in (inputs, outputs):
        sheet.append(["Наименование", *YEARS])
    for name, values in [("Метанол", (400, 380)), ("Курс USD", (90, 92)), ("Капекс", (50, 50))]:
        inputs.append([name, *values])
    calc["B5"], calc["C5"] = "=Inputs!B2*Inputs!B3", "=Inputs!C2*Inputs!C3"
    outputs.append(["Выручка", "=Calc!B5", "=Calc!C5"])
    outputs.append(["EBITDA", "=B2/36-50", "=C2/36-71"])
    outputs.append(["DSCR", "=B3/Inputs!B4/13", "=C3/Inputs!C4/13"])
    wb.save(path)


# ---------------------------------------------------------------- texts, references, evals.yaml, shipped catalogue


class TestSkillTexts:
    """FR-18: plan template in §11 order, threshold only from the user, default shift, branch «why»."""

    @staticmethod
    def in_order(text: str, *parts: str) -> bool:
        positions = [text.index(p) for p in parts]
        return positions == sorted(positions)

    def test_covenant_plan_and_rules(self) -> None:
        text = body("covenant-headroom")
        assert len(text.splitlines()) <= 300
        plan_part = text[text.index("## Шаблон плана") : text.index("## Шаг 1")]
        assert self.in_order(plan_part, "Метрики и пороги", "База", "Стресс-сетка", "Точка пробоя", "Запас прочности")
        assert "**Порог — только из сообщения пользователя.**" in text
        assert "не указан порог" in text
        assert "`blocked`" in text
        assert "не предлагай число в вопросе" in text
        assert "Нет года" in text
        assert "`direction: min`" in text
        assert "`direction: max`" in text
        assert "references/covenants.md" in text
        for operation in ("`headroom`", "`crossing`", "`pct_change`"):
            assert operation in text

    def test_covenants_reference_has_directions_and_no_thresholds(self) -> None:
        text = body("covenant-headroom", "references/covenants.md")
        table = text[text.index("## Показатели") : text.index("## Как читать")]
        for metric in ("DSCR", "ICR", "LLCR", "Debt/EBITDA", "Net Debt/EBITDA"):
            assert metric in table
        assert "не ниже порога" in table
        assert "не выше порога" in table
        assert not re.search(r"\d+[.,]\d+|\b\d+(?:\s*x|\s*раз)\b", table)  # no threshold numbers (user decision)

    def test_sensitivity_plan_and_rules(self) -> None:
        text = body("sensitivity-drivers")
        assert len(text.splitlines()) <= 300
        plan_part = text[text.index("## Ветка А. Шаблон плана") : text.index("### Шаг 1")]
        assert self.in_order(plan_part, "Драйверы", "Сдвиг", "Эластичности", "Рейтинг", "Связи в модели")
        assert "**Отдельный вызов `run_scenarios` на каждый драйвер**" in text
        assert "±10 %" in text
        assert "**не спрашивай**" in text
        assert "`step` = `max` − `min`" in text  # two points; a rounded-up step would leave one
        assert "derive`, операция `pct_change`, `a` — база первого драйвера" in text  # the shift is a source
        assert "это не разложение вклада" in text
        assert "Пересчёт (`run_scenarios`) в этой ветке не нужен" in text
        assert "`skill_required`" in text

    @pytest.mark.parametrize("skill", NEW_SKILLS)
    def test_evals_yaml(self, skill: str) -> None:
        known_skills = {s.name for s in validate_skills(SKILLS_DIR, tool_names())}
        cases = evals(skill)
        assert len(cases) >= 5
        assert len({c["id"] for c in cases}) == len(cases)
        for case in cases:
            expect = case["expect"]
            assert case["query"].strip()
            assert expect["skill"] in known_skills
            assert set(expect["tools"]) <= tool_names()
            assert expect["answer"] in {"analytic", "question"}
            assert isinstance(expect["plan_includes"], list)
            assert isinstance(expect["caveat_includes"], list)
        assert {c["expect"]["answer"] for c in cases} == {"analytic", "question"}

    def test_sensitivity_evals_expect_the_skill_tool(self) -> None:
        cases = {c["id"]: c["expect"] for c in evals("sensitivity-drivers")}
        assert "trace_dependencies" in cases["SD1"]["tools"]
        assert "trace_dependencies" in cases["SD3"]["tools"]
        assert "run_scenarios" not in cases["SD3"]["tools"]


class TestShipped:
    """FR-18 «Skills сценариев в поставке»; ADR-5: 8 tools before a skill is read, 9 after sensitivity-drivers."""

    def test_all_valid(self) -> None:
        skills = {s.name: s for s in validate_skills(SKILLS_DIR, tool_names())}
        assert set(ALL_SKILLS) <= set(skills)
        assert skills["sensitivity-drivers"].include_tools == ("trace_dependencies",)
        assert not skills["covenant-headroom"].include_tools

    async def test_catalogue_and_tools(self, tmp_path: Path) -> None:
        model = ScriptedChatModel(
            steps=[
                read_skill("covenant-headroom"),
                read_skill("sensitivity-drivers", "r2"),
                AIMessage("Здравствуйте."),
            ]
        )
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode="off").build()
        await agent.ainvoke({"messages": [("user", "Привет")]}, context=RequestContext(user_id="u1", session_id="s1"))
        system = model.calls[0]["messages"][0]
        assert isinstance(system, SystemMessage)
        for name in ALL_SKILLS:
            assert f"/skills/{name}/SKILL.md" in system.text
        assert len(model.calls[0]["tools"]) == 8
        assert "trace_dependencies" not in model.calls[0]["tools"]
        assert model.calls[1]["tools"] == model.calls[0]["tools"]  # covenant-headroom opens nothing
        assert len(model.calls[2]["tools"]) == 9
        assert "trace_dependencies" in model.calls[2]["tools"]


# ---------------------------------------------------------------- turns on the scripted model (design D7)


def grid(input_name: str, output: str, year: int, points: dict[float, float]) -> ExcelAnalysisToolResult:
    frame = pd.DataFrame([{input_name: x, f"{output}_{year}": y} for x, y in points.items()])
    return ExcelAnalysisToolResult(status="OK", result="таблица", content={"result_df": frame})


class Turn:
    """One turn: shipped skills, real catalog, engine replaced by grids per input, workbook for trace_dependencies."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.picks: list[Pick] = []
        self.grids: dict[str, ExcelAnalysisToolResult] = {}
        self.engine_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(rt, "analyze_with_v1", self._analyzer)
        monkeypatch.setattr(calc_tools, "analyze_excel_model", self._grid)

    async def _analyzer(self, _message: str, _index: object) -> list[Pick]:
        return self.picks

    def _grid(self, **kwargs: Any) -> ExcelAnalysisToolResult:
        self.engine_calls.append(kwargs)
        return self.grids[kwargs["input_names"][0]]

    async def run(self, question: str, steps: list, plan_mode: str = "auto") -> dict:
        store = await uploaded_store("u1", INDEX, str(self.tmp_path))
        write_workbook(self.tmp_path / "user_file_u1.xlsx")
        agent = AgentFactory(
            ScriptedChatModel(steps=steps),
            default_catalog(),
            store=store,
            upload_dir=str(self.tmp_path),
            plan_mode=plan_mode,
        ).build()
        return await agent.ainvoke(
            {"messages": [("user", question)]}, context=RequestContext(user_id="u1", session_id="s1")
        )


@pytest.fixture
def turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inline_gate: object) -> Turn:
    return Turn(monkeypatch, tmp_path)


def checked(out: dict, expected: str) -> str:
    """The answer passed AnswerGroundingGuard unchanged (FR-13) and has no structure or hygiene issues."""
    answer = out["messages"][-1].text
    assert answer == expected
    assert structure_issues(answer) == []
    assert hygiene_issues(answer) == []
    return answer


def resolve(*terms: tuple[str, str]) -> AIMessage:
    return tool_call("resolve_terms", "t1", terms=[{"text": t, "role": r} for t, r in terms])


U2_PLAN = (
    "Метрики и пороги: DSCR, цена метанола, порог 1,2",
    "База DSCR и цены метанола в 2026, запас до порога",
    "Стресс-сетка DSCR 2026 при падении цены на 10–30 %",
    "Точка пробоя",
    "Запас прочности цены",
    "Вывод",
)
U2_ANSWER = """\
## Вывод
Запаса по DSCR 2026 хватает: 1,45 против порога 1,2. Ковенант нарушается при цене метанола 310 USD/т — на 22,5 % ниже базы.

## Ключевые цифры
| Показатель | Год | Значение | Ед. |
| --- | --- | --- | --- |
| DSCR, база | 2026 | 1,45 | раз |
| Запас до порога 1,2 | 2026 | 0,25 | раз |
| Запас, % от порога | 2026 | 20,8 | % |
| DSCR при Метанол = 280 | 2026 | 1,05 | раз |
| DSCR при Метанол = 360 | 2026 | 1,45 | раз |
| Цена пробоя | 2026 | 310 | USD/т |

## Анализ
При падении цены на 10–30 % DSCR снижается равномерно и пересекает порог между точками 300 и 320.

## Оговорки
Порог 1,2 взят из запроса, ковенант — не ниже порога. Шаг не назван — сетка из 5 точек: 280, 300, 320, 340, 360. Точка пробоя получена линейной интерполяцией.
"""


class TestCovenantU2:
    """FR-18 «Запас и точка пробоя ковенанта (U2)»."""

    async def test_turn(self, turn: Turn) -> None:
        turn.picks = [Pick("input", "цена метанола", "IN0"), Pick("output", "DSCR", "OUT2")]
        turn.grids["Метанол"] = grid(
            "Метанол", "DSCR", 2026, {280.0: 1.05, 300.0: 1.15, 320.0: 1.25, 340.0: 1.35, 360.0: 1.45}
        )
        points = [
            {"x": 280, "y": 1.05},
            {"x": 300, "y": 1.15},
            {"x": 320, "y": 1.25},
            {"x": 340, "y": 1.35},
            {"x": 360, "y": 1.45},
        ]
        out = await turn.run(
            "DSCR 2026 при падении цены метанола на 10–30 %: при какой цене DSCR = 1,2 и хватает ли запаса?",
            [
                read_skill("covenant-headroom"),
                tool_call("write_todos", "c1", todos=plan(*U2_PLAN)),
                resolve(("цена метанола", "input"), ("DSCR", "output")),
                tool_call("get_values", "c3", ids=["OUT2", "IN0"], years=[2026]),
                tool_call(
                    "derive",
                    "c4",
                    operation="headroom",
                    a={"value": 1.45, "label": "DSCR 2026"},
                    threshold=1.2,
                    direction="min",
                ),
                tool_call(
                    "run_scenarios",
                    "c5",
                    inputs=[{"id": "IN0", "min": 280, "max": 360, "step": 20}],
                    outputs=["OUT2"],
                    year=2026,
                ),
                tool_call("derive", "c6", operation="crossing", points=points, threshold=1.2),
                tool_call(
                    "derive",
                    "c7",
                    operation="pct_change",
                    a={"value": 400, "label": "Метанол, база"},
                    b={"value": 310, "label": "Метанол, пробой"},
                ),
                tool_call("write_todos", "c8", todos=plan(*U2_PLAN, status="completed")),
                AIMessage(U2_ANSWER),
            ],
        )
        results = tool_results(out)
        assert "Запас по ковенантам" in str(results[0][1])
        derived = [r for n, r in results if n == "derive"]
        assert [r["status"] for r in derived] == ["ok", "ok", "ok"]
        assert derived[0]["data"]["operands"][1]["source"] == "сообщение пользователя"  # threshold from the query
        assert [n["value"] for n in derived[1]["numbers"]] == [pytest.approx(310)]
        assert [t["status"] for t in out["todos"]] == ["completed"] * len(U2_PLAN)
        assert turn.engine_calls[0]["ranges"] == [[280, 360]]
        checked(out, U2_ANSWER)


NO_THRESHOLD_QUESTION = "Какой порог по DSCR установлен в кредитной документации?"


class TestNoThreshold:
    """FR-18 «Порог ковенанта не назван» (FR-10)."""

    async def test_turn(self, turn: Turn) -> None:
        turn.picks = [Pick("output", "DSCR", "OUT2")]
        blocked = [
            {"content": "Метрики и пороги: DSCR", "status": "blocked", "reason": "не указан порог"},
        ]
        out = await turn.run(
            "Хватит ли запаса по DSCR в 2026 году?",
            [
                read_skill("covenant-headroom"),
                tool_call("write_todos", "c1", todos=plan("Метрики и пороги: DSCR", "База и запас", "Вывод")),
                resolve(("DSCR", "output")),
                tool_call("write_todos", "c3", todos=blocked),
                AIMessage(NO_THRESHOLD_QUESTION),
            ],
        )
        assert "derive" not in [n for n, _ in tool_results(out)]
        assert turn.engine_calls == []
        assert [t["status"] for t in out["todos"]] == ["blocked"]
        answer = out["messages"][-1].text
        assert answer == NO_THRESHOLD_QUESTION
        assert not is_analytic(answer)
        assert not re.search(r"\d", answer.replace("2026", ""))  # no threshold number the user did not give


RANK_PLAN = (
    "Драйверы EBITDA 2026",
    "Сдвиг ±10 % по каждому драйверу",
    "Эластичности",
    "Рейтинг драйверов",
    "Связи в модели",
    "Вывод",
)
RANK_ANSWER = """\
## Вывод
Сильнее всего EBITDA 2026 зависит от цены метанола: эластичность 1,06 против 0,5 у курса.

## Ключевые цифры
| Драйвер | База | EBITDA при −10 % | EBITDA при +10 % | Эластичность |
| --- | --- | --- | --- | --- |
| Метанол | 400 | 850 | 1 050 | 1,06 |
| Курс USD | 90 | 900 | 1 000 | 0,5 |

## Анализ
Оба драйвера действуют в одну сторону: рост цены и курса увеличивает EBITDA; цена метанола влияет сильнее.

## Где в модели
Метанол и курс → Выручка → EBITDA.

## Оговорки
Сдвиг ±10 % от базы выбран по умолчанию; эластичность посчитана по двум точкам и верна для небольших сдвигов.
"""


class TestRanking:
    """FR-18 «Рейтинг драйверов» (ветка А)."""

    async def test_turn(self, turn: Turn) -> None:
        turn.picks = [Pick("output", "EBITDA", "OUT1")]
        turn.grids["Метанол"] = grid("Метанол", "EBITDA", 2026, {360.0: 850.0, 440.0: 1050.0})
        turn.grids["Курс USD"] = grid("Курс USD", "EBITDA", 2026, {81.0: 900.0, 99.0: 1000.0})
        out = await turn.run(
            "Что сильнее влияет на EBITDA 2026?",
            [
                read_skill("sensitivity-drivers"),
                tool_call("write_todos", "c1", todos=plan(*RANK_PLAN)),
                resolve(("EBITDA", "output")),
                tool_call("trace_dependencies", "c3", output_id="OUT1", year=2026),
                tool_call("get_values", "c4", ids=["IN0", "IN1"], years=[2026]),
                tool_call(
                    "run_scenarios",
                    "c5",
                    inputs=[{"id": "IN0", "min": 360, "max": 440, "step": 80}],
                    outputs=["OUT1"],
                    year=2026,
                ),
                tool_call(
                    "run_scenarios",
                    "c6",
                    inputs=[{"id": "IN1", "min": 81, "max": 99, "step": 18}],
                    outputs=["OUT1"],
                    year=2026,
                ),
                tool_call(
                    "derive", "c7", operation="elasticity", points=[{"x": 360, "y": 850}, {"x": 440, "y": 1050}]
                ),
                tool_call("derive", "c8", operation="elasticity", points=[{"x": 81, "y": 900}, {"x": 99, "y": 1000}]),
                tool_call(
                    "derive",
                    "c9",
                    operation="pct_change",
                    a={"value": 400, "label": "Метанол, база"},
                    b={"value": 440, "label": "Метанол, +сдвиг"},
                ),
                tool_call("write_todos", "c10", todos=plan(*RANK_PLAN, status="completed")),
                AIMessage(RANK_ANSWER),
            ],
        )
        results = {n: r for n, r in tool_results(out) if n == "trace_dependencies"}
        traced = results["trace_dependencies"]
        assert traced["status"] == "ok"
        assert [i["id"] for i in traced["data"]["inputs"]] == ["IN0", "IN1"]  # Капекс does not feed EBITDA
        assert traced["data"]["outputs"] == [{"id": "OUT0", "name": "Выручка", "depth": 1}]
        assert [c["input_names"] for c in turn.engine_calls] == [["Метанол"], ["Курс USD"]]  # one grid per driver
        elasticities = [r["numbers"][0]["value"] for n, r in tool_results(out) if n == "derive"][:2]
        assert elasticities == [pytest.approx(1.0588, abs=1e-3), pytest.approx(0.5)]
        checked(out, RANK_ANSWER)


WHY_PLAN = (
    "Сопоставить DSCR",
    "Значения DSCR за 2026 и 2027",
    "Связи DSCR в модели",
    "Значения входов за оба года",
    "Изменения входов",
    "Вывод",
)
WHY_ANSWER = """\
## Вывод
DSCR снизился с 1,45 в 2026 до 1,3 в 2027 — на 10,3 %. По времени это совпало с падением цены метанола на 5 %.

## Ключевые цифры
| Показатель | 2026 | 2027 | Изменение, % |
| --- | --- | --- | --- |
| DSCR | 1,45 | 1,3 | −10,3 |
| Метанол, USD/т | 400 | 380 | −5 |
| Курс USD | 90 | 92 | 2,22 |
| Капекс | 50 | 50 | 0 |

## Где в модели
Метанол и курс → Выручка → EBITDA → DSCR; Капекс входит в DSCR напрямую.

## Оговорки
Изменения входов совпали по времени с изменением показателя; это не разложение вклада каждого входа.
"""


class TestWhyDropped:
    """FR-18 «Почему просел показатель (U8)» (ветка Б): no recalculation."""

    async def test_turn(self, turn: Turn) -> None:
        turn.picks = [Pick("output", "DSCR", "OUT2")]
        out = await turn.run(
            "Почему DSCR 2027 ниже, чем в 2026?",
            [
                read_skill("sensitivity-drivers"),
                tool_call("write_todos", "c1", todos=plan(*WHY_PLAN)),
                resolve(("DSCR", "output")),
                tool_call("get_values", "c3", ids=["OUT2"], years=[2026, 2027]),
                tool_call("trace_dependencies", "c4", output_id="OUT2", year=2027),
                tool_call("get_values", "c5", ids=["IN0", "IN1", "IN2"], years=[2026, 2027]),
                tool_call(
                    "derive",
                    "c6",
                    operation="pct_change",
                    a={"value": 1.45, "label": "DSCR 2026"},
                    b={"value": 1.3, "label": "DSCR 2027"},
                ),
                tool_call(
                    "derive",
                    "c7",
                    operation="pct_change",
                    a={"value": 400, "label": "Метанол 2026"},
                    b={"value": 380, "label": "Метанол 2027"},
                ),
                tool_call(
                    "derive",
                    "c8",
                    operation="pct_change",
                    a={"value": 90, "label": "Курс 2026"},
                    b={"value": 92, "label": "Курс 2027"},
                ),
                tool_call("write_todos", "c9", todos=plan(*WHY_PLAN, status="completed")),
                AIMessage(WHY_ANSWER),
            ],
        )
        names = [n for n, _ in tool_results(out)]
        assert "run_scenarios" not in names
        assert turn.engine_calls == []
        traced = dict(tool_results(out))["trace_dependencies"]
        assert {i["id"] for i in traced["data"]["inputs"]} == {"IN0", "IN1", "IN2"}
        assert {o["id"] for o in traced["data"]["outputs"]} == {"OUT0", "OUT1"}
        checked(out, WHY_ANSWER)


class TestHiddenTool:
    """FR-18 «Скрытый инструмент до чтения отклоняется» / «после чтения выполняется» / «неизвестный инструмент»."""

    async def test_refused_then_opened(self, turn: Turn) -> None:
        out = await turn.run(
            "От чего зависит EBITDA 2026?",
            [
                tool_call("trace_dependencies", "c1", output_id="OUT1", year=2026),
                tool_call("write_file", "w1", file_path="/skills/x.md", content="x"),
                read_skill("sensitivity-drivers"),
                tool_call("trace_dependencies", "c2", output_id="OUT1", year=2026),
                AIMessage("EBITDA зависит от цены метанола и курса."),
            ],
            plan_mode="off",
        )
        results = tool_results(out)
        refused, unknown, _read, traced = results
        assert refused[1]["code"] == "skill_required"
        assert "/skills/sensitivity-drivers/SKILL.md" in refused[1]["hint"]
        assert "skill_required" not in str(unknown[1])
        assert "not a valid tool" in str(unknown[1])
        assert traced[1]["status"] == "ok"
        assert [s["tool"] for s in out["step_results"]] == ["trace_dependencies"]  # the refused call never ran

    async def test_hidden_again_after_compression(self, tmp_path: Path) -> None:
        """FR-18 «Инструмент снова скрыт после сжатия истории»: skill_required with the same path."""
        skills = tmp_path / "skills"
        shutil.copytree(SKILLS_DIR / "sensitivity-drivers", skills / "sensitivity-drivers")
        (skills / "pad-skill").mkdir()
        (skills / "pad-skill" / "SKILL.md").write_text(
            f"---\nname: pad-skill\ndescription: Справочник. Reference.\n---\n\n# Справочник\n\n{PADDING * 2}",
            encoding="utf-8",
        )
        steps = [
            read_skill("sensitivity-drivers", "s1"),
            *[tool_call("read_file", f"x{i}", file_path="/skills/pad-skill/SKILL.md", limit=400) for i in range(6)],
            tool_call("trace_dependencies", "c1", output_id="OUT1", year=2026),
            read_skill("sensitivity-drivers", "s2"),
            tool_call("trace_dependencies", "c2", output_id="OUT1", year=2026),
            AIMessage("Готово."),
        ]
        model = SummarizingModel(steps=steps)
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        write_workbook(tmp_path / "user_file_u1.xlsx")
        session = Session(model, store, tmp_path, plan_mode="off", skills_dir=skills, pinned_skills=(), budget=BUDGET)
        out = await session.ask("От чего зависит EBITDA 2026?")
        assert compressed_calls(model)  # the first read went into the summary
        traced = [
            (m.tool_call_id, json.loads(m.content))
            for m in out["messages"]
            if getattr(m, "name", "") == "trace_dependencies"
        ]
        assert traced[0][1]["code"] == "skill_required"
        assert "/skills/sensitivity-drivers/SKILL.md" in traced[0][1]["hint"]
        assert traced[1][1]["status"] == "ok"
