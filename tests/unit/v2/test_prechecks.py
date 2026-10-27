"""S1.4: model index with years/values, calculation prechecks before the engine (FR-6, FR-10), reachable (FR-11)."""

import json
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from tests.unit.v2.fakes import FakeContext, ScriptedChatModel, tool_call

from aigw_service.api.v1.subagents.utils import _build_catalog_with_ids
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.calc_tools import target_envelope
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from aigw_service.api.v2.tools.index_builder import build_model_index
from aigw_service.api.v2.tools.prechecks import GridInput, check_scenarios, check_target
from aigw_service.config.agent.config import AgentSettings
from excel_engine import ExcelAnalysisToolResult, ModelInputAnalysisToolResult

WORKBOOK = str(Path(__file__).resolve().parents[3] / "models" / "Methanex_FinModel.xlsx")
REAL = json.loads((Path(__file__).parent / "data" / "calc_results.json").read_text(encoding="utf-8"))
YEARS = tuple(range(2018, 2033))
INDEX = ModelIndex(
    inputs={"IN1": "Метанол", "IN2": "Инфляция", "IN3": "Коэффициент цены", "IN4": "Курс USD/RUB"},
    outputs={"OUT0": "EBITDA", "OUT1": "Прибыль от реализации"},
    input_values={"IN1": {2026: 386.7}, "IN2": {2025: 0.05}, "IN3": {2026: 1.0}, "IN4": {2026: 95.0}},
    output_years={"OUT0": YEARS, "OUT1": ()},
)
MAX_GRID = 400


def grid(*items: tuple) -> list[GridInput]:
    return [GridInput(*i) for i in items]


def code_of(call: Any) -> str:
    with pytest.raises(ToolError) as exc_info:
        call()
    return exc_info.value.code


@pytest.fixture(scope="module")
def methanex() -> ModelIndex:
    return build_model_index(WORKBOOK)


class TestIndexBuilder:
    def test_same_ids_as_v1_catalog(self, methanex: ModelIndex) -> None:
        inputs, outputs = _build_catalog_with_ids(WORKBOOK)
        assert dict(methanex.inputs) == inputs
        assert dict(methanex.outputs) == outputs

    def test_values_and_years(self, methanex: ModelIndex) -> None:
        methanol = next(k for k, v in methanex.inputs.items() if v == "Метанол")
        ebitda = next(k for k, v in methanex.outputs.items() if v == "EBITDA")
        empty = next(k for k, v in methanex.outputs.items() if v == "Прибыль от реализации")
        assert methanex.input_values[methanol][2026] == pytest.approx(386.7, abs=0.1)
        assert methanex.output_years[ebitda][0] == 2018
        assert methanex.output_years[empty] == ()

    def test_output_values_units_and_types(self, methanex: ModelIndex) -> None:
        revenue = next(k for k, v in methanex.outputs.items() if v == "Выручка")
        assert methanex.output_values[revenue][2018] == pytest.approx(3931.847)
        assert methanex.units[revenue] == "USD млн"
        assert methanex.output_types[revenue] == "Общий (обязательный)"
        growth = next(k for k, v in methanex.inputs.items() if v == "Рост реального ВВП")
        assert methanex.units[growth] == "%"

    def test_placeholder_unit_is_skipped(self, methanex: ModelIndex) -> None:
        assert "[требуется заполнить]" not in set(methanex.units.values())

    def test_registry_round_trip_keeps_new_fields(self, methanex: ModelIndex) -> None:
        from aigw_service.api.v2.turn.registry import _dump, _load

        restored = _load(_dump(methanex))
        assert restored.output_values == methanex.output_values
        assert restored.units == methanex.units
        assert restored.output_types == methanex.output_types

    def test_registry_record_without_new_fields(self) -> None:
        from aigw_service.api.v2.turn.registry import _load

        old = {"inputs": {"IN0": "a"}, "outputs": {"OUT0": "b"}, "input_values": {}, "output_years": {"OUT0": [2026]}}
        index = _load(old)
        assert dict(index.output_values) == {}
        assert dict(index.units) == {}


