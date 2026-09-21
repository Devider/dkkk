#!/usr/bin/env python3
"""
Aggregate experiment run statistics from all results CSV files in a given
subfolder under experiments/.

Usage:
    poetry run python scripts/explore_experiment.py <folder_name> [--no-mismatches] [--export-problems] [--output <path>]

Examples:
    poetry run python scripts/explore_experiment.py full_run_real_models
    poetry run python scripts/explore_experiment.py full_run_real_models --no-mismatches
    poetry run python scripts/explore_experiment.py full_run_real_models --export-problems
    poetry run python scripts/explore_experiment.py full_run_real_models --export-problems --output /path/problems

Export modes:
    --export-problems  — Export failed test cases to Excel (.xlsx) with two sheets: EMA and IFT.
                         Each problem type gets its own row (e.g., input_mismatch, output_mismatch).
                         Clean IDs used (A001, T011) without file suffix.
    --output <path>    — Output Excel path (default: experiments/<folder>/issues.xlsx)
"""

import argparse
import ast
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = PROJECT_ROOT / "experiments"


# ── Helper functions (duplicated from make_report.py) ──────────────────────


def _safe_literal_eval(value: Any) -> Any:
    """ast.literal_eval that maps NaN/None to an empty list (CSV-safe)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, str) and value.strip() == "":
        return []
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        # If it's not a valid Python literal, return as-is (might be plain text)
        return value


def _list_match(actual: list, expected: list) -> bool:
    """Check if two string-lists match regardless of order and whitespace."""
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    if len(actual) != len(expected):
        return False
    return set(str(s).strip() for s in actual) == set(str(s).strip() for s in expected)


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
    """
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    if len(actual) != len(expected):
        return False
    actual_set = frozenset(tuple(float(v) for v in r) for r in actual if isinstance(r, list) and len(r) == 2)
    expected_set = frozenset(tuple(float(v) for v in r) for r in expected if isinstance(r, list) and len(r) == 2)
    return actual_set == expected_set


def _convert_and_verify(df: pd.DataFrame, cols: list[str]) -> None:
    """Apply ast.literal_eval to string columns and verify all values are lists."""
    for col in cols:
        if col not in df.columns:
            raise ValueError(f"Column '{col}' not found. Available: {list(df.columns)}")
        df[col] = df[col].apply(_safe_literal_eval)
        bad = df[~df[col].apply(lambda x: isinstance(x, list))]
        if len(bad) > 0:
            idx = bad.index[0]
            val = bad.iloc[0]
            raise ValueError(f"After conversion, column '{col}' still has non-list at row {idx}: {val!r}")


