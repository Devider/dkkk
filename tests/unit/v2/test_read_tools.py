"""S1.7: ``get_values`` and ``describe_model`` read the model index by ID, without recalculation (ADR-5, design D4)."""

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from tests.unit.v2.fakes import FakeContext, uploaded_store
from tests.unit.v2.test_prechecks import WORKBOOK

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from aigw_service.api.v2.tools.index_builder import build_model_index
from aigw_service.api.v2.tools.read_tools import (
    SHARE_NOTE,
    DescribeModelArgs,
    GetValuesArgs,
    describe_model,
    get_values,
)
from aigw_service.config import APP_CONFIG

INDEX = ModelIndex(
    inputs={"IN1": "Метанол", "IN2": "Инфляция"},
    outputs={"OUT0": "EBITDA", "OUT1": "Операционные показатели", "OUT2": "DSCR"},
    input_values={"IN1": {2025: 380.0, 2026: 386.7}, "IN2": {2026: 0.05}},
    output_years={"OUT0": (2025, 2026), "OUT1": (), "OUT2": (2026,)},
    output_values={"OUT0": {2025: 1100.0, 2026: 1265.94}, "OUT2": {2026: 1.8}},
    units={"IN1": "USD/т", "IN2": "%", "OUT0": "USD млн"},
    output_types={"OUT0": "Общий (обязательный)", "OUT1": "Общий (обязательный)", "OUT2": "Специальный"},
)


def values(index: ModelIndex = INDEX, **args: Any) -> Any:
    return get_values(GetValuesArgs.model_validate(args), FakeContext(model_index=index))


def failure_code(**args: Any) -> str:
    with pytest.raises(ToolError) as exc_info:
        values(**args)
    return exc_info.value.code


class TestGetValues:
    def test_output_for_years(self) -> None:
        out = values(ids=["OUT0"], years=[2025, 2026])
        assert [(n.value, n.label, n.unit) for n in out.numbers] == [
            (1100.0, "EBITDA, 2025", "USD млн"),
            (1265.94, "EBITDA, 2026", "USD млн"),
        ]
        assert out.notes == []

    def test_input_base_values_without_years(self) -> None:
        out = values(ids=["IN1"])
        assert [n.value for n in out.numbers] == [380.0, 386.7]
        assert out.data["IN1"]["values"] == {"2025": 380.0, "2026": 386.7}

    def test_year_without_value_goes_to_notes(self) -> None:
        out = values(ids=["OUT0"], years=[2026, 2040])
        assert [n.value for n in out.numbers] == [1265.94]
        assert out.notes == ["Нет значения: EBITDA — 2040"]

    def test_share_unit_and_note(self) -> None:
        out = values(ids=["IN2"])
        assert out.numbers[0].unit == "доля"
        assert SHARE_NOTE in out.notes

    def test_ids_in_order_one_after_another(self) -> None:
        out = values(ids=["OUT2", "OUT0"])
        assert [n.label for n in out.numbers] == ["DSCR, 2026", "EBITDA, 2025", "EBITDA, 2026"]
        assert out.numbers[0].unit is None

    def test_section_header_is_no_values(self) -> None:
        assert failure_code(ids=["OUT1"]) == ErrorCode.NO_VALUES

    @pytest.mark.parametrize("item_id", ["OUT99", "IN99", "EBITDA"])
    def test_unknown_id(self, item_id: str) -> None:
        assert failure_code(ids=[item_id]) == ErrorCode.UNKNOWN_ID

    def test_no_ids_is_missing_input(self) -> None:
        assert failure_code(ids=[]) == ErrorCode.MISSING_INPUT


class TestDescribeModel:
    def test_card(self) -> None:
        out = describe_model(DescribeModelArgs(), FakeContext(model_index=INDEX))
        assert out.data["horizon"] == {"first": 2025, "last": 2026, "years": 2}
        assert out.data["counts"] == {"inputs": 2, "outputs": 2, "headers": 1}
        assert out.data["by_type"] == {"Общий (обязательный)": 1, "Специальный": 1}
        assert [o["id"] for o in out.data["outputs"]] == ["OUT0", "OUT2"]
        assert "2025–2026" in out.result
        assert "входов: 2" in out.result
        labels = {n.label: n.value for n in out.numbers}
        assert labels["Входов модели"] == 2
        assert labels["Выходов типа «Специальный»"] == 1

    def test_empty_index(self) -> None:
        out = describe_model(DescribeModelArgs(), FakeContext(model_index=ModelIndex()))
        assert out.data["horizon"] is None
        assert out.data["outputs"] == []


@pytest.fixture(scope="module")
def methanex() -> ModelIndex:
    return build_model_index(WORKBOOK)


class TestMethanex:
    def test_revenue_by_id(self, methanex: ModelIndex) -> None:
        revenue = next(k for k, v in methanex.outputs.items() if v == "Выручка")
        out = values(methanex, ids=[revenue], years=[2018])
        assert (out.numbers[0].value, out.numbers[0].unit) == (pytest.approx(3931.847), "USD млн")

    def test_card(self, methanex: ModelIndex) -> None:
        out = describe_model(DescribeModelArgs(), FakeContext(model_index=methanex))
        assert out.data["counts"]["inputs"] == len(methanex.inputs)
        assert out.data["by_type"]["Общий (обязательный)"] > 0

    async def test_long_card_is_offloaded(self, methanex: ModelIndex, tmp_path: Path) -> None:
        store = await uploaded_store("u1", methanex, str(tmp_path))
        agent = AgentFactory(
            ScriptedChatModel(steps=[tool_call("describe_model"), AIMessage("ок")]),
            default_catalog(),
            store=store,
            upload_dir=str(tmp_path),
            plan_mode="off",
            pinned_skills=(),
        ).build()
        out = await agent.ainvoke(
            {"messages": [HumanMessage("расскажи о модели")]}, context=RequestContext(user_id="u1", session_id="s1")
        )
        shown = next(m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == "describe_model")
        envelope = json.loads(shown.content)
        assert len(shown.content) <= APP_CONFIG.agent.result_max_chars
        assert envelope["ref"].startswith("/results/")
        assert "data" not in envelope
