"""S6.2 (ADR-14, AR-Q17, FR-22, FR-24, FR-26), S6.4 (FR-23): TurnLocks, TurnStream, AgentV2.stream, full-stack turns."""

import asyncio
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from gigachat.exceptions import ForbiddenError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_api import STOP_BODY
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_resilience import raising, response_error

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import cancellation, resilience
from aigw_service.api.v2.prompts.middleware import CANCELLED_ANSWER, GROUNDING_PLACEHOLDER
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.stream import translator as translator_mod
from aigw_service.api.v2.stream import turn as stream_turn
from aigw_service.api.v2.stream.events import StreamEvent
from aigw_service.api.v2.stream.hygiene import violations
from aigw_service.api.v2.stream.labels import FAILURE_VIEW, RETRY_TEXT, step_summary
from aigw_service.api.v2.stream.turn import FAILED_TEXT, STREAM_MODE, STREAM_VERSION, TurnStream
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError, ToolSuccess, to_json
from aigw_service.api.v2.turn.lock import TurnBusyError, TurnLocks
from aigw_service.config import APP_CONFIG
from aigw_service.exceptions import StopEventError

QUESTION = "EBITDA 2026 при цене метанола 300–500?"
ANSWER_U2 = "EBITDA 2026 растёт с 1200 до 1501 при цене метанола от 300 до 500, то есть на 301."
TERMINAL = {"answer", "error"}
RETRY_ANSWER = "Сценарии посчитаны после повтора расчёта."


def ctx(session: str = "s1", **kw: Any) -> RequestContext:
    return RequestContext(user_id="u1", session_id=session, trace_id="t-1", **kw)


async def collect(stream: TurnStream) -> list[StreamEvent]:
    return [e async for e in stream]


def kinds(events: list[StreamEvent]) -> list[str]:
    return [e.event for e in events]


def assert_clean(events: list[StreamEvent]) -> None:
    found = [v for e in events for v in violations(e)]
    assert found == []


def assert_contract(events: list[StreamEvent]) -> None:
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].event == "turn.started"
    assert sum(e.event in TERMINAL for e in events) == 1
    assert events[-1].event in TERMINAL
    assert {e.trace_id for e in events} == {"t-1"}


# ---------------------------------------------------------------- TurnLocks.acquire


class TestAcquire:
    def test_busy_and_release(self) -> None:
        locks = TurnLocks()
        release = locks.acquire("u1:s1")
        with pytest.raises(TurnBusyError):
            locks.acquire("u1:s1")
        release()
        assert locks.busy == frozenset()

    def test_second_release_does_not_free_a_new_hold(self) -> None:
        locks = TurnLocks()
        first = locks.acquire("u1:s1")
        first()
        second = locks.acquire("u1:s1")
        first()  # stale release
        assert locks.busy == frozenset({"u1:s1"})
        second()
        assert locks.busy == frozenset()

    def test_hold_still_works(self) -> None:
        locks = TurnLocks()
        with locks.hold("u1:s1"):
            assert locks.busy == frozenset({"u1:s1"})
            with pytest.raises(TurnBusyError), locks.hold("u1:s1"):
                pass
        assert locks.busy == frozenset()


# ---------------------------------------------------------------- TurnStream on a fake agent


class FakeAgent:
    """``astream`` yields the given parts (a delay between them), then raises ``error`` if set."""

    def __init__(self, parts: list[dict], error: BaseException | None = None, delay: float = 0.0) -> None:
        self.parts, self.error, self.delay = parts, error, delay
        self.kwargs: dict[str, Any] = {}
        self.finished = False

    async def astream(self, agent_input: dict, **kwargs: Any) -> Any:
        self.kwargs = {"input": agent_input, **kwargs}
        for part in self.parts:
            await asyncio.sleep(self.delay)
            yield part
        if self.error is not None:
            raise self.error
        self.finished = True


