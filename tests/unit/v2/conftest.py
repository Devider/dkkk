"""Unit tests never start a real LibreOffice pool: calculation tools get an in-process gate."""

import json
from typing import Any

import pandas as pd
import pytest
from tests.unit.v2.fakes import InlineGate

from aigw_service.api.v2.middleware import context_meter
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.resolve_terms import Pick
from excel_engine import ExcelAnalysisToolResult


@pytest.fixture(autouse=True)
def inline_gate(monkeypatch: pytest.MonkeyPatch) -> InlineGate:
    gate = InlineGate()
    monkeypatch.setattr(calc_tools, "_gate", lambda: gate)
    return gate


@pytest.fixture
def domain(monkeypatch: pytest.MonkeyPatch, inline_gate: Any) -> list[dict]:
    """resolve_terms and the engine without LLM and LibreOffice; returns the engine calls."""
    calls: list[dict] = []

    async def analyzer(_message: str, _index: Any) -> list[Pick]:
        return [Pick("input", "метанол", "IN1"), Pick("output", "EBITDA", "OUT0")]

    def engine(**kw: Any) -> ExcelAnalysisToolResult:
        calls.append(kw)
        frame = pd.DataFrame({"Метанол": [300, 400, 500], "EBITDA": [1200.0, 1350.5, 1501.0]})
        return ExcelAnalysisToolResult(status="OK", result="", content={"result_df": frame})

    monkeypatch.setattr(rt, "analyze_with_v1", analyzer)
    monkeypatch.setattr(calc_tools, "analyze_excel_model", engine)
    return calls


@pytest.fixture
def layer_logs(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """``CONTEXT LAYERS`` records written by ContextMeter (S2.7)."""
    logged: list[dict] = []

    def info(message: str, *args: Any) -> None:
        if message.startswith("CONTEXT LAYERS"):
            logged.append(json.loads(args[0]))

    monkeypatch.setattr(context_meter.logger, "info", info)
    return logged