class TestMaxGridSetting:
    def test_default_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert AgentSettings().max_grid == 400
        monkeypatch.setenv("AGENT_MAX_GRID", "1000")
        assert AgentSettings().max_grid == 1000


SCENARIO_ERRORS = [
    ("no year", grid(("IN1", 300, 500, 100)), ["OUT0"], None, ErrorCode.MISSING_INPUT),
    ("no range", grid(("IN1", None, None, None)), ["OUT0"], 2026, ErrorCode.MISSING_INPUT),
    ("no outputs", grid(("IN1", 300, 500, 100)), [], 2026, ErrorCode.MISSING_INPUT),
    ("unknown input", grid(("IN99", 300, 500, 100)), ["OUT0"], 2026, ErrorCode.UNKNOWN_ID),
    ("unknown output", grid(("IN1", 300, 500, 100)), ["OUT99"], 2026, ErrorCode.UNKNOWN_ID),
    ("header output", grid(("IN1", 300, 500, 100)), ["OUT1"], 2026, ErrorCode.NO_VALUES),
    ("year out of horizon", grid(("IN1", 300, 500, 100)), ["OUT0"], 2040, ErrorCode.INVALID_ARGS),
    ("min > max", grid(("IN1", 500, 300, 100)), ["OUT0"], 2026, ErrorCode.INVALID_ARGS),
    ("zero step", grid(("IN1", 300, 500, 0)), ["OUT0"], 2026, ErrorCode.INVALID_ARGS),
    ("grid too large", grid(("IN1", 300, 500, 10), ("IN4", 80, 100, 1)), ["OUT0"], 2026, ErrorCode.INVALID_ARGS),
    ("percent for a share", grid(("IN2", 5, 10, 1)), ["OUT0"], 2026, ErrorCode.BAD_UNITS),
    ("share for a big number", grid(("IN4", 0.9, 1.1, 0.1)), ["OUT0"], 2026, ErrorCode.BAD_UNITS),
]
SCENARIO_OK = [
    ("price range", grid(("IN1", 300, 500, 100))),
    ("single point", grid(("IN1", 450, 450, 1))),
    ("coefficient around 1", grid(("IN3", 0.8, 1.2, 0.1))),
    ("share as share", grid(("IN2", 0.03, 0.08, 0.01))),
    ("exactly at limit", grid(("IN1", 0, 399, 1))),
]


class TestScenarioChecks:
    @pytest.mark.parametrize(
        ("_name", "items", "outputs", "year", "code"), SCENARIO_ERRORS, ids=[c[0] for c in SCENARIO_ERRORS]
    )
    def test_errors(self, _name: str, items: list, outputs: list, year: int | None, code: ErrorCode) -> None:
        assert code_of(lambda: check_scenarios(INDEX, items, outputs, year, MAX_GRID)) == code

    @pytest.mark.parametrize(("_name", "items"), SCENARIO_OK, ids=[c[0] for c in SCENARIO_OK])
    def test_allowed(self, _name: str, items: list) -> None:
        check_scenarios(INDEX, items, ["OUT0"], 2026, MAX_GRID)

    def test_missing_is_reported_before_unknown_id(self) -> None:
        assert (
            code_of(lambda: check_scenarios(INDEX, grid(("IN99", None, None, None)), ["OUT0"], 2026, MAX_GRID))
            == ErrorCode.MISSING_INPUT
        )

    def test_all_missing_named_at_once(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            check_scenarios(INDEX, grid(("IN1", None, None, None)), [], None, MAX_GRID)
        assert "год расчёта" in exc_info.value.message
        assert "выходы" in exc_info.value.message
        assert "Метанол" in exc_info.value.message

    def test_index_without_years_skips_year_and_units_checks(self) -> None:
        bare = ModelIndex(dict(INDEX.inputs), dict(INDEX.outputs))
        check_scenarios(bare, grid(("IN2", 5, 10, 1)), ["OUT1"], 2099, MAX_GRID)

    def test_bad_units_shows_base_value(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            check_scenarios(INDEX, grid(("IN2", 5, 10, 1)), ["OUT0"], 2026, MAX_GRID)
        assert "0,05" in exc_info.value.message


class TestTargetChecks:
    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"year": None}, ErrorCode.MISSING_INPUT),
            ({"target": None}, ErrorCode.MISSING_INPUT),
            ({"input_ids": []}, ErrorCode.MISSING_INPUT),
            ({"output_id": "OUT99"}, ErrorCode.UNKNOWN_ID),
            ({"input_ids": ["IN99"]}, ErrorCode.UNKNOWN_ID),
            ({"output_id": "OUT1"}, ErrorCode.NO_VALUES),
            ({"year": 2040}, ErrorCode.INVALID_ARGS),
            ({"tolerance": 0}, ErrorCode.INVALID_ARGS),
        ],
    )
    def test_errors(self, kwargs: dict, code: ErrorCode) -> None:
        args = {"output_id": "OUT0", "year": 2026, "target": 1000.0, "input_ids": ["IN1"], "tolerance": 0.1} | kwargs
        assert code_of(lambda: check_target(INDEX, **args)) == code

    def test_valid(self) -> None:
        check_target(INDEX, "OUT0", 2026, 1000.0, ["IN1"], 0.1)