def _collect_mismatches(
    df: pd.DataFrame,
    match_col: str,
    label: str,
    mismatches: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Collect failing IDs for a metric into a shared dict.

    Args:
        mismatches: Shared dict {'label': [ids]} accumulates results.

    Returns:
        The shared mismatches dict.
    """
    if mismatches is None:
        mismatches = {}
    failed = df[df[match_col] == False]["id"].tolist()
    if failed:
        mismatches[label] = failed
    return mismatches


# ── Mask builders ─────────────────────────────────────────────────────────


def _build_exclusion_mask(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Build separate masks for processing errors and intentional skips.

    Returns:
        (error_mask, skip_mask) — rows to be excluded from all calculations.
        error_mask: actual_class == "unknown"
        skip_mask: actual_class empty AND comment contains "Пропускаем:"
    """
    # actual_class == "unknown" → ошибка обработки
    error_mask = df["actual_class"] == "unknown"

    # actual_class пустой + намеренный пропуск
    actual_empty = df["actual_class"].isna() | (df["actual_class"].astype(str).str.strip() == "")
    comment = df["comment"].astype(str).str.strip()
    skip_mask = actual_empty & df["comment"].notna() & comment.str.contains("Пропускаем:", na=False)

    return error_mask, skip_mask


def _build_classification_only_mask(df: pd.DataFrame, exclusion_mask: pd.Series) -> pd.Series:
    """Build mask for rows with incorrect classification (excluded from metric calculations).

    These are rows where actual_class != expected_class, but are NOT excluded
    (i.e. not unknown and not intentionally skipped).
    They count only in overall classification statistics, not in per-metric calculations.
    """
    classification_wrong = df["expected_class"] != df["actual_class"]
    # Not excluded (not unknown, not intentional skip)
    not_excluded = ~exclusion_mask
    return classification_wrong & not_excluded


# ── File discovery & loading ──────────────────────────────────────────────


def _find_csv_files(folder_name: str) -> list[Path]:
    """Find all results CSV files in the root of experiments/<folder_name>/."""
    folder = EXPERIMENTS_DIR / folder_name
    if not folder.is_dir():
        print(f"Error: folder 'experiments/{folder_name}' not found.")
        sys.exit(1)

    files = sorted(
        p
        for p in folder.iterdir()
        if p.is_file()
        and "_results" in p.name
        and p.suffix.lower() in {".csv", ".xlsx", ".xls"}
        and not p.name.startswith("~$")
        and not p.name.startswith(".~lock.")
    )

    if not files:
        print(f"Error: no *_results*.csv found in experiments/{folder_name}/")
        sys.exit(1)

    return files


def _extract_file_suffix(filepath: Path) -> str:
    """Extract unique suffix from filename (session-id between prefix and date).

    E.g. ema_6m576vu02eud_2026-09-08_13-58-45_results.csv → 6m576vu02eud
    """
    name = filepath.stem  # without .csv
    # Remove leading prefix (ema_, ift_)
    parts = name.split("_", 1)
    if len(parts) < 2:
        return name
    prefix_removed = parts[1]  # e.g. 6m576vu02eud_2026-09-08_13-58-45_results
    # Session-id is the part before the first date-like pattern (YYYY-MM-DD or YYYY-MM-DD_hh-mm-ss)
    date_parts = prefix_removed.split("_")
    # Look for a 4-digit year (e.g. "2026" from "2026-09-08")
    for i, part in enumerate(date_parts):
        if part.startswith("20") or part.startswith("19"):
            if part[:4].isdigit() and len(part) >= 4:
                return "_".join(date_parts[:i])
    # Fallback: return everything up to "unified"
    if "unified" in prefix_removed:
        return prefix_removed.split("_unified")[0]
    return prefix_removed


def _load_all_csv(files: list[Path]) -> pd.DataFrame:
    """Read all CSV files and concatenate them into a single DataFrame.

    Preserves original 'id' (e.g. A001, T001) in _clean_id column.
    Appends file suffix to 'id' for uniqueness in mismatch output.
    """
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            suffix = _extract_file_suffix(f)
            source_hash = _extract_hash_from_filename(f)
            # Preserve original clean id (A001, T001 etc.) before modification
            if "id" in df.columns:
                df["_clean_id"] = df["id"].astype(str)
                df["id"] = df["id"].astype(str) + "_" + suffix
            df["_source_file"] = f.name  # track origin
            df["_source_hash"] = source_hash
            dfs.append(df)
        except Exception as e:
            print(f"Warning: could not read {f.name}: {e}")

    if not dfs:
        print("Error: no data loaded.")
        sys.exit(1)

    return pd.concat(dfs, ignore_index=True)


# ── Statistics: Overall ──────────────────────────────────────────────────


def _overall_stats(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series, int, int, int, int]:
    """Calculate overall statistics and return masks for downstream analysis.

    Returns:
        (df_valid, exclusion_mask, class_only_mask, errors_count, skipped_count, correct_class, incorrect_class)
        - df_valid: DataFrame with excluded rows removed
        - exclusion_mask: rows fully excluded (unknown + intentional skip)
        - class_only_mask: rows with incorrect classification (metrics only)
        - errors_count: number of processing errors (actual_class == "unknown")
        - skipped_count: number of intentionally skipped rows
        - correct_class: number of correctly classified queries
        - incorrect_class: number of incorrectly classified queries (not excluded)
    """
    # Build masks
    error_mask, skip_mask = _build_exclusion_mask(df)
    exclusion_mask = error_mask | skip_mask
    errors_count = int(error_mask.sum())  # только actual_class == "unknown"
    skipped_count = int(skip_mask.sum())

    # Classification-only mask (incorrect classification, not excluded)
    class_only_mask = _build_classification_only_mask(df, exclusion_mask)

    # Valid rows: exclude both processing errors and intentional skips
    valid_mask = ~exclusion_mask
    df_valid = df[valid_mask].reset_index(drop=True)

    # Classification accuracy on valid rows
    valid_df = df[valid_mask]
    correct_class = int((valid_df["expected_class"] == valid_df["actual_class"]).sum())
    incorrect_class = int(class_only_mask.sum())

    return df_valid, exclusion_mask, class_only_mask, errors_count, skipped_count, correct_class, incorrect_class


# ── Statistics: analyze_excel_model ───────────────────────────────────────


def _report_ema(
    df: pd.DataFrame,
    exclusion_mask: pd.Series,
    class_only_mask: pd.Series,
    show_mismatches: bool,
) -> tuple[int, dict[str, list[str]] | None]:
    """Compute EMA stats; return (df_total, metric_mismatches).

    Only correctly classified rows participate in metric calculations.
    Incorrectly classified (but not excluded) rows are only counted in classification stats.
    """
    df = df[~exclusion_mask].reset_index(drop=True)
    df = df[df["expected_class"] == "analyze_excel_model"].reset_index(drop=True)
    if df.empty:
        print("\n📊 No analyze_excel_model records found.")
        return (0, None)

    df_total = len(df)
    has_steps = "actual_steps" in df.columns and "expected_steps" in df.columns

    # Classification mask (only correct classification rows will be used for metrics)
    df["classification_pass"] = df["expected_class"] == df["actual_class"]
    correct_classification = df["classification_pass"].sum()

    # Convert list-string columns
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

    # All pass (query passes if classification is correct AND ALL metrics match)
    if has_steps:
        all_pass = (
            df["classification_pass"]
            & df["inputs_match"]
            & df["outputs_match"]
            & df["ranges_match"]
            & df["steps_match"]
        ).sum()
    else:
        all_pass = (df["classification_pass"] & df["inputs_match"] & df["outputs_match"] & df["ranges_match"]).sum()

    # Parameter-level totals — только по корректно классифицированным
    correct_mask = df["classification_pass"]
    total_input = int(df.loc[correct_mask, "input_param_count"].sum())
    matched_input = int(df.loc[correct_mask & df["inputs_match"], "input_param_count"].sum())
    total_output = int(df.loc[correct_mask, "output_param_count"].sum())
    matched_output = int(df.loc[correct_mask & df["outputs_match"], "output_param_count"].sum())
    total_range = int(df.loc[correct_mask, "range_param_count"].sum())
    matched_range = int(df.loc[correct_mask & df["ranges_match"], "range_param_count"].sum())
    total_step = int(df.loc[correct_mask, "step_param_count"].sum())
    matched_step = int(df.loc[correct_mask & df["steps_match"], "step_param_count"].sum())
    total_year = int(df.loc[correct_mask, "year_param_count"].sum())
    matched_year_val = int(df.loc[correct_mask & df["year_pass"], "year_param_count"].sum())

    # Compute totals
    if has_steps:
        total_all = total_input + total_output + total_range + total_step + total_year
        matched_all = matched_input + matched_output + matched_range + matched_step + matched_year_val
    else:
        total_all = total_input + total_output + total_range + total_year
        matched_all = matched_input + matched_output + matched_range + matched_year_val

    # Parameter-level matches (for mismatch collection — only correct classification)
    matched_inputs = int(df.loc[correct_mask & df["inputs_match"], "input_param_count"].sum())
    matched_outputs = int(df.loc[correct_mask & df["outputs_match"], "output_param_count"].sum())
    matched_ranges = int(df.loc[correct_mask & df["ranges_match"], "range_param_count"].sum())
    matched_steps = int(df.loc[correct_mask & df["steps_match"], "step_param_count"].sum())

    # Print stats
    print(f"\n📊 analyze_excel_model ({df_total} queries)\n")
    print(f"  ✅ Perfect call rate:    {int(all_pass)}/{df_total} ({round(all_pass / df_total * 100, 2)}%)")
    print(
        f"  ✅ Classification rate:   {int(correct_classification)}/{df_total} ({round(correct_classification / df_total * 100, 2)}%)"
    )
    # print(f"  ✅ Inputs matched:       {int(matched_inputs)}/{total_input} ({round(matched_inputs / total_input * 100, 2) if total_input > 0 else 0}%)")
    # print(f"  ✅ Outputs matched:      {int(matched_outputs)}/{total_output} ({round(matched_outputs / total_output * 100, 2) if total_output > 0 else 0}%)")
    # print(f"  ✅ Ranges matched:       {int(matched_ranges)}/{total_range} ({round(matched_ranges / total_range * 100, 2) if total_range > 0 else 0}%)")
    # if has_steps:
    #     step_pct = round(matched_steps / total_step * 100, 2) if total_step > 0 else 0.0
    #     print(f"  ✅ Steps matched:        {int(matched_steps)}/{total_step} ({step_pct}%)")
    # print(f"  ✅ Year:                 {matched_year_val}/{total_year} ({round(matched_year_val / total_year * 100, 2)}%)")
    if total_all > 0:
        print(f"  ✅ Total params:         {matched_all}/{total_all} ({round(matched_all / total_all * 100, 2)}%)")
        print(
            f"      Input params:       {matched_input}/{total_input} ({round(matched_input / total_input * 100, 2)}%)"
        )
        print(
            f"      Output params:      {matched_output}/{total_output} ({round(matched_output / total_output * 100, 2)}%)"
        )
        print(
            f"      Range params:       {matched_range}/{total_range} ({round(matched_range / total_range * 100, 2)}%)"
        )
        if has_steps:
            print(
                f"      Step params:        {matched_step}/{total_step} ({round(matched_step / total_step * 100, 2) if total_step > 0 else 0}%)"
            )
        print(
            f"      Year:               {matched_year_val}/{total_year} ({round(matched_year_val / total_year * 100, 2)}%)"
        )

    # --- Collect metric mismatches (only from correctly classified rows) ---
    metric_mm: dict[str, list[str]] | None = None
    if show_mismatches:
        # Filter to correctly classified only for mismatch collection
        correct_df = df[df["classification_pass"]].copy()
        metric_mm = {}
        _collect_mismatches(correct_df, "inputs_match", "Inputs", metric_mm)
        _collect_mismatches(correct_df, "outputs_match", "Outputs", metric_mm)
        _collect_mismatches(correct_df, "ranges_match", "Ranges", metric_mm)
        if has_steps:
            _collect_mismatches(correct_df, "steps_match", "Steps", metric_mm)
        _collect_mismatches(correct_df, "year_pass", "Year", metric_mm)

    return (df_total, metric_mm)


# ── Statistics: analyze_model_inputs_for_target ──────────────────────────


def _report_ift(
    df: pd.DataFrame,
    exclusion_mask: pd.Series,
    class_only_mask: pd.Series,
    show_mismatches: bool,
) -> tuple[int, dict[str, list[str]] | None]:
    """Compute IFT stats; return (df_total, metric_mismatches).

    Only correctly classified rows participate in metric calculations.
    Incorrectly classified (but not excluded) rows are only counted in classification stats.
    """
    df = df[~exclusion_mask].reset_index(drop=True)
    df = df[df["expected_class"] == "analyze_model_inputs_for_target"].reset_index(drop=True)
    if df.empty:
        print("\n📊 No analyze_model_inputs_for_target records found.")
        return (0, None)

    df_total = len(df)

    # Classification mask (only correct classification rows will be used for metrics)
    df["classification_pass"] = df["expected_class"] == df["actual_class"]
    correct_classification = df["classification_pass"].sum()

    # Convert inputs
    df["expected_inputs"] = df["expected_inputs"].apply(_safe_literal_eval)
    df["actual_inputs"] = df["actual_inputs"].apply(_safe_literal_eval)

    # Pass booleans
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

    # All pass: classification correct AND all 4 metrics
    df["passed"] = (
        df["classification_pass"] & df["inputs_match"] & df["output_pass"] & df["target_pass"] & df["year_pass"]
    )

    passed = df["passed"].sum()
    passed_share = round(passed / df_total * 100, 2)

    # Parameter-level — только по корректно классифицированным
    correct_mask = df["classification_pass"]
    total_input = int(df.loc[correct_mask, "expected_inputs"].apply(len).sum())
    matched_input = int(df.loc[correct_mask & df["inputs_match"], "expected_inputs"].apply(len).sum())
    total_output = int(df.loc[correct_mask, "expected_output"].count())
    matched_output = int(df.loc[correct_mask & df["output_pass"], "expected_output"].count())
    total_target = int(df.loc[correct_mask, "expected_target"].count())
    matched_target = int(df.loc[correct_mask & df["target_pass"], "expected_target"].count())
    total_year = int(df.loc[correct_mask, "expected_year"].count())
    matched_year = int(df.loc[correct_mask & df["year_pass"], "expected_year"].count())

    total_all = total_input + total_output + total_target + total_year
    matched_all = matched_input + matched_output + matched_target + matched_year
    share_all = round(matched_all / total_all * 100, 2) if total_all > 0 else 0.0

    # Print stats
    print(f"\n📊 analyze_model_inputs_for_target ({df_total} queries)\n")
    print(f"  ✅ Perfect call rate:   {passed}/{df_total} ({passed_share}%)")
    print(
        f"  ✅ Classification rate:  {int(correct_classification)}/{df_total} ({round(correct_classification / df_total * 100, 2)}%)"
    )
    if total_all > 0:
        print(f"  ✅ Total params:         {matched_all}/{total_all} ({share_all}%)")
        print(
            f"      Input params:       {matched_input}/{total_input} ({round(matched_input / total_input * 100, 2) if total_input > 0 else 0}%)"
        )
        print(
            f"      Output params:      {matched_output}/{total_output} ({round(matched_output / total_output * 100, 2) if total_output > 0 else 0}%)"
        )
        print(
            f"      Target:             {matched_target}/{total_target} ({round(matched_target / total_target * 100, 2) if total_target > 0 else 0}%)"
        )
        print(f"      Year:               {matched_year}/{total_year} ({round(matched_year / total_year * 100, 2)}%)")

    # --- Collect metric mismatches (only from correctly classified rows) ---
    metric_mm: dict[str, list[str]] | None = None
    if show_mismatches:
        correct_df = df[df["classification_pass"]].copy()
        metric_mm = {}
        _collect_mismatches(correct_df, "inputs_match", "Inputs", metric_mm)
        _collect_mismatches(correct_df, "output_pass", "Output", metric_mm)
        _collect_mismatches(correct_df, "target_pass", "Target", metric_mm)
        _collect_mismatches(correct_df, "year_pass", "Year", metric_mm)

    return (df_total, metric_mm)


# ── Main ─────────────────────────────────────────────────────────────────


# ── Extract hash from filename ────────────────────────────────────────────


def _extract_hash_from_filename(filepath: Path) -> str:
    """Extract hash (model identifier) from filename.

    E.g. ema_2poj431skazt_2026-09-08_12-47-13_results.csv → 2poj431skazt
         ift_2poj431skazt_2026-09-11_17-25-00_results.csv → 2poj431skazt
    """
    name = filepath.stem  # without .csv
    # Remove leading prefix (ema_, ift_)
    parts = name.split("_", 1)
    if len(parts) < 2:
        return name
    prefix_removed = parts[1]  # e.g. 2poj431skazt_2026-09-08_12-47-13_results
    # Hash is the part before the first date-like pattern (YYYY-MM-DD or YYYY-MM-DD_hh-mm-ss)
    date_parts = prefix_removed.split("_")
    for i, part in enumerate(date_parts):
        if part.startswith("20") or part.startswith("19"):
            if part[:4].isdigit() and len(part) >= 4:
                return "_".join(date_parts[:i])
    # Fallback: return everything up to "unified"
    if "unified" in prefix_removed:
        return prefix_removed.split("_unified")[0]
    return prefix_removed


# ── Problem extraction (one row per problem type) ─────────────────────────

# Проблема: classification_mismatch / input_mismatch / output_mismatch / range_mismatch /
#           step_mismatch / year_mismatch / output_value_mismatch / target_mismatch

_PROBLEM_TYPES_EMA = [
    ("classification_mismatch", "expected_class", "actual_class"),
    ("input_mismatch", "expected_inputs", "actual_inputs", _list_match),
    ("output_mismatch", "expected_outputs", "actual_outputs", _list_match),
    ("range_mismatch", "expected_ranges", "actual_ranges", _ranges_match),
    ("step_mismatch", "expected_steps", "actual_steps", _steps_match),
    ("year_mismatch", "expected_year", "actual_year"),
]

_PROBLEM_TYPES_IFT = [
    ("classification_mismatch", "expected_class", "actual_class"),
    ("input_mismatch", "expected_inputs", "actual_inputs", _list_match),
    ("output_value_mismatch", "expected_output", "actual_output"),
    ("target_mismatch", "expected_target", "actual_target"),
    ("year_mismatch", "expected_year", "actual_year"),
]


def _check_match(func_or_eq: Any, actual: Any, expected: Any) -> bool:
    """Check if actual matches expected using a comparison function or equality."""
    if callable(func_or_eq):
        return func_or_eq(actual, expected)
    # Float/int comparison: 15.0 == 15 → True
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9)
    return actual == expected


