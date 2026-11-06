"""Excel handler using LibreOffice Calc via UNO (pyuno bridge).

``ExcelWorkbook`` spawns its own headless ``soffice`` instance (a private
socket + throwaway user profile), opens the workbook there, and evaluates all
formulas with the real Calc engine -- no in-process formula engine.

Documents are modified in memory only. ``save()`` is intentionally absent: the
service never hands the modified file to a client, and recalculated values live
in the remote Calc document, not in openpyxl.

Exception -- the upload correction flow (:func:`correct_workbook_on_disk`):
when a client uploads a model, it is corrected **once** on disk (external links
stripped from the XML/ZIP package, long ``CHOOSE`` normalised to ``INDEX`` via
UNO) and the corrected file overwrites ``user_file_{user_id}.{ext}``; the
original client file is kept alongside as ``user_file_{user_id}_original.{ext}``
for debugging. Every other flow still never writes the document.

The correction applies to OOXML (``.xlsx``) only: the XML/ZIP stage and the
UNO ``Calc MS Excel 2007 XML`` save filter are OOXML-specific, so legacy
``.xls`` (BIFF8) uploads are skipped unchanged.
"""

import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
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
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

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

    def __init__(self, file_path: str, logger=None):
        self.file_path = os.path.abspath(file_path)
        self._logger = logger
        self._session = None
        self._book: Optional[CalcBook] = None
        self._open()

    def _open(self):
        self.close()
        session = LibreOfficeSession(logger=self._logger)
        session.start()
        try:
            book = CalcBook(session, self.file_path, logger=self._logger)
        except Exception:
            session.stop()
            raise
        self._session = session
        self._book = book
        book.calculate()

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
        """Recalculate changed dependencies in the Calc engine (incremental,
        like Excel/desktop LibreOffice): only "dirty" cells whose inputs
        changed are recomputed, so heavy/unreadable cells outside the changed
        chain are left untouched.

        *outputs* is accepted for API compatibility and ignored.
        """
        self._book.calculate()

    def fix_long_choose(self, max_args: Optional[int] = None) -> dict:
        """Rewrite long ``CHOOSE`` formulas to ``INDEX`` (see
        :meth:`CalcBook.fix_long_choose`).  Used by the upload correction."""
        if max_args is None:
            return self._book.fix_long_choose()
        return self._book.fix_long_choose(max_args)

    def save_as_xlsx(self, path: str) -> None:
        """Store the workbook to *path* as OOXML (see
        :meth:`CalcBook.save_as_xlsx`).  Used by the upload correction; the
        output path must differ from the open (ReadOnly) source path."""
        self._book.save_as_xlsx(path)

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
            book.calculate()
            results = [None] * len(out_cells)
            for sheet, entries in out_by_sheet.items():
                r1 = min(r for _, r, _ in entries)
                r2 = max(r for _, r, _ in entries)
                c1 = min(c for _, _, c in entries)
                c2 = max(c for _, _, c in entries)
                block = book.read_block(sheet, r1, c1, r2, c2)
                for idx, row, col in entries:
                    raw = block[row - r1][col - c1]
                    if raw is None:
                        # Bulk getDataArray can't tell "error formula" from
                        # "empty" (both read as None). Re-probe via _scalar()
                        # (cell.getError()) so evaluate() returns three
                        # distinguishable categories: float | str#…! | None.
                        raw = book.get_value(sheet, row, col)
                    results[idx] = raw
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


# ===================================================================
# External-link removal (XML/ZIP stage of the upload correction)
# ===================================================================
#
# Adapted from the neighbouring project's proven implementation
# (``dkkk_tests/xl_depers/depersonalize.py``, section 6.6
# ``strip_external_links``).  It edits the OOXML package directly, preserving
# the cached cell values -- the equivalent of Excel's "Break Links".
#
# Deviations from the reference (see design D3):
#   * ``strip_external_links`` receives a **local copy** of the part list,
#     because it mutates its ``names`` argument via ``names.remove(part)``;
#   * reference ``logging`` calls are dropped in favour of loguru.

