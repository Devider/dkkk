"""Unit tests for lo_calc_all_fix output classification (tasks 3.1 / 3.2).

Covers ``_classify_output`` — the only self-contained unit added on the tools
side. The engine-side three-category contract (float / error str#…! / None)
and the end-to-end content block are integration concerns exercised against the
LO bridge (see design D2 and tasks 4.1–4.3).
"""

import pytest

from aigw_service.api.v1.tools import _classify_output


class TestClassifyOutput:
    """Classification of a compiled-function output value → (display, reason).

    Per spec "Явное уведомление" (Вариант 4): an output is "uncalculated" if it
    is a formula error (``str#…!``) or empty/None; a legitimate ``0.0`` is never
    reported as uncalculated.
    """

    def test_legitimate_float(self):
        display, reason = _classify_output(3.14159)
        assert reason is None
        assert display == round(3.14159, 3)

    def test_legitimate_zero_is_not_uncalculated(self):
        # 0.0 is a legitimate result and must NOT be flagged.
        display, reason = _classify_output(0.0)
        assert reason is None
        assert display == 0.0

    def test_formula_error_is_uncalculated(self):
        # Formula error string from _scalar (e.g. #ЗНАЧ!/#ДЕЛ/0!/#Н/Д).
        for err in ("#ЗНАЧ!", "#ДЕЛ/0!", "#Н/Д", "#ИМЯ?"):
            display, reason = _classify_output(err)
            assert reason == "formula_error", err
            assert display is None

    def test_none_is_uncalculated_empty(self):
        display, reason = _classify_output(None)
        assert reason == "empty"
        assert display is None

    def test_nested_list_unwraps(self):
        # evaluate() returns bare scalars, but _formula_scalar tolerated lists.
        display, reason = _classify_output([27.43385982230997])
        assert reason is None
        assert display == round(27.43385982230997, 3)

    def test_error_list_unwraps(self):
        display, reason = _classify_output(["#ЗНАЧ!"])
        assert reason == "formula_error"
        assert display is None