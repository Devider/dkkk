"""Scenario skills of agent v2 on Methanex (stories S3.3 design D5, S3.4 design D7).

Queries and expectations come from ``evals.yaml`` of the scenario skills (all four, or those given by ``--skills``). Live mode (default): GigaChat by ``.env`` and
LibreOffice — run inside the app image. ``--scripted``: a scripted model, no network and no engine, checks the harness
(it calls no tools, so ``tools_ok`` is false there except for questions).

    python scripts/eval/scenario_skills.py [--scripted] [--skills NAME ...] [--only ID ...]
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

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# pylint: disable=wrong-import-position
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from aigw_service.api.v2.answer_check import hygiene_issues, is_analytic, sections, structure_issues
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.models import build_model
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.skills_check import SKILLS_DIR
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.turn.registry import ModelRegistry
from aigw_service.config import APP_CONFIG

SKILLS = ("target-seeking", "scenario-analysis", "covenant-headroom", "sensitivity-drivers")
WORKBOOK = REPO / "models" / "Methanex_FinModel.xlsx"
OUT_DIR = REPO / "scripts" / "eval" / "results"
USER = "eval0001"
ENGINE_TOOLS = default_catalog().engine_tool_names()


def load_cases(skills: tuple[str, ...] = SKILLS) -> list[dict]:
    cases = []
    for skill in skills:
        cases += yaml.safe_load((SKILLS_DIR / skill / "evals.yaml").read_text(encoding="utf-8"))
    return cases


async def uploaded(store: Any, upload_dir: Path) -> None:
    """As ``/upload`` at AGENT_ENGINE=v2: the user's file, the memory record and the registry entry."""
    stored = f"user_file_{USER}.xlsx"
    shutil.copy(WORKBOOK, upload_dir / stored)
    digest = await ModelRegistry(store).register(upload_dir / stored, file_name=WORKBOOK.name)
    await store.aput(("memories", USER), USER, {"filename": stored, "file_hash": digest})


def scripted_model(case: dict) -> ScriptedChatModel:
    """Reads the expected skill, writes a plan with the expected words and answers as expected — no tools."""
    expect = case["expect"]
    read = tool_call("read_file", "r1", file_path=f"/skills/{expect['skill']}/SKILL.md")
    words = expect["plan_includes"] or ["расчёт"]
    if expect["answer"] == "question":
        todos = [{"content": f"Шаг: {word}", "status": "blocked", "reason": "не хватает данных"} for word in words]
        return ScriptedChatModel(
            steps=[read, tool_call("write_todos", "p1", todos=todos), AIMessage("Уточните недостающие данные.")]
        )
    todos = [{"content": f"Шаг: {word}", "status": "completed"} for word in words]
    caveats = "; ".join(expect["caveat_includes"]) or "нет"
    answer = AIMessage(f"## Вывод\nРасчёт выполнен.\n\n## Оговорки\nДопущения: {caveats}.\n")
    # No tool results here: AnswerGroundingGuard (FR-13) returns an answer with numbers («10 %») for regeneration
    regenerations = [answer] * APP_CONFIG.agent.grounding_max_returns
    return ScriptedChatModel(steps=[read, tool_call("write_todos", "p1", todos=todos), answer, *regenerations])


def skills_read(messages: list) -> list[str]:
    paths = [
        call["args"].get("file_path", "")
        for m in messages
        if isinstance(m, AIMessage)
        for call in m.tool_calls
        if call["name"] == "read_file"
    ]
    return [p.split("/")[2] for p in paths if p.startswith("/skills/") and p.count("/") >= 3]


def caveats_of(answer: str) -> str:
    found = sections(answer)
    for i, (name, _, body_start) in enumerate(found):
        if name == "Оговорки":
            return answer[body_start : found[i + 1][1] if i + 1 < len(found) else len(answer)]
    return ""


