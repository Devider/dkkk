"""Live LibreOffice integration tests for the CHOOSE -> INDEX normalisation.

These exercise the real UNO bridge against a real model, so they need
LibreOffice + pyuno and the model fixture on disk.  They are skipped when
either is unavailable (CI / host without LO).
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


@pytest.fixture
def model_copy(tmp_path):
    dest = tmp_path / MODEL.name
    shutil.copy2(MODEL, dest)
    return str(dest)


def test_fix_long_choose_resolves_error(model_copy):
    """All long CHOOSE are rewritten and Dashboard!D34 stops being Err:504."""
    from openpyxl.utils.cell import coordinate_to_tuple

    from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

    session = LibreOfficeSession()
    session.start()
    try:
        book = CalcBook(session, model_copy)
        before = book.get_value("Dashboard", 34, 4)
        assert isinstance(before, str) and before.startswith(("Ошибка", "Err")), before

        result = book.fix_long_choose()

        assert result["fixed"] >= 1
        assert result["fixed"] == result["scanned"]
        assert result["unfixable"] == []

        book.calculate()
        after = book.get_value("Dashboard", 34, 4)
        # index $D$30 == 1 -> CHOOSE picks the first value arg (column F).
        f34 = book.get_value("Dashboard", 34, 6)
        assert after == f34

        row, col = coordinate_to_tuple("D34")
        formula = book.sheet("Dashboard").getCellByPosition(col - 1, row - 1).getFormula()
        assert formula.startswith("=INDEX(")
        assert ";$D$30)" in formula
        book.close()
    finally:
        session.stop()


def test_fix_long_choose_preserves_outputs(model_copy):
    """Unrelated outputs keep their values across the rewrite + save."""
    from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

    out = os.path.join(tempfile.gettempdir(), "lo_correct_out.xlsx")
    session = LibreOfficeSession()
    session.start()
    try:
        book = CalcBook(session, model_copy)
        before = book.get_value("Outputs", 69, 15)
        book.fix_long_choose()
        book.save_as_xlsx(out)
        book.close()
    finally:
        session.stop()

    session2 = LibreOfficeSession()
    session2.start()
    try:
        book2 = CalcBook(session2, out)
        after = book2.get_value("Outputs", 69, 15)
        assert after == pytest.approx(before, rel=1e-9)
        book2.close()
    finally:
        session2.stop()
        if os.path.exists(out):
            os.remove(out)


REAL_MODEL = Path(__file__).resolve().parents[3] / "real_models" / "2poj431skazt_depersonalized.xlsx"


def _make_long_choose_formula(index_cell: str, row: int, first_col_idx: int, n_args: int) -> str:
    from openpyxl.utils import get_column_letter

    args = ",".join(f"{get_column_letter(first_col_idx + i)}{row}" for i in range(n_args))
    return f"=CHOOSE({index_cell},{args})"


def _make_synthetic_book(path, hidden_formula=None, far_corner_junk=False):
    """Small book: Dashboard with one long flat CHOOSE, optional hidden sheet
    and a 'Junk' sheet whose stray cells sit near the absolute grid corner
    (inflating the LO used area to ~17 billion coordinates, like the real
    depersonalized model)."""
    import openpyxl

    wb = openpyxl.Workbook()
    dash = wb.active
    dash.title = "Dashboard"
    dash["B1"] = 1
    dash["A2"] = _make_long_choose_formula("$B$1", 3, 3, 40)
    for i in range(40):
        dash.cell(row=3, column=3 + i, value=i + 1)
    if hidden_formula is not None:
        ws = wb.create_sheet("HiddenCalc")
        ws.sheet_state = "hidden"
        ws["A1"] = 1
        ws["A2"] = 2
        ws["A3"] = hidden_formula
    if far_corner_junk:
        junk = wb.create_sheet("Junk")
        junk["XES1048551"] = 1
        junk["XET1048551"] = 2
        junk["XET1048553"] = 3
    wb.save(path)


def test_fix_skips_hidden_sheets(tmp_path):
    """Hidden sheets are excluded per business rules and reported."""
    from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

    path = tmp_path / "hidden_sheet.xlsx"
    _make_synthetic_book(str(path), hidden_formula="=SUM(A1:A2)")

    session = LibreOfficeSession()
    session.start()
    try:
        book = CalcBook(session, str(path))
        result = book.fix_long_choose()

        assert result["fixed"] == 1
        assert result["scanned"] == 1
        assert result["unfixable"] == []
        assert result["skipped_hidden"] == 1
        hidden_formula = book.sheet("HiddenCalc").getCellByPosition(0, 2).getFormula()
        assert hidden_formula.upper().startswith("=SUM"), hidden_formula
        book.close()
    finally:
        session.stop()


def test_fix_survives_far_corner_junk(tmp_path):
    """Stray cells near the grid corner must not explode the scan."""
    from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

    path = tmp_path / "far_corner_junk.xlsx"
    _make_synthetic_book(str(path), far_corner_junk=True)

    session = LibreOfficeSession()
    session.start()
    try:
        book = CalcBook(session, str(path))
        result = book.fix_long_choose()

        assert result["fixed"] == 1
        assert result["scanned"] == 1
        assert result["unfixable"] == []
        assert result["formulas_scanned"] >= 1
        rewritten = book.sheet("Dashboard").getCellByPosition(0, 1).getFormula()
        assert rewritten.startswith("=INDEX("), rewritten
        book.close()
    finally:
        session.stop()


@pytest.mark.skipif(not REAL_MODEL.exists(), reason=f"real model fixture missing: {REAL_MODEL}")
def test_fix_survives_real_depersonalized_model(tmp_path):
    """The full correction pass completes on the OOMing real model copy."""
    from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

    dest = tmp_path / REAL_MODEL.name
    shutil.copy2(REAL_MODEL, dest)

    session = LibreOfficeSession()
    session.start()
    try:
        book = CalcBook(session, str(dest))
        result = book.fix_long_choose()

        assert result["formulas_scanned"] > 0
        assert result["fixed"] == result["scanned"] - len(result["unfixable"])
        assert "Выбрать язык" in book.sheet_names
        book.close()
    finally:
        session.stop()
