"""S0.2 spike checks C1–C10: GigaChat inside langchain `create_agent` (see openspec change s0-2-gigachat-agent-spike)."""

import asyncio
import copy
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fixtures import (
    ANSWER_SYSTEM_PROMPT,
    BASE_TOOLS,
    FR12_SECTIONS,
    LONG_QUERIES,
    calc_headroom,
    get_metric,
    open_skill,
    scenario_grid,
)
from gigachat.exceptions import ForbiddenError
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelRetryMiddleware, TodoListMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_gigachat import GigaChat

from aigw_service.config import APP_CONFIG

STOP_MARKER = b"temporarily unavailable due to technical reasons"
RUNS = 3

Handler = Callable[[ModelRequest], Awaitable[Any]]


# ---------------------------------------------------------------- helpers


def make_model(**overrides: Any) -> GigaChat:
    """GigaChat built exactly like the service builds it: LOCAL + .env decide api_key vs certificates."""
    params = dict(APP_CONFIG.gigachat.base_params)
    params["model"] = overrides.pop("model", APP_CONFIG.gigachat.model)
    params.update(overrides)
    return GigaChat(**params)


def ai_messages(messages: list[BaseMessage]) -> list[AIMessage]:
    return [m for m in messages if isinstance(m, AIMessage)]


def tool_sequence(messages: list[BaseMessage]) -> list[str]:
    return [tc["name"] for m in ai_messages(messages) for tc in m.tool_calls]


def final_text(messages: list[BaseMessage]) -> str:
    last = messages[-1]
    if isinstance(last, AIMessage) and not last.tool_calls:
        return str(last.content)
    return ""


def error_info(exc: BaseException) -> dict:
    return {"error": type(exc).__name__, "message": str(exc)[:300]}


def summarize(check: str, runs: list[dict], **extra: Any) -> dict:
    ok = sum(1 for r in runs if r.get("ok"))
    return {"check": check, "ok_rate": f"{ok}/{len(runs)}", "runs": runs, **extra}


async def run_agent(agent: Any, query: str) -> list[BaseMessage]:
    out = await agent.ainvoke({"messages": [HumanMessage(query)]})
    return out["messages"]


def _tool_name(t: Any) -> str | None:
    return getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)


# ---------------------------------------------------------------- C1 basic loop


async def c1() -> dict:
    runs = []
    for _ in range(RUNS):
        try:
            agent = create_agent(
                make_model(), BASE_TOOLS, system_prompt="Ты финансовый аналитик. Используй инструменты."
            )
            msgs = await run_agent(agent, "Какая EBITDA в 2026 году? Используй инструмент.")
            text = final_text(msgs)
            runs.append(
                {
                    "ok": "get_metric" in tool_sequence(msgs) and bool(text),
                    "tools": tool_sequence(msgs),
                    "answer": text[:200],
                }
            )
        except Exception as exc:
            runs.append({"ok": False, **error_info(exc)})
    return summarize("C1", runs)


# ---------------------------------------------------------------- C2 token refresh


async def c2() -> dict:
    model = make_model()
    await model.ainvoke("Ответь одним словом: ок")
    client = model._client  # pylint: disable=protected-access
    token = getattr(client, "_access_token", None)
    if token is None:
        return {"check": "C2", "skipped": "нет OAuth-токена (режим сертификатов)"}
    runs = []

    expired = copy.copy(token)
    expired.expires_at = 1
    client._access_token = expired  # pylint: disable=protected-access
    try:
        await model.ainvoke("Ответь одним словом: ок")
        new = client._access_token  # pylint: disable=protected-access
        runs.append({"case": "expired", "ok": new.access_token != token.access_token or new.expires_at != 1})
    except Exception as exc:
        runs.append({"case": "expired", "ok": False, **error_info(exc)})

    revoked = copy.copy(client._access_token)  # pylint: disable=protected-access
    revoked.access_token = "invalid-token"
    revoked.expires_at = int(time.time() * 1000) + 3_600_000
    client._access_token = revoked  # pylint: disable=protected-access
    try:
        await model.ainvoke("Ответь одним словом: ок")
        runs.append({"case": "revoked_not_expired", "ok": True})
    except Exception as exc:
        runs.append({"case": "revoked_not_expired", "ok": False, **error_info(exc)})
    return summarize("C2", runs)


# ---------------------------------------------------------------- C3 forced tool choice