def answer_part(text: str = "Ответ") -> dict:
    return {"type": "updates", "ns": (), "data": {"model": {"messages": [AIMessage(text, id="a1")]}}}


class TestTurnStream:
    async def test_answer(self) -> None:
        agent, locks = FakeAgent([answer_part()]), TurnLocks()
        context = ctx()
        events = await collect(TurnStream(agent, {"messages": []}, context, {"c": 1}, locks.acquire("u1:s1")))
        assert kinds(events) == ["turn.started", "phase", "answer"]
        assert events[0].session_id == "s1"  # type: ignore[attr-defined]
        assert_contract(events)
        assert agent.kwargs["stream_mode"] == STREAM_MODE
        assert agent.kwargs["version"] == STREAM_VERSION
        assert agent.kwargs["context"] is context
        assert agent.kwargs["config"] == {"c": 1}
        assert locks.busy == frozenset()

    async def test_stop_event(self) -> None:
        error = StopEventError(user_message="GigaChat временно недоступен", url="u")
        events = await collect(TurnStream(FakeAgent([answer_part()], error), {}, ctx(), {}, lambda: None))
        assert events[-1].event == "error"
        assert (events[-1].kind, events[-1].text) == ("unavailable", "GigaChat временно недоступен")  # type: ignore[attr-defined]
        assert_contract(events)

    async def test_unexpected_error(self) -> None:
        events = await collect(TurnStream(FakeAgent([], ValueError("{secret} boom")), {}, ctx(), {}, lambda: None))
        assert kinds(events) == ["turn.started", "error"]
        assert (events[-1].kind, events[-1].text) == ("failed", FAILED_TEXT)  # type: ignore[attr-defined]
        assert "secret" not in events[-1].model_dump_json()
        assert "ValueError" not in events[-1].model_dump_json()

    async def test_summary_right_before_answer(self) -> None:
        plan = {"type": "updates", "ns": (), "data": {"tools": {"todos": todos("completed", "skipped")}}}
        events = await collect(TurnStream(FakeAgent([plan, answer_part()]), {}, ctx(), {}, lambda: None))
        assert kinds(events)[-2:] == ["summary", "answer"]
        assert_contract(events)

    @pytest.mark.parametrize("error", [StopEventError(user_message="недоступен", url="u"), ValueError("boom")])
    async def test_error_has_no_summary(self, error: BaseException) -> None:
        plan = {"type": "updates", "ns": (), "data": {"tools": {"todos": todos("completed")}}}
        events = await collect(TurnStream(FakeAgent([plan, answer_part()], error), {}, ctx(), {}, lambda: None))
        assert events[-1].event == "error"
        assert "summary" not in kinds(events)

    async def test_no_answer_is_failed(self) -> None:
        events = await collect(TurnStream(FakeAgent([]), {}, ctx(), {}, lambda: None))
        assert events[-1].event == "error"
        assert events[-1].kind == "failed"

    async def test_consumer_leaves(self) -> None:
        agent, locks, context = FakeAgent([answer_part()] * 3, delay=0.05), TurnLocks(), ctx()
        stream = TurnStream(agent, {}, context, {}, locks.acquire("u1:s1"))
        events = stream.__aiter__()
        assert (await anext(events)).event == "turn.started"
        await events.aclose()
        assert context.cancel.is_set()
        assert not stream.task.done()
        assert locks.busy == frozenset({"u1:s1"})
        await stream.task
        assert agent.finished
        assert locks.busy == frozenset()

    async def test_aclose_after_the_end_does_not_cancel(self) -> None:
        context = ctx()
        stream = TurnStream(FakeAgent([answer_part()]), {}, context, {}, lambda: None)
        await collect(stream)
        await stream.task
        await stream.aclose()
        assert not context.cancel.is_set()

    async def test_task_is_kept_alive(self) -> None:
        stream = TurnStream(FakeAgent([answer_part()], delay=0.01), {}, ctx(), {}, lambda: None)
        assert stream.task in stream_turn._RUNNING  # pylint: disable=protected-access
        await stream.task
        await asyncio.sleep(0)
        assert stream.task not in stream_turn._RUNNING  # pylint: disable=protected-access


