"""Unit tests for the CHOOSE -> INDEX normalisation helpers (task 2.3).

The formula parsing/rewrite is pure Python (no LibreOffice bridge), so these
tests run anywhere.  End-to-end semantics of the rewrite (that
``INDEX(range; idx)`` equals the original ``CHOOSE`` for the same index) are
covered by the live LibreOffice integration test.
"""

import pytest

from aigw_service.api.v1.lo_backend import (
    _choose_to_index,
    _col_letter,
    _col_number,
    _looks_like_long_choose,
    _split_args,
)


def _long_choose(row: int = 33, n: int = 40, index: str = "$D$30") -> str:
    refs = ";".join(f"{_col_letter(c)}{row}" for c in range(6, 6 + n))
    return f"=CHOOSE({index};{refs})"


class TestColumnHelpers:
    def test_letters_roundtrip(self):
        for n, letters in [(1, "A"), (26, "Z"), (27, "AA"), (28, "AB"), (54, "BB"), (55, "BC")]:
            assert _col_letter(n) == letters
            assert _col_number(letters) == n


class TestSplitArgs:
    def test_semicolon_separated(self):
        assert _split_args("$D$30;F33;G33") == ["$D$30", "F33", "G33"]

    def test_nested_parens_not_split(self):
        assert _split_args("A1;SUM(B1;C1);D1") == ["A1", "SUM(B1;C1)", "D1"]

    def test_quoted_separator_not_split(self):
        assert _split_args('A1;"a;b";C1') == ["A1", '"a;b"', "C1"]

    def test_escaped_quote_inside_string(self):
        assert _split_args('"a"";b";C1') == ['"a"";b"', "C1"]

    def test_unbalanced_returns_none(self):
        assert _split_args("A1;(B1") is None

    def test_comma_separator(self):
        assert _split_args("A1,B1,C1") == ["A1", "B1", "C1"]


class TestLooksLikeLongChoose:
    def test_long_choose_is_detected(self):
        assert _looks_like_long_choose(_long_choose(n=40), 30) is True

    def test_short_choose_is_not_detected(self):
        assert _looks_like_long_choose("=CHOOSE($F40;M75;M98;M121;M144)", 30) is False

    def test_exactly_31_value_args_is_long(self):
        # 1 index + 31 values = 32 args -> long (>30 values? no: >max_args values)
        # max_args=30 value args; 30 values is boundary -> not long
        f30 = _long_choose(n=30)
        assert _looks_like_long_choose(f30, 30) is False
        f31 = _long_choose(n=31)
        assert _looks_like_long_choose(f31, 30) is True

    def test_case_insensitive(self):
        assert _looks_like_long_choose(_long_choose(n=40).replace("CHOOSE", "choose"), 30) is True
        assert _looks_like_long_choose(_long_choose(n=40).replace("CHOOSE", "Choose"), 30) is True

    def test_wrapped_in_if_is_detected(self):
        f = "=IF(A1=0;0;" + _long_choose(n=40)[1:] + ")"
        assert _looks_like_long_choose(f, 30) is True

    def test_unrelated_function_not_detected(self):
        assert _looks_like_long_choose("=SUM(A1:A100)", 30) is False


class TestChooseToIndex:
    def test_contiguous_single_row_is_rewritten(self):
        new, reason = _choose_to_index("=CHOOSE($D$30;F33;G33;H33)")
        assert reason is None
        assert new == "=INDEX(F33:H33;$D$30)"

    def test_relative_refs_stay_relative(self):
        new, _ = _choose_to_index(_long_choose(row=33, n=50))
        assert new == "=INDEX(F33:BC33;$D$30)"

    def test_absolute_refs_stay_absolute(self):
        refs = ";".join(f"$F${33}" if i == 0 else f"${_col_letter(6 + i)}${33}" for i in range(4))
        new, reason = _choose_to_index(f"=CHOOSE($D$30;{refs})")
        assert reason is None
        assert new == "=INDEX($F$33:$I$33;$D$30)"

    def test_column_gap_is_unfixable(self):
        new, reason = _choose_to_index("=CHOOSE($D$30;F33;G33;I33)")
        assert new is None
        assert reason == "non_contiguous_columns"

    def test_multiple_rows_is_unfixable(self):
        new, reason = _choose_to_index("=CHOOSE($D$30;F33;G34;H35)")
        assert new is None
        assert reason == "multiple_rows"

    def test_cross_sheet_ref_is_unfixable(self):
        new, reason = _choose_to_index("=CHOOSE($D$30;F33;Other!G33)")
        assert new is None
        assert reason is not None and reason.startswith("non_reference_arg")

    def test_formula_argument_is_unfixable(self):
        new, reason = _choose_to_index("=CHOOSE($D$30;F33;SUM(G33;H33))")
        assert new is None
        assert reason is not None and reason.startswith("non_reference_arg")

    def test_mixed_abs_is_unfixable(self):
        new, reason = _choose_to_index("=CHOOSE($D$30;F33;$G$33)")
        assert new is None
        assert reason in ("mixed_column_abs", "mixed_row_abs")

    def test_surrounding_text_is_preserved(self):
        new, reason = _choose_to_index("=IF(A1=0;0;CHOOSE($D$30;F33;G33;H33))")
        assert reason is None
        assert new == "=IF(A1=0;0;INDEX(F33:H33;$D$30))"

    def test_preserves_original_index_expression(self):
        new, _ = _choose_to_index("=CHOOSE(A1*2+1;F33;G33;H33)")
        assert new == "=INDEX(F33:H33;A1*2+1)"

    def test_lowercase_function_is_rewritten(self):
        new, reason = _choose_to_index("=choose($D$30;F33;G33;H33)")
        assert reason is None
        assert new == "=INDEX(F33:H33;$D$30)"


class TestChooseIndexSemantics:
    """The rewritten INDEX must select the same element CHOOSE would.

    Mirrors Calc's INDEX(range; position) for a single-row range, without the
    bridge: pick the idx-th value argument.
    """

    @pytest.mark.parametrize("idx", [1, 2, 3, 20, 50])
    def test_position_semantics(self, idx):
        row = 40
        n = 50  # F..BC inclusive
        choose = _long_choose(row=row, n=n)
        new, reason = _choose_to_index(choose)
        assert reason is None
        # INDEX(F40:BC40; $D$30) -> the idx-th cell of the row range.
        assert new == f"=INDEX(F{row}:BC{row};$D$30)"
        # The idx-th argument of the CHOOSE was column F + (idx-1); its value
        # is what the rewritten INDEX returns for this index.
        args = _split_args(choose[choose.index("(") + 1 : -1])
        assert args[idx] == f"{_col_letter(6 + idx - 1)}{row}"