# Namespace URIs below are ECMA-376 (OOXML) string identifiers, never fetched
# over the network; the http:// scheme is mandated by the spec itself.
NS = {"sml": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}  # NOSONAR
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"  # NOSONAR
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"  # NOSONAR
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"  # NOSONAR
EXTERNAL_LINK_TYPE_SUFFIX = "/externalLink"

# Cached value: number (optionally signed / exponent) or already-textual string.
_RE_NUM = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")

# External-link markers in a formula <f>:
#   "[14]Inputs!AC14"                -- external book by numeric index;
#   "'[C:\path\Book.xlsx]'Sheet!A1"  -- external book by quoted path.
# Does NOT match structural refs like "Table[Column]" (no number in []).
_RE_EXT_F = re.compile(r"(\[\d+\])|('\[)")


def _formula_is_external(ftext: str) -> bool:
    """True if the formula text references an external workbook."""
    return bool(_RE_EXT_F.search(ftext))


def _relationship_type_is_external(el: ET.Element) -> bool:
    """True if a Relationship element points at an external link."""
    return el.get("Type", "").endswith(EXTERNAL_LINK_TYPE_SUFFIX)


def _normalize_external_cell_t(cell: ET.Element, v_text: str) -> None:
    """Normalise a cell's ``t`` attribute after an external link is broken.

    A numeric cached value is stored as a number (drop ``t``/``t="n"``);
    otherwise the cache is text and gets ``t="str"``.
    """
    if _RE_NUM.match(v_text.strip()):
        if cell.get("t") == "str":
            del cell.attrib["t"]
    else:
        cell.set("t", "str")


def remove_external_links_from_cells(sheet_path: os.PathLike) -> int:
    """Drop external formulas from a worksheet, keeping cached values.

    - cells with an ``r:id`` attribute (external-link marker) lose ``r:id`` and
      their ``<f>``;
    - cells whose ``<f>`` text references an external book (e.g.
      ``"[14]Inputs!AC14"`` or ``"'[path]Sheet'!A1"``) lose their ``<f>``;
    - the ``<v>`` cached value is preserved and ``t`` is normalised
      (Excel's "Break Links" equivalent).

    Returns the number of affected cells.
    """
    with open(sheet_path, "rb") as f:
        xml_bytes = f.read()

    root = ET.fromstring(xml_bytes)
    changed = 0

    for cell in root.findall(".//sml:c", NS):
        is_external = False
        rid = cell.get(f"{{{R_NS}}}id")
        if rid is not None:
            del cell.attrib[f"{{{R_NS}}}id"]
            is_external = True
        f_elem = cell.find("sml:f", NS)
        if f_elem is not None and f_elem.text and _formula_is_external(f_elem.text):
            is_external = True
        if not is_external:
            continue
        if f_elem is not None:
            cell.remove(f_elem)
        v_elem = cell.find("sml:v", NS)
        if v_elem is not None and v_elem.text:
            _normalize_external_cell_t(cell, v_elem.text)
        changed += 1

    if changed:
        out = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        with open(sheet_path, "wb") as f:
            f.write(out)
    return changed


def _strip_external_rels(rels_path: os.PathLike) -> int:
    """Remove externalLink entries from a relationships file.

    Returns the number of removed entries.
    """
    if not os.path.isfile(rels_path):
        return 0
    with open(rels_path, "rb") as f:
        root = ET.fromstring(f.read())
    to_remove = [el for el in root.findall(f"{{{PKG_REL_NS}}}Relationship") if _relationship_type_is_external(el)]
    for el in to_remove:
        root.remove(el)
    if to_remove:
        out = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        with open(rels_path, "wb") as f:
            f.write(out)
    return len(to_remove)


