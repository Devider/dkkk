"""Cross-platform Excel handler using openpyxl + formulas (in-memory formula evaluation).

Replaces LibreOffice headless formula recalculation with the pure-Python
``formulas`` library, which parses, compiles, and evaluates Excel formulas
entirely in memory — no external process required.
"""

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import formulas
import openpyxl
from openpyxl.utils import get_column_letter


class ExcelWorkbook:
    """Context manager for cross-platform Excel operations.

    Opens an ``.xlsx`` file with openpyxl for I/O and uses ``formulas`` for
    in-memory formula evaluation.  ``calculate()`` is ~1000× faster than
    the previous LibreOffice-based implementation.

    Usage::

        with ExcelWorkbook("/tmp/model.xlsx") as xl:
            data = xl.get_all_data("Inputs")          # list[list] of values
            xl.set_cell("Inputs", "B12", 150.0)
            xl.calculate()                             # in-memory recalc
            result = xl.get_cell("Outputs", "C5")
            xl.save("/tmp/output.xlsx")                # persist
    """

    def __init__(self, file_path: str):
        self.file_path = os.path.abspath(file_path)
        self._wb: Optional[openpyxl.Workbook] = None  # data_only=False (formulas)
        self._wbv: Optional[openpyxl.Workbook] = None  # data_only=True (cached values)
        self._model: Optional[formulas.ExcelModel] = None
        self._inputs: dict[str, Any] = {}
        self._solution: Optional[dict] = None
        self._open()

    def _open(self):
        self.close()
        self._wb = openpyxl.load_workbook(self.file_path, data_only=False)
        self._wbv = openpyxl.load_workbook(self.file_path, data_only=True)
        self._model = None
        self._inputs = {}
        self._solution = None

    def _ensure_model(self):
        if self._model is None:
            self._model = formulas.ExcelModel().loads(self.file_path).finish()

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for wb in (self._wb, self._wbv):
            if wb is not None:
                wb.close()
        self._wb = None
        self._wbv = None
        self._model = None

    # ------------------------------------------------------------------
    # sheet / cell helpers
    # ------------------------------------------------------------------

    def sheet_names(self) -> list[str]:
        return list(self._wb.sheetnames)

    def get_all_data(self, sheet_name: str) -> Optional[list[list[Any]]]:
        """Return the used range of *sheet_name* as a 2-D list.

        Mirrors ``xlwings.Sheet.used_range.value`` — the first element is
        the header row, subsequent elements are data rows.  Returns
        ``None`` for an empty sheet.
        """
        src = self._wbv if self._wbv is not None else self._wb
        ws = src[sheet_name]
        if ws.max_row is None or ws.max_column is None:
            return None
        rows: list[list[Any]] = [
            list(row)
            for row in ws.iter_rows(
                min_row=ws.min_row,
                max_row=ws.max_row,
                min_col=ws.min_column,
                max_col=ws.max_column,
                values_only=True,
            )
        ]
        return rows if rows else None

    def _formula_ref(self, sheet_name: str, cell_ref: str) -> str:
        """Build a formulas-compatible cell reference.

        ``formulas`` normalises sheet names to uppercase internally,
        so we uppercase *sheet_name* to match.
        """
        fname = os.path.basename(self.file_path)
        return f"'[{fname}]{sheet_name.upper()}'!{cell_ref}"

    def _extract_value(self, val: Any):
        if hasattr(val, "value"):
            return val.value[0, 0]
        return val

    def get_cell(self, sheet_name: str, cell_ref: str) -> Any:
        """Read a single cell.

        If any cells have been modified via ``set_cell()`` the value is
        obtained from the ``formulas`` engine (which evaluates the
        dependency graph in memory).  Otherwise the cached value from
        ``openpyxl`` (``data_only=True``) is returned.
        """
        if not self._inputs:
            src = self._wbv if self._wbv is not None else self._wb
            return src[sheet_name][cell_ref].value

        self._ensure_model()
        ref = self._formula_ref(sheet_name, cell_ref)

        # Check cached solution first (populated by calculate())
        if self._solution is not None:
            val = self._solution.get(ref)
            if val is not None:
                return self._extract_value(val)

        # Evaluate just the requested cell
        solution = self._model.calculate(inputs=self._inputs, outputs=[ref])
        return self._extract_value(solution[ref])

    def set_cell(self, sheet_name: str, cell_ref: str, value: Any):
        """Write a value to the workbook.

        The value is recorded for the ``formulas`` engine so subsequent
        ``get_cell()`` / ``calculate()`` calls see the change.
        """
        ref = self._formula_ref(sheet_name, cell_ref)
        self._inputs[ref] = value
        self._wb[sheet_name][cell_ref].value = value
        if self._wbv is not None:
            self._wbv[sheet_name][cell_ref].value = value

    @staticmethod
    def cell_ref(row: int, col: int) -> str:
        """Return ``"A1"``-style reference for 1‑based *row*, *col*."""
        return f"{get_column_letter(col)}{row}"

    # ------------------------------------------------------------------
    # calculate / save / compile
    # ------------------------------------------------------------------

    def calculate(self, outputs: Optional[list[str]] = None):
        """Recalculate formulas in memory via ``formulas``.

        When *outputs* is ``None`` the full dependency graph is evaluated.
        To avoid the full evaluation cost, pass specific output references
        (e.g. ``["'[model.xlsx]OUTPUTS'!O69"]``).
        """
        if not self._inputs:
            return
        self._ensure_model()
        kwargs: dict[str, Any] = {"inputs": self._inputs}
        if outputs is not None:
            kwargs["outputs"] = outputs
        self._solution = self._model.calculate(**kwargs)

    def save(self, file_path: Optional[str] = None):
        """Save the workbook to disk (preserves formulas)."""
        target = str(file_path) if file_path is not None else self.file_path
        self._wb.save(target)

    def get_compiled_func(self, input_refs: list[str], output_refs: list[str]):
        """Compile a fast function for repeated evaluations.

        Returns a ``DispatchPipe`` that maps *input_refs* → *output_refs*.
        Calling it with scalar values returns a single ``Ranges`` object
        (for one output) or a tuple of ``Ranges`` (for multiple outputs).
        """
        self._ensure_model()
        return self._model.compile(inputs=input_refs, outputs=output_refs)


def copy_to_temp(source_path: str, suffix: str = "") -> str:
    """Copy *source_path* to ``/tmp`` with an optional *suffix* and
    return the new path."""
    src = Path(source_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = src.stem
    if suffix:
        stem = f"{stem}_{suffix}"
    dest = Path("/tmp") / f"{stem}_{ts}{src.suffix}"
    shutil.copy2(str(src), str(dest))
    return str(dest)