class ForceFirstTool(AgentMiddleware):
    """Prototype of ForcePlanFirst: force a named tool on the first model call of the turn."""

    def __init__(self, tool_name: str) -> None:
        super().__init__()
        self.tool_name = tool_name

    async def awrap_model_call(self, request: ModelRequest, handler: Handler) -> Any:
        if not ai_messages(request.messages):
            request = request.override(tool_choice=self.tool_name)
        return await handler(request)


async def c3() -> dict:
    runs = []
    for _ in range(RUNS):
        try:
            agent = create_agent(
                make_model(), BASE_TOOLS, middleware=[TodoListMiddleware(), ForceFirstTool("write_todos")]
            )
            msgs = await run_agent(agent, "Дай EBITDA 2026 и курс USD/RUB 2026.")
            seq = tool_sequence(msgs)
            runs.append({"ok": bool(seq) and seq[0] == "write_todos", "tools": seq, "answer": final_text(msgs)[:200]})
        except Exception as exc:
            runs.append({"ok": False, **error_info(exc)})

    any_choice: dict = {}
    try:
        make_model().bind_tools(BASE_TOOLS, tool_choice="any")
        any_choice["strict"] = "accepted"
    except Exception as exc:
        any_choice["strict"] = error_info(exc)
    try:
        bound = make_model(allow_any_tool_choice_fallback=True).bind_tools(BASE_TOOLS, tool_choice="any")
        reply = await bound.ainvoke("Привет! Как дела?")
        any_choice["fallback_tool_calls"] = [tc["name"] for tc in reply.tool_calls]
    except Exception as exc:
        any_choice["fallback"] = error_info(exc)
    return summarize("C3", runs, tool_choice_any=any_choice)


# ---------------------------------------------------------------- C4 several calls in one response


async def c4() -> dict:
    runs = []
    for _ in range(RUNS):
        try:
            agent = create_agent(make_model(), BASE_TOOLS)
            msgs = await run_agent(
                agent, "Одновременно получи EBITDA 2026 и курс USD/RUB 2026 — вызови оба инструмента сразу."
            )
            per_msg = [len(m.tool_calls) for m in ai_messages(msgs) if m.tool_calls]
            runs.append(
                {"ok": bool(final_text(msgs)), "tool_calls_per_message": per_msg, "tools": tool_sequence(msgs)}
            )
        except Exception as exc:
            runs.append({"ok": False, **error_info(exc)})
    return summarize("C4", runs)


# ---------------------------------------------------------------- C5 functions_state_id across a long loop


async def c5() -> dict:
    runs = []
    for _ in range(RUNS):
        try:
            agent = create_agent(make_model(), BASE_TOOLS)
            msgs = await run_agent(
                agent,
                "Получи EBITDA за 2025, 2026 и 2027 годы — по одному вызову инструмента на каждый год, затем сравни.",
            )
            calls = [m for m in ai_messages(msgs) if m.tool_calls]
            with_state = sum(1 for m in calls if m.additional_kwargs.get("functions_state_id"))
            runs.append(
                {
                    "ok": len(calls) >= 3 and bool(final_text(msgs)),
                    "tool_turns": len(calls),
                    "with_functions_state_id": with_state,
                }
            )
        except Exception as exc:
            runs.append({"ok": False, **error_info(exc)})
    return summarize("C5", runs)


# ---------------------------------------------------------------- C6 tool set changes mid-turn


class SkillGate(AgentMiddleware):
    """Prototype of skill-tool visibility: calc_headroom is hidden until open_skill has been called."""

    async def awrap_model_call(self, request: ModelRequest, handler: Handler) -> Any:
        opened = any(isinstance(m, ToolMessage) and m.name == "open_skill" for m in request.messages)
        if not opened:
            request = request.override(tools=[t for t in request.tools if _tool_name(t) != "calc_headroom"])
        return await handler(request)


async def c6() -> dict:
    runs = []
    for _ in range(RUNS):
        try:
            agent = create_agent(
                make_model(),
                [get_metric, open_skill, calc_headroom],
                system_prompt="Для расчёта запаса по ковенантам сначала открой skill covenant-headroom.",
                middleware=[SkillGate()],
            )
            msgs = await run_agent(agent, "Какой запас по DSCR 2026 до порога ковенанты 1,2?")
            seq = tool_sequence(msgs)
            in_order = (
                "open_skill" in seq and "calc_headroom" in seq and seq.index("open_skill") < seq.index("calc_headroom")
            )
            runs.append({"ok": in_order and bool(final_text(msgs)), "tools": seq, "answer": final_text(msgs)[:200]})
        except Exception as exc:
            runs.append({"ok": False, **error_info(exc)})
    return summarize("C6", runs)


