"""S2.8 (AR-Q10, AR-Q12, FR-8, AR-Q5): turn deadline, client disconnect, a consistent checkpoint afterwards."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import cancellation
from aigw_service.api.v2.middleware.limits import limit_hit, turn_timeout
from aigw_service.api.v2.middleware.plan import NOT_DONE
from aigw_service.api.v2.prompts.middleware import (
    CANCELLED_ANSWER,
    TIMEOUT_FALLBACK,
    TIMEOUT_PROMPT,
    TIMEOUT_REASON,
)
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.turn.disconnect import disconnect_watch, watch_disconnect
from aigw_service.api.v2.turn.lock import TurnLocks
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent.config import AgentSettings
from excel_engine import ExcelAnalysisToolResult

DEADLINE = 100.0
LATE = 200.0
EBITDA = 1234.5
PLAN = [{"content": "Посчитать сценарии", "status": "in_progress"}]


def scenarios(call_id: str, low: int = 300) -> AIMessage:
    return tool_call(
        "run_scenarios",
        call_id,
        inputs=[{"id": "IN1", "min": low, "max": low, "step": 1}],
        outputs=["OUT0"],
        year=2026,
    )


class Clock:
    """The monotonic clock Cancellation reads; tests move it instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def late(self) -> None:
        self.now = LATE


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(cancellation, "_now", lambda: fake.now)
    return fake


class Turn:
    """One session over one store and checkpointer; ``during_calc`` runs inside the engine call."""

    def __init__(self, tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path, self.store, self.saver = tmp_path, store, InMemorySaver()
        self.engine: list[dict] = []
        self.during_calc: Any = None
        self.context: RequestContext | None = None
        monkeypatch.setattr(calc_tools, "analyze_excel_model", self._run)

    def _run(self, **kw: Any) -> ExcelAnalysisToolResult:
        self.engine.append(kw)
        if self.during_calc is not None:
            self.during_calc()
        frame = pd.DataFrame([{kw["input_names"][0]: kw["ranges"][0][0], "EBITDA": EBITDA}])
        return ExcelAnalysisToolResult(status="OK", result="1 сценарий", content={"result_df": frame})

    async def __call__(self, steps: list, mode: str = "off", text: str = "EBITDA 2026 при 300?") -> tuple[dict, Any]:
        model = ScriptedChatModel(steps=steps)
        agent = AgentFactory(
            model,
            default_catalog(),
            store=self.store,
            upload_dir=str(self.tmp_path),
            plan_mode=mode,
            checkpointer=self.saver,
        ).build()
        self.context = RequestContext(user_id="u1", session_id="s1", deadline=DEADLINE)
        out = await agent.ainvoke(
            {"messages": [HumanMessage(text)]},
            config={"configurable": {"thread_id": "u1:s1"}},
            context=self.context,
        )
        return out, model


@pytest.fixture
async def turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline_gate: Any) -> Turn:
    return Turn(tmp_path, await uploaded_store("u1", INDEX, str(tmp_path)), monkeypatch)


def quote_numbers(messages: list[BaseMessage]) -> AIMessage:
    numbers = [
        n["value"]
        for m in messages
        if isinstance(m, ToolMessage) and m.name == "run_scenarios"
        for n in json.loads(m.content).get("numbers", [])
    ]
    return AIMessage(f"Частичный ответ: EBITDA 2026 — {numbers[0]} млн руб.; время хода истекло.")


def codes(out: dict) -> list[str]:
    return [
        json.loads(m.content).get("code") or "ok"
        for m in out["messages"]
        if isinstance(m, ToolMessage) and m.name != "write_todos"
    ]


def unanswered_calls(out: dict) -> list[str]:
    answered = {m.tool_call_id for m in out["messages"] if isinstance(m, ToolMessage)}
    return [
        c["id"] for m in out["messages"] if isinstance(m, AIMessage) for c in m.tool_calls if c["id"] not in answered
    ]


