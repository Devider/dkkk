"""S1.2 (FR-7): result envelope, error codes, v1 status classification, calculation envelopes on real results."""

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel
from tests.unit.v2.fakes import FakeContext, ScriptedChatModel, tool_call

from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.adapter import ToolSpec, build_tool
from aigw_service.api.v2.tools.calc_tools import scenarios_envelope, target_envelope
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import (
    DEFAULT_HINTS,
    ERROR_TEXT,
    ErrorCode,
    Number,
    ToolError,
    ToolSuccess,
    failure,
    failure_from_exception,
    to_json,
)
from aigw_service.api.v2.tools.v1_results import UNREACHED_NOTE, classify_v1
from excel_engine import ExcelAnalysisToolResult, ModelInputAnalysisToolResult

# Real results of the Methanex workbook (captured in Docker, story S1.2)
REAL = json.loads((Path(__file__).parent / "data" / "calc_results.json").read_text(encoding="utf-8"))
INDEX = ModelIndex({"IN1": "Метанол"}, {"OUT0": "EBITDA"})
SERVER_PATH_KEYS = ("results_file", "scenario_file", "minmax_matrix_file", "scenario_matrix_file", "/tmp/")


def ema_result(with_frame: bool = True) -> ExcelAnalysisToolResult:
    content = dict(REAL["ema"]["content"])
    if with_frame:
        content["result_df"] = pd.DataFrame(REAL["ema"]["result_df_records"], columns=REAL["ema"]["result_df_columns"])
    return ExcelAnalysisToolResult(status="OK", result=REAL["ema"]["result"], content=content)


async def tool_message(tools: list, call: AIMessage) -> dict:
    agent = create_agent(ScriptedChatModel(steps=[call, AIMessage("ок")]), tools, context_schema=FakeContext)
    out = await agent.ainvoke({"messages": [HumanMessage("ход")]}, context=FakeContext(model_index=INDEX))
    return json.loads(next(m for m in out["messages"] if isinstance(m, ToolMessage)).content)


class TestCatalogue:
    """FR-7 «Справочник кодов ошибок»."""

    def test_thirteen_codes_each_with_text_and_hint(self) -> None:
        assert len(ErrorCode) == 13
        for code in ErrorCode:
            assert ERROR_TEXT[code].strip()
            assert DEFAULT_HINTS[code].strip()

    def test_default_hint(self) -> None:
        assert failure("no_values").hint == DEFAULT_HINTS[ErrorCode.NO_VALUES]

    def test_specific_hint_overrides_default(self) -> None:
        assert failure("no_values", hint="Возьми IN13").hint == "Возьми IN13"

    def test_unknown_code_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a valid ErrorCode"):
            ToolError("disk_full")


class TestSerialization:
    def test_no_null_or_empty_fields(self) -> None:
        text = to_json(ToolSuccess(result="ok"))
        assert json.loads(text) == {"status": "ok", "result": "ok"}

    def test_failure_shape(self) -> None:
        assert set(json.loads(to_json(failure("unknown_id")))) == {"status", "error", "code", "hint"}


class TestUnexpectedExceptions:
    """FR-7: an unknown exception becomes internal_error without its text (FR-14)."""

    def test_internal_error_hides_exception_text(self) -> None:
        exc = RuntimeError("cell {'[Methanex.xlsx]OUTPUTS'!P143} at /tmp/user_file_u1.xlsx")
        envelope = failure_from_exception(exc)  # braces must not break loguru either
        assert envelope.code == ErrorCode.INTERNAL_ERROR
        assert "OUTPUTS" not in to_json(envelope)
        assert "/tmp/" not in to_json(envelope)

    def test_tool_error_keeps_its_code(self) -> None:
        assert failure_from_exception(ToolError("engine_timeout")).code == ErrorCode.ENGINE_TIMEOUT


class TestAdapterEnvelope:
    """FR-7 «Неизвестный ID индекса»: the adapter answers with unknown_id and never calls the domain."""

    async def test_unknown_id_without_domain_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = []
        monkeypatch.setattr(calc_tools, "analyze_model_inputs_for_target", lambda **kw: called.append(kw))
        call = tool_call("find_input_for_target", output_id="OUT999", year=2026, target=1000, input_ids=["IN1"])
        envelope = await tool_message(default_catalog().base_tools(), call)
        assert envelope["code"] == "unknown_id"
        assert "resolve_terms" in envelope["hint"]
        assert not called

    async def test_unexpected_exception_propagates_to_middleware(self) -> None:
        class Args(BaseModel):
            x: int

        def boom(_args: Args, _ctx: Any) -> ToolSuccess:
            raise ZeroDivisionError("division by zero")

        with pytest.raises(ZeroDivisionError):  # ToolErrorMiddleware (S2.5) turns it into internal_error
            await tool_message([build_tool(ToolSpec("boom", "Сбой", Args, boom))], tool_call("boom", x=1))


