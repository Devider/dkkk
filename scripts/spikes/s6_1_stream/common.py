"""Shared bench pieces: the full v2 stack on a scripted model, fakes of the engine and the semantic layer.

Nothing in ``src/`` is changed: fakes are installed by replacing module attributes, as the unit tests do.
"""

import asyncio
import json
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from tests.unit.v2.fakes import ScriptedChatModel, tool_call, uploaded_store

from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.preview_check import PreviewChecker, PreviewModelCheck
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from aigw_service.api.v2.tools.resolve_terms import Pick
from excel_engine import ExcelAnalysisToolResult

YEARS = (2025, 2026, 2027)
INDEX = ModelIndex(
    inputs={"IN1": "Метанол", "IN2": "Инфляция"},
    outputs={"OUT0": "EBITDA", "OUT1": "DSCR"},
    input_values={"IN1": {2026: 386.7}, "IN2": {2025: 0.05}},
    output_years={"OUT0": YEARS, "OUT1": YEARS},
)
QUESTION = "EBITDA 2026 при цене метанола 300–500?"
PLAN = [
    {"content": "Сопоставить термины", "status": "in_progress"},
    {"content": "Посчитать сценарии", "status": "pending"},
]
DONE = [{**t, "status": "completed"} for t in PLAN]
GOOD_ANSWER = "EBITDA 2026 растёт с 1200 до 1501 при цене метанола от 300 до 500."
BAD_ANSWER = "EBITDA 2026 достигает 9999 при цене метанола 500."


