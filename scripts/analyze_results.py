#!/usr/bin/env python3
"""Analyze tool query results JSON and print structured error analysis.

Pipeline (что откуда берётся):
  1. Загружает results JSON из run_tool_queries.py.
  2. Собирает все comparison entries (плоский список) — каждая запись
     содержит {id, tool, field, status, alias, expected, resolved, similarity}.
  3. Для каждого раздела агрегирует эти записи по-своему.

Секции вывода (сверху вниз) и как их читать:

  Summary — tool_stats из JSON (queries_passed, params_passed).
    Итоговые метрики «сверху». Если queries_passed = 0 — ни один
    запрос не прошёл полностью (все поля), но params_passed может
    быть > 0 (отдельные поля правильные).

  Error Type Distribution — распределение статусов сравнения:
    • MISMATCH — alias зарезолвился, но не в то каноническое имя.
      Причина: jaccard_similarity выбрала ближайшее, но неправильное
      имя (cross-lingual или слишком похожие имена в листе).
    • RESOLUTION_ERROR — сервер не смог найти ячейку по alias'у
      (например, английский термин без совпадений в Excel).
    • NO_MATCH — find_matching_cell/Outputs вернул None (ни одно
      имя не прошло порог). От RESOLUTION_ERROR отличается тем,
      что сервер не упал, а просто не нашёл совпадений.
    • LENGTH_MISMATCH — разная длина списков. Для target-тула
      была структурная проблема output_name vs output_names
      (после фикса исчезнет).
    • MISSING — поле есть в expected, но отсутствует в actual.

  Field-Level Accuracy — сколько раз каждое поле (year, input_names,
    output_names, target_value) прошло проверку.
    total = сколько раз это поле ожидалось во всех expected_call.
    pass = total минус число ошибок по этому полю.
    Позволяет увидеть: «year почти всегда правильный (99%),
    а input_names валятся в 60% случаев» — и понять, на чём
    фокусироваться.

  Confusion Matrix — только MISMATCH по именам (expected → resolved).
    Показывает систематические ошибки Jaccard-резолвинга.
    Например: "Чистый долг (Net Debt)" → "Net Debt/EBITDA" (38 раз).
    Чем больше count, тем более систематическая проблема — нужно
    либо править синонимы, либо дообучать LLM давать другие алиасы.

  NO_MATCH Aliases — алиасы, которые ни разу не зарезолвились.
    Почти всегда английские слова (revenue, cash balance, D&A).
    Симптом: LLM даёт английский алиас, канонические имена русские,
    Jaccard-пересечение = 0.

  Resolution Errors — серверные ошибки при резолве.
    Какие алиасы падают с «No match found for query: X».
    Английские термины, которые не прошли _ensure_cell.

  Similarity Distribution — гистограмма Jaccard similarity для
    всех MISMATCH. Позволяет отделить:
    • sim < 0.2 — cross-lingual (разные алфавиты, почти всегда 0)
    • 0.2–0.4 — низкое совпадение, Jaccard выбрал случайно
    • 0.6–1.0 — почти правильное имя, но resolver выбрал другое
    Если большая часть в < 0.2 — проблема в разнице языков, и
    Jaccard тут не поможет (нужны синонимы или перевод).

  Input Count vs Accuracy — группировка по числу input_names.
    Показывает, деградирует ли accuracy с ростом числа входов.
    Если accuracy падает — LLM не справляется с большим контекстом
    и начинает путать алиасы.

Usage:
    python scripts/analyze_results.py test_output/tool_query_results.json
    python scripts/analyze_results.py results.json --top-n 30
    python scripts/analyze_results.py results.json --csv analysis.csv
"""

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


