"""Calculation engine for agent v2: copy of the v1 calculations and LibreOffice backend (story S0.7).

Bodies are copied unchanged from ``aigw_service.api.v1`` (only imports differ); the agent v2 adapter
calls this package, v1 keeps its own code until it is removed (story S5.4).
"""

from excel_engine.calculations import (
    ExcelAnalysisToolResult,
    ModelInputAnalysisToolResult,
    analyze_excel_model,
    analyze_model_inputs_for_target,
)
from excel_engine.excel_handler import ExcelWorkbook

__all__ = [
    "ExcelAnalysisToolResult",
    "ExcelWorkbook",
    "ModelInputAnalysisToolResult",
    "analyze_excel_model",
    "analyze_model_inputs_for_target",
]
