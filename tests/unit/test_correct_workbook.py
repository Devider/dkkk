"""Unit tests for the upload-correction orchestrator statuses (tasks 3.1/3.2).

The UNO stage is monkeypatched out so these run without LibreOffice.  Cases:
``OK``/``OK_NO_CHANGES``/``WARN_UNFIXABLE``/``ERROR`` and the ``.xls`` skip.
"""

import os

import pytest

from aigw_service.api.v1 import excel_handler as eh


class _FakeXL:
    """Stand-in for ExcelWorkbook exposing the public correction methods."""

    instance = None

    def __init__(self, path):
        self.saved_to = None
        self.closed = False
        _FakeXL.instance = self

    def fix_long_choose(self):
        return _FakeXL.choose_result

    def save_as_xlsx(self, path):
        self.saved_to = path
        with open(path, "wb") as f:
            f.write(b"corrected")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


@pytest.fixture
def no_lo(monkeypatch):
    """Patch out external-link stripping and the UNO workbook stage."""
    monkeypatch.setattr(eh, "disable_external_links_in_xlsx", lambda path: {"skipped": True})
    monkeypatch.setattr(eh, "ExcelWorkbook", _FakeXL)
    _FakeXL.choose_result = {"fixed": 0, "unfixable": [], "scanned": 0}
    yield


def _make_xlsx(tmp_path):
    p = tmp_path / "book.xlsx"
    p.write_bytes(b"dummy")
    return str(p)


class TestCorrectionStatuses:
    def test_ok_when_changes_made(self, tmp_path, monkeypatch, no_lo):
        _FakeXL.choose_result = {"fixed": 3, "unfixable": [], "scanned": 3}
        monkeypatch.setattr(eh, "disable_external_links_in_xlsx", lambda path: {"parts": 5, "cells": 0})
        res = eh.correct_workbook_on_disk(_make_xlsx(tmp_path))
        assert res["status"] == eh.STATUS_OK
        assert "исправлено длинных CHOOSE: 3" in res["message"] or "3" in res["message"]

    def test_ok_no_changes(self, tmp_path, no_lo):
        _FakeXL.choose_result = {"fixed": 0, "unfixable": [], "scanned": 0}
        res = eh.correct_workbook_on_disk(_make_xlsx(tmp_path))
        assert res["status"] == eh.STATUS_OK_NO_CHANGES

    def test_warn_unfixable(self, tmp_path, no_lo):
        _FakeXL.choose_result = {
            "fixed": 1,
            "unfixable": [{"cell": "Dashboard!D5", "formula": "=CHOOSE(...)", "reason": "multiple_rows"}],
            "scanned": 2,
        }
        res = eh.correct_workbook_on_disk(_make_xlsx(tmp_path))
        assert res["status"] == eh.STATUS_WARN_UNFIXABLE
        assert "Dashboard!D5" in res["message"]
        assert "WARN" not in res["message"]  # message is human-readable, not the code

    def test_error_on_stage_failure(self, tmp_path, monkeypatch, no_lo):
        def boom(path):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(eh, "ExcelWorkbook", boom)
        res = eh.correct_workbook_on_disk(_make_xlsx(tmp_path))
        assert res["status"] == eh.STATUS_ERROR
        assert "disk on fire" in res["message"]

    def test_ok_no_changes_still_writes_corrected_copy(self, tmp_path, no_lo):
        # Even when nothing is fixed, the UNO stage re-saves the book (so the
        # cached values are materialised by Calc). The original path survives.
        path = _make_xlsx(tmp_path)
        _FakeXL.choose_result = {"fixed": 0, "unfixable": [], "scanned": 0}
        res = eh.correct_workbook_on_disk(path)
        assert res["status"] == eh.STATUS_OK_NO_CHANGES
        assert os.path.exists(path)


class TestXlsSkip:
    def test_xls_is_skipped_unchanged(self, tmp_path):
        p = tmp_path / "legacy.xls"
        p.write_bytes(b"BIFF8-ish bytes")
        before = p.read_bytes()
        res = eh.correct_workbook_on_disk(str(p))
        assert res["status"] == eh.STATUS_OK_NO_CHANGES
        assert res["report"]["skipped_reason"] == "not_ooxml"
        assert p.read_bytes() == before
        assert ".xlsx" in res["message"]

    def test_unknown_extension_skipped(self, tmp_path):
        p = tmp_path / "weird.bin"
        p.write_bytes(b"x")
        res = eh.correct_workbook_on_disk(str(p))
        assert res["report"]["skipped_reason"] == "not_ooxml"


class TestFlags:
    def test_disable_flag_off_skips_xml_stage(self, tmp_path, monkeypatch, no_lo):
        called = {"n": 0}

        def spy(path):
            called["n"] += 1
            return {"parts": 1}

        monkeypatch.setattr(eh, "disable_external_links_in_xlsx", spy)
        _FakeXL.choose_result = {"fixed": 1, "unfixable": [], "scanned": 1}
        eh.correct_workbook_on_disk(_make_xlsx(tmp_path), disable_external_links=False)
        assert called["n"] == 0

    def test_fix_flag_off_skips_uno_stage(self, tmp_path, monkeypatch, no_lo):
        monkeypatch.setattr(eh, "ExcelWorkbook", lambda path: pytest.fail("UNO stage should be skipped"))
        res = eh.correct_workbook_on_disk(_make_xlsx(tmp_path), fix_long_choose=False)
        assert res["report"]["choose"]["skipped"] is True
