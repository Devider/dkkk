"""Cross-platform Excel handler using openpyxl + LibreOffice headless.

Replaces xlwings (Windows/Mac Excel COM) with:
  - openpyxl for reading/writing .xlsx cell values
  - LibreOffice Calc --headless --convert-to for formula recalculation

This works on any OS where LibreOffice is installed (Linux, macOS, Windows).
"""

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import openpyxl
from openpyxl.utils import get_column_letter


class LibreOfficeNotAvailableError(RuntimeError):
    """Raised when LibreOffice is not installed or cannot recalculate."""


def _check_libreoffice() -> str:
    """Return the libreoffice binary path or raise."""
    for bin_name in ("libreoffice", "soffice"):
        try:
            result = subprocess.run(
                [bin_name, "--version"],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                return bin_name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    raise LibreOfficeNotAvailableError(
        "LibreOffice not found. Install it: sudo apt-get install libreoffice-calc"
    )


class ExcelWorkbook:
    """Context manager for cross-platform Excel operations.

    Opens an .xlsx file with openpyxl.  On ``calculate()`` the workbook is
    saved, LibreOffice headless is invoked to recalculate formulas (updating
    cached values), and the workbook is reloaded with ``data_only=True`` so
    further reads return computed values.

    Usage::

        with ExcelWorkbook("/tmp/model.xlsx") as xl:
            data = xl.get_all_data("Inputs")          # list[list] of values
            xl.set_cell("Inputs", "B12", 150.0)
            xl.calculate()                             # LO recalculates
            result = xl.get_cell("Outputs", "C5")
            xl.save("/tmp/output.xlsx")                # persist
    """

    def __init__(self, file_path: str):
        self.file_path = os.path.abspath(file_path)
        self._wb: Optional[openpyxl.Workbook] = None
        self._data_only = False
        self._open()

    def _open(self):
        self._wb = openpyxl.load_workbook(
            self.file_path, data_only=self._data_only,
        )

    # ------------------------------------------------------------------
    # context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._wb is not None:
            self._wb.close()
            self._wb = None

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
        ws = self._wb[sheet_name]
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

    def get_cell(self, sheet_name: str, cell_ref: str) -> Any:
        return self._wb[sheet_name][cell_ref].value

    def set_cell(self, sheet_name: str, cell_ref: str, value: Any):
        self._wb[sheet_name][cell_ref].value = value

    @staticmethod
    def cell_ref(row: int, col: int) -> str:
        """Return ``"A1"``-style reference for 1‑based *row*, *col*."""
        return f"{get_column_letter(col)}{row}"

    # ------------------------------------------------------------------
    # save / recalculate
    # ------------------------------------------------------------------

    def save(self, file_path: Optional[str] = None):
        target = str(file_path) if file_path is not None else self.file_path
        self._wb.save(target)

    def calculate(self):
        """Force full formula recalculation via LibreOffice headless.

        1. Saves the workbook via openpyxl.
        2. Closes the workbook.
        3. Spawns ``libreoffice --headless --convert-to xlsx`` which opens
           the file (triggering recalculation) and writes updated cached
           values.
        4. Re-opens the workbook with ``data_only=True`` so subsequent
           ``get_cell`` / ``get_all_data`` calls return computed values.
        """
        self.save()
        self.close()

        lo = _check_libreoffice()
        try:
            subprocess.run(
                [
                    lo,
                    "--headless",
                    "--norestore",
                    "--calc",
                    self.file_path,
                    "--convert-to",
                    "xlsx:Calc MS Excel 2007 XML",
                    "--outdir",
                    str(Path(self.file_path).parent),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"LibreOffice recalculation failed (exit {exc.returncode}): "
                f"{exc.stderr.decode(errors='replace')}"
            ) from exc
        except FileNotFoundError as exc:
            raise LibreOfficeNotAvailableError(
                "LibreOffice binary not found."
            ) from exc

        self._data_only = True
        self._open()


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