# ---------------------------------------------------------------- AgentV2.stream on a fake agent


class TestServiceStream:
    async def test_busy_before_the_first_event(self) -> None:
        locks = TurnLocks()
        first = AgentV2(FakeAgent([answer_part()], delay=0.05), locks).stream("q", ctx(), {})
        with pytest.raises(TurnBusyError):
            AgentV2(FakeAgent([answer_part()]), locks).stream("q", ctx(), {})
        events = await collect(first)
        assert events[-1].event == "answer"  # the running turn went on

    async def test_message_and_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "turn_timeout", 300)
        agent, context = FakeAgent([answer_part()]), ctx()
        before = time.monotonic()
        await collect(AgentV2(agent, TurnLocks()).stream("Вопрос", context, {}))
        assert context.deadline is not None
        assert before + 299 < context.deadline < time.monotonic() + 301
        (message,) = agent.kwargs["input"]["messages"]
        assert isinstance(message, HumanMessage)
        assert message.content == "Вопрос"

    async def test_given_deadline_is_kept(self) -> None:
        context = ctx(deadline=12.5)
        await collect(AgentV2(FakeAgent([answer_part()]), TurnLocks()).stream("q", context, {}))
        assert context.deadline == 12.5

    def test_lock_released_when_the_task_cannot_start(self) -> None:
        locks = TurnLocks()
        with pytest.raises(RuntimeError):  # no running event loop
            AgentV2(FakeAgent([]), locks).stream("q", ctx(), {})
        assert locks.busy == frozenset()


# ---------------------------------------------------------------- full stack on the scripted model


class SlowGate:
    """Engine gate with an awaitable delay (the inline gate runs the engine without yielding)."""

    def __init__(self, delay: float, errors: list[Exception | None] | None = None) -> None:
        self.delay, self.errors = delay, list(errors or [])
        self.runs = self.finished = 0

    async def run(self, _session_key: str, func: Any, /, **kwargs: Any) -> Any:
        self.runs += 1
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        await asyncio.sleep(self.delay)
        result = func(**kwargs)
        self.finished += 1
        return result


