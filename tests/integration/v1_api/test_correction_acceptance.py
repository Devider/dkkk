"""End-to-end acceptance test for the main goal of the correction (task 3.5).

Why: without the correction, a long ``CHOOSE`` yields ``Err:504`` and, once an
input feeding it is perturbed, the dependent value stays broken (collapses to
an error/0).  After the one-time upload correction the same perturbation
recalculates correctly.

Uses the real model via the real correction path and the real UNO engine, so it
is skipped when LibreOffice/pyuno or the model fixture is unavailable.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

MODEL = Path(__file__).resolve().parents[3] / "models" / "06.10.2026.xlsx"


def _uno_available() -> bool:
    try:
        from aigw_service.api.v1.lo_backend import ensure_uno

        ensure_uno()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not MODEL.exists(), reason=f"model fixture missing: {MODEL}"),
    pytest.mark.skipif(not _uno_available(), reason="LibreOffice/pyuno not available"),
]

DASHBOARD_ROW = 34  # a row carrying the long CHOOSE
INDEX_CELL = (30, 4)  # Dashboard!$D$30 -- the CHOOSE selector
CHOOSE_CELL = (34, 4)  # Dashboard!D34  -- the long CHOOSE itself


def _is_error(value) -> bool:
    return isinstance(value, str) and value.startswith(("Ошибка", "Err", "#"))


def _perturbed_value(path: str):
    """Perturb the CHOOSE selector (index 1 -> 2) and read the result cell."""
    from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

    session = LibreOfficeSession()
    session.start()
    try:
        book = CalcBook(session, path)
        session_row, session_col = INDEX_CELL
        # Second value argument of the CHOOSE on the same row (column G).
        second_arg = book.get_value("Dashboard", DASHBOARD_ROW, 7)
        book.set_value("Dashboard", session_row, session_col, 2)
        book.calculate()
        result = book.get_value("Dashboard", *CHOOSE_CELL)
        book.close()
    finally:
        session.stop()
    return result, second_arg


def test_perturbation_collapses_without_correction():
    """Baseline: the uncorrected model stays broken after perturbation."""
    from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

    path = os.path.join(tempfile.gettempdir(), "acc_orig_baseline.xlsx")
    shutil.copy2(MODEL, path)
    try:
        session = LibreOfficeSession()
        session.start()
        try:
            book = CalcBook(session, path)
            initial = book.get_value("Dashboard", *CHOOSE_CELL)
            book.close()
        finally:
            session.stop()
        assert _is_error(initial), initial

        result, _ = _perturbed_value(path)
        assert _is_error(result), result
    finally:
        os.remove(path)


def test_correction_makes_perturbation_recalculate():
    """After correction, perturbing the selector recalculates the row correctly."""
    from aigw_service.api.v1.excel_handler import correct_workbook_on_disk

    path = os.path.join(tempfile.gettempdir(), "acc_corrected.xlsx")
    shutil.copy2(MODEL, path)
    try:
        correction = correct_workbook_on_disk(path)
        assert correction["status"] in ("OK", "WARN_UNFIXABLE")

        result, second_arg = _perturbed_value(path)
        # With index == 2, CHOOSE/INDEX returns its second value argument.
        assert not _is_error(result), result
        assert result == second_arg, (result, second_arg)
        assert result not in (0, None), result
    finally:
        os.remove(path)