def _extract_row_problems(
    row: pd.Series,
    scenario_type: str,
    source_hash: str,
    problem_types: list,
) -> list[dict]:
    """Extract problems for a single row, one row per problem type.

    Returns a list of dicts: {hash, problem_type, source_file, prompt, expected_class, actual_class, ...}
    Only returns rows that have at least one mismatch.
    """
    problems = []

    for problem_info in problem_types:
        problem_type = problem_info[0]

        # Skip classification_mismatch for now — it's handled separately
        # (classification_mismatch rows are not processed for metrics)
        if problem_type == "classification_mismatch":
            continue

        # Get column names and optional match function
        # Format: (problem_type, expected_col, actual_col[, match_func])
        if len(problem_info) >= 4:
            expected_col = problem_info[1]
            actual_col = problem_info[2]
            match_func = problem_info[3]
        else:
            expected_col = problem_info[1]
            actual_col = problem_info[2]
            match_func = None

        actual_val = row.get(actual_col)
        expected_val = row.get(expected_col)

        # Convert list-string columns and handle NaN/None (NaN/None → [])
        actual_val = _safe_literal_eval(actual_val)
        expected_val = _safe_literal_eval(expected_val)

        # Ensure list consistency: if one is list and other is not → empty list
        if isinstance(actual_val, list) and not isinstance(expected_val, list):
            expected_val = []
        if isinstance(expected_val, list) and not isinstance(actual_val, list):
            actual_val = []

        matches = _check_match(match_func, actual_val, expected_val)

        if not matches:
            problems.append(
                {
                    "hash": source_hash,
                    "problem_type": problem_type,
                }
            )

    return problems