class Stack:
    """Builds a fresh agent per run (own store and upload dir) over the same script."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.runs = 0
        self.models: list[ScriptedChatModel] = []

    async def agent(
        self,
        steps: Callable[[], list],
        *,
        upload: bool = True,
        store: Any = None,
        checkpointer: Any = None,
        plan_mode: str | None = None,
        upload_dir: Path | None = None,
    ) -> Any:
        self.runs += 1
        if upload_dir is None:
            upload_dir = self.tmp_path / f"run{self.runs}"
            upload_dir.mkdir()
        if store is None:
            store = await uploaded_store("u1", INDEX, str(upload_dir)) if upload else InMemoryStore()
        model = ScriptedChatModel(steps=steps())
        self.models.append(model)
        factory = AgentFactory(
            model,
            default_catalog(),
            store=store,
            upload_dir=str(upload_dir),
            checkpointer=checkpointer,
            plan_mode=plan_mode,
        )
        return factory.build()

    async def both(self, steps: Callable[[], list], text: str = QUESTION, **kw: Any) -> tuple[list[StreamEvent], str]:
        """The same script through ``stream()`` and through ``answer()``: events and the ``answer()`` text."""
        events = await collect(AgentV2(await self.agent(steps, **kw), TurnLocks()).stream(text, ctx(), {}))
        expected = await AgentV2(await self.agent(steps, **kw), TurnLocks()).answer(text, ctx(), {})
        assert_contract(events)
        assert_clean(events)
        return events, expected


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


def todos(*statuses: str, reason: str | None = None) -> list[dict]:
    titles = ["Сопоставить показатели", "Посчитать сценарии", "Оценить изменение EBITDA"]
    steps = []
    for title, status in zip(titles, statuses, strict=False):
        step = {"content": title, "status": status}
        if status in ("blocked", "skipped"):
            step["reason"] = reason or "нет данных"
        steps.append(step)
    return steps


def scenarios(call_id: str, low: int = 300, high: int = 500, input_id: str = "IN1") -> AIMessage:
    return tool_call(
        "run_scenarios",
        call_id,
        inputs=[{"id": input_id, "min": low, "max": high, "step": 100}],
        outputs=["OUT0"],
        year=2026,
    )


def u2_steps(*answers: Any) -> list:
    return [
        tool_call("write_todos", "c1", todos=todos("in_progress", "pending", "pending")),
        tool_call(
            "resolve_terms",
            "c2",
            terms=[{"text": "цена метанола", "role": "input"}, {"text": "EBITDA", "role": "output"}],
        ),
        tool_call("write_todos", "c3", todos=todos("completed", "in_progress", "pending")),
        scenarios("c4"),
        tool_call("write_todos", "c5", todos=todos("completed", "completed", "in_progress")),
        tool_call(
            "derive",
            "c6",
            operation="difference",
            a={"value": 1200, "label": "EBITDA при 300"},
            b={"value": 1501, "label": "EBITDA при 500"},
        ),
        tool_call("write_todos", "c7", todos=todos("completed", "completed", "completed")),
        *(answers or [AIMessage(ANSWER_U2)]),
    ]


def flow(events: list[StreamEvent]) -> list[str]:
    """Event kinds with the phase spelled out: ``phase:planning``."""
    return [f"phase:{e.phase}" if e.event == "phase" else e.event for e in events]  # type: ignore[attr-defined]


def blocked_steps() -> list:
    """U5: a calculation, then a step that waits for the user."""
    return [
        tool_call("write_todos", "c1", todos=todos("in_progress", "pending")),
        scenarios("c2"),
        tool_call("write_todos", "c3", todos=todos("completed", "blocked", reason="не указан год")),
        AIMessage("Сценарии посчитаны. За какой год нужен второй расчёт?"),
    ]


NUMBER = re.compile(r"\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?")


def envelope_numbers(content: Any) -> set[str]:
    """Numbers of an envelope's ``result`` and ``numbers`` (FR-23: the only source of numbers in a summary)."""
    envelope = json.loads(content)
    found = set(NUMBER.findall(envelope.get("result", "")))
    for item in envelope.get("numbers", []):
        value = item["value"]
        found |= (
            {f"{value:g}", str(value), str(int(value))} if float(value).is_integer() else {f"{value:g}", str(value)}
        )
    return found


def text_of(events: list[StreamEvent]) -> str:
    return json.dumps([e.data() for e in events if e.event != "answer"], ensure_ascii=False)


