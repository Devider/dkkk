"""Excel handler using LibreOffice Calc via UNO (pyuno bridge).

``ExcelWorkbook`` spawns its own headless ``soffice`` instance (a private
socket + throwaway user profile), opens the workbook there, and evaluates all
formulas with the real Calc engine -- no in-process formula engine.

Documents are modified in memory only. ``save()`` is intentionally absent: the
service never hands the modified file to a client, and recalculated values live
in the remote Calc document, not in openpyxl.
"""

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Monkey-patch openpyxl 3.1.5  —  MultiCellRange.__init__ silently drops
# CellRange substrings that fail to parse (non-deterministic bug triggered
# by certain conditional-formatting / data-validation `sqref` entries in
# merged-cell-heavy sheets).  Applied once at import time.
# ---------------------------------------------------------------------------
import openpyxl.worksheet.cell_range as _openpyxl_cr
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple

from aigw_service.api.v1.lo_backend import CalcBook, LibreOfficeSession

_orig_multicellrange_init = _openpyxl_cr.MultiCellRange.__init__


def _patched_multicellrange_init(self, ranges=None):
    if ranges is None:
        ranges = set()
    if isinstance(ranges, str):
        parts = ranges.split()
        good: list[str] = []
        for r in parts:
            try:
                _openpyxl_cr.CellRange(r)
                good.append(r)
            except Exception:
                pass
        ranges = [_openpyxl_cr.CellRange(r) for r in good]
    _orig_multicellrange_init(self, ranges)


_openpyxl_cr.MultiCellRange.__init__ = _patched_multicellrange_init


def _parse_ref(ref: str) -> tuple[str, int, int]:
    """Parse ``"'[model.xlsx]INPUTS'!AH340"`` → ``("INPUTS", 340, 34)``.

    Cell references are addressed by (sheet, row, col) with 1-based row/col.
    """
    sheet_part, cell = ref.split("!")
    sheet = sheet_part.split("]")[-1].strip("'").strip()
    row, col = coordinate_to_tuple(cell)
    return sheet, row, col


class ExcelWorkbook:
    """Context manager for Excel operations backed by headless LibreOffice.

    Usage::

        with ExcelWorkbook("model.xlsx") as xl:
            data = xl.get_all_data("Inputs")          # list[list] of values
            xl.set_cell("Inputs", "B12", 150.0)
            xl.calculate()                             # Calc engine recalc
            result = xl.get_cell("Outputs", "C5")
    """

    def __init__(self, file_path: str):
        self.file_path = os.path.abspath(file_path)
        self._session = None
        self._book: Optional[CalcBook] = None
        self._open()

    def _open(self):
        self.close()
        session = LibreOfficeSession()
        session.start()
        try:
            book = CalcBook(session, self.file_path)
        except Exception:
            session.stop()
            raise
        self._session = session
        self._book = book
        book.calculate_all()

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._book is not None:
            self._book.close()
            self._book = None
        if self._session is not None:
            self._session.stop()
            self._session = None

    # ------------------------------------------------------------------
    # sheet / cell helpers
    # ------------------------------------------------------------------

    def sheet_names(self) -> list:
        return list(self._book.sheet_names)

    def get_all_data(self, sheet_name: str) -> Optional[list[list[Any]]]:
        """Return the used range of *sheet_name* as a 2-D list.

        Mirrors ``xlwings.Sheet.used_range.value`` -- the first element is
        the header row, subsequent elements are data rows.  Returns ``None``
        for an empty sheet.
        """
        sh = self._book.sheet(sheet_name)
        cur = sh.createCursor()
        cur.gotoEndOfUsedArea(False)
        addr = cur.RangeAddress
        if addr.EndRow < 0 or addr.EndColumn < 0:
            return None
        data = self._book.read_block(sheet_name, 1, 1, addr.EndRow + 1, addr.EndColumn + 1)
        return data if data else None

    def get_cell(self, sheet_name: str, cell_ref: str) -> Any:
        """Read a single cell from the Calc engine."""
        row, col = coordinate_to_tuple(cell_ref)
        return self._book.get_value(sheet_name, row, col)

    def set_cell(self, sheet_name: str, cell_ref: str, value: Any):
        """Write a value to the workbook (Calc engine)."""
        row, col = coordinate_to_tuple(cell_ref)
        self._book.set_value(sheet_name, row, col, value)

    @staticmethod
    def cell_ref(row: int, col: int) -> str:
        """Return ``"A1"``-style reference for 1‑based *row*, *col*."""
        return f"{get_column_letter(col)}{row}"

    # ------------------------------------------------------------------
    # calculate / compile
    # ------------------------------------------------------------------

    def calculate(self, outputs: Optional[list] = None):
        """Recalculate all formulas in the Calc engine.

        *outputs* is accepted for API compatibility and ignored (Calc always
        recalculates the full dependency graph).
        """
        self._book.calculate_all()

    def get_compiled_func(self, input_refs: list, output_refs: list):
        """Build a callable for repeated scenario evaluation.

        Returns a function mapping *input_refs* (in order) → a list of native
        output values, one per *output_ref*.  Each call pushes the inputs into
        the Calc engine and recalculates.  Inputs and outputs are batched into
        single ``setDataArray`` / ``getDataArray`` round-trips where possible
        to minimise UNO IPC overhead.
        """
        book = self._book
        actual_names = {name.casefold(): name for name in book.sheet_names}
        in_cells = [_parse_ref(r) for r in input_refs]
        out_cells = [_parse_ref(r) for r in output_refs]

        def resolve(name: str) -> str:
            return actual_names.get(name.casefold(), name)

        # Group outputs by sheet so each sheet is read with one getDataArray.
        out_by_sheet: dict[str, list[tuple[int, int, int, int]]] = {}
        for idx, (sheet, row, col) in enumerate(out_cells):
            out_by_sheet.setdefault(resolve(sheet), []).append((idx, row, col))

        def evaluate(*values):
            for (sheet, row, col), v in zip(in_cells, values, strict=True):
                book.set_value(resolve(sheet), row, col, v)
            book.calculate_all()
            results = [None] * len(out_cells)
            for sheet, entries in out_by_sheet.items():
                r1 = min(r for _, r, _ in entries)
                r2 = max(r for _, r, _ in entries)
                c1 = min(c for _, _, c in entries)
                c2 = max(c for _, _, c in entries)
                block = book.read_block(sheet, r1, c1, r2, c2)
                for idx, row, col in entries:
                    results[idx] = block[row - r1][col - c1]
            return results

        return evaluate


def copy_to_temp(source_path: str, suffix: str = "") -> str:
    """Copy *source_path* to the temp directory with an optional *suffix* and
    return the new path."""
    src = Path(source_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = src.stem
    if suffix:
        stem = f"{stem}_{suffix}"
    dest = Path(tempfile.gettempdir()) / f"{stem}_{ts}{src.suffix}"
    shutil.copy2(str(src), str(dest))
    return str(dest)
