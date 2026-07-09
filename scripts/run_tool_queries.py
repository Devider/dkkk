#!/usr/bin/env python3
"""Test tool query correctness against a running server.

Reads prompts + expected_call from Excel, sends to the agent,
captures tool args from server.log (via x-trace-id), resolves names
through the same Jaccard pipeline used in production, and compares.

Usage:
    python scripts/run_tool_queries.py --url http://localhost:8080 --log server.log
    python scripts/run_tool_queries.py --subset 10               # first 10 per sheet
    python scripts/run_tool_queries.py --resume results.json     # continue from checkpoint
    python scripts/run_tool_queries.py --verbose                  # detailed per-field output
    python scripts/run_tool_queries.py --csv report.csv           # write CSV comparison dump
"""

import argparse
import ast
import csv
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import openpyxl

from aigw_service.api.v1.tools import (
    create_input_mapping,
    create_output_mapping,
    find_matching_cell,
    find_matching_outputs,
)

SYSTEM_FIELDS = {"thread_id", "user_id", "file_name"}


def parse_args():
    parser = argparse.ArgumentParser(description="Run tool query tests")
    parser.add_argument("--url", default="http://localhost:8080", help="Server URL")
    parser.add_argument("--log", default="server.log", help="Path to server log file")
    parser.add_argument(
        "--queries",
        default="tests/data/Methanex_tool_test_queries.xlsx",
        help="Excel test data",
    )
    parser.add_argument(
        "--model",
        default="models/model.xlsx",
        help="Path to the financial model .xlsx (for name resolution)",
    )
    parser.add_argument(
        "--subset",
        type=int,
        default=0,
        help="First N queries from each sheet (0 = all)",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Resume from results JSON file",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output results file (auto-generated if not set)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="HTTP request timeout in seconds",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed per-field comparison",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Write comparison details to CSV file",
    )
    parser.add_argument(
        "--upload",
        type=str,
        default=None,
        help="Upload this .xlsx file to the server before running tests",
    )
    return parser.parse_args()


def read_queries(path: str, subset_per_sheet: int = 0) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    queries: list[dict] = []

    for sheet_name in ("analyze_excel_model", "analyze_model_inputs_for_target"):
        ws = wb[sheet_name]
        headers = [c.value for c in ws[1]]
        col_map: dict[str, int] = {h: i for i, h in enumerate(headers)}

        tool = sheet_name
        sheet_queries: list[dict] = []

        for row_idx in range(2, ws.max_row + 1):
            row = [c.value for c in ws[row_idx]]
            if not row or not row[col_map.get("ID", 0)]:
                continue

            prompt = str(row[col_map["Запрос (prompt)"]])
            expected_call = json.loads(row[col_map["expected_call (JSON)"]])

            sheet_queries.append(
                {
                    "id": str(row[col_map["ID"]]),
                    "prompt": prompt,
                    "expected": expected_call,
                    "tool": tool,
                }
            )

        if subset_per_sheet > 0:
            sheet_queries = sheet_queries[:subset_per_sheet]
        queries.extend(sheet_queries)

    return queries


def build_mappings(model_path: str) -> tuple[dict, dict]:
    wb = openpyxl.load_workbook(model_path, data_only=True)

    ws_in = wb["Inputs"]
    inputs_data: list[list] = [
        [c.value for c in row]
        for row in ws_in.iter_rows(min_row=1, max_row=ws_in.max_row, max_col=ws_in.max_column)
    ]

    ws_out = wb["Outputs"]
    outputs_data: list[list] = [
        [c.value for c in row]
        for row in ws_out.iter_rows(min_row=1, max_row=ws_out.max_row, max_col=ws_out.max_column)
    ]

    wb.close()

    input_mapping = create_input_mapping(inputs_data)
    output_mapping = create_output_mapping(outputs_data)
    return input_mapping, output_mapping