class TestStreamScenarios:
    async def test_u2(self, stack: Stack, domain: list) -> None:
        events, expected = await stack.both(u2_steps)
        assert flow(events) == [
            "turn.started",
            "phase:planning",
            "plan",
            "phase:executing",
            "tool.started",
            "tool.finished",
            "plan",
            "tool.started",
            "tool.finished",
            "plan",
            "tool.started",
            "tool.finished",
            "plan",
            "phase:answering",
            "phase:checking",
            "summary",
            "answer",
        ]
        started = [e for e in events if e.event == "tool.started"]
        assert [(e.call, e.step) for e in started] == [(1, 1), (2, 2), (3, 3)]  # type: ignore[attr-defined]
        assert started[0].label == "Сопоставляю показатели: цена метанола, EBITDA"  # type: ignore[attr-defined]
        assert started[1].label == "Считаю сценарии: EBITDA при Метанол от 300 до 500"  # type: ignore[attr-defined]
        assert started[2].label == "Считаю разницу"  # type: ignore[attr-defined]
        finished = [e for e in events if e.event == "tool.finished"]
        assert {e.outcome for e in finished} == {"ok"}  # type: ignore[attr-defined]
        answer = events[-1]
        assert answer.text == expected == ANSWER_U2
        assert answer.outcome == "full"
        assert [s.status for s in events[-5].steps] == ["completed"] * 3  # type: ignore[attr-defined]
        assert all(e.summary for e in finished)  # type: ignore[attr-defined]
        summary = events[-2]
        assert (summary.completed, summary.skipped, summary.blocked) == (3, 0, 0)  # type: ignore[attr-defined]

    async def test_early_answer_with_open_step(self, stack: Stack, domain: list) -> None:
        def steps() -> list:
            return [
                tool_call("write_todos", "c1", todos=todos("in_progress")),
                AIMessage("Ранний ответ без расчёта."),
                scenarios("c2"),
                tool_call("write_todos", "c3", todos=todos("completed")),
                AIMessage("EBITDA 2026 — от 1200 до 1501."),
            ]

        events, expected = await stack.both(steps)
        assert "Ранний ответ" not in text_of(events)
        assert events[-1].text == expected == "EBITDA 2026 — от 1200 до 1501."  # type: ignore[attr-defined]
        assert "tool.started" in kinds(events)[kinds(events).index("phase") :]  # the turn went on

    async def test_invented_number_is_regenerated(self, stack: Stack, domain: list) -> None:
        events, expected = await stack.both(
            lambda: u2_steps(AIMessage("EBITDA 2026 достигает 9999."), AIMessage(ANSWER_U2))
        )
        assert "9999" not in text_of(events)
        assert events[-1].text == expected == ANSWER_U2  # type: ignore[attr-defined]

    async def test_invented_number_removed(self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "grounding_max_returns", 1)
        bad = "EBITDA 2026 достигает 9999."
        events, expected = await stack.both(lambda: u2_steps(AIMessage(bad), AIMessage(bad)))
        assert GROUNDING_PLACEHOLDER in events[-1].text  # type: ignore[attr-defined]
        assert events[-1].text == expected  # type: ignore[attr-defined]
        assert "9999" not in json.dumps([e.data() for e in events], ensure_ascii=False)

    async def test_no_workbook(self, stack: Stack) -> None:
        events, expected = await stack.both(list, upload=False)
        assert kinds(events) == ["turn.started", "answer"]
        assert events[-1].outcome == "refusal"
        assert events[-1].text == expected

    async def test_language_refusal(self, stack: Stack) -> None:
        events, expected = await stack.both(list, text="Wie hoch ist das EBITDA im Jahr 2026 bei diesem Preis?")
        assert kinds(events) == ["turn.started", "answer"]
        assert events[-1].outcome == "refusal"
        assert events[-1].text == expected

    async def test_limit(self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "max_model_calls", 2)
        events, expected = await stack.both(
            lambda: [
                tool_call("write_todos", "c1", todos=todos("in_progress", "pending")),
                AIMessage("Частичный ответ."),
            ]
        )
        assert events[-1].outcome == "partial"
        assert events[-1].text == expected
        assert expected.startswith("Частичный ответ.")

    async def test_timeout(self, stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cancellation, "_now", lambda: 200.0)
        steps = lambda: [AIMessage("Время вышло, ответ неполный.")]
        events = await collect(
            AgentV2(await stack.agent(steps), TurnLocks()).stream(QUESTION, ctx(deadline=100.0), {})
        )
        expected = await AgentV2(await stack.agent(steps), TurnLocks()).answer(QUESTION, ctx(deadline=100.0), {})
        assert_contract(events)
        assert events[-1].outcome == "partial"
        assert events[-1].text == expected

    async def test_blocked_step_is_clarify(self, stack: Stack) -> None:
        def steps() -> list:
            return [
                tool_call("write_todos", "c1", todos=todos("in_progress")),
                tool_call("get_values", "c2", ids=[]),
                tool_call("write_todos", "c3", todos=todos("blocked", reason="не указан показатель")),
                AIMessage("Уточните, какой показатель нужен?"),
            ]

        events, expected = await stack.both(steps)
        finished = next(e for e in events if e.event == "tool.finished")
        assert (finished.outcome, finished.text) == FAILURE_VIEW[ErrorCode.MISSING_INPUT]  # type: ignore[attr-defined]
        assert events[-1].outcome == "clarify"
        assert events[-1].text == expected

    async def test_tool_errors(self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch) -> None:
        def steps() -> list:
            return [
                tool_call("write_todos", "c1", todos=todos("in_progress")),
                scenarios("c2", input_id="IN99"),  # unknown_id
                scenarios("c3", 300, 300),  # ambiguous_term from the gate
                scenarios("c4", 400, 400),  # ok
                scenarios("c5", 400, 400),  # CalcCache hit
                scenarios("c6", 500, 500),  # engine_unavailable twice: the system retry is used up
                tool_call("write_todos", "c7", todos=todos("completed")),
                AIMessage("Часть сценариев посчитана, один шаг пропущен."),
            ]

        unavailable = ToolError(ErrorCode.ENGINE_UNAVAILABLE)
        gates: list[SlowGate] = []

        def new_gate() -> SlowGate:
            gates.append(SlowGate(0, [ToolError(ErrorCode.AMBIGUOUS_TERM), None, unavailable, unavailable]))
            return gates[-1]

        current: dict[str, SlowGate] = {}
        monkeypatch.setattr(calc_tools, "_gate", lambda: current.setdefault(str(stack.runs), new_gate()))
        events, expected = await stack.both(steps)
        finished = [e for e in events if e.event == "tool.finished"]
        outcomes = [e.outcome for e in finished]  # type: ignore[attr-defined]
        assert outcomes == ["retry", "retry", "ok", "ok", "retry", "failed"]  # S6.4: the system retry is visible
        assert [e.text for e in finished] == [  # type: ignore[attr-defined]
            FAILURE_VIEW[ErrorCode.UNKNOWN_ID][1],
            FAILURE_VIEW[ErrorCode.AMBIGUOUS_TERM][1],
            None,
            None,
            RETRY_TEXT[ErrorCode.ENGINE_UNAVAILABLE],
            FAILURE_VIEW[ErrorCode.ENGINE_UNAVAILABLE][1],
        ]
        assert gates[0].runs == 4  # the cache hit never reached the engine
        assert events[-1].text == expected  # type: ignore[attr-defined]

    async def test_stop_event(self, stack: Stack) -> None:
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            raising(response_error(ForbiddenError, 403, STOP_BODY)),
        ]
        locks = TurnLocks()
        events = await collect(AgentV2(await stack.agent(steps), locks).stream(QUESTION, ctx(), {}))
        assert_contract(events)
        assert events[-1].event == "error"
        assert events[-1].kind == "unavailable"
        assert "answer" not in kinds(events)
        assert locks.busy == frozenset()

    async def test_unexpected_error(self, stack: Stack) -> None:
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            raising(ValueError("{secret} boom")),
        ]
        locks = TurnLocks()
        events = await collect(AgentV2(await stack.agent(steps), locks).stream(QUESTION, ctx(), {}))
        assert_contract(events)
        assert_clean(events)
        assert events[-1].event == "error"
        assert events[-1].kind == "failed"
        assert "secret" not in events[-1].model_dump_json()
        assert locks.busy == frozenset()

    async def test_consumer_leaves_during_calculation(
        self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = SlowGate(0.5)
        monkeypatch.setattr(calc_tools, "_gate", lambda: gate)
        store, saver = await uploaded_store("u1", INDEX, str(stack.tmp_path)), InMemorySaver()
        config = {"configurable": {"thread_id": "u1:s1"}}
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            scenarios("c2"),
            AIMessage("Не дойдёт."),
        ]
        locks, context = TurnLocks(), ctx()
        service = AgentV2(await stack.agent(steps, store=store, checkpointer=saver, upload_dir=stack.tmp_path), locks)
        stream = service.stream(QUESTION, context, config)
        events = stream.__aiter__()
        async for event in events:
            if event.event == "tool.started":
                break
        assert event.event == "tool.started"
        while gate.runs == 0:  # tool.started comes before the tool runs: wait until the calculation is inside
            await asyncio.sleep(0.01)
        await events.aclose()
        assert context.cancel.is_set()
        with pytest.raises(TurnBusyError):
            locks.acquire("u1:s1")
        await stream.task
        assert gate.finished == 1
        assert len(stack.models[-1].calls) == 2
        assert locks.busy == frozenset()
        state = await service.agent.aget_state(config)
        assert state.values["messages"][-1].content == CANCELLED_ANSWER
        next_turn = AgentV2(
            await stack.agent(
                lambda: [AIMessage("Новый ход.")], store=store, checkpointer=saver, upload_dir=stack.tmp_path
            ),
            locks,
        )
        assert await next_turn.answer("Что дальше?", ctx(), config) == "Новый ход."

    async def test_slow_consumer(self, stack: Stack, domain: list) -> None:
        stream = AgentV2(await stack.agent(u2_steps), TurnLocks()).stream(QUESTION, ctx(), {})
        events: list[StreamEvent] = []
        done_at: int | None = None
        async for event in stream:
            events.append(event)
            if done_at is None and stream.task.done():
                done_at = len(events)
            await asyncio.sleep(0.05)
        assert done_at is not None
        assert done_at <= len(events) // 2
        assert_contract(events)
        assert events[-1].text == ANSWER_U2  # type: ignore[attr-defined]

    @pytest.mark.parametrize("steps", [u2_steps, blocked_steps], ids=["U2", "U5"])
    async def test_summary_numbers_from_own_envelope(self, stack: Stack, domain: list, steps: Any) -> None:
        saver, config = InMemorySaver(), {"configurable": {"thread_id": "u1:s1"}}
        service = AgentV2(await stack.agent(steps, checkpointer=saver), TurnLocks())
        events = await collect(service.stream(QUESTION, ctx(), config))
        messages = (await service.agent.aget_state(config)).values["messages"]
        call_ids = [
            c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls if c["name"] != "write_todos"
        ]
        contents = {m.tool_call_id: m.content for m in messages if isinstance(m, ToolMessage)}
        finished = [e for e in events if e.event == "tool.finished" and e.outcome == "ok"]  # type: ignore[attr-defined]
        assert finished
        for event in finished:
            own = envelope_numbers(contents[call_ids[event.call - 1]])  # type: ignore[attr-defined]
            assert event.summary  # type: ignore[attr-defined]
            assert set(NUMBER.findall(event.summary)) <= own  # type: ignore[attr-defined]
            assert len(event.summary) <= 300  # type: ignore[attr-defined]

    async def test_engine_retry_then_ok(self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resilience, "ENGINE_RETRY_DELAY", 0.0)
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            scenarios("c2"),
            tool_call("write_todos", "c3", todos=todos("completed")),
            AIMessage(RETRY_ANSWER),
        ]
        current: dict[str, SlowGate] = {}
        new_gate = lambda: SlowGate(0, [ToolError(ErrorCode.ENGINE_UNAVAILABLE)])
        monkeypatch.setattr(calc_tools, "_gate", lambda: current.setdefault(str(stack.runs), new_gate()))
        events, expected = await stack.both(steps)
        calls = [(e.event, e.call, getattr(e, "outcome", None)) for e in events if e.event.startswith("tool.")]
        assert calls == [
            ("tool.started", 1, None),
            ("tool.finished", 1, "retry"),
            ("tool.started", 1, None),
            ("tool.finished", 1, "ok"),
        ]
        retry = next(e for e in events if getattr(e, "outcome", None) == "retry")
        assert retry.text == RETRY_TEXT[ErrorCode.ENGINE_UNAVAILABLE]  # type: ignore[attr-defined]
        assert retry.summary is None  # type: ignore[attr-defined]
        assert events[-1].text == expected == RETRY_ANSWER  # type: ignore[attr-defined]

    async def test_engine_unavailable_twice_one_retry(
        self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resilience, "ENGINE_RETRY_DELAY", 0.0)
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            scenarios("c2"),
            tool_call("write_todos", "c3", todos=todos("skipped", reason="движок недоступен")),
            AIMessage("Расчёт не удался, шаг пропущен."),
        ]
        unavailable = ToolError(ErrorCode.ENGINE_UNAVAILABLE)
        current: dict[str, SlowGate] = {}
        new_gate = lambda: SlowGate(0, [unavailable, unavailable])
        monkeypatch.setattr(calc_tools, "_gate", lambda: current.setdefault(str(stack.runs), new_gate()))
        events, _ = await stack.both(steps)
        outcomes = [e.outcome for e in events if e.event == "tool.finished"]  # type: ignore[attr-defined]
        assert outcomes == ["retry", "failed"]
        assert events[-2].event == "summary"
        assert events[-2].skipped == 1  # type: ignore[attr-defined]

    async def test_phases_after_rejected_answer(self, stack: Stack, domain: list) -> None:
        def steps() -> list:
            return [
                tool_call("write_todos", "c1", todos=todos("in_progress")),
                AIMessage("Ранний ответ без расчёта."),
                scenarios("c2"),
                tool_call("write_todos", "c3", todos=todos("completed")),
                AIMessage("EBITDA 2026 — от 1200 до 1501."),
            ]

        events, _ = await stack.both(steps)
        phases = [f for f in flow(events) if f.startswith("phase:")]
        assert phases == [
            "phase:planning",
            "phase:executing",
            "phase:checking",
            "phase:executing",
            "phase:answering",
            "phase:checking",
        ]

    async def test_limit_gives_answering_and_summary(
        self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "max_model_calls", 2)
        events, _ = await stack.both(
            lambda: [
                tool_call("write_todos", "c1", todos=todos("in_progress", "pending")),
                AIMessage("Частичный ответ."),
            ]
        )
        phases = [f for f in flow(events) if f.startswith("phase:")]
        assert phases[-2:] == ["phase:answering", "phase:checking"]
        assert flow(events)[-2:] == ["summary", "answer"]  # the plan guard closes open steps after the answer
        assert events[-1].outcome == "partial"  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "kw", [{"upload": False}, {"text": "Wie hoch ist das EBITDA im Jahr 2026 bei diesem Preis?"}]
    )
    async def test_refusal_has_no_phase_or_summary(self, stack: Stack, kw: dict) -> None:
        events, _ = await stack.both(list, **kw)
        assert kinds(events) == ["turn.started", "answer"]

    async def test_same_number_of_llm_calls(self, stack: Stack, domain: list) -> None:
        await stack.both(u2_steps)
        streamed, plain = stack.models[-2], stack.models[-1]
        assert len(streamed.calls) == len(plain.calls) > 0

    async def test_summaries_are_clean(self, stack: Stack, domain: list, monkeypatch: pytest.MonkeyPatch) -> None:
        dirty = ToolSuccess(result="Посчитано для IN1 и OUT0 через run_scenarios, полный результат /results/c2.json")
        dirty_envelope = json.loads(to_json(dirty))
        monkeypatch.setattr(translator_mod, "step_summary", lambda env: step_summary(dirty_envelope) if env else None)
        events, _ = await stack.both(u2_steps)
        summaries = [e.summary for e in events if e.event == "tool.finished"]  # type: ignore[attr-defined]
        assert summaries
        assert all(s and "IN1" not in s and "/results/" not in s and "run_scenarios" not in s for s in summaries)
