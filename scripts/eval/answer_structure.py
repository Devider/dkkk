"""Answer structure (FR-12) and hygiene (FR-14) of agent v2 on Methanex (story S3.2).

Live mode (default): GigaChat by ``.env`` and LibreOffice — run inside the app image. ``--scripted``: a scripted model,
no network and no engine, checks the harness itself.

    python scripts/eval/answer_structure.py [--scripted] [--only N]
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from aigw_service.api.v2.answer_check import hygiene_issues, is_analytic, structure_issues
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.models import build_model
from aigw_service.api.v2.scripted import ScriptedChatModel
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.turn.registry import ModelRegistry

WORKBOOK = REPO / "models" / "Methanex_FinModel.xlsx"
OUT_DIR = REPO / "scripts" / "eval" / "results"
USER = "eval0001"

QUERIES = [
    {
        "id": "Q1",
        "kind": "analytic",
        "text": "Как изменится EBITDA в 2026 году при цене метанола от 300 до 500 с шагом 100?",
    },
    {
        "id": "Q2",
        "kind": "analytic",
        "text": "Посчитай EBITDA 2027 при изменении цены метанола на ±20 % от базы, шаг 10 %.",
    },
    {"id": "Q3", "kind": "analytic", "text": "Какой должна быть цена метанола, чтобы EBITDA в 2026 году была 1000?"},
    {"id": "Q4", "kind": "analytic", "text": "Сравни EBITDA 2026 и 2028 при цене метанола 350 и 450."},
    {"id": "Q5", "kind": "question", "text": "Посчитай EBITDA при цене метанола от 300 до 500."},
    {
        "id": "Q6",
        "kind": "analytic",
        "text": "What happens to EBITDA in 2026 if the methanol price is 300, 400 or 500?",
    },
]

SCRIPTED_ANSWER = """\
## Вывод
EBITDA растёт вместе с ценой метанола.

## Ключевые цифры
| Показатель | Год | Значение | Ед. |
| --- | --- | --- | --- |
| EBITDA при Метанол = 300 | 2026 | 1 200 | млн руб. |
"""


async def uploaded(store: Any, upload_dir: Path) -> None:
    """As ``/upload`` at AGENT_ENGINE=v2: the user's file, the memory record and the registry entry."""
    stored = f"user_file_{USER}.xlsx"
    shutil.copy(WORKBOOK, upload_dir / stored)
    digest = await ModelRegistry(store).register(upload_dir / stored, file_name=WORKBOOK.name)
    await store.aput(("memories", USER), USER, {"filename": stored, "file_hash": digest})


def model_for(scripted: bool, query: dict) -> Any:
    if not scripted:
        return build_model()
    text = SCRIPTED_ANSWER if query["kind"] == "analytic" else "За какой год посчитать?"
    return ScriptedChatModel(steps=[AIMessage(text)])


async def run_query(query: dict, scripted: bool, store: Any, upload_dir: Path) -> dict:
    model = model_for(scripted, query)
    agent = AgentFactory(
        model, default_catalog(), store=store, upload_dir=str(upload_dir), checkpointer=InMemorySaver()
    ).build()
    session = f"eval-{query['id']}"
    started = time.monotonic()
    try:
        out = await agent.ainvoke(
            {"messages": [("user", query["text"])]},
            config={"configurable": {"thread_id": f"{USER}:{session}"}, "recursion_limit": 100},
            context=RequestContext(user_id=USER, session_id=session),
        )
    except Exception as exc:
        return {**query, "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.monotonic() - started, 1)}
    answer = out["messages"][-1].text
    tools = [m.name for m in out["messages"] if isinstance(m, ToolMessage)]
    analytic = is_analytic(answer)
    asked = not analytic and any(t.get("status") == "blocked" for t in out.get("todos") or [])
    return {
        **query,
        "seconds": round(time.monotonic() - started, 1),
        "tools": tools,
        "plan": [{k: t.get(k) for k in ("content", "status", "reason") if t.get(k)} for t in out.get("todos") or []],
        "analytic": analytic,
        "asked_user": asked,  # a question instead of a calculation: no sections by design, counted apart
        "structure_issues": structure_issues(answer)
        if analytic or (query["kind"] == "analytic" and not asked)
        else [],
        "hygiene_issues": hygiene_issues(answer),
        "answer": answer,
    }


def summary(results: list[dict]) -> dict:
    analytic = [r for r in results if r["kind"] == "analytic" and "error" not in r and not r["asked_user"]]
    return {
        "errors": sum("error" in r for r in results),
        "hygiene_violations": sum(bool(r.get("hygiene_issues")) for r in results),
        "structure_ok": f"{sum(not r['structure_issues'] for r in analytic)}/{len(analytic)}",
        "asked_instead_of_answer": [r["id"] for r in results if r["kind"] == "analytic" and r.get("asked_user")],
        "questions_without_sections": sum(
            not r.get("analytic") for r in results if r["kind"] == "question" and "error" not in r
        ),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scripted", action="store_true", help="scripted model: no network, no engine")
    parser.add_argument("--only", type=int, default=0, help="first N queries")
    args = parser.parse_args()

    store = InMemoryStore()
    upload_dir = Path(tempfile.mkdtemp(prefix="answer-eval-"))
    await uploaded(store, upload_dir)
    queries = QUERIES[: args.only] if args.only else QUERIES
    results = []
    for query in queries:
        result = await run_query(query, args.scripted, store, upload_dir)
        print(
            json.dumps(
                {k: result.get(k) for k in ("id", "seconds", "structure_issues", "hygiene_issues", "error")},
                ensure_ascii=False,
            )
        )
        results.append(result)
    report = {"mode": "scripted" if args.scripted else "gigachat", "summary": summary(results), "results": results}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = OUT_DIR / f"answer_structure_{report['mode']}_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(path)


if __name__ == "__main__":
    asyncio.run(main())
