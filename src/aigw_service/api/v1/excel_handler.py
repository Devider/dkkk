"""Cross-platform Excel handler using openpyxl + LibreOffice headless.

Replaces xlwings (Windows/Mac Excel COM) with:
  - openpyxl for reading/writing .xlsx cell values
  - LibreOffice Calc --headless --convert-to for formula recalculation

This works on any OS where LibreOffice is installed (Linux, macOS, Windows).
"""

import io
import os
import shutil
import subprocess
import uuid
import zipfile
import xml.etree.ElementTree as ET
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
        self._wb: Optional[openpyxl.Workbook] = None   # data_only=False (formulas)
        self._wbv: Optional[openpyxl.Workbook] = None  # data_only=True (computed values)
        self._data_only = False
        self._open()

    def _open(self):
        self.close()
        self._wb = openpyxl.load_workbook(self.file_path, data_only=False)
        if self._data_only:
            self._wbv = openpyxl.load_workbook(self.file_path, data_only=True)

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

    def get_cell(self, sheet_name: str, cell_ref: str) -> Any:
        src = self._wbv if self._wbv is not None else self._wb
        return src[sheet_name][cell_ref].value

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
        """Save formulas workbook (``data_only=False``) — always preserves formulas."""
        target = str(file_path) if file_path is not None else self.file_path
        self._wb.save(target)

    @staticmethod
    def _clear_cached_formula_values(file_path: str):
        """Modify ``.xlsx`` in-place so LibreOffice recalculates every formula.

        Two changes are made to the ZIP-internal XML:

        1. Sets ``fullCalcOnLoad="1"`` on ``<calcPr>`` in ``xl/workbook.xml``.
        2. Clears the cached-``<v>`` element of every cell that carries a
           ``<f>`` (formula) element.

        This forces LO to treat the file as "dirty" and recompute all
        formulas, preventing it from reusing stale cached values.
        """
        NS_SPREADSHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        ET.register_namespace("", NS_SPREADSHEET)

        with open(file_path, "rb") as f:
            src = io.BytesIO(f.read())

        out = io.BytesIO()
        with zipfile.ZipFile(src, "r") as zin:
            items = zin.infolist()
            sheet_xmls = [
                i.filename for i in items
                if i.filename.startswith("xl/worksheets/sheet") and i.filename.endswith(".xml")
            ]

            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in items:
                    content = zin.read(item.filename)
                    fname = item.filename

                    if fname == "xl/workbook.xml":
                        root = ET.fromstring(content)
                        calcPr = root.find(f"{{{NS_SPREADSHEET}}}calcPr")
                        if calcPr is not None:
                            calcPr.set("fullCalcOnLoad", "1")
                        content = ET.tostring(root, encoding="unicode", xml_declaration=True).encode("utf-8")
                    elif fname in sheet_xmls:
                        root = ET.fromstring(content)
                        modified = False
                        for c in root.findall(f".//{{{NS_SPREADSHEET}}}c"):
                            f_el = c.find(f"{{{NS_SPREADSHEET}}}f")
                            v_el = c.find(f"{{{NS_SPREADSHEET}}}v")
                            if f_el is not None and v_el is not None:
                                v_el.text = None
                                modified = True
                        if modified:
                            content = ET.tostring(root, encoding="unicode", xml_declaration=True).encode("utf-8")
                    zout.writestr(item, content)

        with open(file_path, "wb") as f:
            f.write(out.getvalue())

    def calculate(self):
        """Force full formula recalculation via LibreOffice headless.

        1. Saves the workbook via openpyxl.
        2. Closes the workbook.
        3. Patches in-file XML to add ``fullCalcOnLoad=1`` and clear stale
           cached values — this forces LibreOffice to recalculate.
        4. Copies the file to a temp path (LO cannot safely overwrite the
           file it read as input).
        5. Converts the temp XLSX → ODS (LibreOffice recalculates).
        6. Converts the ODS → XLSX (cached values are written back).
        7. Copies the temp XLSX back to ``self.file_path``.
        8. Re-opens the workbook with ``data_only=True`` so subsequent
           ``get_cell`` / ``get_all_data`` calls return computed values.

        A unique ``-env:UserInstallation`` directory is used on each call
        to avoid locking / stale-profile issues.
        """
        self.save()
        self.close()

        lo = _check_libreoffice()
        file_path = self.file_path
        parent = str(Path(file_path).parent)
        profile_dir = f"/tmp/lo_{uuid.uuid4().hex[:12]}"
        user_installation = f"file://{profile_dir}"
        uid = uuid.uuid4().hex[:8]

        self._clear_cached_formula_values(file_path)

        temp_xlsx = file_path.replace(".xlsx", f"_lo_{uid}.xlsx")
        temp_ods = temp_xlsx.replace(".xlsx", ".ods")

        shutil.copy2(file_path, temp_xlsx)

        lo_base = [
            lo,
            f"-env:UserInstallation={user_installation}",
            "--headless", "--norestore",
            "--nofirststartwizard", "--nologo", "--nodefault",
        ]

        try:
            subprocess.run(
                [*lo_base, "--convert-to", "ods", "--outdir", parent, temp_xlsx],
                check=True, capture_output=True, timeout=300,
            )
            subprocess.run(
                [*lo_base, "--convert-to", "xlsx:Calc MS Excel 2007 XML",
                 "--outdir", parent, temp_ods],
                check=True, capture_output=True, timeout=300,
            )
            shutil.copy2(temp_xlsx, file_path)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"LibreOffice recalculation failed (exit {exc.returncode}): "
                f"{exc.stderr.decode(errors='replace')}"
            ) from exc
        except FileNotFoundError as exc:
            raise LibreOfficeNotAvailableError(
                "LibreOffice binary not found."
            ) from exc
        finally:
            for p in (temp_ods, temp_xlsx):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except OSError:
                pass

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