class TestDomainNotCalledOnError:
    """FR-10 / §10 п. 2: every precheck failure stops before the engine; a missing year is not a schema error."""

    @pytest.fixture
    def domain(self, monkeypatch: pytest.MonkeyPatch) -> list:
        calls: list = []
        monkeypatch.setattr(
            calc_tools,
            "analyze_excel_model",
            lambda **kw: calls.append(kw) or ExcelAnalysisToolResult(status="OK", result="", content={}),
        )
        monkeypatch.setattr(
            calc_tools,
            "analyze_model_inputs_for_target",
            lambda **kw: calls.append(kw) or ModelInputAnalysisToolResult(status="OK", result="", content={}),
        )
        return calls

    async def envelope(self, call: AIMessage) -> dict:
        agent = create_agent(
            ScriptedChatModel(steps=[call, AIMessage("ок")]),
            default_catalog().base_tools(),
            context_schema=FakeContext,
        )
        out = await agent.ainvoke({"messages": [HumanMessage("ход")]}, context=FakeContext(model_index=INDEX))
        return json.loads(next(m for m in out["messages"] if isinstance(m, ToolMessage)).content)

    async def test_missing_year_is_missing_input(self, domain: list) -> None:
        env = await self.envelope(
            tool_call("run_scenarios", inputs=[{"id": "IN1", "min": 300, "max": 500, "step": 100}], outputs=["OUT0"])
        )
        assert env["code"] == "missing_input"
        assert not domain

    async def test_bad_units_stops_engine(self, domain: list) -> None:
        env = await self.envelope(
            tool_call(
                "run_scenarios", inputs=[{"id": "IN2", "min": 5, "max": 10, "step": 1}], outputs=["OUT0"], year=2026
            )
        )
        assert env["code"] == "bad_units"
        assert not domain

    async def test_missing_target_is_missing_input(self, domain: list) -> None:
        env = await self.envelope(tool_call("find_input_for_target", output_id="OUT0", year=2026, input_ids=["IN1"]))
        assert env["code"] == "missing_input"
        assert not domain

    async def test_valid_call_reaches_engine(self, domain: list) -> None:
        env = await self.envelope(
            tool_call(
                "run_scenarios",
                inputs=[{"id": "IN1", "min": 300, "max": 500, "step": 100}],
                outputs=["OUT0"],
                year=2026,
            )
        )
        assert env["status"] == "ok"
        assert len(domain) == 1


class TestReachable:
    """FR-11 «Недостижимая цель — результат анализа»."""

    def test_reached(self) -> None:
        env = target_envelope(ModelInputAnalysisToolResult(**REAL["ift_ok"]), "EBITDA", 2026)
        assert env.data["reachable"] is True

    def test_unreached_is_success_with_nearest_point(self) -> None:
        env = target_envelope(ModelInputAnalysisToolResult(**REAL["ift_unreachable"]), "EBITDA", 2026)
        assert env.status == "ok"
        assert env.data["reachable"] is False
        assert env.data["scenarios"]
        assert env.notes
