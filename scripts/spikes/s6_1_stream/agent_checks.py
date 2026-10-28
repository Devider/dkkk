"""P1–P6: the full v2 stack on a scripted model — what ``astream`` gives, no network, no LibreOffice."""

import asyncio
import time
from typing import Any

from common import (
    BAD_ANSWER,
    DONE,
    GOOD_ANSWER,
    PLAN,
    QUESTION,
    SpikeGate,
    build_agent,
    install_fakes,
    message_view,
    nodes_in,
    part_view,
    record,
    scripted_preview,
    tool_call,
    u2_steps,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.constants import TAG_NOSTREAM
from progress import WRITERS, reporter
from tests.unit.v2.fakes import ScriptedChatModel

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.middleware import summarization
from aigw_service.api.v2.middleware.summarization import ContextBudget
from aigw_service.api.v2.turn.lock import TurnLocks

MODES = ["updates", "custom", "messages"]


def ctx(session: str = "s1") -> RequestContext:
    return RequestContext(user_id="u1", session_id=session, trace_id="t")


def compact(parts: list[dict]) -> list[str]:
    """One line per part — the order of events is the point of P1."""
    lines = []
    for p in parts:
        if p["mode"] == "updates":
            for node, delta in (p["data"] or {}).items():
                msgs = delta.get("messages", []) if isinstance(delta, dict) else []
                keys = [k for k in delta if k != "messages"] if isinstance(delta, dict) else []
                desc = "; ".join(
                    m.get("tool") or ",".join(m.get("tool_calls", [])) or m.get("content", m.get("type", ""))
                    for m in msgs
                )
                lines.append(f"{p['t']:6.3f} U {node} msgs=[{desc}] keys={keys}")
        elif p["mode"] == "messages":
            m, meta = p["msg"], p["meta"]
            what = ",".join(m.get("tool_calls", [])) or m.get("tool") or m.get("content", "")
            lines.append(
                f"{p['t']:6.3f} M {m['type']} node={meta.get('langgraph_node')} tags={meta.get('tags')} {what}"
            )
        else:
            lines.append(f"{p['t']:6.3f} C {p['data']}")
    return lines


async def p1() -> dict[str, Any]:
    install_fakes(SpikeGate(delay=0.5))
    agent = await build_agent(ScriptedChatModel(steps=u2_steps()))
    run = await record(agent, ctx(), modes=MODES)
    times = [p["t"] for p in run["parts"]]
    return {
        "check": "P1",
        "nodes": nodes_in(run["parts"]),
        "events": compact(run["parts"]),
        "spread_s": [times[0], times[-1]] if times else None,
        "seconds": run["seconds"],
        "parts": run["parts"],
    }


async def p2() -> dict[str, Any]:
    install_fakes(SpikeGate())
    agent = await build_agent(ScriptedChatModel(steps=u2_steps()))
    try:
        run = await record(agent, ctx(), modes=MODES, version="v2")
    except TypeError as exc:
        return {"check": "P2", "version_v2": False, "error": str(exc)}
    first = {p["mode"]: p for p in reversed(run["parts"])}
    raw = []
    async for part in (await build_agent(ScriptedChatModel(steps=u2_steps()))).astream(
        {"messages": [("user", QUESTION)]}, context=ctx("s2"), stream_mode=MODES, version="v2"
    ):
        raw.append(part)
        if len(raw) >= 3:
            break
    return {
        "check": "P2",
        "version_v2": True,
        "raw_type": type(raw[0]).__name__,
        "raw_keys": sorted(raw[0].keys()) if isinstance(raw[0], dict) else None,
        "sample_by_mode": first,
        "events": compact(run["parts"]),
    }


async def p3() -> dict[str, Any]:
    out: dict[str, Any] = {"check": "P3"}
    # grounding: an answer with an unconfirmed number goes back once (AnswerGroundingGuard)
    install_fakes(SpikeGate())
    agent = await build_agent(ScriptedChatModel(steps=u2_steps([AIMessage(BAD_ANSWER), AIMessage(GOOD_ANSWER)])))
    run = await record(agent, ctx("g"), modes=MODES)
    out["grounding_events"] = compact(run["parts"])
    out["grounding_bad_text_parts"] = [p for p in run["parts"] if "9999" in str(p)]
    # plan: an answer while a step is open goes back with a reminder (PlanCompletionGuard)
    steps = [
        tool_call("write_todos", "c1", todos=PLAN),
        *u2_steps()[1:3],
        AIMessage("Рано: " + GOOD_ANSWER),
        tool_call("write_todos", "c4", todos=DONE),
        AIMessage(GOOD_ANSWER),
    ]
    install_fakes(SpikeGate())
    agent = await build_agent(ScriptedChatModel(steps=steps))
    run = await record(agent, ctx("p"), modes=MODES)
    out["plan_events"] = compact(run["parts"])
    out["plan_early_text_parts"] = [p for p in run["parts"] if "Рано" in str(p)]
    return out


async def p4() -> dict[str, Any]:
    out: dict[str, Any] = {"check": "P4"}
    install_fakes(SpikeGate(failures=1))
    outer, inner = reporter("outer"), reporter("inner")
    agent = await build_agent(ScriptedChatModel(steps=u2_steps()), place={"Telemetry": outer}, extra=[inner])
    run = await record(agent, ctx("r"), modes=["updates", "custom"])
    out["custom"] = [p["data"] for p in run["parts"] if p["mode"] == "custom"]
    out["events"] = compact(run["parts"])
    # the same stack under ainvoke: stream_writer must be a no-op, never an error
    WRITERS.clear()
    install_fakes(SpikeGate())
    agent = await build_agent(ScriptedChatModel(steps=u2_steps()), place={"Telemetry": reporter("outer")})
    result = await agent.ainvoke({"messages": [("user", QUESTION)]}, context=ctx("i"))
    out["ainvoke_ok"] = result["messages"][-1].content == GOOD_ANSWER
    out["ainvoke_writers"] = sorted({w["writer"] for w in WRITERS})
    return out


class SummaryAwareModel(ScriptedChatModel):
    """Answers the summary request itself, so the script cursor stays on the agent's steps."""

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        if len(messages) == 1 and isinstance(messages[0], HumanMessage) and not self._tools:
            return ChatResult(generations=[ChatGeneration(message=AIMessage("Сводка ранней части разговора."))])
        return super()._generate(messages, stop, run_manager, **kw)


def tag_summary_nostream() -> None:
    """Variant of P5: the summary call carries LangGraph's ``nostream`` tag, so ``messages`` skips it."""
    orig = summarization.summarizer

    def tagged(model: Any) -> Any:
        return orig(model.with_config(tags=[TAG_NOSTREAM]))

    summarization.summarizer = tagged


async def p5() -> dict[str, Any]:
    """Which ``messages`` parts come from the main call and which from nested ones (summary, resolve_terms, preview)."""
    first = await _p5_run()
    tag_summary_nostream()
    second = await _p5_run()
    return {"check": "P5", "plain": first, "summary_nostream": second}


async def _p5_run() -> dict[str, Any]:
    analyzer = ScriptedChatModel(steps=[AIMessage("analyzer: метанол=IN1")] * 5)
    install_fakes(SpikeGate(), analyzer_model=analyzer)
    model = SummaryAwareModel(steps=u2_steps())
    budget = ContextBudget(max_prompt_tokens=4000, trigger=0.9, keep=0.35)
    agent = await build_agent(model, preview=scripted_preview(), prompt_budget=budget)
    t0 = time.monotonic()
    parts, metas = [], []
    async for mode, data in agent.astream(
        {"messages": [("user", QUESTION)]}, context=ctx("m"), stream_mode=["messages"]
    ):
        msg, meta = data
        parts.append(part_view((mode, data), t0))
        metas.append({"msg": message_view(msg), "meta": {k: v for k, v in meta.items() if not k.startswith("__")}})
    return {
        "messages_parts": len(parts),
        "by_source": [
            {
                "content": m["msg"].get("content") or ",".join(m["msg"].get("tool_calls", [])) or m["msg"].get("tool"),
                "type": m["msg"]["type"],
                "node": m["meta"].get("langgraph_node"),
                "tags": m["meta"].get("tags"),
                "checkpoint_ns": m["meta"].get("checkpoint_ns"),
                "ls_model_type": m["meta"].get("ls_model_type"),
                "meta_keys": sorted(m["meta"]),
            }
            for m in metas
        ],
        "summary_called": any("Сводка" in str(m) for m in metas),
        "analyzer_called": len(analyzer.calls),
    }


class StreamTurn:
    """ADR-14 prototype: the turn is its own task, parts go to a queue; the consumer may go away at any time."""

    def __init__(self, agent: Any, locks: TurnLocks) -> None:
        self.agent = agent
        self.locks = locks
        self.task: asyncio.Task | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.released_at: float | None = None

    async def _run(self, context: RequestContext) -> Any:
        try:
            with self.locks.hold(context.thread_id):
                async for part in self.agent.astream(
                    {"messages": [("user", QUESTION)]}, context=context, stream_mode=["updates", "custom"]
                ):
                    self.queue.put_nowait(part)
        finally:
            self.released_at = time.monotonic()
            self.queue.put_nowait(None)

    def start(self, context: RequestContext) -> None:
        self.task = asyncio.create_task(self._run(context))

    async def parts(self) -> Any:
        while (part := await self.queue.get()) is not None:
            yield part


async def p6() -> dict[str, Any]:
    gate = SpikeGate(delay=1.0)
    install_fakes(gate)
    agent = await build_agent(ScriptedChatModel(steps=u2_steps()))
    locks = TurnLocks()
    turn = StreamTurn(agent, locks)
    context = ctx("q")
    turn.start(context)

    async def consumer() -> int:
        n = 0
        async for _ in turn.parts():
            n += 1
        return n

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0.3)  # the consumer goes away mid-turn (like a cancelled StreamingResponse generator)
    cancelled_at = time.monotonic()
    consumer_task.cancel()
    busy_after_cancel = context.thread_id in locks.busy
    await turn.task
    return {
        "check": "P6",
        "busy_right_after_consumer_cancel": busy_after_cancel,
        "lock_released_after_cancel_s": round(turn.released_at - cancelled_at, 3),
        "engine_finished": gate.finished,
        "engine_cancelled": gate.cancelled,
        "turn_exception": repr(turn.task.exception()),
    }


CHECKS = {"P1": p1, "P2": p2, "P3": p3, "P4": p4, "P5": p5, "P6": p6}