def extract_tool_args(log_file: str, trace_id: str) -> dict | None:
    """Search log file for a TOOL ARGS line with matching rqUId."""
    target = f'"rqUId": "{trace_id}"'

    with open(log_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if target not in line or "TOOL ARGS" not in line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", "")
            prefix = "TOOL ARGS: "
            if prefix not in msg:
                continue

            dict_str = msg.split(prefix, 1)[1]
            try:
                return ast.literal_eval(dict_str)
            except (ValueError, SyntaxError):
                continue

    return None


def _approx_equal(a, b, tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < tol
    return a == b


def _normalize(actual: dict) -> dict:
    norm = dict(actual)

    # output_years [y, y, ...] → year (all equal → scalar)
    if "output_years" in norm and "year" not in norm:
        oy = norm.pop("output_years")
        if isinstance(oy, list) and len(oy) > 0 and len(set(oy)) == 1:
            norm["year"] = oy[0]

    # ranges with fused [start, end, step] → [start, end]
    ranges = norm.get("ranges")
    if isinstance(ranges, list):
        cleaned = []
        for r in ranges:
            if isinstance(r, (list, tuple)) and len(r) == 3:
                cleaned.append([r[0], r[1]])
            else:
                cleaned.append(r)
        norm["ranges"] = cleaned

    if "steps" in norm and norm["steps"] is None:
        norm.pop("steps")

    return norm


# ---------------------------------------------------------------------------
# Structured comparison entry
# ---------------------------------------------------------------------------
def _entry(
    field: str,
    status: str,
    *,
    alias=None,
    resolved=None,
    expected=None,
    actual=None,
    similarity=None,
    detail=None,
) -> dict:
    return {
        "field": field,
        "alias": alias,
        "resolved": resolved,
        "expected": expected,
        "actual": actual,
        "similarity": similarity,
        "status": status,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Compare expected vs actual using the production name-resolution pipeline
# Returns list of structured comparison entries
# ---------------------------------------------------------------------------
def compare(
    expected: dict,
    actual: dict,
    tool: str,
    *,
    input_mapping: dict,
    output_mapping: dict,
) -> tuple[list[dict], dict]:
    entries: list[dict] = []

    actual = _normalize(actual)
    for sf in SYSTEM_FIELDS:
        actual.pop(sf, None)

    default_year = expected.get("year")

    # ---- Scalars: year / target_value ----
    for key in ("year", "target_value"):
        exp_val = expected.get(key)
        act_val = actual.get(key)
        if not _approx_equal(exp_val, act_val):
            raw = actual.get("output_years") if key == "year" and "output_years" in actual else None
            detail = None
            if raw is not None:
                detail = f"output_years={raw}"
            entries.append(_entry(key, "MISMATCH", expected=exp_val, actual=act_val, detail=detail))

    # ---- Ranges / steps ----
    for key in ("ranges", "steps"):
        exp_val = expected.get(key)
        act_val = actual.get(key)
        if isinstance(exp_val, list) and isinstance(act_val, list):
            if len(exp_val) != len(act_val):
                entries.append(
                    _entry(key, "LENGTH_MISMATCH", expected=len(exp_val), actual=len(act_val))
                )
                continue
            for i, (e, a_) in enumerate(zip(exp_val, act_val, strict=True)):
                fq_key = f"{key}[{i}]"
                if isinstance(e, list) and isinstance(a_, list):
                    if len(e) != len(a_):
                        entries.append(
                            _entry(fq_key, "LENGTH_MISMATCH", expected=len(e), actual=len(a_))
                        )
                    else:
                        for j, (ev, av) in enumerate(zip(e, a_, strict=True)):
                            if not _approx_equal(ev, av):
                                entries.append(
                                    _entry(f"{fq_key}[{j}]", "MISMATCH", expected=ev, actual=av)
                                )
                elif not _approx_equal(e, a_):
                    entries.append(_entry(fq_key, "MISMATCH", expected=e, actual=a_))

    # ---- input_names ----
    exp_in: list[str] = expected.get("input_names", [])
    act_in: list[str] = actual.get("input_names", [])
    if len(exp_in) != len(act_in):
        entries.append(
            _entry(
                "input_names",
                "LENGTH_MISMATCH",
                expected=len(exp_in),
                actual=len(act_in),
                alias=act_in,
            )
        )
    else:
        for i, (exp_name, act_alias) in enumerate(zip(exp_in, act_in, strict=True)):
            fq_key = f"input_names[{i}]"
            try:
                result = find_matching_cell(
                    act_alias, input_mapping, default_year=default_year, return_best_score=True
                )
                _cell_ref, resolved, best_score = result
            except Exception as e:
                entries.append(
                    _entry(fq_key, "RESOLUTION_ERROR", alias=act_alias, expected=exp_name, detail=str(e))
                )
                continue

            if not resolved:
                entries.append(
                    _entry(fq_key, "NO_MATCH", alias=act_alias, expected=exp_name, similarity=best_score)
                )
            elif resolved != exp_name:
                entries.append(
                    _entry(
                        fq_key,
                        "MISMATCH",
                        alias=act_alias,
                        resolved=resolved,
                        expected=exp_name,
                        similarity=best_score,
                    )
                )

    # ---- output_names ----
    exp_out: list[str] = expected.get("output_names", [])
    act_out: list[str] = actual.get("output_names", [])
    if len(exp_out) != len(act_out):
        entries.append(
            _entry(
                "output_names",
                "LENGTH_MISMATCH",
                expected=len(exp_out),
                actual=len(act_out),
                alias=act_out,
            )
        )
    else:
        for i, (exp_name, act_alias) in enumerate(zip(exp_out, act_out, strict=True)):
            fq_key = f"output_names[{i}]"
            try:
                result = find_matching_outputs(act_alias, output_mapping, return_best_score=True)
                best_score = result.pop("_best_score", 0.0) if isinstance(result, dict) else 0.0
            except Exception as e:
                entries.append(
                    _entry(fq_key, "RESOLUTION_ERROR", alias=act_alias, expected=exp_name, detail=str(e))
                )
                continue

            if not result:
                entries.append(
                    _entry(fq_key, "NO_MATCH", alias=act_alias, expected=exp_name, similarity=best_score)
                )
            else:
                resolved_name = next(iter(result.keys()))
                if resolved_name != exp_name:
                    entries.append(
                        _entry(
                            fq_key,
                            "MISMATCH",
                            alias=act_alias,
                            resolved=resolved_name,
                            expected=exp_name,
                            similarity=best_score,
                        )
                    )

    total_checks = 1 + len(exp_in) + len(exp_out)  # year + inputs + outputs
    param_field_prefixes = ("input", "output", "year")
    failed_checks = sum(
        1 for e in entries if e["field"].startswith(param_field_prefixes)
    )

    return entries, {
        "total": total_checks,
        "failed": failed_checks,
        "passed": total_checks - failed_checks,
    }


# ---------------------------------------------------------------------------
# Pretty-print a single comparison entry in verbose mode
# ---------------------------------------------------------------------------
def _format_entry(e: dict) -> str:
    parts = [f"  {e['field']}:"]
    if e["alias"] is not None:
        parts.append(f"    alias:     {e['alias']!r}")
    if e["resolved"] is not None:
        sim = f"  (sim: {e['similarity']:.3f})" if e["similarity"] is not None else ""
        parts.append(f"    resolved:  {e['resolved']!r}{sim}")
    if e["expected"] is not None:
        parts.append(f"    expected:  {e['expected']!r}")
    if e["actual"] is not None:
        parts.append(f"    actual:    {e['actual']!r}")
    if e["similarity"] is not None and e["resolved"] is None and e["detail"] is None:
        parts.append(f"    similarity: {e['similarity']:.3f}")
    if e["detail"] is not None:
        parts.append(f"    detail:    {e['detail']}")
    parts.append(f"    status:    {e['status']}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Write CSV with all comparison details
# ---------------------------------------------------------------------------
def write_csv(csv_path: str, all_entries: list[dict]) -> None:
    fieldnames = ["id", "field", "alias", "resolved", "expected", "actual", "similarity", "status", "detail"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_entries)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    args = parse_args()
    url = args.url.rstrip("/")
    log_file = Path(args.log)
    queries_file = Path(args.queries)
    model_file = Path(args.model)

    for fpath, label in [
        (log_file, "log"),
        (queries_file, "queries"),
        (model_file, "model"),
    ]:
        if not fpath.exists():
            print(f"ERROR: {label} file not found: {fpath}")
            return 1

    # ---- Build name-resolution mappings ----
    print(f"Building mappings from {model_file}...")
    try:
        input_mapping, output_mapping = build_mappings(str(model_file))
        print(f"  Input mapping: {len(input_mapping.get('row_mapping', {}))} entries")
        print(f"  Output mapping: {len(output_mapping.get('output_mapping', {}))} entries")
    except Exception as e:
        print(f"  ERROR building mappings: {e}")
        return 1

    # ---- Read test data ----
    print(f"Reading queries from {queries_file}...")
    queries = read_queries(str(queries_file), subset_per_sheet=args.subset)
    total_queries = len(queries)
    print(
        f"  Total: {total_queries} queries"
        f" ({sum(1 for q in queries if q['tool']=='analyze_excel_model')} analyze,"
        f" {sum(1 for q in queries if q['tool']=='analyze_model_inputs_for_target')} target)"
    )
    if args.subset > 0:
        print(f"  Subset: first {args.subset} per sheet")

    # ---- Resume support ----
    completed_ids: set[str] = set()
    if args.resume:
        resume_path = Path(args.resume)
        if resume_path.exists():
            with open(resume_path) as f:
                saved = json.load(f)
            completed_ids = {r["id"] for r in saved.get("results", [])}
            print(f"  Resuming: {len(completed_ids)} already completed")
            queries = [q for q in queries if q["id"] not in completed_ids]

    if not queries:
        print("Nothing to run.")
        return 0

    # ---- Check server ----
    health_url = f"{url}/health"
    print(f"Checking server at {health_url}...")
    try:
        resp = httpx.get(health_url, timeout=5)
        resp.raise_for_status()
        print(f"  Server OK ({resp.status_code})")
    except Exception as e:
        print(f"  ERROR: Server not reachable: {e}")
        return 1

    # ---- Upload model file if requested ----
    if args.upload:
        upload_path = Path(args.upload)
        if not upload_path.exists():
            print(f"  ERROR: upload file not found: {upload_path}")
            return 1
        print(f"Uploading {upload_path} to server...")
        trace_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        upload_headers = {
            "x-trace-id": trace_id,
            "x-client-id": "CI12345678",
            "x-request-time": now,
            "x-session-id": trace_id,
            "x-user-id": "test",
        }
        try:
            with open(upload_path, "rb") as f:
                resp = httpx.post(
                    f"{url}/api/v1/upload",
                    files={"file": (upload_path.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                    headers=upload_headers,
                    timeout=30,
                )
            resp.raise_for_status()
            print(f"  Upload OK ({resp.status_code})")
        except Exception as e:
            print(f"  ERROR uploading file: {e}")
            return 1

    # ---- Run tests ----
    results: list[dict] = []
    csv_entries: list[dict] = []
    passed = 0
    failed = 0
    errors = 0

    log_file_path = str(log_file)

    tool_stats: dict[str, dict] = {
        "analyze_excel_model": {"queries": 0, "queries_passed": 0, "params_total": 0, "params_passed": 0},
        "analyze_model_inputs_for_target": {"queries": 0, "queries_passed": 0, "params_total": 0, "params_passed": 0},
    }

    for i, q in enumerate(queries):
        trace_id = str(uuid.uuid4())
        label = f"[{i+1}/{len(queries)}] {q['id']}"
        prompt_preview = q["prompt"][:80].replace("\n", " ")
        ts = tool_stats[q["tool"]]
        ts["queries"] += 1

        now = datetime.now(UTC).isoformat()
        headers = {
            "x-trace-id": trace_id,
            "x-client-id": "CI12345678",
            "x-request-time": now,
            "x-session-id": trace_id,
            "x-user-id": "test",
            "Content-Type": "application/json",
        }

        try:
            resp = httpx.post(
                f"{url}/api/v1/invoke-agent",
                json={"message": q["prompt"]},
                headers=headers,
                timeout=args.timeout,
            )
            resp.raise_for_status()

            actual = extract_tool_args(log_file_path, trace_id)

            result_entry: dict = {
                "id": q["id"],
                "tool": q["tool"],
                "prompt": q["prompt"],
                "expected": q["expected"],
                "actual": actual,
            }

            if actual is None:
                result_entry["status"] = "FAIL"
                result_entry["diffs"] = ["No TOOL ARGS found in log"]
                print("FAIL (no tool call)")
                failed += 1
            else:
                entries, stats = compare(
                    q["expected"],
                    actual,
                    q["tool"],
                    input_mapping=input_mapping,
                    output_mapping=output_mapping,
                )
                ts["params_total"] += stats["total"]
                ts["params_passed"] += stats["passed"]

                # Flatten diffs to strings for backward compat in JSON
                flat_diffs = [f"{e['field']}: {e['status']}" + (f" — {e['detail']}" if e['detail'] else "") for e in entries]

                # Add to CSV
                for e in entries:
                    csv_entries.append({"id": q["id"], **e})

                status_line = f"FAIL ({stats['passed']}/{stats['total']} params)"
                if entries:
                    result_entry["status"] = "FAIL"
                    result_entry["diffs"] = flat_diffs
                    result_entry["comparison"] = entries
                    result_entry["param_stats"] = stats
                    print(status_line)
                    if args.verbose:
                        for e in entries:
                            print()
                            print(_format_entry(e))
                    else:
                        for d in flat_diffs:
                            print(f"  {d}")
                    failed += 1
                else:
                    result_entry["status"] = "PASS"
                    result_entry["param_stats"] = stats
                    print("PASS")
                    ts["queries_passed"] += 1
                    passed += 1

            results.append(result_entry)

        except Exception as e:
            print(f"ERROR: {e}")
            errors += 1
            results.append(
                {
                    "id": q["id"],
                    "tool": q["tool"],
                    "prompt": q["prompt"],
                    "status": "ERROR",
                    "error": str(e),
                }
            )

        # Save checkpoint after each test
        output_path = (
            args.output
            or f"test_output/tool_query_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w") as f:
            json.dump(
                {
                    "summary": {
                        "total": passed + failed + errors,
                        "passed": passed,
                        "failed": failed,
                        "errors": errors,
                    },
                    "tool_stats": tool_stats,
                    "results": results,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

    # ---- Write CSV ----
    if args.csv and csv_entries:
        write_csv(args.csv, csv_entries)
        print(f"CSV comparison written to {args.csv}")

    # ---- Summary ----
    total = passed + failed + errors
    pct = passed / total * 100 if total else 0
    print(f"\n{'=' * 60}")

    for tool_key, label in [
        ("analyze_excel_model", "analyze_excel_model"),
        ("analyze_model_inputs_for_target", "analyze_model_inputs_for_target"),
    ]:
        ts = tool_stats[tool_key]
        if ts["queries"] == 0:
            continue
        q_pct = ts["queries_passed"] / ts["queries"] * 100
        p_pct = ts["params_passed"] / ts["params_total"] * 100 if ts["params_total"] else 0
        print(f"=== {label} ({ts['queries']} queries) ===")
        print(f"  PASS: {ts['queries_passed']}/{ts['queries']} ({q_pct:.1f}%)")
        print(f"  Params: {ts['params_passed']}/{ts['params_total']} correct ({p_pct:.1f}%)")
        print()

    print(f"=== TOTAL ({total} queries) ===")
    print(f"  PASS: {passed}/{total} ({pct:.1f}%)")

    all_params_total = sum(ts["params_total"] for ts in tool_stats.values())
    all_params_passed = sum(ts["params_passed"] for ts in tool_stats.values())
    if all_params_total > 0:
        all_pct = all_params_passed / all_params_total * 100
        print(f"  Params: {all_params_passed}/{all_params_total} correct ({all_pct:.1f}%)")

    print(f"Saved to {out_file}")

    return 0 if failed == 0 and errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