def _collect_all_problems(df: pd.DataFrame) -> pd.DataFrame:
    """Collect all problems from the DataFrame, one row per problem type.

    Returns a DataFrame with columns: hash, problem_type, source_file, and full row data.
    Processing errors (actual_class == "unknown") and intentional skips are excluded.
    Classification mismatches are included as a separate problem type.

    Problem types:
        - classification_mismatch: wrong scenario classification
        - input_mismatch: input parameters don't match (EMA + IFT)
        - output_mismatch: output parameters don't match (EMA only)
        - range_mismatch: ranges don't match (EMA only)
        - step_mismatch: steps don't match (EMA only)
        - year_mismatch: year doesn't match (EMA + IFT)
        - output_value_mismatch: output value doesn't match (IFT only)
        - target_mismatch: target doesn't match (IFT only)
    """
    # Build exclusion mask (exclude unknown + intentional skip)
    error_mask, skip_mask = _build_exclusion_mask(df)
    exclusion_mask = error_mask | skip_mask
    valid_df = df[~exclusion_mask].reset_index(drop=True)

    all_problems = []
    all_full_rows = []

    for _, row in valid_df.iterrows():
        scenario = row.get("expected_class", "")
        source_hash = row.get("_source_hash", "")
        source_file = row.get("_source_file", "")

        if scenario == "analyze_excel_model":
            problem_types = _PROBLEM_TYPES_EMA
        elif scenario == "analyze_model_inputs_for_target":
            problem_types = _PROBLEM_TYPES_IFT
        else:
            continue

        # Check classification
        class_matches = row.get("expected_class") == row.get("actual_class")
        if not class_matches:
            row_data = row.to_dict()
            row_data["hash"] = source_hash
            row_data["problem_type"] = "classification_mismatch"
            all_full_rows.append(row_data)
            # Skip metric checks for misclassified rows
            continue

        # Check metric problems
        problems = _extract_row_problems(row, scenario, source_hash, problem_types)
        for p in problems:
            row_data = row.to_dict()
            row_data["hash"] = source_hash
            row_data["problem_type"] = p["problem_type"]
            all_full_rows.append(row_data)

    if not all_full_rows:
        return pd.DataFrame(columns=["hash", "problem_type", "source_file", "id", "expected_class", "actual_class"])

    return pd.DataFrame(all_full_rows)


