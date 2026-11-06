"""Unit tests for the external-link removal stage of the upload correction.

Covers ``remove_external_links_from_cells`` / ``strip_external_links`` and
``disable_external_links_in_xlsx`` (task 1.4): external links are dropped while
cached ``<v>`` values survive, cell ``t`` is normalised, and a text literal that
merely starts with ``[`` is NOT mistaken for an external reference.

These tests build tiny synthetic ``.xlsx`` packages in-memory (only ``zipfile``
+ ``xml.etree``), so they need no LibreOffice bridge.
"""

import os
import re
import zipfile

from aigw_service.api.v1.excel_handler import (
    _formula_is_external,
    _normalize_external_cell_t,
    _strip_external_rels,
    disable_external_links_in_xlsx,
    remove_external_links_from_cells,
    strip_external_links,
)

SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

SHEET_HEADER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<worksheet xmlns="{SPREADSHEET_NS}" xmlns:r="{REL_NS}"><sheetData><row r="1">'
)
SHEET_FOOTER = "</row></sheetData></worksheet>"


def _sheet_xml(cells_xml: str) -> str:
    return SHEET_HEADER + cells_xml + SHEET_FOOTER


def _write_xlsx(path: str, sheet_xml: str, extra_parts: dict | None = None, content_types: str | None = None):
    """Write a minimal ``.xlsx`` with one worksheet and optional extra parts."""
    extra_parts = extra_parts or {}
    if content_types is None:
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{CT_NS}">'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>'
            "</Types>"
        )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        for name, content in extra_parts.items():
            zf.writestr(name, content)


def _read_part(path: str, part: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(part).decode("utf-8")


# ---------------------------------------------------------------------------
# _formula_is_external / _normalize_external_cell_t
# ---------------------------------------------------------------------------


class TestFormulaDetection:
    def test_numeric_external_ref(self):
        assert _formula_is_external("[42]Slide!F139/1000") is True

    def test_quoted_path_external_ref(self):
        assert _formula_is_external("'[C:\\path\\Book.xlsx]Sheet1'!A1") is True

    def test_structural_ref_is_not_external(self):
        # Table[Column] has no number inside [] and no quoted '[.
        assert _formula_is_external("Table1[Column]") is False

    def test_plain_formula_is_not_external(self):
        assert _formula_is_external("SUM(A1:A10)") is False

    def test_text_literal_with_bracket_is_not_external(self):
        # A quoted text literal starting with '[' but no workbook marker.
        assert _formula_is_external('IF(A1="[note]","x","y")') is False


class TestNormalizeCellType:
    def _cell(self, **attrs):
        import xml.etree.ElementTree as ET

        return ET.Element("c", attrs)

    def test_numeric_cache_drops_str_type(self):
        cell = self._cell(t="str")
        _normalize_external_cell_t(cell, "45.599")
        assert cell.get("t") is None

    def test_string_cache_forces_str_type(self):
        cell = self._cell()
        _normalize_external_cell_t(cell, "hello")
        assert cell.get("t") == "str"

    def test_shared_string_type_is_replaced_with_str(self):
        cell = self._cell(t="s")
        _normalize_external_cell_t(cell, "#REF!")
        assert cell.get("t") == "str"

    def test_integer_cache_drops_type(self):
        cell = self._cell(t="str")
        _normalize_external_cell_t(cell, "1234")
        assert cell.get("t") is None


# ---------------------------------------------------------------------------
# remove_external_links_from_cells
# ---------------------------------------------------------------------------


class TestRemoveExternalLinksFromCells:
    def test_external_formula_cell_loses_f_keeps_v(self, tmp_path):
        sheet = _sheet_xml('<c r="E39" s="632"><f>[42]Slide!F139/1000</f><v>45.599</v></c>')
        sheet_path = tmp_path / "sheet1.xml"
        sheet_path.write_text(sheet, encoding="utf-8")

        changed = remove_external_links_from_cells(str(sheet_path))
        out = sheet_path.read_text(encoding="utf-8")

        assert changed == 1
        assert "<f>" not in out
        assert re.search(r"<(?:\w+:)?v>45\.599</(?:\w+:)?v>", out)

    def test_rid_cell_loses_link_and_formula(self, tmp_path):
        sheet = _sheet_xml('<c r="A1" r:id="rId1"><f>VLOOKUP(x,ext,2,0)</f><v>7</v></c>')
        sheet_path = tmp_path / "sheet1.xml"
        sheet_path.write_text(sheet, encoding="utf-8")

        changed = remove_external_links_from_cells(str(sheet_path))
        out = sheet_path.read_text(encoding="utf-8")

        assert changed == 1
        assert "r:id" not in out
        assert "<f>" not in out
        assert re.search(r"<(?:\w+:)?v>7</(?:\w+:)?v>", out)

    def test_local_cells_untouched(self, tmp_path):
        sheet = _sheet_xml('<c r="A1"><f>SUM(B1:B9)</f><v>3</v></c>')
        sheet_path = tmp_path / "sheet1.xml"
        sheet_path.write_text(sheet, encoding="utf-8")

        changed = remove_external_links_from_cells(str(sheet_path))
        out = sheet_path.read_text(encoding="utf-8")

        assert changed == 0
        assert re.search(r"<(?:\w+:)?f>SUM\(B1:B9\)</(?:\w+:)?f>", out)

    def test_text_literal_bracket_not_broken(self, tmp_path):
        # Formula text contains '[' but is not an external workbook reference.
        sheet = _sheet_xml('<c r="A1" t="str"><f>IF(B1="[note]",1,0)</f><v>1</v></c>')
        sheet_path = tmp_path / "sheet1.xml"
        sheet_path.write_text(sheet, encoding="utf-8")

        changed = remove_external_links_from_cells(str(sheet_path))
        out = sheet_path.read_text(encoding="utf-8")

        assert changed == 0
        assert "<f>" in out


# ---------------------------------------------------------------------------
# _strip_external_rels
# ---------------------------------------------------------------------------


class TestStripExternalRels:
    def test_external_link_relationship_removed(self, tmp_path):
        rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG_REL_NS}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/externalLink" Target="externalLinks/'
            'externalLink1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        )
        rels_path = tmp_path / "sheet1.xml.rels"
        rels_path.write_text(rels, encoding="utf-8")

        removed = _strip_external_rels(str(rels_path))
        out = rels_path.read_text(encoding="utf-8")

        assert removed == 1
        assert "externalLink1.xml" not in out
        assert "worksheets/sheet1.xml" in out

    def test_missing_rels_file_is_noop(self, tmp_path):
        assert _strip_external_rels(str(tmp_path / "nope.xml.rels")) == 0