# ---------------------------------------------------------------- C7 last message is not human


def _history_with_tool_result() -> list[BaseMessage]:
    return [
        HumanMessage("Какая EBITDA в 2026 году?"),
        AIMessage("", tool_calls=[{"name": "get_metric", "args": {"name": "EBITDA", "year": 2026}, "id": "call_1"}]),
        ToolMessage('{"result": "EBITDA 2026 = 1265.9", "value": 1265.9}', tool_call_id="call_1", name="get_metric"),
    ]


def _meaningful(text: str) -> bool:
    return len(text.strip()) >= 20


async def c7() -> dict:
    runs = []
    for _ in range(RUNS):
        case: dict = {}
        try:  # (a) model call right after ToolMessage inside the agent loop
            agent = create_agent(make_model(), BASE_TOOLS)
            text = final_text(await run_agent(agent, "Какая EBITDA в 2026 году?"))
            case["after_tool_in_loop"] = {"ok": _meaningful(text), "answer": text[:150]}
        except Exception as exc:
            case["after_tool_in_loop"] = {"ok": False, **error_info(exc)}
        try:  # (b) no tools bound, last message is ToolMessage (partial answer after a limit)
            reply = await make_model().ainvoke(_history_with_tool_result())
            case["after_tool_no_tools"] = {"ok": _meaningful(str(reply.content)), "answer": str(reply.content)[:150]}
        except Exception as exc:
            case["after_tool_no_tools"] = {"ok": False, **error_info(exc)}
        try:  # (c) last message is a plain AIMessage
            reply = await make_model().ainvoke(
                [HumanMessage("Какая EBITDA в 2026 году?"), AIMessage("EBITDA 2026 = 1265,9 млн USD.")]
            )
            case["after_ai"] = {"ok": _meaningful(str(reply.content)), "answer": str(reply.content)[:150]}
        except Exception as exc:
            case["after_ai"] = {"ok": False, **error_info(exc)}
        case["ok"] = all(v["ok"] for v in case.values())
        runs.append(case)
    return summarize("C7", runs)


# ---------------------------------------------------------------- C8 StopEvent


