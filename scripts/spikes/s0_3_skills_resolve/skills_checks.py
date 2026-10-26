"""S0.3 spike checks K1–K9: ScriptedChatModel loop, deepagents skills, read_file-only filesystem, /results/."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend
from deepagents.middleware import FilesystemMiddleware, SkillsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, Field
from scripted import ScriptedChatModel, call, last_tool_json

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass
class RequestContext:
    """Prototype of the v2 runtime context (spec §9)."""

    user_id: str
    session_id: str = "s1"


# ---------------------------------------------------------------- stub domain tools


class Term(BaseModel):
    """Термин из запроса пользователя."""

    text: str = Field(description="Термин как в запросе, например «цена метанола»")
    role: Literal["input", "output"] = Field(description="input — параметр модели, output — показатель")


@tool
def resolve_terms(terms: list[Term]) -> str:
    """Сопоставляет термины пользователя с показателями модели и возвращает кандидатов с оценками."""
    table = {"цена метанола": ("IN12", "Метанол, контрактная цена"), "dscr": ("OUT40", "DSCR")}
    out = []
    for term in terms:
        term = term if isinstance(term, dict) else term.model_dump()
        key = term["text"].lower()
        cid, name = table.get(key, ("IN0", "?"))
        out.append({"text": term["text"], "candidates": [{"id": cid, "name": name, "score": 0.9}]})
    return json.dumps({"result": "сопоставлено", "data": out}, ensure_ascii=False)


@tool
def run_scenarios(input_id: str, min_value: float, max_value: float, step: float, output_id: str, year: int) -> str:
    """Пересчитывает выход модели по сетке значений входа (заглушка движка)."""
    grid = [{"x": x, "y": round(1.0 + x / 1000, 3)} for x in range(int(min_value), int(max_value) + 1, int(step))]
    return json.dumps({"result": f"{len(grid)} точек", "data": {"input": input_id, "output": output_id, "grid": grid}})


@tool
def calc_headroom(metric: str, threshold: float) -> str:
    """Запас показателя до порога ковенанты (открывается skill covenant-headroom)."""
    return json.dumps({"result": f"запас {metric} до {threshold}: 10.8 %"}, ensure_ascii=False)


@tool
def derive(operation: str, a: float, b: float) -> str:
    """Производные числа: разница и процент изменения (открывается skill economic-answer)."""
    return json.dumps({"result": round(b - a, 4) if operation == "diff" else round((b - a) / a * 100, 2)})


@tool
async def big_result(runtime: ToolRuntime) -> str:
    """Возвращает длинный результат (проверка выноса в /results/)."""
    _ = runtime
    return "\n".join(f"точка {i}: DSCR {1 + i / 1000:.3f}" for i in range(400))


# ---------------------------------------------------------------- wiring helpers


def results_ns(rt: Any) -> tuple[str, ...]:
    """/results/ namespace from the request context: per user, never from the path (spec §13)."""
    return ("results", rt.context.user_id)


def make_backend(store: InMemoryStore, skills_mode: str = "fs", artifacts_root: str = "/") -> CompositeBackend:
    if skills_mode == "fs":
        skills_backend: Any = FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True)
    else:
        skills_backend = StoreBackend(namespace=lambda _rt: ("skills",), store=store)
    return CompositeBackend(
        default=StateBackend(),
        routes={"/skills/": skills_backend, "/results/": StoreBackend(namespace=results_ns, store=store)},
        artifacts_root=artifacts_root,
    )


def make_agent(model: ScriptedChatModel, store: InMemoryStore, *, tools: list | None = None, **kw: Any) -> Any:
    backend = kw.pop("backend", None) or make_backend(store)
    return create_agent(
        model,
        tools or [resolve_terms, run_scenarios],
        middleware=[
            SkillsMiddleware(backend=backend, sources=["/skills/"], tools=[calc_headroom, derive]),
            FilesystemMiddleware(backend=backend, tools=["read_file"], **kw),
            TodoListMiddleware(),
        ],
        context_schema=RequestContext,
        checkpointer=InMemorySaver(),
        store=store,
    )


async def invoke(agent: Any, text: str, user: str = "u1", thread: str = "t1", **state: Any) -> dict:
    return await agent.ainvoke(
        {"messages": [HumanMessage(text)], **state},
        config={"configurable": {"thread_id": f"{user}:{thread}"}},
        context=RequestContext(user_id=user),
    )


def tool_messages(messages: list[BaseMessage]) -> list[dict]:
    return [
        {"name": m.name, "status": m.status, "content": str(m.content)[:160]}
        for m in messages
        if isinstance(m, ToolMessage)
    ]


def final(text: str) -> AIMessage:
    return AIMessage(text)


def system_text(messages: list[BaseMessage]) -> str:
    return "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))


# ---------------------------------------------------------------- K1 full scripted turn


async def k1() -> dict:
    def scenarios(messages: list[BaseMessage], _tools: list[str]) -> AIMessage:
        data = last_tool_json(messages)["data"]
        ids = {d["text"].lower(): d["candidates"][0]["id"] for d in data}
        return call(
            "run_scenarios",
            input_id=ids["цена метанола"],
            min_value=300,
            max_value=450,
            step=50,
            output_id=ids["dscr"],
            year=2026,
        )

    plan = [
        {"content": "Сопоставить термины", "status": "in_progress"},
        {"content": "Посчитать сценарии", "status": "pending"},
        {"content": "Сформулировать вывод", "status": "pending"},
    ]
    done = [{**t, "status": "completed"} for t in plan]
    model = ScriptedChatModel(
        steps=[
            call("write_todos", todos=plan),
            call(
                "resolve_terms", terms=[{"text": "цена метанола", "role": "input"}, {"text": "DSCR", "role": "output"}]
            ),
            scenarios,
            call("write_todos", todos=done),
            final("DSCR 2026 растёт с 1,3 до 1,45 при цене метанола от 300 до 450."),
        ]
    )
    out = await invoke(make_agent(model, InMemoryStore()), "DSCR 2026 при цене метанола 300–450?")
    seq = [tc["name"] for m in out["messages"] if isinstance(m, AIMessage) for tc in m.tool_calls]
    scen_args = next(
        tc["args"]
        for m in out["messages"]
        if isinstance(m, AIMessage)
        for tc in m.tool_calls
        if tc["name"] == "run_scenarios"
    )
    ok = seq == ["write_todos", "resolve_terms", "run_scenarios", "write_todos"] and scen_args["input_id"] == "IN12"
    return {
        "check": "K1",
        "ok_rate": "1/1" if ok else "0/1",
        "tools": seq,
        "todos": out.get("todos"),
        "run_scenarios_args": scen_args,
    }


# ---------------------------------------------------------------- K2 skill catalog + tool disclosure


async def k2() -> dict:
    model = ScriptedChatModel(
        steps=[
            call("calc_headroom", metric="DSCR", threshold=1.2),
            call("read_file", file_path="/skills/covenant-headroom/SKILL.md", limit=1000),
            call("calc_headroom", metric="DSCR", threshold=1.2),
            final("Запас DSCR 10,8 %."),
        ]
    )
    out = await invoke(make_agent(model, InMemoryStore()), "Хватит ли запаса по DSCR?")
    first_system = ""
    tms = tool_messages(out["messages"])
    calls = model.calls
    # system prompt of call 1 is not kept in state; check the catalog through the read tool path instead
    seen = [c["tools"] for c in calls]
    ok = (
        "calc_headroom" not in seen[0]
        and tms[0]["status"] == "error"
        and tms[1]["status"] != "error"
        and "calc_headroom" in seen[2]
        and tms[2]["status"] != "error"
    )
    return {
        "check": "K2",
        "ok_rate": "1/1" if ok else "0/1",
        "tools_per_call": seen,
        "tool_messages": tms,
        "system": first_system,
    }


# ---------------------------------------------------------------- K3 pinned skill


async def k3() -> dict:
    def inspect_turn(messages: list[BaseMessage], tools: list[str]) -> AIMessage:
        pinned = [
            m
            for m in messages
            if isinstance(m, HumanMessage) and m.additional_kwargs.get("lc_source") == "pinned_skill"
        ]
        return AIMessage(json.dumps({"pinned_copies": len(pinned), "derive_visible": "derive" in tools}))

    model = ScriptedChatModel(steps=[inspect_turn, inspect_turn])
    agent = make_agent(model, InMemoryStore())
    t1 = await invoke(agent, "Ход 1", pinned_skills=["economic-answer"])
    t2 = await invoke(agent, "Ход 2", pinned_skills=["economic-answer"])
    r1, r2 = json.loads(t1["messages"][-1].content), json.loads(t2["messages"][-1].content)
    ok = r1["pinned_copies"] >= 1 and r1["derive_visible"]
    return {"check": "K3", "ok_rate": "1/1" if ok else "0/1", "turn1": r1, "turn2": r2}


# ---------------------------------------------------------------- K4 read_file only


async def k4() -> dict:
    model = ScriptedChatModel(steps=[call("write_file", file_path="/results/x.txt", content="x"), final("ок")])
    out = await invoke(make_agent(model, InMemoryStore()), "Запиши файл")
    seen = model.calls[0]["tools"]
    forbidden = {"write_file", "edit_file", "ls", "glob", "grep", "execute", "delete"} & set(seen)
    tms = tool_messages(out["messages"])
    ok = not forbidden and tms and tms[0]["status"] == "error"
    return {"check": "K4", "ok_rate": "1/1" if ok else "0/1", "visible_tools": seen, "write_attempt": tms}


# ---------------------------------------------------------------- K5 where skills live


async def _read_paths(backend_mode: str, store: InMemoryStore, paths: list[str]) -> list[dict]:
    steps: list[Any] = [call("read_file", file_path=p, limit=50) for p in paths] + [final("ок")]
    model = ScriptedChatModel(steps=steps)
    out = await invoke(make_agent(model, store, backend=make_backend(store, backend_mode)), "Прочитай файлы")
    return [{"path": p, **t} for p, t in zip(paths, tool_messages(out["messages"]), strict=False)]


async def k5() -> dict:
    escape = ["/skills/covenant-headroom/SKILL.md", "/skills/../scripted.py", "/skills/../../../../.env", "/etc/hosts"]
    fs = await _read_paths("fs", InMemoryStore(), escape)

    store = InMemoryStore()
    seed = StoreBackend(namespace=lambda _rt: ("skills",), store=store)
    for path in SKILLS_DIR.rglob("SKILL.md"):
        seed.write("/" + str(path.relative_to(SKILLS_DIR)), path.read_text(encoding="utf-8"))
    st = await _read_paths("store", store, escape[:2])

    leaked = [
        r["path"]
        for r in fs[1:]
        if r["status"] != "error" and ("import" in r["content"] or "=" in r["content"] or "localhost" in r["content"])
    ]
    ok = fs[0]["status"] != "error" and not leaked and st[0]["status"] != "error"
    return {
        "check": "K5",
        "ok_rate": "1/1" if ok else "0/1",
        "filesystem_route": fs,
        "store_seeded": st,
        "leaked": leaked,
    }


# ---------------------------------------------------------------- K6 /results/ isolation


@tool
async def save_result(runtime: ToolRuntime) -> str:
    """Сохраняет полный результат расчёта в /results/ (прототип адаптера)."""
    backend = StoreBackend(namespace=results_ns, store=runtime.store)
    backend.write("/turn1/call_1.json", json.dumps({"grid": [1, 2, 3], "owner": runtime.context.user_id}))
    return json.dumps({"result": "сохранено", "ref": "/results/turn1/call_1.json"})


async def k6() -> dict:
    store = InMemoryStore()
    owner = ScriptedChatModel(
        steps=[call("save_result"), call("read_file", file_path="/results/turn1/call_1.json"), final("ок")]
    )
    out_a = await invoke(make_agent(owner, store, tools=[save_result]), "Посчитай", user="alice", thread="same")
    other = ScriptedChatModel(steps=[call("read_file", file_path="/results/turn1/call_1.json"), final("ок")])
    out_b = await invoke(make_agent(other, store, tools=[save_result]), "Покажи чужое", user="bob", thread="same")
    a, b = tool_messages(out_a["messages"]), tool_messages(out_b["messages"])
    ok = "alice" in a[1]["content"] and "alice" not in b[0]["content"]
    return {"check": "K6", "ok_rate": "1/1" if ok else "0/1", "owner_reads": a, "other_user_reads": b}


# ---------------------------------------------------------------- K7 built-in eviction


async def k7() -> dict:
    cases = {}
    for root in ("/", "/results/"):
        store = InMemoryStore()
        backend = make_backend(store, artifacts_root=root)
        model = ScriptedChatModel(steps=[call("big_result"), final("ок")])
        out = await invoke(
            make_agent(model, store, tools=[big_result], backend=backend, tool_token_limit_before_evict=200),
            "Большой результат",
            user="alice",
        )
        tm = next(m for m in out["messages"] if isinstance(m, ToolMessage))
        in_store = [(i.namespace, i.key) for i in store.search(("results", "alice"))]
        cases[root] = {
            "tool_message_chars": len(str(tm.content)),
            "preview": str(tm.content)[:300],
            "store_items_results_alice": in_store,
            "state_files": list((out.get("files") or {}).keys()),
        }
    evicted = cases["/results/"]["tool_message_chars"] < 2000 and cases["/results/"]["store_items_results_alice"]
    return {"check": "K7", "ok_rate": "1/1" if evicted else "0/1", "cases": cases}


# ---------------------------------------------------------------- K8 invalid skill


async def k8() -> dict:
    def capture(messages: list[BaseMessage], _tools: list[str]) -> AIMessage:
        return AIMessage(system_text(messages))

    model = ScriptedChatModel(steps=[capture])
    try:
        out = await invoke(make_agent(model, InMemoryStore()), "Какие skills есть?")
    except Exception as exc:
        return {"check": "K8", "ok_rate": "raised", "error": type(exc).__name__, "message": str(exc)[:300]}
    system = out["messages"][-1].content
    names = [n for n in ("covenant-headroom", "economic-answer", "Broken Skill", "broken-skill") if n in system]
    meta = [s.get("name") for s in out.get("skills_metadata") or []]
    warn_idx = system.find("arn")
    return {
        "check": "K8",
        "ok_rate": "info",
        "skills_in_prompt": names,
        "skills_metadata": meta,
        "warning_excerpt": system[max(0, warn_idx - 200) : warn_idx + 300] if warn_idx >= 0 else None,
        "catalog_in_system_prompt": "covenant-headroom" in system,
    }


CHECKS = {"K1": k1, "K2": k2, "K3": k3, "K4": k4, "K5": k5, "K6": k6, "K7": k7, "K8": k8}


# ---------------------------------------------------------------- K9 live GigaChat reads a skill by its description

SKILLS_PROMPT_RU = (
    "## Skills\n\nУ тебя есть библиотека skills — методик для типовых задач аналитика.\n\n"
    "{skills_locations}{skills_load_warnings}\n\n**Доступные skills:**\n\n{skills_list}\n\n"
    "Если запрос подходит под описание skill, сначала прочитай его целиком: `read_file` по указанному пути "
    "с `limit=1000`, затем следуй методике. Инструменты skill становятся доступны после чтения."
)


async def k9() -> dict:
    from langchain_gigachat import GigaChat  # pylint: disable=import-outside-toplevel

    from aigw_service.config import APP_CONFIG  # pylint: disable=import-outside-toplevel

    def build(prompt: str | None) -> Any:
        store = InMemoryStore()
        backend = make_backend(store)
        skills_kw = {"system_prompt": prompt} if prompt else {}
        return create_agent(
            GigaChat(**APP_CONFIG.gigachat.base_params, model=APP_CONFIG.gigachat.model),
            [resolve_terms, run_scenarios],
            system_prompt="Ты кредитный аналитик. Работай только с инструментами.",
            middleware=[
                SkillsMiddleware(backend=backend, sources=["/skills/"], tools=[calc_headroom, derive], **skills_kw),
                FilesystemMiddleware(backend=backend, tools=["read_file"]),
            ],
            context_schema=RequestContext,
            checkpointer=InMemorySaver(),
            store=store,
        )

    variants = {}
    for label, prompt in (("default_en", None), ("russian", SKILLS_PROMPT_RU)):
        runs = []
        for i in range(3):
            try:
                out = await invoke(
                    build(prompt), "Хватит ли запаса по ковенанте DSCR ≥ 1,2 в 2026 году?", thread=f"{label}{i}"
                )
                seq = [
                    (tc["name"], tc["args"].get("file_path", ""))
                    for m in out["messages"]
                    if isinstance(m, AIMessage)
                    for tc in m.tool_calls
                ]
                read = any(n == "read_file" and "covenant-headroom" in p for n, p in seq)
                runs.append(
                    {"ok": read, "tools": [n for n, _ in seq], "answer": str(out["messages"][-1].content)[:200]}
                )
            except Exception as exc:
                runs.append({"ok": False, "error": type(exc).__name__, "message": str(exc)[:300]})
        variants[label] = {"ok_rate": f"{sum(r['ok'] for r in runs)}/3", "runs": runs}
    total = sum(int(v["ok_rate"][0]) for v in variants.values())
    return {"check": "K9", "ok_rate": f"{total}/6", "variants": variants}


CHECKS["K9"] = k9
