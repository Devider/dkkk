"""S0.3 spike checks R1–R4: resolve_terms options over the unchanged semantic layer, measured on the Methanex gold set."""

import asyncio
import json
import random
from functools import cache
from pathlib import Path
from typing import Any

import openpyxl
from langchain_core.messages import HumanMessage

REPO = Path(__file__).resolve().parents[3]
WORKBOOK = REPO / "models" / "Methanex_FinModel.xlsx"
GOLD = REPO / "tests" / "data" / "Methanex_tool_test_queries.xlsx"
SEED, N_IFT, N_EMA, TOP_K, CONCURRENCY, RATE_LIMIT_RETRIES = 42, 20, 10, 3, 1, 4


# ---------------------------------------------------------------- data


@cache
def catalog() -> tuple[dict, dict, dict, dict]:
    """(inputs INid→name, outputs OUTid→name, input_mapping, output_mapping) — exactly as v1 builds them."""
    from aigw_service.api.v1.subagents.utils import (  # pylint: disable=import-outside-toplevel
        _build_catalog_with_ids,
        build_mappings,
    )

    ins, outs = _build_catalog_with_ids(str(WORKBOOK))
    in_map, out_map = build_mappings(str(WORKBOOK))
    return ins, outs, in_map, out_map


def _rows(sheet: str) -> list[dict]:
    wb = openpyxl.load_workbook(GOLD, read_only=True)
    rows = list(wb[sheet].iter_rows(values_only=True))
    header = rows[0]
    return [dict(zip(header, r, strict=False)) for r in rows[1:] if r[0]]


def _list(cell: Any) -> list[str]:
    if cell is None:
        return []
    try:
        value = json.loads(cell)
    except (TypeError, ValueError):
        return [str(cell)]
    return [str(v).strip() for v in (value if isinstance(value, list) else [value])]


@cache
def sample() -> list[dict]:
    """Fixed sample: N_IFT target-seeking + N_EMA scenario queries with gold names and aliases."""
    rng = random.Random(SEED)
    items = []
    for kind, sheet, n in (("ift", "analyze_model_inputs_for_target", N_IFT), ("ema", "analyze_excel_model", N_EMA)):
        items.extend(
            {
                "id": r["ID"],
                "kind": kind,
                "query": r["Запрос (prompt)"],
                "gold_inputs": _list(r["input_names (эталон, канон.)"]),
                "gold_outputs": _list(r["output_names (эталон, канон.)"]),
                "input_aliases": _list(r["input_aliases (в запросе)"]),
                "output_aliases": _list(r.get("output_alias (в запросе)") or r.get("output_aliases (в запросе)")),
            }
            for r in rng.sample(_rows(sheet), n)
        )
    return items


# ---------------------------------------------------------------- analyzers (unchanged v1 semantic layer)


@cache
def analyzers() -> dict:
    from aigw_service.api.v1 import main_graph as mg  # pylint: disable=import-outside-toplevel

    common = {"lookup_prompt_template": mg.LOOKUP_MORE_PROMPT}
    return {
        "ift": mg.AnalyzerSubAgent(
            state_schema=mg.InputsForTargetAnalizerState,
            input_schema=mg.InputsForTargetAnalizerInput,
            output_schema=mg.InputsForTargetAnalizerOutput,
            llm_output_schema=mg.QueryAnalysisIFT,
            name="ift_subagent",
            analyze_prompt_template=mg.IFT_ANALIZER_PROMPT,
            **common,
        ),
        "ema": mg.AnalyzerSubAgent(
            state_schema=mg.ExcelModelAnalizerState,
            input_schema=mg.ExcelModelAnalizerInput,
            output_schema=mg.ExcelModelAnalizerOutput,
            llm_output_schema=mg.QueryAnalysisEMA,
            name="ema_subagent",
            analyze_prompt_template=mg.EMA_ANALIZER_PROMPT,
            **common,
        ),
    }


async def _analyze(item: dict, text: str) -> dict:
    ins, outs, _, _ = catalog()
    response = await analyzers()[item["kind"]].ainvoke([HumanMessage(text)], ins, outs)
    q, resolved = response.get("q_analysis"), response.get("resolved_inputs") or []
    inputs = [(r.mentioned_input_name, r.equivalent_input_name.strip()) for r in resolved]
    if item["kind"] == "ift":
        outputs = [q.output_name.strip()] if q and q.output_name else []
    else:
        outputs = [o.equivalent_output_name.strip() for o in (q.mentioned_outputs if q else [])]
    return {"inputs": inputs, "outputs": outputs}


async def _with_rate_limit_retry(item: dict, text: str) -> dict:
    """GigaChat answers 429 under parallel load; the client does not retry it (spike finding)."""
    from gigachat.exceptions import RateLimitError  # pylint: disable=import-outside-toplevel

    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            return await _analyze(item, text)
        except RateLimitError:
            _RATE_LIMITED["n"] += 1
            await asyncio.sleep(5 * (attempt + 1))
    return await _analyze(item, text)


_RATE_LIMITED = {"n": 0}


def _score(pred: list[str], gold: list[str]) -> tuple[int, int, int]:
    """(hits, gold size, extra predictions)."""
    gold_set, pred_set = set(gold), set(pred)
    return len(gold_set & pred_set), len(gold_set), len(pred_set - gold_set)