class Probe(AgentMiddleware):
    """Records exceptions passing through wrap_model_call."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    async def awrap_model_call(self, request: ModelRequest, handler: Handler) -> Any:
        try:
            return await handler(request)
        except Exception as exc:
            self.seen.append(type(exc).__name__)
            raise


def _install_stop_event(model: GigaChat) -> dict:
    calls = {"n": 0}

    async def fake_achat(*_args: Any, **_kwargs: Any) -> Any:
        calls["n"] += 1
        raise ForbiddenError("https://gigachat/api/v1/chat/completions", 403, STOP_MARKER, None)

    model._client.achat = fake_achat  # pylint: disable=protected-access
    return calls


def _is_stop_event(exc: BaseException) -> bool:
    return isinstance(exc, ForbiddenError) and STOP_MARKER in (exc.content or b"").lower()


class StopEventGuard(AgentMiddleware):
    """Prototype: StopEvent → StopEventError at wrap_model_call, outer to ModelRetryMiddleware."""

    async def awrap_model_call(self, request: ModelRequest, handler: Handler) -> Any:
        from aigw_service.exceptions import StopEventError  # pylint: disable=import-outside-toplevel

        try:
            return await handler(request)
        except ForbiddenError as exc:
            if _is_stop_event(exc):
                raise StopEventError(
                    user_message="GigaChat временно недоступен", url=str(exc.url), reason=str(exc.content)
                ) from exc
            raise


async def _stop_event_case(model: GigaChat, middleware: list[AgentMiddleware]) -> dict:
    calls = _install_stop_event(model)
    probe = Probe()
    agent = create_agent(model, BASE_TOOLS, middleware=[probe, *middleware])
    try:
        msgs = await run_agent(agent, "Какая EBITDA в 2026 году?")
        outcome: dict = {"raised": None, "final_message": final_text(msgs)[:200] or str(msgs[-1].content)[:200]}
    except Exception as exc:
        outcome = {"raised": type(exc).__name__}
    return {**outcome, "api_calls": calls["n"], "seen_in_wrap_model_call": probe.seen}


async def c8() -> dict:
    fast_retry = {"max_retries": 2, "initial_delay": 0.05, "jitter": False}
    cases: dict = {}
    cases["retry_default"] = await _stop_event_case(make_model(), [ModelRetryMiddleware(**fast_retry)])
    cases["retry_on_failure_error"] = await _stop_event_case(
        make_model(), [ModelRetryMiddleware(on_failure="error", **fast_retry)]
    )

    from aigw_service.context import _wrap_llm_with_stop_event  # pylint: disable=import-outside-toplevel
    from aigw_service.core.llm_executor import Agent  # pylint: disable=import-outside-toplevel

    params = dict(APP_CONFIG.gigachat.base_params)
    wrapped = _wrap_llm_with_stop_event(Agent(**params, model=APP_CONFIG.gigachat.model), _NullLogger())
    cases["v1_subclass_wrapper"] = await _stop_event_case(wrapped, [])

    cases["guard_prototype"] = await _stop_event_case(
        make_model(),
        [
            StopEventGuard(),
            ModelRetryMiddleware(retry_on=lambda e: not _is_stop_event(e), on_failure="error", **fast_retry),
        ],
    )
    guard = cases["guard_prototype"]
    return {
        "check": "C8",
        "ok_rate": "1/1" if guard.get("raised") == "StopEventError" and guard["api_calls"] == 1 else "0/1",
        "cases": cases,
    }


class _NullLogger:
    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass


# ---------------------------------------------------------------- C9 A/B preview check


async def c9() -> dict:
    from aigw_service.core.llm_executor import Agent  # pylint: disable=import-outside-toplevel

    counts = {"invoke": 0, "ainvoke": 0, "bind_tools": 0}

    class CountingAgent(Agent):
        def invoke(self, *args: Any, **kwargs: Any) -> Any:
            counts["invoke"] += 1
            return super().invoke(*args, **kwargs)

        async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
            counts["ainvoke"] += 1
            return await super().ainvoke(*args, **kwargs)

        def bind_tools(self, *args: Any, **kwargs: Any) -> Any:
            counts["bind_tools"] += 1
            return super().bind_tools(*args, **kwargs)

    params = dict(APP_CONFIG.gigachat.base_params)
    agent = create_agent(CountingAgent(**params, model=APP_CONFIG.gigachat.model), BASE_TOOLS)
    msgs = await run_agent(agent, "Какая EBITDA в 2026 году?")
    model_calls = len(ai_messages(msgs))
    subclass = {
        "model_calls": model_calls,
        "override_calls": counts,
        "ainvoke_bypassed": counts["ainvoke"] == 0 and model_calls > 0,
    }

    preview = PreviewModelCheck(make_model(model=f"{APP_CONFIG.gigachat.model}-preview"))
    agent = create_agent(make_model(), BASE_TOOLS, middleware=[preview])
    started = time.monotonic()
    text = final_text(await run_agent(agent, "Какая EBITDA в 2026 году?"))
    turn_s = round(time.monotonic() - started, 2)
    await asyncio.gather(*preview.tasks, return_exceptions=True)
    middleware = {
        "answer_ok": bool(text),
        "turn_seconds": turn_s,
        "max_overhead_ms": preview.max_overhead_ms,
        "preview_results": preview.results,
    }
    ok = subclass["ainvoke_bypassed"] and bool(text) and preview.max_overhead_ms < 50
    return {"check": "C9", "ok_rate": "1/1" if ok else "0/1", "subclass": subclass, "middleware_prototype": middleware}


class PreviewModelCheck(AgentMiddleware):
    """Prototype of the A/B preview check as middleware: fire-and-forget call, never touches the response."""

    def __init__(self, preview: GigaChat) -> None:
        super().__init__()
        self.preview = preview
        self.tasks: list[asyncio.Task] = []
        self.results: list[dict] = []
        self.max_overhead_ms = 0.0

    async def awrap_model_call(self, request: ModelRequest, handler: Handler) -> Any:
        response = await handler(request)
        handled = time.monotonic()
        self.tasks.append(asyncio.create_task(self._compare(request)))
        self.max_overhead_ms = max(self.max_overhead_ms, round((time.monotonic() - handled) * 1000, 2))
        return response

    async def _compare(self, request: ModelRequest) -> None:
        try:
            messages = ([request.system_message] if request.system_message else []) + list(request.messages)
            reply = await self.preview.bind_tools(request.tools).ainvoke(messages)
            self.results.append({"ok": True, "tool_calls": [tc["name"] for tc in reply.tool_calls]})
        except Exception as exc:
            self.results.append({"ok": False, **error_info(exc)})


# ---------------------------------------------------------------- C10 long context final answer (ADR-9)


@tool
def run_scenarios(input_name: str, min_value: float, max_value: float, step: float, year: int) -> str:
    """Пересчитывает выходы модели по сетке значений входа."""
    return ""


_NUM_RE = re.compile(r"(?<![\w.])\d[\d\s]*(?:[.,]\d+)?")


def _numbers(text: str) -> set[float]:
    found = set()
    for raw in _NUM_RE.findall(text):
        try:
            found.add(float(raw.replace(" ", "").replace(" ", "").replace(",", ".")))
        except ValueError:
            continue
    return found


def _grounding(answer: str, sources: str) -> dict:
    src = _numbers(sources)
    src_rounded = {round(v, d) for v in src for d in (0, 1, 2)} | src
    nums = {n for n in _numbers(answer) if not (2020 <= n <= 2040 and n.is_integer())}
    ungrounded = sorted(n for n in nums if n not in src_rounded)
    return {"numbers": len(nums), "ungrounded": ungrounded[:20]}


def _assess(answer: str, sources: str) -> dict:
    sections = [s for s in FR12_SECTIONS if s.lower() in answer.lower()]
    letters = [c for c in answer if c.isalpha()]
    cyr = round(sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / max(len(letters), 1), 2)
    grounding = _grounding(answer, sources)
    ok = {"Вывод", "Ключевые цифры"} <= set(sections) and not grounding["ungrounded"] and cyr >= 0.8
    return {"ok": ok, "sections": sections, "cyrillic_share": cyr, **grounding}


def _long_history(query: str, seed: int) -> tuple[list[BaseMessage], str]:
    grid_a = scenario_grid(seed, 220)
    grid_b = scenario_grid(seed + 1, 220)
    base = '{"result": "Базовые значения 2026: цена метанола 420 USD/т; DSCR 1.33; Debt/EBITDA 2.71; EBITDA 1265.9"}'
    calls = [
        (
            "run_scenarios",
            {"input_name": "цена метанола", "min_value": 250, "max_value": 797.5, "step": 2.5, "year": 2026},
            grid_a,
        ),
        (
            "run_scenarios",
            {"input_name": "цена метанола", "min_value": 250, "max_value": 797.5, "step": 2.5, "year": 2026},
            grid_b,
        ),
        ("get_metric", {"name": "DSCR", "year": 2026}, base),
    ]
    messages: list[BaseMessage] = [HumanMessage(query)]
    for i, (name, args, result) in enumerate(calls):
        messages.append(AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{i}"}]))
        messages.append(ToolMessage(result, tool_call_id=f"call_{i}", name=name))
    return messages, f"{query}\n{grid_a}\n{grid_b}\n{base}"


async def c10() -> dict:
    runs = []
    for seed, query in enumerate(LONG_QUERIES):
        history, sources = _long_history(query, seed * 10)
        run: dict = {"query": query}
        try:  # ADR-9: the agent answers in the long context, tools still bound
            bound = make_model().bind_tools([*BASE_TOOLS, run_scenarios])
            reply = await bound.ainvoke([SystemMessage(ANSWER_SYSTEM_PROMPT), *history])
            text = str(reply.content)
            run["agent"] = {
                "tool_calls_instead_of_answer": [tc["name"] for tc in reply.tool_calls],
                "input_tokens": (reply.usage_metadata or {}).get("input_tokens"),
                "answer": text,
                **_assess(text, sources),
            }
        except Exception as exc:
            run["agent"] = {"ok": False, **error_info(exc)}
        try:  # alternative: separate synthesizer call with clean context
            prompt = f"{query}\n\nРезультаты расчётов:\n{sources[len(query) + 1 :]}"
            reply = await make_model().ainvoke([SystemMessage(ANSWER_SYSTEM_PROMPT), HumanMessage(prompt)])
            text = str(reply.content)
            run["synthesizer"] = {
                "input_tokens": (reply.usage_metadata or {}).get("input_tokens"),
                "answer": text,
                **_assess(text, sources),
            }
        except Exception as exc:
            run["synthesizer"] = {"ok": False, **error_info(exc)}
        run["ok"] = run["agent"].get("ok", False)
        runs.append(run)
    synth_ok = sum(1 for r in runs if r["synthesizer"].get("ok"))
    return summarize("C10", runs, synthesizer_ok_rate=f"{synth_ok}/{len(runs)}")


CHECKS = {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5, "C6": c6, "C7": c7, "C8": c8, "C9": c9, "C10": c10}