class SpikeGate:
    """EngineGate stand-in: runs in-process, may sleep (a long calculation) and fail the first N calls."""

    def __init__(self, delay: float = 0.0, failures: int = 0, cancel_aware: bool = True) -> None:
        self.delay = delay
        self.failures = failures
        self.calls = 0
        self.cancelled = 0
        self.finished = 0
        self.cancel_aware = cancel_aware

    async def run(self, session_key: str, func: Callable[..., Any], /, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise ToolError(ErrorCode.ENGINE_UNAVAILABLE)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1  # a cancel in the middle of a calculation: what soffice would see
            raise
        self.finished += 1
        return func(**kwargs)


def engine(**_kw: Any) -> ExcelAnalysisToolResult:
    frame = pd.DataFrame({"Метанол": [300, 400, 500], "EBITDA": [1200.0, 1350.5, 1501.0]})
    return ExcelAnalysisToolResult(status="OK", result="", content={"result_df": frame})


def install_fakes(gate: SpikeGate, analyzer_model: BaseChatModel | None = None) -> None:
    """``resolve_terms`` and the engine without LibreOffice; ``analyzer_model`` stands in for ``APP_CTX.llm``."""

    async def analyzer(message: str, _index: Any) -> list[Pick]:
        if analyzer_model is not None:  # a nested LLM call inside the tool, as AnalyzerSubAgent makes
            await analyzer_model.ainvoke(message)
        return [Pick("input", "метанол", "IN1"), Pick("output", "EBITDA", "OUT0")]

    rt.analyze_with_v1 = analyzer
    calc_tools.analyze_excel_model = engine
    calc_tools._gate = lambda: gate  # pylint: disable=protected-access


def u2_steps(answer: Sequence[AIMessage | Callable[..., AIMessage]] = ()) -> list[Any]:
    """U2: plan → resolve_terms → run_scenarios → plan closed → answer (``answer`` replaces the last step)."""
    return [
        tool_call("write_todos", "c1", todos=PLAN),
        tool_call(
            "resolve_terms", "c2", terms=[{"text": "метанол", "role": "input"}, {"text": "EBITDA", "role": "output"}]
        ),
        tool_call(
            "run_scenarios",
            "c3",
            inputs=[{"id": "IN1", "min": 300, "max": 500, "step": 100}],
            outputs=["OUT0"],
            year=2026,
        ),
        tool_call("write_todos", "c4", todos=DONE),
        *(answer or [AIMessage(GOOD_ANSWER)]),
    ]


class PlacedFactory(AgentFactory):
    """AgentFactory with extra middleware placed right after another one (by class name) instead of the default."""

    def __init__(self, *args: Any, place: dict[str, AgentMiddleware] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.place = place or {}

    def middleware(self) -> list[AgentMiddleware]:
        out: list[AgentMiddleware] = []
        for mw in super().middleware():
            out.append(mw)
            if (extra := self.place.get(type(mw).__name__)) is not None:
                out.append(extra)
        return out


async def build_agent(
    model: BaseChatModel,
    *,
    place: dict[str, AgentMiddleware] | None = None,
    extra: Sequence[AgentMiddleware] = (),
    preview: PreviewModelCheck | None = None,
    checkpointer: Any = None,
    prompt_budget: Any = None,
) -> Any:
    tmp = tempfile.mkdtemp(prefix="s6_1_")
    store = await uploaded_store("u1", INDEX, tmp)
    return PlacedFactory(
        model,
        default_catalog(),
        middleware=extra,
        store=store,
        upload_dir=tmp,
        place=place,
        preview_check=preview,
        checkpointer=checkpointer,
        prompt_budget=prompt_budget,
    ).build()


def scripted_preview() -> PreviewModelCheck:
    """Preview A/B on scripted models, every call sampled — to see whether its calls leak into the stream."""
    many = [AIMessage("preview")] * 50
    checker = PreviewChecker(ScriptedChatModel(steps=many), ScriptedChatModel(steps=[AIMessage("0.9")] * 50), "m")
    return PreviewModelCheck(checker, 1.0)


# --- recording of stream parts -------------------------------------------------------------------------------


def short(text: Any, n: int = 70) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def message_view(m: Any) -> dict[str, Any]:
    if not isinstance(m, BaseMessage):
        return {"raw": type(m).__name__, "repr": short(repr(m))}
    view: dict[str, Any] = {"type": type(m).__name__, "id": m.id}
    if m.content:
        view["content"] = short(m.content if isinstance(m.content, str) else str(m.content))
    if getattr(m, "tool_calls", None):
        view["tool_calls"] = [f"{c['name']}#{c.get('id')}" for c in m.tool_calls]
    if isinstance(m, ToolMessage):
        view["tool"] = f"{m.name}#{m.tool_call_id}"
        view["status"] = m.status
    if isinstance(m, AIMessageChunk) and m.chunk_position:
        view["chunk_position"] = m.chunk_position
    return view


def update_view(data: Any) -> Any:
    """``{node: state delta}``; messages shortened, other keys listed with a short value."""
    if not isinstance(data, dict):
        return short(data)
    out: dict[str, Any] = {}
    for node, delta in data.items():
        if not isinstance(delta, dict):
            out[node] = short(delta) if delta is not None else None
            continue
        item: dict[str, Any] = {}
        for key, value in delta.items():
            if key == "messages":
                msgs = value if isinstance(value, list) else [value]
                item["messages"] = [message_view(m) for m in msgs]
            else:
                item[key] = short(value, 60)
        out[node] = item
    return out


META_KEYS = (
    "langgraph_node",
    "langgraph_step",
    "langgraph_triggers",
    "checkpoint_ns",
    "tags",
    "ls_model_name",
    "name",
)


def part_view(part: Any, t0: float) -> dict[str, Any]:
    """One part of ``astream``: tuple (mode, data), (ns, mode, data) or a v2 dict — kept in a common shape."""
    at = round(time.monotonic() - t0, 3)
    if isinstance(part, dict):  # version="v2"
        mode, data, ns = part.get("type"), part.get("data"), part.get("ns")
        extra = {k: short(v) for k, v in part.items() if k not in ("type", "data", "ns")}
    elif isinstance(part, tuple) and len(part) == 3:
        ns, mode, data = part
        extra = {}
    else:
        mode, data = part
        ns, extra = None, {}
    view: dict[str, Any] = {"t": at, "mode": mode}
    if ns:
        view["ns"] = list(ns)
    if extra:
        view["extra"] = extra
    if mode == "updates":
        view["data"] = update_view(data)
    elif mode == "messages":
        msg, meta = data
        view["msg"] = message_view(msg)
        view["meta"] = {k: meta.get(k) for k in META_KEYS if k in meta}
    else:
        view["data"] = data
    return view


async def record(agent: Any, context: Any, *, modes: Sequence[str], version: str | None = None, **kw: Any) -> dict:
    """Run one turn with ``astream`` and keep every part; ``final`` is the last AIMessage of the state."""
    t0 = time.monotonic()
    extra = {"version": version} if version else {}
    stream = agent.astream({"messages": [("user", QUESTION)]}, context=context, stream_mode=list(modes), **extra, **kw)
    parts = [part_view(part, t0) async for part in stream]
    return {"parts": parts, "seconds": round(time.monotonic() - t0, 3)}


def nodes_in(parts: list[dict]) -> list[str]:
    seen: list[str] = []
    for p in parts:
        if p["mode"] == "updates" and isinstance(p["data"], dict):
            for node in p["data"]:
                if node not in seen:
                    seen.append(node)
    return seen


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