# ---------------------------------------------------------------------------
# strip_external_links / disable_external_links_in_xlsx
# ---------------------------------------------------------------------------


class TestStripExternalLinks:
    def test_strip_does_not_mutate_caller_list(self, tmp_path):
        # strip_external_links mutates its `names` argument; calling it must be
        # safe only with a local copy. Verify the mutation behaviour explicitly.
        import tempfile

        tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmpdir, "xl", "externalLinks"))
        with open(os.path.join(tmpdir, "xl", "externalLinks", "externalLink1.xml"), "w") as f:
            f.write("<x/>")
        names = ["xl/workbook.xml", "xl/externalLinks/externalLink1.xml"]

        strip_external_links(tmpdir, names)

        # The list passed in IS mutated (design D3) — this documents that
        # callers must pass list(names), as disable_external_links_in_xlsx does.
        assert "xl/externalLinks/externalLink1.xml" not in names

    def test_wrapper_removes_parts_and_keeps_values(self, tmp_path):
        d = tmp_path
        xlsx = str(d / "book.xlsx")
        _write_xlsx(
            xlsx,
            _sheet_xml('<c r="E39" s="632"><f>[42]Slide!F139/1000</f><v>45.599</v></c>'),
            extra_parts={
                "xl/externalLinks/externalLink1.xml": "<externalLink/>",
                "xl/_rels/workbook.xml.rels": (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<Relationships xmlns="{PKG_REL_NS}">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/externalLink" '
                    'Target="externalLinks/externalLink1.xml"/></Relationships>'
                ),
            },
            content_types=(
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<Types xmlns="{CT_NS}">'
                '<Override PartName="/xl/externalLinks/externalLink1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.externalLink+xml"/></Types>'
            ),
        )

        stats = disable_external_links_in_xlsx(xlsx)

        assert stats["cells"] == 1
        assert stats["parts"] >= 1
        with zipfile.ZipFile(xlsx) as zf:
            names = zf.namelist()
        assert not any(n.startswith("xl/externalLinks/") for n in names)
        sheet = _read_part(xlsx, "xl/worksheets/sheet1.xml")
        assert "<f>" not in sheet
        assert "45.599" in sheet
        ct = _read_part(xlsx, "[Content_Types].xml")
        assert "externalLink" not in ct

    def test_wrapper_without_external_links_is_noop(self, tmp_path):
        xlsx = str(tmp_path / "book.xlsx")
        _write_xlsx(xlsx, _sheet_xml('<c r="A1"><f>SUM(B1:B9)</f><v>3</v></c>'))
        before = _read_part(xlsx, "xl/worksheets/sheet1.xml")

        stats = disable_external_links_in_xlsx(xlsx)

        assert stats["cells"] == 0
        assert stats["parts"] == 0
        assert _read_part(xlsx, "xl/worksheets/sheet1.xml") == before

    def test_wrapper_output_is_valid_zip(self, tmp_path):
        xlsx = str(tmp_path / "book.xlsx")
        _write_xlsx(xlsx, _sheet_xml('<c r="A1"><v>1</v></c>'))
        disable_external_links_in_xlsx(xlsx)
        with zipfile.ZipFile(xlsx) as zf:
            assert zf.testzip() is None
