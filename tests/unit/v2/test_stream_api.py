"""S6.3 (FR-21, FR-25, FR-26, AR-Q16, AR-Q17): ``/invoke-agent/stream`` over HTTP.

ASGI tests (``httpx.ASGITransport`` returns the body only when the app is done) check frames, order and
outcomes. Delivery as the turn goes, the heartbeat and a client that goes away mid-calculation are checked on a
live uvicorn started in the test's own event loop.
"""

import asyncio
import json
import os
import socket
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from gigachat.exceptions import ForbiddenError
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from tests.unit.test_log_requests import RecordingLogger
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_api import STOP_BODY, Api, answer_text, v2
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_resilience import raising, response_error
from tests.unit.v2.test_stream_turn import ANSWER_U2, QUESTION, scenarios, todos, u2_steps
from tests.unit.v2.test_turn_context import HEADERS

from aigw_service.api.v2.middleware import cancellation
from aigw_service.api.v2.prompts.middleware import CANCELLED_ANSWER
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.stream import sse
from aigw_service.api.v2.stream.events import EVENT_TYPES, StreamEvent
from aigw_service.api.v2.stream.hygiene import violations
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.turn.lock import TURN_BUSY_MESSAGE, TurnLocks
from aigw_service.context import APP_CTX

SESSION = "9b2f6c1e-1d2a-4b3c-8d4e-5f6a7b8c9d0e"
STREAM = "/api/v1/invoke-agent/stream"
TERMINAL = {"answer", "error"}
PING = ": ping"


def headers_for(user: str = "u1", session: str | None = SESSION) -> dict[str, str]:
    return {**HEADERS, "x-user-id": user} | ({"x-session-id": session} if session else {})


def parse_sse(body: str) -> tuple[list[StreamEvent], int]:
    """Events of a whole SSE body and the number of ``: ping`` comments; checks ``id`` = ``seq``."""
    events, pings = [], 0
    for frame in body.split("\n\n"):
        if not frame:
            continue
        if frame == PING:
            pings += 1
            continue
        fields = dict(line.split(": ", 1) for line in frame.split("\n"))
        assert set(fields) == {"event", "id", "data"}
        data = json.loads(fields["data"])
        assert int(fields["id"]) == data["seq"]
        events.append(EVENT_TYPES[fields["event"]](**data))
    return events, pings


def kinds(events: list[StreamEvent]) -> list[str]:
    return [e.event for e in events]


def assert_stream_contract(events: list[StreamEvent]) -> None:
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].event == "turn.started"
    assert sum(e.event in TERMINAL for e in events) == 1
    assert events[-1].event in TERMINAL
    assert {e.trace_id for e in events} == {HEADERS["x-trace-id"]}
    assert [v for e in events for v in violations(e)] == []


class StreamApi(Api):
    async def stream(self, text: str = QUESTION, user: str = "u1", session: str | None = SESSION) -> Any:
        return await self.client.post(STREAM, json={"message": text}, headers=headers_for(user, session))


async def fresh(steps: Callable[[], list], tmp_path: Path, name: str, **kw: Any) -> tuple[AgentV2, ScriptedChatModel]:
    """A new agent over the script, with its own upload dir and store."""
    upload_dir = tmp_path / name
    upload_dir.mkdir()
    store = kw.pop("store", None) or await uploaded_store("u1", INDEX, str(upload_dir))
    model = ScriptedChatModel(steps=steps())
    return v2(model, store, upload_dir, **kw), model


async def stream_and_zip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, steps: Callable[[], list], **kw: Any
) -> tuple[list[StreamEvent], str]:
    """The same script through ``/invoke-agent/stream`` and ``/invoke-agent``: events and ``txt_response.txt``."""
    agent, _ = await fresh(steps, tmp_path, "stream", **kw)
    response = await StreamApi(monkeypatch, agent).stream()
    assert response.status_code == 200
    events, _ = parse_sse(response.text)
    assert_stream_contract(events)
    agent, _ = await fresh(steps, tmp_path, "zip", **kw)
    zipped = await StreamApi(monkeypatch, agent).ask(QUESTION, session=SESSION)
    return events, answer_text(zipped)


def text_without_answer(events: list[StreamEvent]) -> str:
    return json.dumps([e.data() for e in events if e.event != "answer"], ensure_ascii=False)


