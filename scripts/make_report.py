import argparse
import ast
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "tests/test_results"


def _read_results(file_path: Path) -> pd.DataFrame:
    """Read results file as DataFrame (CSV or XLSX)."""
    if file_path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(file_path, usecols="A:U")
    return pd.read_csv(file_path)


def _excel_range_to_usecols(spec: str, total_cols: int) -> list[int]:
    """Translate Excel-style column range like 'A:P' into zero-based indices."""
    cols: list[int] = []
    for part in spec.split(":"):
        idx = 0
        for ch in part.upper():
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
        cols.append(idx - 1)
    if len(cols) == 1:
        cols.append(total_cols - 1)
    return list(range(cols[0], cols[1] + 1))


def _safe_literal_eval(value: Any) -> Any:
    """ast.literal_eval that maps NaN/None to an empty list (CSV-safe)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str) and value.strip() == "":
        return []
    return ast.literal_eval(value)


def _list_match(actual: list, expected: list) -> bool:
    """Check if two string-lists match regardless of order."""
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    if len(actual) != len(expected):
        return False
    return set(actual) == set(expected)


def _steps_match(actual: list, expected: list) -> bool:
    """Check if two step lists match after sorting, comparing floats."""
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    if len(actual) != len(expected):
        return False
    a_sorted = sorted(float(v) for v in actual)
    e_sorted = sorted(float(v) for v in expected)
    return all(math.isclose(a, e, rel_tol=1e-9, abs_tol=1e-9) for a, e in zip(a_sorted, e_sorted, strict=False))


def _ranges_match(actual: list, expected: list) -> bool:
    """Check if two ranges lists match regardless of order.

    Each is a list of [start, end] pairs. All pairs must be identical
    (order-independent — uses frozenset). Values compared as floats.
    Different lengths → False.
    """
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    if len(actual) != len(expected):
        return False
    # Normalize inner lists to tuples of floats for set comparison
    actual_set = frozenset(tuple(float(v) for v in r) for r in actual if isinstance(r, list) and len(r) == 2)
    expected_set = frozenset(tuple(float(v) for v in r) for r in expected if isinstance(r, list) and len(r) == 2)
    return actual_set == expected_set


def _convert_and_verify(df: pd.DataFrame, cols: list[str]) -> None:
    """Apply ast.literal_eval to string columns and verify all values are lists.

    ``cols`` — names of columns whose values are serialized lists (e.g.
    ``"['a', 'b']"``) that need to be converted to Python lists.

    Raises ValueError if a column is missing or a cell is not a list
    after conversion.
    """
    for col in cols:
        if col not in df.columns:
            raise ValueError(f"Column '{col}' not found. Available: {list(df.columns)}")
        df[col] = df[col].apply(_safe_literal_eval)
        bad = df[~df[col].apply(lambda x: isinstance(x, list))]
        if len(bad) > 0:
            idx = bad.index[0]
            val = bad.iloc[0]
            raise ValueError(f"After conversion, column '{col}' still has non-list at row {idx}: {val!r}")


def _resolve(path_arg: str | None) -> Path:
    """Prepend PROJECT_ROOT to a user-supplied path."""
    return PROJECT_ROOT / path_arg


def _find_latest(directory: Path, must_contain: str) -> Path | None:
    """Find the latest results file under ``directory`` whose name contains
    ``must_contain`` (preferring *_v1.xlsx over CSV)."""
    candidates = [
        p
        for p in directory.iterdir()
        if p.is_file()
        and must_contain in p.name
        and p.suffix.lower() in {".csv", ".xlsx", ".xls"}
        and not p.name.startswith("~$")
        and not p.name.startswith(".~lock.")
    ]
    if not candidates:
        return None
    xlsx = [p for p in candidates if p.name.endswith("_v1.xlsx")]
    if xlsx:
        return max(xlsx, key=lambda p: p.stat().st_mtime)
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _collect_mismatches(
    df: pd.DataFrame, match_col: str, label: str, mismatches: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Collect failing IDs for a metric into a shared dict.

    Args:
        mismatches: Shared dict {'label': [ids]} accumulates results.
                    Created on first call if None.

    Returns:
        The shared mismatches dict.
    """
    if mismatches is None:
        mismatches = {}
    failed = df[df[match_col] == False]["id"].tolist()
    if failed:
        mismatches[label] = failed
    return mismatches