class TestSettings:
    """AR-Q5 «Недопустимое время хода», AR-Q10 «Таймаут из настроек»."""

    def test_default(self) -> None:
        assert AgentSettings().turn_timeout == 300.0

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_TURN_TIMEOUT", "45")
        assert AgentSettings().turn_timeout == 45.0

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_not_positive_is_rejected(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("AGENT_TURN_TIMEOUT", value)
        with pytest.raises(ValidationError, match="greater than 0"):
            AgentSettings()


class StubAgent:
    def __init__(self) -> None:
        self.context: RequestContext | None = None

    async def ainvoke(self, _input: Any, config: Any, context: RequestContext) -> dict:
        self.context = context
        return {"messages": [AIMessage("ок")]}


class TestDeadline:
    """Design D1: AgentV2.answer sets the turn deadline from AGENT_TURN_TIMEOUT."""

    async def test_answer_sets_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "turn_timeout", 30.0)
        stub = StubAgent()
        before = cancellation.time.monotonic()
        await AgentV2(stub, locks=TurnLocks()).answer("x", RequestContext(user_id="u1", session_id="s1"), {})
        assert stub.context is not None
        assert before + 30.0 <= stub.context.deadline <= cancellation.time.monotonic() + 30.0

    async def test_given_deadline_is_kept(self) -> None:
        stub = StubAgent()
        await AgentV2(stub, locks=TurnLocks()).answer(
            "x", RequestContext(user_id="u1", session_id="s1", deadline=5.0), {}
        )
        assert stub.context is not None
        assert stub.context.deadline == 5.0

    def test_no_deadline_never_expires(self) -> None:
        assert not cancellation._expired(RequestContext(user_id="u1", session_id="s1"))  # pylint: disable=protected-access


class TestPrompts:
    @pytest.mark.parametrize("text", [TIMEOUT_PROMPT, TIMEOUT_FALLBACK, TIMEOUT_REASON, CANCELLED_ANSWER])
    def test_russian(self, text: str) -> None:
        assert re.search("[а-яё]", text, re.IGNORECASE)
        assert not re.search("[a-z]", text, re.IGNORECASE)


class TestTimeout:
    """AR-Q10 «Таймаут хода», FR-8 «Истекло время хода»."""

    async def test_timeout_gives_partial_answer(self, turn: Turn, clock: Clock) -> None:
        def late_tool_call(_messages: list[BaseMessage]) -> AIMessage:
            clock.late()  # the deadline passes while the model is thinking
            return scenarios("c2", 400)

        out, model = await turn([scenarios("c1"), late_tool_call, quote_numbers])
        assert codes(out) == ["ok", "cancelled"]
        assert len(turn.engine) == 1  # «Расчёт после дедлайна не начинается»
        last = model.calls[-1]
        assert last["tools"] == []
        assert last["messages"][-1].content == TIMEOUT_PROMPT
        assert str(EBITDA) in out["messages"][-1].content
        assert turn_timeout(out)

    async def test_running_calculation_is_finished(self, turn: Turn, clock: Clock) -> None:
        turn.during_calc = clock.late  # «Идущий расчёт не прерывается»
        out, model = await turn([scenarios("c1"), quote_numbers])
        assert codes(out) == ["ok"]
        assert model.calls[-1]["tools"] == []
        assert str(EBITDA) in out["messages"][-1].content

    async def test_plan_steps_get_timeout_reason(self, turn: Turn, clock: Clock) -> None:
        turn.during_calc = clock.late
        steps = [tool_call("write_todos", "p", todos=PLAN), scenarios("c1"), quote_numbers]
        out, _model = await turn(steps, mode="auto")
        assert f"**{NOT_DONE}:**" in out["messages"][-1].content
        assert out["todos"][0]["status"] == "skipped"
        assert out["todos"][0]["reason"] == TIMEOUT_REASON

    async def test_empty_last_answer_gets_timeout_text(self, turn: Turn, clock: Clock) -> None:
        turn.during_calc = clock.late
        out, _model = await turn([scenarios("c1"), scenarios("c2", 400)])
        assert out["messages"][-1].content == TIMEOUT_FALLBACK


