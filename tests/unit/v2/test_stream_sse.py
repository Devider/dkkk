"""S6.3 (AR-Q16, AR-Q17, FR-21, FR-25): SSE frames, heartbeat without stopping the turn, the v1 one-shot stream."""

import asyncio
import json
import time
from typing import Any

import pytest
from tests.unit.v2.test_stream_observe import Logger, stream_lines
from tests.unit.v2.test_stream_turn import FakeAgent, answer_part, ctx

from aigw_service.api.v2.stream import sse
from aigw_service.api.v2.stream.events import EventFactory
from aigw_service.api.v2.stream.observe import StreamStats
from aigw_service.api.v2.stream.sse import PING, encode, one_shot, sse_body
from aigw_service.api.v2.stream.turn import FAILED_TEXT, TurnStream
from aigw_service.config.agent.config import AgentSettings
from aigw_service.context import APP_CTX
from aigw_service.exceptions import StopEventError


def parse(frame: bytes) -> tuple[str, int, dict[str, Any]]:
    text = frame.decode()
    assert text.endswith("\n\n")
    lines = text[:-2].split("\n")
    assert [line.split(":", 1)[0] for line in lines] == ["event", "id", "data"]
    return lines[0][len("event: ") :], int(lines[1][len("id: ") :]), json.loads(lines[2][len("data: ") :])