def report_for_ema(
    df: pd.DataFrame,
    show_mismatches: bool = True,
    title: str = "analyze_excel_model",
) -> tuple[int, int, int, int, dict[str, list[str]]]:
    """Compute and print EMA report stats; return (total, matched, skipped, mismatch_block_id).

    ``mismatch_block_id`` — index under which mismatch details are stored
    for deferred printing at the very end.
    """
    # Filter by scenario_type
    df = df[df["scenario_type"] == "analyze_excel_model"].reset_index(drop=True)
    if df.empty:
        print("No EMA records found.")
        return (0, 0, 0, 0, {})

    # --- skipped / error rows ---
    comment = df["comment"].astype(str).str.strip()
    skipped_mask = df["comment"].notna() & comment.str.contains("Пропускаем:", na=False)
    df_total = len(df)
    skipped_count = int(skipped_mask.sum())

    # --- classification ---
    df["classification_pass"] = df["expected_class"] == df["actual_class"]
    correct_classification = df["classification_pass"].sum()

    # --- list columns ---
    list_cols = [
        c
        for c in [
            "actual_inputs",
            "expected_inputs",
            "actual_outputs",
            "expected_outputs",
            "actual_ranges",
            "expected_ranges",
            "actual_steps",
            "expected_steps",
        ]
        if c in df.columns
    ]
    _convert_and_verify(df, list_cols)

    # Match booleans
    df["inputs_match"] = pd.Series(
        [_list_match(a, e) for a, e in zip(df["actual_inputs"], df["expected_inputs"], strict=False)],
        index=df.index,
    )
    df["outputs_match"] = pd.Series(
        [_list_match(a, e) for a, e in zip(df["actual_outputs"], df["expected_outputs"], strict=False)],
        index=df.index,
    )
    df["ranges_match"] = pd.Series(
        [_ranges_match(a, e) for a, e in zip(df["actual_ranges"], df["expected_ranges"], strict=False)],
        index=df.index,
    )
    df["steps_match"] = (
        pd.Series(
            [_steps_match(a, e) for a, e in zip(df["actual_steps"], df["expected_steps"], strict=False)],
            index=df.index,
        )
        if "actual_steps" in df.columns
        else pd.Series(True, index=df.index)
    )
    df["year_pass"] = df["actual_year"] == df["expected_year"]

    # Parameter counts
    df["input_param_count"] = df["actual_inputs"].apply(len)
    df["output_param_count"] = df["actual_outputs"].apply(len)
    df["range_param_count"] = df["actual_ranges"].apply(lambda r: len(r) * 2 if isinstance(r, list) else 0)
    df["step_param_count"] = (
        df["actual_steps"].apply(len) if "actual_steps" in df.columns else pd.Series(0, index=df.index)
    )
    df["year_param_count"] = 1

    matched_inputs = df["inputs_match"].sum()
    matched_outputs = df["outputs_match"].sum()
    matched_ranges = df["ranges_match"].sum()
    matched_steps = df["steps_match"].sum()
    matched_year_pass = df["year_pass"].sum()

    has_steps = "actual_steps" in df.columns and "expected_steps" in df.columns
    if has_steps:
        all_pass = (df["inputs_match"] & df["outputs_match"] & df["ranges_match"] & df["steps_match"]).sum()
    else:
        all_pass = (df["inputs_match"] & df["outputs_match"] & df["ranges_match"]).sum()

    # Parameter-level totals
    total_input_params = df["input_param_count"].sum()
    matched_input_params = df.loc[df["inputs_match"], "input_param_count"].sum()
    total_output_params = df["output_param_count"].sum()
    matched_output_params = df.loc[df["outputs_match"], "output_param_count"].sum()
    total_range_params = df["range_param_count"].sum()
    matched_range_params = df.loc[df["ranges_match"], "range_param_count"].sum()
    total_step_params = df["step_param_count"].sum()
    matched_step_params = df.loc[df["steps_match"], "step_param_count"].sum()
    total_year_params = df["year_param_count"].sum()
    matched_year_params = df.loc[df["year_pass"], "year_param_count"].sum()

    if has_steps:
        total_all_params = total_input_params + total_output_params + total_range_params + total_step_params + total_year_params
        matched_all_params = matched_input_params + matched_output_params + matched_range_params + matched_step_params + matched_year_params
    else:
        total_all_params = total_input_params + total_output_params + total_range_params + total_year_params
        matched_all_params = matched_input_params + matched_output_params + matched_range_params + matched_year_params

    # --- Print stats ---
    print(f"=== {title} ({df_total} queries) ===\n")
    print(f"\tPerfect call rate:   {int(all_pass)}/{df_total} ({round(all_pass / df_total * 100, 2)}%)\n")
    print(f"\tClassification rate:    {correct_classification}/{df_total} ({round(correct_classification / df_total * 100, 2)}%)")
    if skipped_count > 0:
        print(f"\tSkipped intentionally: {skipped_count}")
    print(f"\tInputs matched:       {matched_inputs}/{df_total} ({round(matched_inputs / df_total * 100, 2)}%)")
    print(f"\tOutputs matched:      {matched_outputs}/{df_total} ({round(matched_outputs / df_total * 100, 2)}%)")
    print(f"\tRanges matched:       {matched_ranges}/{df_total} ({round(matched_ranges / df_total * 100, 2)}%)")
    if has_steps:
        print(f"\tSteps matched:        {matched_steps}/{df_total} ({round(matched_steps / df_total * 100, 2)}%)")
    print(f"\tYear:                 {matched_year_params}/{df_total} ({round(matched_year_params / df_total * 100, 2)}%)")
    print(f"\tTotal params:         {matched_all_params}/{total_all_params} ({round(matched_all_params / total_all_params * 100, 2)}%)")
    print(f"\t  Input params:       {int(matched_input_params)}/{int(total_input_params)} ({round(matched_input_params / total_input_params * 100, 2)}%)")
    print(f"\t  Output params:      {int(matched_output_params)}/{int(total_output_params)} ({round(matched_output_params / total_output_params * 100, 2)}%)")
    print(f"\t  Range params:       {int(matched_range_params)}/{int(total_range_params)} ({round(matched_range_params / total_range_params * 100, 2)}%)")
    if has_steps:
        step_pct = round(matched_step_params / total_step_params * 100, 2) if total_step_params > 0 else 0.0
        print(f"\t  Step params:        {int(matched_step_params)}/{int(total_step_params)} ({step_pct}%)")
    print(f"\t  Year params:        {int(matched_year_params)}/{int(total_year_params)} ({round(matched_year_params / total_year_params * 100, 2)}%)\n")

    # --- Collect mismatches (don't print yet) ---
    mismatch_block: dict[str, list[str]] = {}
    if show_mismatches:
        _collect_mismatches(df, "inputs_match", "Inputs", mismatch_block)
        _collect_mismatches(df, "outputs_match", "Outputs", mismatch_block)
        _collect_mismatches(df, "ranges_match", "Ranges", mismatch_block)
        if has_steps:
            _collect_mismatches(df, "steps_match", "Steps", mismatch_block)
        _collect_mismatches(df, "year_pass", "Year", mismatch_block)

    return (df_total, int(all_pass), skipped_count, 0, mismatch_block)