def _accuracy(rows: list[dict], key: str) -> dict:
    hits = sum(r[key][0] for r in rows)
    total = sum(r[key][1] for r in rows)
    extra = sum(r[key][2] for r in rows)
    return {"accuracy": round(hits / total, 3) if total else None, "hits": hits, "total": total, "extra": extra}


async def _run_variant(make_text: Any) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(item: dict) -> dict:
        async with sem:
            try:
                pred = await _with_rate_limit_retry(item, make_text(item))
            except Exception as exc:
                return {
                    "id": item["id"],
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "in": (0, len(item["gold_inputs"]), 0),
                    "out": (0, len(item["gold_outputs"]), 0),
                }
            return {
                "id": item["id"],
                "kind": item["kind"],
                "pred": pred,
                "in": _score([n for _, n in pred["inputs"]], item["gold_inputs"]),
                "out": _score(pred["outputs"], item["gold_outputs"]),
            }

    return await asyncio.gather(*(one(i) for i in sample()))


def _summary(rows: list[dict]) -> dict:
    by_kind = {
        k: {
            "inputs": _accuracy([r for r in rows if r.get("kind") == k], "in"),
            "outputs": _accuracy([r for r in rows if r.get("kind") == k], "out"),
        }
        for k in ("ift", "ema")
    }
    return {
        "inputs": _accuracy(rows, "in"),
        "outputs": _accuracy(rows, "out"),
        "by_kind": by_kind,
        "errors": [r for r in rows if "error" in r],
        "rate_limited_retries": _RATE_LIMITED["n"],
    }


_R1_CACHE: dict = {}


async def r1() -> dict:
    rows = await _run_variant(lambda item: item["query"])
    _R1_CACHE["rows"] = rows
    return {"check": "R1", "ok_rate": "info", **_summary(rows), "rows": rows}


def _terms_text(item: dict) -> str:
    return (
        "Сопоставь термины с моделью Methanex_FinModel.xlsx. "
        f"Входные параметры: {'; '.join(item['input_aliases'])}. "
        f"Выходные показатели: {'; '.join(item['output_aliases'])}."
    )


async def r2() -> dict:
    rows = await _run_variant(_terms_text)
    return {"check": "R2", "ok_rate": "info", **_summary(rows), "rows": rows}


# ---------------------------------------------------------------- Jaccard top-k (new loop over the unchanged primitives)


def jaccard_top(alias: str, mapping: dict, k: int = TOP_K) -> list[tuple[str, float]]:
    from aigw_service.api.v1.tools import jaccard_similarity, normalize_text  # pylint: disable=import-outside-toplevel

    query = normalize_text(alias)
    scored = [(info["original"].strip(), jaccard_similarity(query, norm)) for norm, info in mapping.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


async def r3() -> dict:
    _, _, in_map, out_map = catalog()
    stats = {"inputs": [], "outputs": []}
    for item in sample():
        for role, aliases, gold, mapping in (
            ("inputs", item["input_aliases"], item["gold_inputs"], in_map["row_mapping"]),
            ("outputs", item["output_aliases"], item["gold_outputs"], out_map["output_mapping"]),
        ):
            for alias, gold_name in zip(aliases, gold, strict=False):
                top = jaccard_top(alias, mapping)
                names = [n for n, _ in top]
                stats[role].append(
                    {
                        "alias": alias,
                        "gold": gold_name,
                        "at1": names[:1] == [gold_name],
                        "at3": gold_name in names,
                        "top1_score": round(top[0][1], 3),
                        "top": top,
                    }
                )

    def agg(rows: list[dict]) -> dict:
        n = len(rows) or 1
        right = [r["top1_score"] for r in rows if r["at1"]]
        wrong = [r["top1_score"] for r in rows if not r["at1"]]
        return {
            "n": len(rows),
            "acc_at1": round(sum(r["at1"] for r in rows) / n, 3),
            "acc_at3": round(sum(r["at3"] for r in rows) / n, 3),
            "zero_score_share": round(sum(r["top1_score"] == 0 for r in rows) / n, 3),
            "mean_top1_score_right": round(sum(right) / len(right), 3) if right else None,
            "mean_top1_score_wrong": round(sum(wrong) / len(wrong), 3) if wrong else None,
        }

    return {
        "check": "R3",
        "ok_rate": "info",
        "inputs": agg(stats["inputs"]),
        "outputs": agg(stats["outputs"]),
        "rows": stats,
    }


async def r4() -> dict:
    """Agreement of the analyzer choice with Jaccard top-3 of the alias it reported, as a low-confidence signal."""
    rows = _R1_CACHE.get("rows") or await _run_variant(lambda item: item["query"])
    _, _, in_map, _ = catalog()
    gold_by_id = {i["id"]: set(i["gold_inputs"]) for i in sample()}
    buckets = {"agree": [0, 0], "disagree": [0, 0]}
    for r in rows:
        for mentioned, chosen in (r.get("pred") or {}).get("inputs", []):
            names = [n for n, _ in jaccard_top(mentioned, in_map["row_mapping"])]
            bucket = "agree" if chosen in names else "disagree"
            buckets[bucket][0] += chosen in gold_by_id[r["id"]]
            buckets[bucket][1] += 1
    out = {k: {"n": v[1], "precision": round(v[0] / v[1], 3) if v[1] else None} for k, v in buckets.items()}
    return {"check": "R4", "ok_rate": "info", **out}


CHECKS = {"R1": r1, "R2": r2, "R3": r3, "R4": r4}