class TestDisconnect:
    """AR-Q12 «Разрыв соединения», «Идущий расчёт не прерывается»."""

    async def test_disconnect_during_calculation(self, turn: Turn, clock: Clock) -> None:
        turn.during_calc = lambda: turn.context.cancel.set()  # type: ignore[union-attr]
        out, model = await turn([scenarios("c1"), scenarios("c2", 400), AIMessage("не дойдёт")])
        assert len(model.calls) == 1  # no model call after the disconnect
        assert len(turn.engine) == 1
        assert codes(out) == ["ok"]  # the running calculation reached the history
        assert out["messages"][-1].content == CANCELLED_ANSWER

    async def test_disconnect_stops_tools(self, turn: Turn, clock: Clock) -> None:
        def disconnect(_messages: list[BaseMessage]) -> AIMessage:
            turn.context.cancel.set()  # type: ignore[union-attr]
            return scenarios("c1")

        out, model = await turn([disconnect, AIMessage("не дойдёт")])
        assert codes(out) == ["cancelled"]
        assert turn.engine == []
        assert len(model.calls) == 1


class TestCheckpoint:
    """AR-Q12 «Чекпоинт цел»."""

    async def test_next_turn_after_disconnect(self, turn: Turn, clock: Clock) -> None:
        turn.during_calc = lambda: turn.context.cancel.set()  # type: ignore[union-attr]
        first, _ = await turn([scenarios("c1"), AIMessage("не дойдёт")])
        assert first["messages"][-1].content == CANCELLED_ANSWER
        turn.during_calc = None
        second, _ = await turn([scenarios("c2", 400), AIMessage("Готово.")], text="А при 400?")
        assert unanswered_calls(second) == []
        assert second["messages"][-1].content == "Готово."
        assert codes(second)[-1] == "ok"

    async def test_next_turn_after_timeout(self, turn: Turn, clock: Clock) -> None:
        turn.during_calc = clock.late
        first, _ = await turn([scenarios("c1"), quote_numbers])
        assert limit_hit(first)
        clock.now = 0.0
        turn.during_calc = None
        second, _ = await turn([scenarios("c2", 400), AIMessage("Готово.")], text="А при 400?")
        assert unanswered_calls(second) == []
        assert not limit_hit(second)
        assert second["messages"][-1].content == "Готово."


class FakeRequest:
    """Starlette request stand-in: ``receive`` yields leftover body messages, then a disconnect (or never)."""

    def __init__(self, before: int, disconnects: bool = True) -> None:
        self.calls, self.before, self.disconnects = 0, before, disconnects

    async def _receive(self) -> dict:
        self.calls += 1
        if self.calls <= self.before:
            return {"type": "http.request", "body": b"", "more_body": False}
        if not self.disconnects:
            await asyncio.Event().wait()  # the client stays
        return {"type": "http.disconnect"}

    @property
    def receive(self) -> Any:
        return self._receive


class TestWatcher:
    """Design D2: waiting on receive sets the cancel flag; the watcher never outlives the turn."""

    async def test_sets_flag_on_disconnect(self) -> None:
        event = asyncio.Event()
        request = FakeRequest(before=2)
        await asyncio.wait_for(watch_disconnect(request, event), timeout=1)
        assert event.is_set()
        assert request.calls == 3

    async def test_watch_is_cancelled_after_the_turn(self) -> None:
        event = asyncio.Event()
        async with disconnect_watch(FakeRequest(before=0, disconnects=False), event):
            await asyncio.sleep(0.01)
        assert not event.is_set()
        assert all(t.get_coro().__name__ != "watch_disconnect" for t in asyncio.all_tasks())  # type: ignore[union-attr]

    async def test_flag_set_during_the_turn(self) -> None:
        event = asyncio.Event()
        async with disconnect_watch(FakeRequest(before=1), event):
            await asyncio.wait_for(event.wait(), timeout=1)
        assert event.is_set()