def _export_problems_to_excel(problems_df: pd.DataFrame, output_path: Path) -> None:
    """Export problems DataFrame to a single Excel file with two sheets: EMA and IFT.

    EMA sheet (id starts with A): analyze_excel_model results
    IFT sheet (id starts with T): analyze_model_inputs_for_target results
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    # Clean id is in _clean_id column (original without file suffix)
    problems_df = problems_df.copy()
    problems_df["_clean_id"] = problems_df.get("_clean_id", problems_df.get("id", ""))

    # Split by scenario: A* = EMA, T* = IFT
    ema_mask = problems_df["_clean_id"].astype(str).str.startswith("A", na=False)
    ift_mask = problems_df["_clean_id"].astype(str).str.startswith("T", na=False)

    ema_df = problems_df[ema_mask].copy()
    ift_df = problems_df[ift_mask].copy()

    # Common output columns
    output_cols = [
        "hash",
        "problem_type",
        "source_file",
        "id",
        "expected_class",
        "actual_class",
        "prompt",
        "expected_inputs",
        "actual_inputs",
        "expected_outputs",
        "actual_outputs",
        "expected_year",
        "actual_year",
        "expected_ranges",
        "actual_ranges",
        "expected_steps",
        "actual_steps",
        "expected_output",
        "actual_output",
        "expected_target",
        "actual_target",
        "final_state",
        "duration_sec",
        "comment",
    ]

    def _build_rows(df: pd.DataFrame) -> list[dict]:
        rows = []
        for _, row in df.iterrows():
            output_row = {
                "hash": row.get("hash", ""),
                "problem_type": row.get("problem_type", ""),
                "source_file": row.get("_source_file", row.get("source_file", "")),
                "id": row.get("_clean_id", ""),
                "expected_class": row.get("expected_class", ""),
                "actual_class": row.get("actual_class", ""),
            }
            for col in output_cols:
                if col not in output_row and col in row.index:
                    output_row[col] = row[col]
            rows.append(output_row)
        return rows

    # Excel styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_alignment = Alignment(wrap_text=True, vertical="top")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    def _write_sheet(wb: Workbook, sheet_name: str, label: str, data_rows: list[dict]) -> None:
        ws = wb.create_sheet(title=sheet_name)

        if data_rows:
            headers = list(data_rows[0].keys())
            # Write headers
            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_idx, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
                cell.border = thin_border

            # Write data
            for row_idx, row_data in enumerate(data_rows, 2):
                for col_idx, header in enumerate(headers, 1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=row_data.get(header, ""))
                    cell.alignment = data_alignment
                    cell.border = thin_border

            # Auto-adjust column widths
            for col_idx, header in enumerate(headers, 1):
                max_length = len(header)
                for row_idx in range(2, len(data_rows) + 2):
                    cell_value = ws.cell(row=row_idx, column=col_idx).value
                    if cell_value:
                        max_length = max(max_length, min(len(str(cell_value)), 100))
                ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max(max_length + 2, 15)
        else:
            ws.cell(row=1, column=1, value=f"No {label} problems found")
            ws.cell(row=1, column=1).font = Font(italic=True)

        # Freeze header row
        ws.freeze_panes = "A2"

        print(f"\n💾 {label} sheet: {len(data_rows)} rows written")

        if data_rows:
            print(f"\n📊 {label} problem type distribution:")
            type_counts = pd.Series([r["problem_type"] for r in data_rows]).value_counts()
            for problem_type, count in type_counts.items():
                print(f"   {problem_type}: {count}")

            print(f"\n📊 {label} hash distribution:")
            hash_counts = pd.Series([r["hash"] for r in data_rows]).value_counts()
            for model_hash, count in hash_counts.items():
                print(f"   {model_hash}: {count} problems")

    # Build workbook
    wb = Workbook()
    # Remove default sheet
    del wb["Sheet"]

    # Write EMA sheet
    ema_rows = _build_rows(ema_df) if not ema_df.empty else []
    if "EMA" in wb.sheetnames:
        del wb["EMA"]
    _write_sheet(wb, "EMA", "EMA", ema_rows)

    # Write IFT sheet
    ift_rows = _build_rows(ift_df) if not ift_df.empty else []
    if "IFT" in wb.sheetnames:
        del wb["IFT"]
    _write_sheet(wb, "IFT", "IFT", ift_rows)

    # Save
    wb.save(output_path)

    total = len(ema_rows) + len(ift_rows)
    print(f"\n{'=' * 60}")
    print(f"📊 Total: {total} problems (EMA: {len(ema_rows)}, IFT: {len(ift_rows)})")
    print(f"💾 Saved to: {output_path}")


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate experiment run statistics from all results CSV files.",
    )
    parser.add_argument(
        "folder",
        help="Subfolder name under experiments/ (e.g. full_run_real_models)",
    )
    parser.add_argument(
        "--no-mismatches",
        action="store_true",
        help="Only print statistics, omit mismatch ID lists.",
    )
    parser.add_argument(
        "--export-problems",
        action="store_true",
        help="Export failed test cases to Excel (.xlsx) with two sheets: EMA and IFT.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output Excel path for problems (default: experiments/<folder>/issues.xlsx).",
    )
    args = parser.parse_args()

    folder_name = args.folder

    # Find and load all CSV files
    csv_files = _find_csv_files(folder_name)
    df = _load_all_csv(csv_files)

    print(f"\n{'=' * 60}")
    print(f"📁 Experiment run: experiments/{folder_name}/")
    print(f"📄 Files: {len(csv_files)} ({', '.join(f.name for f in csv_files)})")
    print(f"{'=' * 60}")
    # Overall stats (returns masks, doesn't print)
    _, exclusion_mask, class_only_mask, errors_count, skipped_count, correct_class, incorrect_class = _overall_stats(
        df,
    )
    total_queries = correct_class + incorrect_class + errors_count + skipped_count

    # Per-scenario stats (collect mismatch data)
    _, ema_mm = _report_ema(df, exclusion_mask, class_only_mask, show_mismatches=not args.no_mismatches)
    print("---")
    _, ift_mm = _report_ift(df, exclusion_mask, class_only_mask, show_mismatches=not args.no_mismatches)

    print("---")
    print("=== General ===\n")
    print(f"\tTotal queries:            {total_queries}")
    if errors_count > 0:
        print(f"\tОшибка обработки:         {errors_count} (actual_class == 'unknown')")
    if skipped_count > 0:
        print(f"\tНамеренный пропуск:        {skipped_count}")
    class_total = correct_class + incorrect_class
    print(
        f"\tCorrect classification:   {correct_class}/{class_total} ({round(correct_class / class_total * 100, 2)}%)"
    )
    if incorrect_class > 0:
        print(
            f"\tIncorrect classification: {incorrect_class}/{class_total} ({round(incorrect_class / class_total * 100, 2)}%)\n"
        )

    # Mismatch details (deferred)
    if not args.no_mismatches:
        print("---")
        print("=== Mismatch Details ===\n")
        # Metric mismatches
        for label_prefix, mm_block in [
            ("EMA", ema_mm),
            ("IFT", ift_mm),
        ]:
            if mm_block:
                for name, ids in mm_block.items():
                    print(f"{label_prefix} {name} mismatch: {ids}")
        print()

    # Export problems to Excel
    if args.export_problems:
        problems_df = _collect_all_problems(df)
        if args.output:
            output_path = Path(args.output)
            # Ensure .xlsx extension
            if not output_path.suffix.lower() in (".xlsx", ".xls"):
                output_path = output_path.with_suffix(".xlsx")
        else:
            output_path = EXPERIMENTS_DIR / folder_name / "issues.xlsx"
        _export_problems_to_excel(problems_df, output_path)


if __name__ == "__main__":
    sys.exit(main())
