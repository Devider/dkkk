"""resolve_terms per-param accuracy on the Methanex gold set vs the v1 analyzer (story S1.3, FR-6).

Live GigaChat, not pytest. Same sample as spike S0.3 (seed 42: 20 target-seeking + 10 scenario queries);
the v1 baseline is the spike's R1 run on that sample.

    python scripts/eval/resolve_terms_accuracy.py [--baseline <spike resolve.json>]
"""

import argparse
import asyncio
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import openpyxl

from aigw_service.api.v1.subagents.utils import _build_catalog_with_ids
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.resolve_terms import ResolveTermsArgs, Term, resolve_terms

WORKBOOK = REPO / "models" / "Methanex_FinModel.xlsx"
GOLD = REPO / "tests" / "data" / "Methanex_tool_test_queries.xlsx"
SPIKE_R1 = REPO / "scripts" / "spikes" / "s0_3_skills_resolve" / "results" / "resolve.json"
SEED, N_IFT, N_EMA = 42, 20, 10


def _list(cell: object) -> list[str]:
    if cell is None:
        return []
    try:
        value = json.loads(str(cell))
    except ValueError:
        return [str(cell)]
    return [str(v).strip() for v in (value if isinstance(value, list) else [value])]


def sample() -> list[dict]:
    """Exactly the spike S0.3 sample (same seed, same order)."""
    wb = openpyxl.load_workbook(GOLD, read_only=True)
    rng = random.Random(SEED)
    items = []
    for kind, sheet, n in (("ift", "analyze_model_inputs_for_target", N_IFT), ("ema", "analyze_excel_model", N_EMA)):
        rows = list(wb[sheet].iter_rows(values_only=True))
        records = [dict(zip(rows[0], r, strict=False)) for r in rows[1:] if r[0]]
        items.extend(
            {
                "id": r["ID"],
                "kind": kind,
                "gold_inputs": _list(r["input_names (эталон, канон.)"]),
                "gold_outputs": _list(r["output_names (эталон, канон.)"]),
                "input_aliases": _list(r["input_aliases (в запросе)"]),
                "output_aliases": _list(r.get("output_alias (в запросе)") or r.get("output_aliases (в запросе)")),
            }
            for r in rng.sample(records, n)
        )
    return items


def score(pred: list[str], gold: list[str]) -> tuple[int, int]:
    return len(set(pred) & set(gold)), len(set(gold))


async def run(index: ModelIndex) -> list[dict]:
    ctx = SimpleNamespace(user_id="eval", file_path=str(WORKBOOK), model_index=index)
    rows = []
    for item in sample():
        terms = [Term(text=a, role="input") for a in item["input_aliases"]]
        terms += [Term(text=a, role="output") for a in item["output_aliases"]]
        try:
            envelope = await resolve_terms(ResolveTermsArgs(terms=terms), ctx)
        except Exception as exc:
            rows.append({"id": item["id"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        terms_out = envelope.data if envelope.status == "ok" else []
        first = {(t["role"], t["text"]): t["candidates"][0]["name"] for t in terms_out if t["status"] == "ok"}
        pred_in = [first[("input", a)] for a in item["input_aliases"] if ("input", a) in first]
        pred_out = [first[("output", a)] for a in item["output_aliases"] if ("output", a) in first]
        gold = set(item["gold_inputs"]) | set(item["gold_outputs"])
        confidence = [
            {"confidence": t["confidence"], "correct": t["candidates"][0]["name"] in gold}
            for t in terms_out
            if t["status"] == "ok"
        ]
        rows.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "in": score(pred_in, item["gold_inputs"]),
                "out": score(pred_out, item["gold_outputs"]),
                "confidence": confidence,
            }
        )
        print(item["id"], rows[-1]["in"], rows[-1]["out"], flush=True)
    return rows


def accuracy(rows: list[dict], key: str, ids: set[str] | None = None) -> dict:
    picked = [r for r in rows if "error" not in r and (ids is None or r["id"] in ids)]
    hits, total = sum(r[key][0] for r in picked), sum(r[key][1] for r in picked)
    return {
        "accuracy": round(hits / total, 3) if total else None,
        "hits": hits,
        "total": total,
        "queries": len(picked),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=str(SPIKE_R1), help="spike S0.3 resolve.json with the R1 run")
    args = parser.parse_args()

    inputs, outputs = _build_catalog_with_ids(str(WORKBOOK))
    rows = asyncio.run(run(ModelIndex(inputs, outputs)))
    r1 = next(r for r in json.loads(Path(args.baseline).read_text(encoding="utf-8"))["results"] if r["check"] == "R1")[
        "rows"
    ]
    common = {r["id"] for r in rows if "error" not in r} & {r["id"] for r in r1 if "error" not in r}
    conf = [c for r in rows if "error" not in r for c in r["confidence"]]
    by_level = {
        level: {
            "n": len(group := [c for c in conf if c["confidence"] == level]),
            "precision": round(sum(c["correct"] for c in group) / len(group), 3) if group else None,
        }
        for level in ("high", "low")
    }
    report = {
        "started": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "resolve_terms": {"inputs": accuracy(rows, "in"), "outputs": accuracy(rows, "out")},
        "common_queries": len(common),
        "on_common": {
            "resolve_terms": {"inputs": accuracy(rows, "in", common), "outputs": accuracy(rows, "out", common)},
            "v1_r1": {"inputs": accuracy(r1, "in", common), "outputs": accuracy(r1, "out", common)},
        },
        "confidence": by_level,
        "errors": [r for r in rows if "error" in r],
        "rows": rows,
    }
    out = REPO / "scripts" / "eval" / "results" / f"resolve_terms_{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("rows",)}, ensure_ascii=False, indent=2))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