def strip_external_links(tmpdir: str, names: list) -> dict:
    """Remove external links from an unpacked ``.xlsx`` package (*tmpdir*),
    preserving cached cell values.

    Touches the coherent package parts (design D2/D3):
    - cells with ``r:id`` / external formula -- value preserved;
    - ``xl/worksheets/_rels/*.xml.rels`` externalLink entries;
    - ``xl/externalLinks/*`` parts (and their rels);
    - ``xl/_rels/workbook.xml.rels`` externalLink entries;
    - ``[Content_Types].xml`` externalLink declarations.

    *names* is the archive's part list.  It is mutated here (external-link
    parts are removed so repacking skips them), so callers MUST pass a local
    copy (``list(names)``).

    Returns stats: ``{"cells", "sheet_rels", "parts", "workbook_rels", "content_types"}``.
    """
    stats = {"cells": 0, "sheet_rels": 0, "parts": 0, "workbook_rels": 0, "content_types": 0}

    worksheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    for w in worksheets:
        w_path = os.path.join(tmpdir, w)
        if not os.path.isfile(w_path):
            continue
        stats["cells"] += remove_external_links_from_cells(w_path)

    sheet_rels = sorted(n for n in names if n.startswith("xl/worksheets/_rels/") and n.endswith(".xml.rels"))
    for r in sheet_rels:
        stats["sheet_rels"] += _strip_external_rels(os.path.join(tmpdir, r))

    ext_parts = [n for n in names if n.startswith("xl/externalLinks/")]
    for part in ext_parts:
        full = os.path.join(tmpdir, part)
        if os.path.isfile(full):
            os.remove(full)
            stats["parts"] += 1
        if part in names:
            names.remove(part)

    wb_rels = os.path.join(tmpdir, "xl", "_rels", "workbook.xml.rels")
    stats["workbook_rels"] += _strip_external_rels(wb_rels)

    ct_path = os.path.join(tmpdir, "[Content_Types].xml")
    if os.path.isfile(ct_path) and ext_parts:
        with open(ct_path, "rb") as f:
            ct_root = ET.fromstring(f.read())
        removed_ct = 0
        for ov in ct_root.findall(f"{{{CONTENT_TYPES_NS}}}Override"):
            pn = ov.get("PartName", "")
            if pn.startswith("/xl/externalLinks/") or "externalLink" in pn:
                ct_root.remove(ov)
                removed_ct += 1
        if removed_ct:
            out = ET.tostring(ct_root, encoding="utf-8", xml_declaration=True)
            with open(ct_path, "wb") as f:
                f.write(out)
            stats["content_types"] = removed_ct

    return stats