def report_for_ift(
    df: pd.DataFrame,
    show_mismatches: bool = True,
    title: str = "analyze_model_inputs_for_target",
) -> tuple[int, int, int, int, dict[str, list[str]]]:
    """Compute and print IFT report stats; return (total, matched, skipped, mismatch_block_id)."""
    # Filter by scenario_type
    df = df[df["scenario_type"] == "analyze_model_inputs_for_target"].reset_index(drop=True)
    if df.empty:
        print("No IFT records found.")
        return (0, 0, 0, 0, {})

    # --- skipped / error rows ---
    comment = df["comment"].astype(str).str.strip()
    skipped_mask = df["comment"].notna() & comment.str.contains("Пропускаем:", na=False)
    df_total = len(df)
    skipped_count = int(skipped_mask.sum())

    # --- list columns ---
    df["expected_inputs"] = df["expected_inputs"].apply(_safe_literal_eval)
    df["actual_inputs"] = df["actual_inputs"].apply(_safe_literal_eval)
    df["output_pass"] = df["actual_output"] == df["expected_output"]
    df["target_pass"] = df["actual_target"] == df["expected_target"]
    df["year_pass"] = df["actual_year"] == df["expected_year"]
    df["inputs_match"] = pd.Series(
        [
            _list_match(actual or [], expected or [])
            for actual, expected in zip(df["actual_inputs"], df["expected_inputs"], strict=False)
        ],
        index=df.index,
    )

    # Pass = all 4 metrics match
    df["passed"] = df["inputs_match"] & df["output_pass"] & df["target_pass"] & df["year_pass"]

    passed = df["passed"].sum()
    passed_share = round(passed / df_total * 100, 2)

    # Classification
    df["classification_pass"] = df["expected_class"] == df["actual_class"]
    correct_classification = df["classification_pass"].sum()

    # Parameter-level
    total_input_params = df["expected_inputs"].apply(len).sum()
    matched_input_params = df.loc[df["inputs_match"], "expected_inputs"].apply(len).sum()
    total_output_params = df["expected_output"].count()
    matched_output_params = df["output_pass"].sum()
    total_target_params = df["expected_target"].count()
    matched_target_params = df["target_pass"].sum()
    total_year_params = df["expected_year"].count()
    matched_year_params = df["year_pass"].sum()

    total_params = total_input_params + total_output_params + total_target_params + total_year_params
    detected_params = matched_input_params + matched_output_params + matched_target_params + matched_year_params
    share_params = round(detected_params / total_params * 100, 2) if total_params > 0 else 0.0

    # --- Print stats ---
    print(f"=== {title} ({df_total} queries) ===\n")
    print(f"\tPerfect call rate: {passed}/{df_total} ({passed_share}%)\n")
    print(f"\tClassification rate:  {correct_classification}/{df_total} ({round(correct_classification / df_total * 100, 2)}%)")
    if skipped_count > 0:
        print(f"\tSkipped intentionally: {skipped_count}")
    print(f"\tTotal params:         {detected_params}/{total_params} ({share_params}%)")
    print(f"\t  Input params:       {int(matched_input_params)}/{int(total_input_params)} ({round(matched_input_params / total_input_params * 100, 2) if total_input_params > 0 else 0}%)")
    print(f"\t  Output params:      {matched_output_params}/{total_output_params} ({round(matched_output_params / total_output_params * 100, 2) if total_output_params > 0 else 0}%)")
    print(f"\t  Target:             {matched_target_params}/{total_target_params} ({round(matched_target_params / total_target_params * 100, 2) if total_target_params > 0 else 0}%)")
    print(f"\t  Year:               {matched_year_params}/{total_year_params} ({round(matched_year_params / total_year_params * 100, 2)}%)\n")

    # --- Collect mismatches ---
    mismatch_block: dict[str, list[str]] = {}
    if show_mismatches:
        _collect_mismatches(df, "inputs_match", "Inputs", mismatch_block)
        _collect_mismatches(df, "output_pass", "Output", mismatch_block)
        _collect_mismatches(df, "target_pass", "Target", mismatch_block)
        _collect_mismatches(df, "year_pass", "Year", mismatch_block)

    return (df_total, int(passed), skipped_count, 0, mismatch_block)