class TestEncode:
    def test_frame(self) -> None:
        factory = EventFactory("t-1")
        factory.make({"event": "turn.started", "session_id": "s1"})
        event = factory.make({"event": "answer", "text": "Строка 1\nСтрока 2", "outcome": "full"})
        frame = encode(event)
        assert "\\u" not in frame.decode()
        kind, seq, data = parse(frame)
        assert (kind, seq) == ("answer", 2)
        assert data == {
            "v": 1,
            "seq": 2,
            "ts": event.ts,
            "trace_id": "t-1",
            "text": "Строка 1\nСтрока 2",
            "outcome": "full",
        }

    def test_constants(self) -> None:
        assert PING == b": ping\n\n"
        assert sse.SSE_HEADERS == {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        assert not hasattr(sse, "HEARTBEAT_S")  # S6.5: the interval is AGENT_STREAM_HEARTBEAT

    def test_heartbeat_default_is_in_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_STREAM_HEARTBEAT", raising=False)
        assert AgentSettings().stream_heartbeat == 15.0


async def frames_of(body: Any) -> list[bytes]:
    return [chunk async for chunk in body]


class TestSseBody:
    async def test_ping_does_not_stop_the_turn(self) -> None:
        agent, context = FakeAgent([answer_part()], delay=0.5), ctx()
        frames = await frames_of(sse_body(TurnStream(agent, {}, context, {}, lambda: None), heartbeat_s=0.1))
        assert PING in frames
        events = [parse(f)[0] for f in frames if f != PING]
        assert events == ["turn.started", "phase", "answer"]
        assert frames[-1] != PING  # nothing after the terminal frame
        assert not context.cancel.is_set()
        assert agent.finished

    async def test_ends_after_terminal_event(self) -> None:
        async def events() -> Any:
            factory = EventFactory("t-1")
            yield factory.make({"event": "error", "kind": "failed", "text": "x"})
            yield factory.make({"event": "turn.started", "session_id": "s1"})  # never read

        frames = await frames_of(sse_body(events(), heartbeat_s=0.1))
        assert [parse(f)[0] for f in frames] == ["error"]

    async def test_closed_mid_wait_cancels_the_turn_not_the_task(self) -> None:
        agent, context = FakeAgent([answer_part()], delay=0.5), ctx()
        stream = TurnStream(agent, {}, context, {}, lambda: None)
        body = sse_body(stream, heartbeat_s=0.05)
        assert parse(await anext(body))[0] == "turn.started"
        assert await anext(body) == PING
        await body.aclose()  # the client went away while the turn waits
        await asyncio.sleep(0)
        assert context.cancel.is_set()
        assert not stream.task.cancelled()
        await stream.task

    async def test_closed_between_events_closes_the_iterator(self) -> None:
        agent, context = FakeAgent([answer_part()], delay=0.3), ctx()
        stream = TurnStream(agent, {}, context, {}, lambda: None)
        body = sse_body(stream, heartbeat_s=10)
        assert parse(await anext(body))[0] == "turn.started"
        await body.aclose()
        assert context.cancel.is_set()
        await stream.task


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> Logger:
    logger = Logger()
    monkeypatch.setattr(APP_CTX, "get_logger", lambda: logger)
    return logger


class TestSseBodyStats:
    """S6.5 design D1: ``sse_body`` feeds ``StreamStats`` and closes it once."""

    async def test_finished_stream(self, log: Logger) -> None:
        agent, context = FakeAgent([answer_part()], delay=0.3), ctx()
        stream = TurnStream(agent, {}, context, {}, lambda: None)
        stats = StreamStats("t-1", "v2", started=time.perf_counter())
        frames = await frames_of(sse_body(stream, heartbeat_s=0.1, stats=stats))
        (line,) = stream_lines(log)
        kinds = [parse(f)[0] for f in frames if f != PING]
        assert line["events"] == {kind: kinds.count(kind) for kind in kinds}
        assert line["pings"] == frames.count(PING) > 0
        assert (line["outcome"], line["disconnected"], line["engine"]) == ("full", False, "v2")
        assert 0 <= line["first_event_ms"] <= line["duration_ms"]

    async def test_client_gone(self, log: Logger) -> None:
        agent, context = FakeAgent([answer_part()], delay=0.5), ctx()
        stream = TurnStream(agent, {}, context, {}, lambda: None)
        body = sse_body(stream, heartbeat_s=0.05, stats=StreamStats("t-1", "v2", started=time.perf_counter()))
        assert parse(await anext(body))[0] == "turn.started"
        assert await anext(body) == PING
        await body.aclose()
        (line,) = stream_lines(log)
        assert (line["outcome"], line["disconnected"]) == (None, True)
        assert line["events"] == {"turn.started": 1}
        await stream.task

    async def test_turn_id_from_the_turn_stream(self, log: Logger) -> None:
        parts = [{"type": "updates", "ns": (), "data": {"TurnContext.before_agent": {"turn_id": "t9"}}}, answer_part()]
        stream = TurnStream(FakeAgent(parts), {}, ctx(), {}, lambda: None)
        await frames_of(sse_body(stream, heartbeat_s=1, stats=StreamStats("t-1", "v2", started=time.perf_counter())))
        assert stream_lines(log)[0]["turn_id"] == "t9"


class TestOneShot:
    async def test_answer(self) -> None:
        async def run() -> str:
            return "Ответ v1"

        events = [e async for e in one_shot("t-1", "s1", run)]
        assert [(e.event, e.seq) for e in events] == [("turn.started", 1), ("answer", 2)]
        assert events[0].session_id == "s1"  # type: ignore[attr-defined]
        assert events[1].text == "Ответ v1"  # type: ignore[attr-defined]
        assert events[1].outcome == "full"  # type: ignore[attr-defined]

    async def test_stop_event(self) -> None:
        async def run() -> str:
            raise StopEventError(user_message="GigaChat временно недоступен", url="u")

        events = [e async for e in one_shot("t-1", "s1", run)]
        assert events[-1].event == "error"
        assert events[-1].kind == "unavailable"  # type: ignore[attr-defined]
        assert events[-1].text == "GigaChat временно недоступен"  # type: ignore[attr-defined]

    async def test_failure_hides_the_exception(self) -> None:
        async def run() -> str:
            raise ValueError("{secret}")

        events = [e async for e in one_shot("t-1", "s1", run)]
        assert events[-1].kind == "failed"  # type: ignore[attr-defined]
        assert events[-1].text == FAILED_TEXT  # type: ignore[attr-defined]
        assert "secret" not in events[-1].model_dump_json()

    async def test_consumer_leaves_the_turn_goes_on(self) -> None:
        done = asyncio.Event()

        async def run() -> str:
            await asyncio.sleep(0.2)
            done.set()
            return "Ответ"

        stream = one_shot("t-1", "s1", run)
        assert (await anext(stream)).event == "turn.started"
        waiting = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.05)
        waiting.cancel()  # Starlette cancels the response generator on disconnect
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(done.wait(), 1)