def disable_external_links_in_xlsx(path: str) -> dict:
    """Unpack *path* (``.xlsx``), strip external links, repack in place.

    Returns the :func:`strip_external_links` stats.  A local copy of the part
    list is passed so the archive's own ordering is untouched by the in-place
    ``names.remove`` inside :func:`strip_external_links`.
    """
    path = os.path.abspath(path)
    tmpdir = tempfile.mkdtemp(prefix="aigw_xlstrip_")
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            zf.extractall(tmpdir)
        # Local copy: strip_external_links mutates `names`.
        stats = strip_external_links(tmpdir, list(names))
        remaining = [n for n in names if not n.startswith("xl/externalLinks/")]
        tmp_zip = path + ".tmp_corrected"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in remaining:
                full = os.path.join(tmpdir, name)
                if os.path.isfile(full):
                    zf.write(full, name)
        os.replace(tmp_zip, path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    logger.info(
        "External links stripped: cells={}, sheet_rels={}, parts={}, workbook_rels={}, content_types={}",
        stats["cells"],
        stats["sheet_rels"],
        stats["parts"],
        stats["workbook_rels"],
        stats["content_types"],
    )
    return stats


# ===================================================================
# Upload correction orchestration
# ===================================================================

# Correction statuses.
STATUS_OK = "OK"
STATUS_OK_NO_CHANGES = "OK_NO_CHANGES"
STATUS_WARN_UNFIXABLE = "WARN_UNFIXABLE"
STATUS_ERROR = "ERROR"

_STATUS_MESSAGES = {
    STATUS_OK: "Файл загружен и обработан: удалены внешние ссылки ({ext_parts} частей), "
    "исправлено длинных CHOOSE: {fixed}.",
    STATUS_OK_NO_CHANGES: "Файл загружен. Коррекция не потребовалась: внешних ссылок нет, "
    "длинных CHOOSE (>{max_args} аргументов) нет.",
    STATUS_WARN_UNFIXABLE: "Файл загружен и частично обработан. "
    "Исправлено длинных CHOOSE: {fixed}. Осталось необрабатываемых ячеек: {n_unfixable} "
    "({cells}). Эти формулы не удалось привести к INDEX (разрыв диапазона или "
    "не-ссылочные аргументы) — исправьте их в модели или продолжайте работу, учитывая риск "
    "некорректного пересчёта этих ячеек.",
    STATUS_ERROR: "Файл загружен, но коррекция завершилась ошибкой: {error}. "
    "Файл сохранён без изменений коррекции — можно продолжать работу, но результаты "
    "по зависимым ячейкам могут быть некорректны.",
}


def _format_report_message(status: str, report: dict) -> str:
    template = _STATUS_MESSAGES.get(status, "{error}")
    unfixable = report.get("choose", {}).get("unfixable", [])
    cells = ", ".join(u["cell"] for u in unfixable[:5])
    if len(unfixable) > 5:
        cells += f" (+{len(unfixable) - 5})"
    return template.format(
        ext_parts=report.get("external_links", {}).get("parts", 0),
        fixed=report.get("choose", {}).get("fixed", 0),
        n_unfixable=len(unfixable),
        cells=cells or "-",
        max_args=report.get("max_choose_args", 30),
        error=report.get("error", "неизвестная ошибка"),
    )


def correct_workbook_on_disk(
    path: str,
    disable_external_links: bool = True,
    fix_long_choose: bool = True,
) -> dict:
    """Correct an uploaded workbook **in place**, once, at upload time.

    Two independent stages, each behind a flag (both default ``True``):

    1. **XML/ZIP** — strip external links, preserving cached values
       (:func:`disable_external_links_in_xlsx`).
    2. **UNO** — open the (already XML-patched) book in Calc, rewrite long
       ``CHOOSE`` to ``INDEX``, and save back via ``storeToURL``.

    Only OOXML ``.xlsx`` is corrected; legacy ``.xls`` is skipped unchanged
    (design D8).  The corrected file overwrites *path*; the caller is
    responsible for keeping the original as ``…_original.ext``.

    Returns ``{"status", "message", "report"}`` where ``report`` carries the
    per-stage statistics.
    """
    path = os.path.abspath(path)
    report: dict = {
        "path": path,
        "disable_external_links": disable_external_links,
        "fix_long_choose": fix_long_choose,
        "max_choose_args": CalcBook.MAX_CHOOSE_ARGS,
        "external_links": {},
        "choose": {"fixed": 0, "unfixable": [], "scanned": 0},
    }

    ext = os.path.splitext(path)[1].lower()
    if ext != ".xlsx":
        report["skipped_reason"] = "not_ooxml"
        message = (
            f"Файл загружен. Коррекция пропущена: поддерживается только формат .xlsx "
            f"(получен {ext or 'без расширения'}). Файл сохранён без изменений."
        )
        return {"status": STATUS_OK_NO_CHANGES, "message": message, "report": report}

    try:
        if disable_external_links:
            report["external_links"] = disable_external_links_in_xlsx(path)
        else:
            report["external_links"] = {"skipped": True}

        if fix_long_choose:
            out_path = path + ".corrected_tmp.xlsx"
            with ExcelWorkbook(path, logger=logger) as xl:
                report["choose"] = xl.fix_long_choose()
                xl.save_as_xlsx(out_path)
            os.replace(out_path, path)
        else:
            report["choose"] = {"skipped": True, "fixed": 0, "unfixable": [], "scanned": 0}
    except Exception as e:
        # pylint: disable=no-member
        logger.opt(exception=True).error("Workbook correction failed: {}", str(e))
        report["error"] = str(e)
        return {
            "status": STATUS_ERROR,
            "message": _format_report_message(STATUS_ERROR, report),
            "report": report,
        }

    unfixable = report["choose"].get("unfixable", [])
    fixed = report["choose"].get("fixed", 0)
    ext_stats = report["external_links"]
    changed = bool(fixed) or bool(ext_stats.get("parts")) or bool(ext_stats.get("cells"))
    if unfixable:
        status = STATUS_WARN_UNFIXABLE
    elif changed:
        status = STATUS_OK
    else:
        status = STATUS_OK_NO_CHANGES

    logger.info(
        "Workbook correction done: status={}, fixed_choose={}, unfixable={}, ext_parts={}",
        status,
        fixed,
        len(unfixable),
        ext_stats.get("parts", 0),
    )
    return {"status": status, "message": _format_report_message(status, report), "report": report}
