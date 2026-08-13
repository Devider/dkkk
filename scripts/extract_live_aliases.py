#!/usr/bin/env python3
"""Extract a live testset (aliases as GigaChat actually parsed them) from a
run_tool_queries.py checkpoint JSON.

The checkpoint stores, per query, the real tool args (`actual`) produced by
the live GigaChat agent and the curated expectations (`expected`). This script
converts queries that actually produced a tool call into the same query format
that bench_resolution.py uses for the .xlsx testset:
``{id, tool, expected, input_aliases, output_aliases | output_alias}``.

Usage:
    python scripts/extract_live_aliases.py [checkpoint.json] [-o live_aliases.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

TOOLS = ("analyze_excel_model", "analyze_model_inputs_for_target")
_YEAR_RE = re.compile(r"\b20\d{2}\b")


def _parse_list(value) -> list[str]:
    if not value:
        return []
    out = []
    for x in value if isinstance(value, list) else [value]:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def extract(checkpoint: dict) -> tuple[list[dict], dict]:
    queries: list[dict] = []
    stats = {
        "total_in_checkpoint": 0,
        "no_tool_call_skipped": 0,
        "error_skipped": 0,
        "converted": 0,
        "unique_aliases": 0,
        "unique_with_year": 0,
        "by_tool": {},
    }
    seen_aliases: set[str] = set()

    for res in checkpoint.get("results", []):
        stats["total_in_checkpoint"] += 1
        status = res.get("status")
        if status == "ERROR" or not res.get("actual"):
            stats["error_skipped" if status == "ERROR" else "no_tool_call_skipped"] += 1
            continue

        actual = res.get("actual") or {}
        expected = res.get("expected") or {}
        tool = actual.get("tool") or res.get("actual_tool") or res.get("tool")
        if tool not in TOOLS:
            tool = res.get("tool")
        if tool not in TOOLS:
            stats["no_tool_call_skipped"] += 1
            continue

        q = {"id": str(res.get("id", "")), "tool": tool, "expected": expected}
        q["input_aliases"] = _parse_list(actual.get("input_names"))
        for a in q["input_aliases"]:
            seen_aliases.add(a.lower())
        if tool == "analyze_excel_model":
            q["output_aliases"] = _parse_list(actual.get("output_names"))
            for a in q["output_aliases"]:
                seen_aliases.add(a.lower())
        else:
            q["output_alias"] = _parse_list([actual.get("output_name")])[0] if actual.get("output_name") else None
            if q["output_alias"]:
                seen_aliases.add(q["output_alias"].lower())

        queries.append(q)
        stats["converted"] += 1
        stats["by_tool"][tool] = stats["by_tool"].get(tool, 0) + 1

    stats["unique_aliases"] = len(seen_aliases)
    stats["unique_with_year"] = sum(1 for a in seen_aliases if _YEAR_RE.search(a))
    return queries, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract live testset from a run_tool_queries checkpoint")
    parser.add_argument(
        "checkpoint",
        nargs="?",
        default="test_output/live_20260715/tool_query_results.json",
        help="run_tool_queries.py checkpoint JSON",
    )
    parser.add_argument("-o", "--out", default="test_output/live_aliases.json")
    args = parser.parse_args()

    src = Path(args.checkpoint)
    if not src.exists():
        print(f"ERROR: checkpoint not found: {src}", file=sys.stderr)
        return 1
    with open(src) as f:
        checkpoint = json.load(f)

    queries, stats = extract(checkpoint)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": str(src),
        "generated_at": datetime.now(UTC).isoformat(),
        "stats": stats,
        "queries": queries,
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print(f"checkpoint      : {src}")
    print(f"queries in ckpt : {stats['total_in_checkpoint']}")
    print(f"converted       : {stats['converted']} (no_tool_call={stats['no_tool_call_skipped']},"
          f" error={stats['error_skipped']})")
    print(f"by_tool         : {stats['by_tool']}")
    print(f"unique aliases  : {stats['unique_aliases']} (with year: {stats['unique_with_year']})")
    print(f"wrote           : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())