def score(case: dict, out: dict) -> dict:
    """Per-query checks of design D5."""
    expect = case["expect"]
    messages = out["messages"]
    answer = messages[-1].text
    tools = [m.name for m in messages if isinstance(m, ToolMessage)]
    read = skills_read(messages)
    plan = [t.get("content", "") for t in out.get("todos") or []]
    analytic = is_analytic(answer)
    other_scenario_skills = set(read) & set(SKILLS) - {expect["skill"]}
    caveats = caveats_of(answer).lower()
    return {
        "skills_read": read,
        "skill_ok": expect["skill"] in read and not other_scenario_skills,
        "plan": [{k: t.get(k) for k in ("content", "status", "reason") if t.get(k)} for t in out.get("todos") or []],
        "plan_ok": all(any(w.lower() in s.lower() for s in plan) for w in expect["plan_includes"]),
        "tools": tools,
        "tools_ok": set(expect["tools"]) <= set(tools),
        "answer_ok": analytic if expect["answer"] == "analytic" else not analytic and not set(tools) & ENGINE_TOOLS,
        "caveats_ok": all(w.lower() in caveats for w in expect["caveat_includes"]),
        "structure_issues": structure_issues(answer) if analytic else [],
        "hygiene_issues": hygiene_issues(answer),
        "answer": answer,
    }


async def run_case(case: dict, scripted: bool, store: Any, upload_dir: Path) -> dict:
    model = scripted_model(case) if scripted else build_model()
    agent = AgentFactory(
        model, default_catalog(), store=store, upload_dir=str(upload_dir), checkpointer=InMemorySaver()
    ).build()
    session = f"eval-{case['id']}"
    started = time.monotonic()
    base = {"id": case["id"], "query": case["query"], "expect": case["expect"]}
    try:
        out = await agent.ainvoke(
            {"messages": [("user", case["query"])]},
            config={"configurable": {"thread_id": f"{USER}:{session}"}, "recursion_limit": 100},
            context=RequestContext(user_id=USER, session_id=session),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught  # one failed query must not stop the run
        return {**base, "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.monotonic() - started, 1)}
    model_calls = sum(isinstance(m, AIMessage) for m in out["messages"])
    return {**base, "seconds": round(time.monotonic() - started, 1), "model_calls": model_calls, **score(case, out)}


CHECKS = ("skill_ok", "plan_ok", "tools_ok", "answer_ok", "caveats_ok")


def summary(results: list[dict]) -> dict:
    done = [r for r in results if "error" not in r]
    report: dict[str, Any] = {"queries": len(results), "errors": len(results) - len(done)}
    for check in CHECKS:
        report[check] = f"{sum(r[check] for r in done)}/{len(done)}"
    report["passed"] = [r["id"] for r in done if all(r[c] for c in CHECKS) and not r["hygiene_issues"]]
    report["structure_violations"] = sum(bool(r["structure_issues"]) for r in done)
    report["hygiene_violations"] = sum(bool(r["hygiene_issues"]) for r in done)
    seconds = sorted(r["seconds"] for r in done)
    report["seconds_median"] = seconds[len(seconds) // 2] if seconds else None
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scripted", action="store_true", help="scripted model: no network, no engine")
    parser.add_argument("--skills", nargs="*", choices=SKILLS, default=list(SKILLS), help="scenario skills to run")
    parser.add_argument("--only", nargs="*", default=[], help="query IDs from evals.yaml, e.g. SA2 TS1")
    args = parser.parse_args()

    store = InMemoryStore()
    upload_dir = Path(tempfile.mkdtemp(prefix="skills-eval-"))
    await uploaded(store, upload_dir)
    cases = [c for c in load_cases(tuple(args.skills)) if not args.only or c["id"] in args.only]
    results = []
    for case in cases:
        result = await run_case(case, args.scripted, store, upload_dir)
        print(json.dumps({k: result.get(k) for k in ("id", "seconds", *CHECKS, "error")}, ensure_ascii=False))
        results.append(result)
    report = {"mode": "scripted" if args.scripted else "gigachat", "summary": summary(results), "results": results}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = OUT_DIR / f"scenario_skills_{report['mode']}_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(path)


if __name__ == "__main__":
    asyncio.run(main())