# ── Main ────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate unified test-results report.")
    parser.add_argument(
        "--path",
        default=None,
        help="Path to unified results file (CSV/XLSX). Default: latest match under tests/test_results.",
    )
    parser.add_argument(
        "--no-mismatches",
        action="store_true",
        help="Only print statistics, omit mismatch ID lists.",
    )
    args = parser.parse_args()

    if args.path:
        results_path = _resolve(args.path)
    else:
        results_path = _find_latest(DEFAULT_RESULTS_DIR, "unified_results")

    if results_path:
        print(f"Report from: {results_path.relative_to(PROJECT_ROOT)}")
        df = _read_results(results_path)

        # Collect stats from both scenario types
        ema_total, ema_passed, ema_skipped, _, ema_mm = report_for_ema(
            df, show_mismatches=not args.no_mismatches
        )
        print("---")
        ift_total, ift_passed, ift_skipped, _, ift_mm = report_for_ift(
            df, show_mismatches=not args.no_mismatches
        )

        total_queries = ema_total + ift_total
        total_skipped = ema_skipped + ift_skipped

        # --- General summary ---
        print("---")
        print("=== General ===\n")
        print(f"\tTotal queries:          {total_queries}")
        if total_skipped > 0:
            print(f"\tSkipped intentionally:  {total_skipped}")
        correct_class = ema_passed + ift_passed  # perfect_call == classification+all_metrics
        incorrect_class = (ema_total - ema_passed) + (ift_total - ift_passed)
        print(f"\tCorrect classification: {correct_class}/{total_queries} ({round(correct_class / total_queries * 100, 2)}%)")
        print(f"\tIncorrect classification: {incorrect_class}/{total_queries} ({round(incorrect_class / total_queries * 100, 2)}%)\n")

        # --- Mismatch details (deferred) ---
        if not args.no_mismatches:
            all_mismatches = {**ema_mm, **ift_mm}
            if all_mismatches:
                print("---")
                print("=== Mismatch Details ===\n")
                for label, ids in all_mismatches.items():
                    print(f"{label} mismatch: {ids}")
                print()
    else:
        print("Unified results not found — skipping.")


if __name__ == "__main__":
    sys.exit(main())