class FakeV1:
    """What ``get_agent`` returns at ``AGENT_ENGINE=v1``: a graph with ``ainvoke``."""

    def __init__(self, text: str = "Ответ v1") -> None:
        self.graph = self
        self.text = text
        self.inputs: list[dict] = []

    async def ainvoke(self, state: dict, config: dict) -> dict:
        self.inputs.append({"state": state, "config": config})
        return {"messages": [AIMessage(self.text)]}


# ---------------------------------------------------------------- ASGI


class TestStreamContract:
    """FR-21: endpoint, frames, the same text as the ZIP, errors before the stream, v1."""

    async def test_frames_and_same_text_as_zip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: list
    ) -> None:
        agent, _ = await fresh(u2_steps, tmp_path, "stream")
        response = await StreamApi(monkeypatch, agent).stream()
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        events, _ = parse_sse(response.text)
        assert_stream_contract(events)
        assert kinds(events)[:5] == ["turn.started", "phase", "plan", "phase", "tool.started"]  # S6.4: phases
        assert kinds(events)[-2:] == ["summary", "answer"]
        assert events[0].session_id == SESSION  # type: ignore[attr-defined]
        agent, _ = await fresh(u2_steps, tmp_path, "zip")
        zipped = await StreamApi(monkeypatch, agent).ask(QUESTION, session=SESSION)
        assert events[-1].event == "answer"
        assert events[-1].text == answer_text(zipped) == ANSWER_U2  # type: ignore[attr-defined]
        assert events[-1].outcome == "full"  # type: ignore[attr-defined]

    async def test_bad_header_is_422(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        agent, model = await fresh(lambda: [AIMessage("Привет!")], tmp_path, "a")
        api = StreamApi(monkeypatch, agent)
        response = await api.client.post(
            STREAM, json={"message": "Привет"}, headers={**headers_for(), "x-client-id": "x"}
        )
        assert response.status_code == 422
        assert not response.headers["content-type"].startswith("text/event-stream")
        assert model.calls == []

    async def test_no_workbook(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        agent, model = await fresh(list, tmp_path, "a", store=InMemoryStore())
        events, _ = parse_sse((await StreamApi(monkeypatch, agent).stream()).text)
        assert kinds(events) == ["turn.started", "answer"]
        assert events[-1].outcome == "refusal"  # type: ignore[attr-defined]
        assert model.calls == []

    async def test_v1_one_shot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        graph = FakeV1()
        api = StreamApi(monkeypatch, graph)  # type: ignore[arg-type]
        response = await api.stream("Привет")
        events, _ = parse_sse(response.text)
        assert_stream_contract(events)
        assert [(e.event, getattr(e, "outcome", None)) for e in events] == [("turn.started", None), ("answer", "full")]
        assert events[-1].text == "Ответ v1"  # type: ignore[attr-defined]
        assert graph.inputs[0]["config"]["metadata"]["endpoint"] == "/invoke-agent/stream"

    async def test_failure_before_the_stream_is_424(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Broken(AgentV2):
            def stream(self, *_a: Any, **_kw: Any) -> Any:
                raise RuntimeError("no event loop for the turn")

        response = await StreamApi(monkeypatch, Broken()).stream()
        assert response.status_code == 424
        assert response.json()["error_description"] == "no event loop for the turn"

    async def test_openapi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = StreamApi(monkeypatch, FakeV1())  # type: ignore[arg-type]
        schema = (await api.client.get("/openapi.json")).json()
        post = schema["paths"][STREAM]["post"]
        assert "text/event-stream" in post["responses"]["200"]["content"]
        assert {"409", "422", "424"} <= set(post["responses"])


class TestStreamOutcomes:
    """FR-25, FR-26: 409 before the stream, StopEvent, timeout, rejected answers stay out of the stream."""

    async def test_busy_session_is_409_before_the_stream(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: list
    ) -> None:
        gate = TimedGate(0.5)
        monkeypatch.setattr(calc_tools, "_gate", lambda: gate)
        locks = TurnLocks()
        steps = lambda: [scenarios("c1"), AIMessage("Сценарии посчитаны.")]
        agent, _ = await fresh(steps, tmp_path, "a", locks=locks, plan_mode="off")
        api = StreamApi(monkeypatch, agent)
        first = asyncio.ensure_future(api.stream())
        while not locks.busy:
            await asyncio.sleep(0.01)
        second = await api.stream()
        assert second.status_code == 409
        assert second.json()["error_description"] == TURN_BUSY_MESSAGE
        events, _ = parse_sse((await first).text)
        assert events[-1].event == "answer"

    async def test_stop_event_is_an_error_event(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            raising(response_error(ForbiddenError, 403, STOP_BODY)),
        ]
        agent, _ = await fresh(steps, tmp_path, "a")
        response = await StreamApi(monkeypatch, agent).stream()
        assert response.status_code == 200
        events, _ = parse_sse(response.text)
        assert_stream_contract(events)
        assert events[-1].event == "error"
        assert events[-1].kind == "unavailable"  # type: ignore[attr-defined]
        assert "answer" not in kinds(events)

    async def test_timeout_is_partial(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: list) -> None:
        def engine_marks_timeout(**_kw: Any) -> Any:
            monkeypatch.setattr(cancellation, "_now", lambda: float("inf"))
            return domain_engine(**_kw)

        domain_engine = calc_tools.analyze_excel_model
        monkeypatch.setattr(calc_tools, "analyze_excel_model", engine_marks_timeout)
        steps = lambda: [scenarios("c1"), AIMessage("Частичный ответ.")]
        agent, _ = await fresh(steps, tmp_path, "a", plan_mode="off")
        events, _ = parse_sse((await StreamApi(monkeypatch, agent).stream()).text)
        assert_stream_contract(events)
        assert events[-1].outcome == "partial"  # type: ignore[attr-defined]
        assert events[-1].text == "Частичный ответ."  # type: ignore[attr-defined]

    async def test_early_answer_with_open_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: list
    ) -> None:
        def steps() -> list:
            return [
                tool_call("write_todos", "c1", todos=todos("in_progress")),
                AIMessage("Ранний ответ без расчёта."),
                scenarios("c2"),
                tool_call("write_todos", "c3", todos=todos("completed")),
                AIMessage("EBITDA 2026 — от 1200 до 1501."),
            ]

        events, zipped = await stream_and_zip(monkeypatch, tmp_path, steps)
        assert "Ранний ответ" not in text_without_answer(events)
        assert events[-1].text == zipped == "EBITDA 2026 — от 1200 до 1501."  # type: ignore[attr-defined]

    async def test_invented_number_is_regenerated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: list
    ) -> None:
        events, zipped = await stream_and_zip(
            monkeypatch, tmp_path, lambda: u2_steps(AIMessage("EBITDA 2026 достигает 9999."), AIMessage(ANSWER_U2))
        )
        assert "9999" not in json.dumps([e.data() for e in events], ensure_ascii=False)
        assert events[-1].text == zipped == ANSWER_U2  # type: ignore[attr-defined]


# ---------------------------------------------------------------- live uvicorn


class TimedGate:
    """Engine gate with an awaitable calculation; optionally the calculation runs a child process (like soffice)."""

    def __init__(self, delay: float, child_s: float = 0.0) -> None:
        self.delay, self.child_s = delay, child_s
        self.started: list[float] = []
        self.finished: list[float] = []
        self.pids: list[int] = []

    async def run(self, _session_key: str, func: Any, /, **kwargs: Any) -> Any:
        self.started.append(time.monotonic())
        if self.child_s:
            child = await asyncio.create_subprocess_exec("sleep", str(self.child_s))
            self.pids.append(child.pid)
            await child.wait()
        await asyncio.sleep(self.delay)
        result = func(**kwargs)
        self.finished.append(time.monotonic())
        return result


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def live() -> AsyncIterator[str]:
    """``app_main`` on a real uvicorn in this event loop; dependency overrides come from ``StreamApi``."""
    from aigw_service.api import app_main  # pylint: disable=import-outside-toplevel

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app_main, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    rec = RecordingLogger()
    monkeypatch.setattr(APP_CTX, "get_logger", lambda: rec)
    return rec


async def read_live(
    base: str, stop_after: str | None = None, until: Callable[[], bool] = lambda: True
) -> list[tuple[float, str]]:
    """Frames (and pings) with the time they arrived; leaves after the frame of type ``stop_after`` once ``until()``.

    ``tool.started`` comes from the model update, before the tool runs: a client leaving right then stops the
    turn before the calculation starts, so ``until`` lets the test leave in the middle of it.
    """
    t0 = time.monotonic()
    got: list[tuple[float, str]] = []
    buffer = ""
    async with (
        httpx.AsyncClient(base_url=base, timeout=30) as client,
        client.stream("POST", STREAM, json={"message": QUESTION}, headers=headers_for()) as response,
    ):
        assert response.status_code == 200
        async for text in response.aiter_text():
            buffer += text
            *frames, buffer = buffer.split("\n\n")
            for frame in frames:
                got.append((time.monotonic() - t0, frame))
                if stop_after and frame.startswith(f"event: {stop_after}\n"):
                    while not until():
                        await asyncio.sleep(0.01)
                    return got
    return got


def frame_kind(frame: str) -> str:
    return PING if frame == PING else frame.split("\n", 1)[0][len("event: ") :]


async def wait_free(locks: TurnLocks, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while locks.busy and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert locks.busy == frozenset()


class TestLiveStream:
    """AR-Q16, AR-Q17 on a live uvicorn behind the service's ``log_requests``."""

    async def test_events_arrive_as_the_turn_goes(  # pylint: disable=too-many-locals
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        domain: list,
        live: str,
        recorder: RecordingLogger,
    ) -> None:
        gate = TimedGate(1.5)
        monkeypatch.setattr(calc_tools, "_gate", lambda: gate)
        monkeypatch.setattr(sse, "HEARTBEAT_S", 0.2)
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            scenarios("c2"),
            tool_call("write_todos", "c3", todos=todos("completed")),
            AIMessage("Сценарии посчитаны."),
        ]
        agent, _ = await fresh(steps, tmp_path, "a")
        StreamApi(monkeypatch, agent)
        t0 = time.monotonic()
        got = await read_live(live)
        calc_end = gate.finished[0] - t0
        times = {frame_kind(f): t for t, f in reversed(got)}  # first arrival of each kind
        assert times["turn.started"] <= 1.0
        assert times["turn.started"] < calc_end
        assert times["tool.started"] < calc_end  # not one piece at the end of the turn
        pings = [t for t, f in got if f == PING]
        assert pings
        assert min(pings) < calc_end
        assert frame_kind(got[-1][1]) == "answer"  # nothing after the terminal frame
        finished = [r for r in recorder.records if r[:2] == ("audit", "BusinessRequestFinished")]
        assert len(finished) == 1
        assert finished[0][2]["text"] == "Сценарии посчитаны."
        assert recorder.durations[-1] >= 1500  # the whole stream

    async def test_disconnect_mid_calc(  # pylint: disable=too-many-locals
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        domain: list,
        live: str,
        recorder: RecordingLogger,
    ) -> None:
        gate = TimedGate(0.5, child_s=1.0)
        monkeypatch.setattr(calc_tools, "_gate", lambda: gate)
        steps = lambda: [
            tool_call("write_todos", "c1", todos=todos("in_progress")),
            scenarios("c2"),
            tool_call("write_todos", "c3", todos=todos("completed")),
            AIMessage("Не должен прозвучать."),
            AIMessage("Привет!"),
        ]
        locks = TurnLocks()
        agent, model = await fresh(steps, tmp_path, "a", locks=locks, checkpointer=InMemorySaver())
        StreamApi(monkeypatch, agent)
        got = await read_live(live, stop_after="tool.started", until=lambda: bool(gate.pids))
        assert frame_kind(got[-1][1]) == "tool.started"
        calls_at_disconnect = len(model.calls)
        async with httpx.AsyncClient(base_url=live, timeout=30) as client:
            busy = await client.post(STREAM, json={"message": "Ещё вопрос"}, headers=headers_for())
            assert busy.status_code == 409  # the turn is finishing: the lock lives as long as the turn
            await wait_free(locks)
            assert len(gate.finished) == 1  # the calculation was not cut
            assert gate.pids
            assert not alive(gate.pids[0])
            assert len(model.calls) == calls_at_disconnect  # no model call after the disconnect
            assert any(r == ("audit", "BusinessRequestFinished", {}, HEADERS["x-trace-id"]) for r in recorder.records)
            nxt = await client.post(STREAM, json={"message": "Привет"}, headers=headers_for())
        events, _ = parse_sse(nxt.text)
        assert events[-1].event == "answer"
        seen = [m.content for m in model.calls[-1]["messages"]]
        assert QUESTION in seen  # the next turn of the session sees its history
        assert CANCELLED_ANSWER in seen  # the stopped turn ended with its own answer, checkpoint intact