# Every ERROR branch of excel_engine/calculations.py (design D3)
V1_CASES = [
    ("ERROR", "Не указаны входные параметры для анализа", ErrorCode.MISSING_INPUT),
    ("ERROR", 'Не удалось определить год из параметра "EBITDA"', ErrorCode.MISSING_INPUT),
    (
        "ERROR",
        "Диапазон слишком широкий: будет проанализировано 200,000 сценариев (максимум 100,000).",
        ErrorCode.INVALID_ARGS,
    ),
    ("ERROR", 'Не удалось найти ячейку для параметра "Метанол": No match found for query: x', ErrorCode.NO_VALUES),
    ("ERROR", 'Выходной параметр "EBITDA" не найден', ErrorCode.NO_VALUES),
    ("ERROR", "Ошибка при поиске выходного параметра: KeyError 'row'", ErrorCode.NO_VALUES),
    ("ERROR", 'Ошибка при поиске выходного параметра "DSCR": boom', ErrorCode.NO_VALUES),
    ("ERROR", "Файл /tmp/user_file_u1.xlsx не найден", ErrorCode.INTERNAL_ERROR),
    ("ERROR", "Ошибка при анализе: Binary URP bridge disposed during call", ErrorCode.ENGINE_UNAVAILABLE),
    ("ERROR", "Ошибка при анализе: soffice did not start", ErrorCode.ENGINE_UNAVAILABLE),
    ("ERROR", "Ошибка при анализе: division by zero", ErrorCode.INTERNAL_ERROR),
    ("ERROR", "Excel-файл содержит повреждённые объединённые ячейки (merged cells).", ErrorCode.INTERNAL_ERROR),
]


class TestClassifyV1:
    """FR-7 «Классификация результатов расчёта v1»."""

    @pytest.mark.parametrize(("status", "text", "code"), V1_CASES)
    def test_error_branches(self, status: str, text: str, code: ErrorCode) -> None:
        result = classify_v1(status, text)
        assert result is not None
        assert result.code == code
        assert result.hint

    @pytest.mark.parametrize(("status", "text", "_code"), V1_CASES)
    def test_no_exception_text_leaks(self, status: str, text: str, _code: ErrorCode) -> None:
        envelope = to_json(classify_v1(status, text))
        for leak in ("No match found", "KeyError", "boom", "URP", "division", "/tmp/"):
            assert leak not in envelope

    def test_success_and_unreached_target_are_not_errors(self) -> None:
        assert classify_v1("OK", "готово") is None
        assert classify_v1("WARNING", REAL["ift_unreachable"]["result"]) is None


class TestCalcEnvelopes:
    """«Единый конверт результата» on real Methanex results."""

    def test_scenarios(self) -> None:
        envelope = scenarios_envelope(ema_result(), ["Метанол"])
        assert len(envelope.data["rows"]) == 3
        ebitda = [n.value for n in envelope.numbers if n.label.startswith("EBITDA_2026")]
        assert ebitda == [714.427, 1074.847, 1435.268]
        assert Number(value=714.427, label="EBITDA_2026 при Метанол = 300.0") in envelope.numbers

    def test_scenarios_without_frame_keep_summary(self) -> None:
        envelope = scenarios_envelope(ema_result(with_frame=False), ["Метанол"])
        assert envelope.data["rows"] == []
        assert envelope.result == REAL["ema"]["result"]

    def test_target_found(self) -> None:
        envelope = target_envelope(ModelInputAnalysisToolResult(**REAL["ift_ok"]), "EBITDA", 2026)
        assert Number(value=379.233, label="Метанол") in envelope.numbers
        assert Number(value=1000.0, label="EBITDA 2026") in envelope.numbers
        assert envelope.notes == []

    def test_target_unreached_is_success_with_nearest_point(self) -> None:
        envelope = target_envelope(ModelInputAnalysisToolResult(**REAL["ift_unreachable"]), "EBITDA", 2026)
        assert envelope.status == "ok"
        assert envelope.notes == [UNREACHED_NOTE]
        assert len(envelope.data["scenarios"]) == 1
        nearest = min(REAL["ift_unreachable"]["content"]["all_scenarios"], key=lambda s: abs(s["deviation"]))
        assert envelope.data["scenarios"][0]["inputs"] == nearest["input_values"]

    @pytest.mark.parametrize("key", ["ift_ok", "ift_unreachable"])
    def test_no_server_paths(self, key: str) -> None:
        text = to_json(target_envelope(ModelInputAnalysisToolResult(**REAL[key]), "EBITDA", 2026))
        assert not any(p in text for p in SERVER_PATH_KEYS)

    def test_no_server_paths_in_scenarios(self) -> None:
        text = to_json(scenarios_envelope(ema_result(), ["Метанол"]))
        assert not any(p in text for p in SERVER_PATH_KEYS)