def load_results(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def flatten_comparisons(data: dict) -> list[dict]:
    results = data.get("results", [])
    flat = []
    for r in results:
        for e in r.get("comparison", []):
            flat.append({
                "id": r["id"],
                "tool": r["tool"],
                "prompt": r.get("prompt", ""),
                "field": e.get("field", "?"),
                "status": e.get("status", "?"),
                "alias": e.get("alias"),
                "expected": e.get("expected"),
                "resolved": e.get("resolved"),
                "actual": e.get("actual"),
                "similarity": e.get("similarity"),
                "detail": e.get("detail"),
            })
    return flat


def print_header(title: str):
    width = 72
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def error_type_distribution(flat: list[dict], tools: list[str]):
    print_header("Error Type Distribution")
    for tool in tools:
        entries = [e for e in flat if e["tool"] == tool]
        total = len(entries)
        if total == 0:
            continue
        counts = Counter(e["status"] for e in entries)
        print(f"\n  {tool} ({total} entries):")
        for status in ["MISMATCH", "RESOLUTION_ERROR", "NO_MATCH", "LENGTH_MISMATCH", "MISSING"]:
            cnt = counts.get(status, 0)
            pct = 100 * cnt / total if total else 0
            bar = "█" * int(cnt / max(total, 1) * 30)
            if cnt or status == "MISMATCH":
                print(f"    {status:<20s} {cnt:>5d}  ({pct:>5.1f}%)  {bar}")


def field_level_accuracy(flat: list[dict], tools: list[str], results: list[dict]):
    print_header("Field-Level Accuracy")
    for tool in tools:
        print(f"\n  {tool}:")
        tool_results = [r for r in results if r["tool"] == tool]
        if not tool_results:
            continue

        entries = [e for e in flat if e["tool"] == tool]
        fail_counts: dict[str, int] = Counter()
        total_counts: dict[str, int] = Counter()

        for r in tool_results:
            expected = r.get("expected", {})
            is_target = tool == "analyze_model_inputs_for_target"
            if is_target:
                if "target_value" in expected:
                    total_counts["target_value"] += 1
                if expected.get("input_names"):
                    for i in range(len(expected["input_names"])):
                        total_counts[f"input_names[{i}]"] += 1
                if expected.get("output_name"):
                    total_counts["output_name"] += 1
            else:
                if "year" in expected:
                    total_counts["year"] += 1
                if expected.get("input_names"):
                    for i in range(len(expected["input_names"])):
                        total_counts[f"input_names[{i}]"] += 1
                if expected.get("output_names"):
                    for i in range(len(expected["output_names"])):
                        total_counts[f"output_names[{i}]"] += 1

        for e in entries:
            if e["status"] in ("MISMATCH", "RESOLUTION_ERROR", "NO_MATCH", "LENGTH_MISMATCH", "MISSING"):
                fail_counts[e["field"]] += 1

        base_totals: dict[str, int] = Counter()
        base_fails: dict[str, int] = Counter()
        for field, cnt in total_counts.items():
            base = field.split("[")[0]
            base_totals[base] += cnt
        for field, cnt in fail_counts.items():
            base = field.split("[")[0]
            base_fails[base] += cnt

        for base in sorted(base_totals):
            total = base_totals[base]
            fails = base_fails.get(base, 0)
            passes = total - fails
            pct = 100 * passes / total if total else 0
            bar = "█" * int(passes / max(total, 1) * 20) + "░" * int(fails / max(total, 1) * 20)
            print(f"    {base:<20s}  {passes:>4d}/{total:<4d}  ({pct:>5.1f}%)  {bar}")


def confusion_matrix(flat: list[dict], tools: list[str], top_n: int):
    print_header(f"Confusion Matrix (top-{top_n})")
    for tool in tools:
        mismatches = [
            e for e in flat
            if e["tool"] == tool
            and e["status"] == "MISMATCH"
            and e.get("expected")
            and e.get("resolved")
        ]
        if not mismatches:
            continue
        pairs = Counter((e["expected"], e["resolved"]) for e in mismatches)
        print(f"\n  {tool} ({len(mismatches)} MISMATCH entries):")
        print(f"  {'Expected':<55s} {'→ Resolved':<45s} {'Count':>6s} {'%':>6s}")
        print(f"  {'─'*55} {'─'*45} {'─'*6} {'─'*6}")
        total_mismatches = len(mismatches)
        for (exp, res), cnt in pairs.most_common(top_n):
            pct = 100 * cnt / total_mismatches
            print(f"  {exp:<55s} {res:<45s} {cnt:>6d} {pct:>5.1f}%")


def no_match_aliases(flat: list[dict], tools: list[str], top_n: int):
    print_header("NO_MATCH Aliases — Never Resolve")
    for tool in tools:
        no_matches = [
            e for e in flat
            if e["tool"] == tool
            and e["status"] == "NO_MATCH"
            and e.get("alias")
        ]
        if not no_matches:
            continue
        alias_counts = Counter(e["alias"] for e in no_matches)
        print(f"\n  {tool} ({len(no_matches)} NO_MATCH entries):")
        print(f"  {'Alias':<45s} {'Count':>6s}")
        print(f"  {'─'*45} {'─'*6}")
        for alias, cnt in alias_counts.most_common(top_n):
            print(f"  {alias:<45s} {cnt:>6d}")


def resolution_errors(flat: list[dict], tools: list[str], top_n: int):
    print_header("Resolution Errors — Server-Side Failures")
    for tool in tools:
        res_errors = [
            e for e in flat
            if e["tool"] == tool
            and e["status"] == "RESOLUTION_ERROR"
        ]
        if not res_errors:
            continue
        alias_counts = Counter(e.get("alias", "?") for e in res_errors)
        detail_counts = Counter(e.get("detail", "?") for e in res_errors)
        print(f"\n  {tool} ({len(res_errors)} RESOLUTION_ERROR entries):")
        print("  Top aliases:")
        for alias, cnt in alias_counts.most_common(top_n):
            print(f"    {alias:<45s} {cnt:>4d}x")
        print("  Most common error:")
        for detail, cnt in detail_counts.most_common(3):
            print(f"    {detail[:100]:<100s} {cnt:>4d}x")


def similarity_distribution(flat: list[dict], tools: list[str]):
    print_header("Similarity Distribution for MISMATCH")
    bins_def = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
    for tool in tools:
        sims = [
            e["similarity"] for e in flat
            if e["tool"] == tool
            and e["status"] == "MISMATCH"
            and e.get("similarity") is not None
        ]
        if not sims:
            continue
        total = len(sims)
        print(f"\n  {tool} ({total} entries, median={sorted(sims)[total//2]:.3f}):")
        print(f"  {'Range':<12s} {'Count':>6s} {'%':>6s}  Bar")
        print(f"  {'─'*12} {'─'*6} {'─'*6}  {'─'*30}")
        for lo, hi in bins_def:
            cnt = sum(1 for s in sims if lo <= s < hi)
            pct = 100 * cnt / total
            bar = "█" * int(cnt / max(total, 1) * 30)
            print(f"  [{lo:<.1f}-{hi:<.1f})   {cnt:>6d} ({pct:>5.1f}%)  {bar}")
        cnt_last = sum(1 for s in sims if s >= 0.8)
        pct = 100 * cnt_last / total
        bar = "█" * int(cnt_last / max(total, 1) * 30)
        print(f"  [0.8-1.0]   {cnt_last:>6d} ({pct:>5.1f}%)  {bar}")


def input_count_vs_accuracy(results: list[dict], tools: list[str]):
    print_header("Input Count vs Accuracy")
    for tool in tools:
        tool_results = [r for r in results if r["tool"] == tool]
        if not tool_results:
            continue
        by_in = defaultdict(lambda: {"queries": 0, "params": 0, "passed": 0})
        for r in tool_results:
            exp = r.get("expected", {})
            n_in = len(exp.get("input_names", []))
            ps = r.get("param_stats", {})
            by_in[n_in]["queries"] += 1
            by_in[n_in]["params"] += ps.get("total", 0)
            by_in[n_in]["passed"] += ps.get("passed", 0)

        print(f"\n  {tool}:")
        print(f"  {'Inputs':>8s} {'Queries':>8s} {'Params':>8s} {'Correct':>8s} {'Accuracy':>8s}")
        print(f"  {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")
        for n in sorted(by_in):
            d = by_in[n]
            acc = 100 * d["passed"] / d["params"] if d["params"] else 0
            print(f"  {n:>8d} {d['queries']:>8d} {d['params']:>8d} {d['passed']:>8d} {acc:>7.1f}%")


def tool_summary(data: dict):
    print_header("Summary")
    tool_stats = data.get("tool_stats", {})
    for tool, stats in tool_stats.items():
        q = stats.get("queries", 0)
        qp = stats.get("queries_passed", 0)
        pt = stats.get("params_total", 0)
        pp = stats.get("params_passed", 0)
        qpct = 100 * qp / q if q else 0
        ppct = 100 * pp / pt if pt else 0
        bar_q = "█" * int(qp / max(q, 1) * 20) + "░" * int((q - qp) / max(q, 1) * 20)
        bar_p = "█" * int(pp / max(pt, 1) * 20) + "░" * int((pt - pp) / max(pt, 1) * 20)
        print(f"\n  {tool}:")
        print(f"    Queries:  {qp}/{q} ({qpct:.1f}%)  {bar_q}")
        print(f"    Params:   {pp}/{pt} ({ppct:.1f}%)  {bar_p}")


def write_csv(flat: list[dict], path: str):
    fieldnames = ["id", "tool", "field", "status", "alias", "expected", "resolved", "actual", "similarity", "detail"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat)
    print(f"\nCSV written to {path} ({len(flat)} rows)")


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze tool query results")
    parser.add_argument("input", type=str, help="Results JSON file (from run_tool_queries.py)")
    parser.add_argument("--top-n", type=int, default=15, help="Top-N for confusion matrix and NO_MATCH lists")
    parser.add_argument("--csv", type=str, default=None, help="Write comparison data to CSV")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"File not found: {input_path}")
        return 1

    data = load_results(str(input_path))
    results = data.get("results", [])
    if not results:
        print("No results found in file")
        return 1

    flat = flatten_comparisons(data)
    tools = sorted({e["tool"] for e in flat})

    tool_summary(data)
    error_type_distribution(flat, tools)
    field_level_accuracy(flat, tools, results)
    confusion_matrix(flat, tools, args.top_n)
    no_match_aliases(flat, tools, args.top_n)
    resolution_errors(flat, tools, args.top_n)
    similarity_distribution(flat, tools)
    input_count_vs_accuracy(results, tools)

    if args.csv:
        write_csv(flat, args.csv)

    return 0


if __name__ == "__main__":
    exit(main())
