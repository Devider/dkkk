"""S1.1/S1.2: v2 calculation tools (IDs + context → envelope) match a direct excel_engine call (needs LibreOffice)."""

import json
import random
import re
from pathlib import Path

import pytest

pytest.importorskip("uno", reason="LibreOffice (python3-uno) is required; run inside the Docker image")

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from tests.unit.v2.fakes import FakeContext, ScriptedChatModel, tool_call

import excel_engine.calculations as engine
from aigw_service.api.v1.subagents.utils import _build_catalog_with_ids
from aigw_service.api.v2.tools.calc_tools import scenarios_envelope, target_envelope
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex

WORKBOOK = str(Path(__file__).resolve().parents[3] / "models" / "Methanex_FinModel.xlsx")
VOLATILE = [
    (re.compile(r"\d{8}_\d{6}"), "<ts>"),
    (re.compile(r'("\w*seconds\w*": )[\d.]+'), r"\1<elapsed>"),
    (re.compile(r"(за )[\d.]+( сек)"), r"\1<elapsed>\2"),
]


def normalize(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    for pattern, repl in VOLATILE:
        text = pattern.sub(repl, text)
    return text


def fresh() -> None:
    engine._ANALYSIS_CACHE.clear()  # pylint: disable=protected-access
    engine._ANALYSIS_CACHE_ORDER.clear()  # pylint: disable=protected-access
    random.seed(0)


@pytest.fixture(scope="module")
def index() -> ModelIndex:
    inputs, outputs = _build_catalog_with_ids(WORKBOOK)
    return ModelIndex(inputs, outputs)


def id_of(mapping: dict, name: str) -> str:
    return next(k for k, v in mapping.items() if v == name)


async def via_tool(call: AIMessage, index: ModelIndex) -> dict:
    fresh()
    agent = create_agent(
        ScriptedChatModel(steps=[call, AIMessage("ок")]), default_catalog().base_tools(), context_schema=FakeContext
    )
    out = await agent.ainvoke(
        {"messages": [HumanMessage("ход")]}, context=FakeContext(file_path=WORKBOOK, model_index=index)
    )
    return json.loads(next(m for m in out["messages"] if isinstance(m, ToolMessage)).content)


async def test_run_scenarios_matches_engine(index: ModelIndex) -> None:
    methanol, ebitda, profit = (
        id_of(index.inputs, "Метанол"),
        id_of(index.outputs, "EBITDA"),
        id_of(index.outputs, "Чистая прибыль"),
    )
    call = tool_call(
        "run_scenarios",
        inputs=[{"id": methanol, "min": 300, "max": 500, "step": 100}],
        outputs=[ebitda, profit],
        year=2026,
    )
    actual = await via_tool(call, index)
    fresh()
    direct = engine.analyze_excel_model(
        file_name=WORKBOOK,
        input_names=["Метанол"],
        output_names=["EBITDA", "Чистая прибыль"],
        output_years=[2026, 2026],
        ranges=[[300, 500]],
        steps=[100],
    )
    assert direct.status == "OK"
    expected = scenarios_envelope(direct, ["Метанол"]).model_dump(mode="json", exclude_none=True)
    assert actual["status"] == "ok"
    assert normalize(actual["data"]) == normalize(expected["data"])
    assert actual["numbers"] == expected["numbers"]


async def test_find_input_for_target_matches_engine(index: ModelIndex) -> None:
    methanol, ebitda = id_of(index.inputs, "Метанол"), id_of(index.outputs, "EBITDA")
    call = tool_call("find_input_for_target", output_id=ebitda, year=2026, target=1000, input_ids=[methanol])
    actual = await via_tool(call, index)
    fresh()
    direct = engine.analyze_model_inputs_for_target(
        file_name=WORKBOOK,
        output_name="EBITDA",
        output_year=2026,
        target_value=1000.0,  # float, as v1 passes it (QueryAnalysisIFT.target_value)
        input_names=["Метанол"],
        tolerance=0.1,
        max_scenarios=1000,
    )
    assert direct.status == "OK"
    expected = target_envelope(direct, "EBITDA", 2026).model_dump(mode="json", exclude_none=True)
    assert actual["status"] == "ok"
    assert normalize(actual["data"]) == normalize(expected["data"])
    assert actual["numbers"] == expected["numbers"]
