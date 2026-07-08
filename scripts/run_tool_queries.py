#!/usr/bin/env python3
"""Test tool query correctness against a running server.

Reads prompts + expected_call from Excel, sends to the agent,
captures tool args from server.log (via x-trace-id), resolves names
through the same Jaccard pipeline used in production, and compares.

Usage:
    python scripts/run_tool_queries.py --url http://localhost:8080 --log server.log
    python scripts/run_tool_queries.py --subset 10               # first 10 per sheet
    python scripts/run_tool_queries.py --resume results.json     # continue from checkpoint
"""

import argparse
import ast
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import openpyxl

# ---------------------------------------------------------------------------
# Imports from the production codebase (resolution pipeline)
# ---------------------------------------------------------------------------
from aigw_service.api.v1.tools import (
    create_input_mapping,
    create_output_mapping,
    find_matching_cell,
    find_matching_outputs,
)

# ---------------------------------------------------------------------------
# System-injected / always-present fields to skip when comparing
# ---------------------------------------------------------------------------
SYSTEM_FIELDS = {"thread_id", "user_id", "file_name"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
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
        help="Run only first N queries per sheet (0 = all)",
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
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Read test queries from the Excel workbook
# ---------------------------------------------------------------------------
def read_queries(path: str) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    queries: list[dict] = []

    for sheet_name in ("analyze_excel_model", "analyze_model_inputs_for_target"):
        ws = wb[sheet_name]
        headers = [c.value for c in ws[1]]
        col_map: dict[str, int] = {h: i for i, h in enumerate(headers)}

        tool = sheet_name

        for row_idx in range(2, ws.max_row + 1):
            row = [c.value for c in ws[row_idx]]
            if not row or not row[col_map.get("ID", 0)]:
                continue

            prompt = str(row[col_map["Запрос (prompt)"]])
            expected_call = json.loads(row[col_map["expected_call (JSON)"]])

            queries.append(
                {
                    "id": str(row[col_map["ID"]]),
                    "prompt": prompt,
                    "expected": expected_call,
                    "tool": tool,
                }
            )

    return queries


# ---------------------------------------------------------------------------
# Build input / output mapping from the model file (replicates what the
# production tools do internally).
# ---------------------------------------------------------------------------
def build_mappings(model_path: str) -> tuple[dict, dict]:
    wb = openpyxl.load_workbook(model_path, data_only=True)

    # Inputs sheet → 2D list (same shape as ExcelWorkbook.get_all_data)
    ws_in = wb["Inputs"]
    inputs_data: list[list] = [
        [c.value for c in row]
        for row in ws_in.iter_rows(min_row=1, max_row=ws_in.max_row, max_col=ws_in.max_column)
    ]

    # Outputs sheet
    ws_out = wb["Outputs"]
    outputs_data: list[list] = [
        [c.value for c in row]
        for row in ws_out.iter_rows(min_row=1, max_row=ws_out.max_row, max_col=ws_out.max_column)
    ]

    wb.close()

    input_mapping = create_input_mapping(inputs_data)
    output_mapping = create_output_mapping(outputs_data)
    return input_mapping, output_mapping


# ---------------------------------------------------------------------------
# Extract tool args dict from a log line (by rqUId)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Numeric comparison with tolerance
# ---------------------------------------------------------------------------
def _approx_equal(a, b, tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < tol
    return a == b


# ---------------------------------------------------------------------------
# Normalize actual tool args to match expected_call structure
# ---------------------------------------------------------------------------
def _normalize(actual: dict) -> dict:
    norm = dict(actual)

    # output_years [y] → year
    if "output_years" in norm and "year" not in norm:
        oy = norm.pop("output_years")
        if isinstance(oy, list) and len(oy) == 1:
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
# Compare expected vs actual using the production name-resolution pipeline
# ---------------------------------------------------------------------------
def compare(
    expected: dict,
    actual: dict,
    tool: str,
    *,
    input_mapping: dict,
    output_mapping: dict,
) -> list[str]:
    diffs: list[str] = []

    # Normalise then strip system fields
    actual = _normalize(actual)
    for sf in SYSTEM_FIELDS:
        actual.pop(sf, None)

    default_year = expected.get("year")

    # ---- Scalars: year / target_value ----
    for key in ("year", "target_value"):
        exp_val = expected.get(key)
        act_val = actual.get(key)
        if not _approx_equal(exp_val, act_val):
            diffs.append(f"{key}: expected {exp_val!r}, got {act_val!r}")

    # ---- Ranges / steps (direct comparison, no resolution needed) ----
    for key in ("ranges", "steps"):
        exp_val = expected.get(key)
        act_val = actual.get(key)
        if isinstance(exp_val, list) and isinstance(act_val, list):
            if len(exp_val) != len(act_val):
                diffs.append(f"{key}: length mismatch expected {len(exp_val)}, got {len(act_val)}")
                continue
            for i, (e, a_) in enumerate(zip(exp_val, act_val, strict=True)):
                if isinstance(e, list) and isinstance(a_, list):
                    if len(e) != len(a_):
                        diffs.append(f"{key}[{i}]: length mismatch")
                    else:
                        for j, (ev, av) in enumerate(zip(e, a_, strict=True)):
                            if not _approx_equal(ev, av):
                                diffs.append(f"{key}[{i}][{j}]: expected {ev!r}, got {av!r}")
                elif not _approx_equal(e, a_):
                    diffs.append(f"{key}[{i}]: expected {e!r}, got {a_!r}")

    # ---- input_names (resolve via production Jaccard pipeline) ----
    exp_in: list[str] = expected.get("input_names", [])
    act_in: list[str] = actual.get("input_names", [])
    if len(exp_in) != len(act_in):
        diffs.append(
            f"input_names: length mismatch expected {len(exp_in)}, got {len(act_in)}"
        )
    else:
        for i, (exp_name, act_alias) in enumerate(zip(exp_in, act_in, strict=True)):
            try:
                _cell_ref, resolved = find_matching_cell(
                    act_alias, input_mapping, default_year=default_year
                )
            except Exception as e:
                diffs.append(f"input_names[{i}]: resolution raised {e}")
                continue

            if not resolved:
                diffs.append(
                    f"input_names[{i}]: resolution FAILED for {act_alias!r} "
                    f"(similarity < 0.1 threshold)"
                )
            elif resolved != exp_name:
                diffs.append(
                    f"input_names[{i}]: resolved to {resolved!r}, "
                    f"expected {exp_name!r}"
                )

    # ---- output_names (resolve via production Jaccard pipeline) ----
    exp_out: list[str] = expected.get("output_names", [])
    act_out: list[str] = actual.get("output_names", [])
    if len(exp_out) != len(act_out):
        diffs.append(
            f"output_names: length mismatch expected {len(exp_out)}, got {len(act_out)}"
        )
    else:
        for i, (exp_name, act_alias) in enumerate(zip(exp_out, act_out, strict=True)):
            try:
                result = find_matching_outputs(act_alias, output_mapping)
            except Exception as e:
                diffs.append(f"output_names[{i}]: resolution raised {e}")
                continue

            if not result:
                diffs.append(
                    f"output_names[{i}]: resolution FAILED for {act_alias!r} "
                    f"(no match in output mapping)"
                )
            else:
                resolved_name = next(iter(result.keys()))
                if resolved_name != exp_name:
                    diffs.append(
                        f"output_names[{i}]: resolved to {resolved_name!r}, "
                        f"expected {exp_name!r}"
                    )

    return diffs


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

    # ---- Build name-resolution mappings (done once) ----
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
    queries = read_queries(str(queries_file))
    total_queries = len(queries)
    print(
        f"  Total: {total_queries} queries"
        f" ({sum(1 for q in queries if q['tool']=='analyze_excel_model')} analyze,"
        f" {sum(1 for q in queries if q['tool']=='analyze_model_inputs_for_target')} target)"
    )

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

    if args.subset > 0:
        queries = queries[: args.subset]
        print(f"  Subset: first {len(queries)} queries")

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

    # ---- Run tests ----
    results: list[dict] = []
    passed = 0
    failed = 0
    errors = 0

    log_file_path = str(log_file)

    for i, q in enumerate(queries):
        trace_id = str(uuid.uuid4())
        label = f"[{i+1}/{len(queries)}] {q['id']}"
        prompt_preview = q["prompt"][:80].replace("\n", " ")
        print(f"{label} {prompt_preview}... ", end="", flush=True)

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
                diffs = compare(
                    q["expected"],
                    actual,
                    q["tool"],
                    input_mapping=input_mapping,
                    output_mapping=output_mapping,
                )
                if diffs:
                    result_entry["status"] = "FAIL"
                    result_entry["diffs"] = diffs
                    print("FAIL")
                    for d in diffs:
                        print(f"  {d}")
                    failed += 1
                else:
                    result_entry["status"] = "PASS"
                    print("PASS")
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
                    "results": results,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

    # ---- Summary ----
    total = passed + failed + errors
    pct = passed / total * 100 if total else 0
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed}/{total} passed, {failed} failed, {errors} errors ({pct:.1f}%)")
    print(f"Results saved to {out_file}")

    return 0 if failed == 0 and errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
