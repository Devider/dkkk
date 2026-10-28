"""S6.5 (AR-Q18): ``StreamStats`` — the ``STREAM`` line and the stream metrics (design D1, D2, D4)."""

import json
from typing import Any

import pytest

from aigw_service.api.v2.stream import observe
from aigw_service.api.v2.stream.events import EventFactory, StreamEvent
from aigw_service.api.v2.stream.observe import StreamStats
from aigw_service.context import APP_CTX

SECRET_TEXT = "Секретный текст ответа 1501"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Logger:
    """Records ``info`` / ``metric`` / ``error``; ``fail`` makes ``info`` raise."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.lines: list[str] = []
        self.metrics: list[tuple[str, Any]] = []
        self.errors: list[str] = []

    def info(self, message: str, *args: Any) -> None:
        if self.fail:
            raise RuntimeError("{boom}")
        self.lines.append(message.format(*args))

    def metric(self, metric_name: str, metric_value: Any) -> None:
        self.metrics.append((metric_name, metric_value))

    def opt(self, **_kw: Any) -> "Logger":
        return self

    def error(self, message: str, *args: Any) -> None:
        self.errors.append(message.format(*args))


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> Logger:
    logger = Logger()
    monkeypatch.setattr(APP_CTX, "get_logger", lambda: logger)
    return logger


def stream_lines(log: Logger) -> list[dict[str, Any]]:
    return [json.loads(line[len("STREAM ") :]) for line in log.lines if line.startswith("STREAM ")]


def events(*drafts: dict[str, Any]) -> list[StreamEvent]:
    factory = EventFactory("t-1")
    return [factory.make(d) for d in drafts]


STARTED = {"event": "turn.started", "session_id": "s1"}
PHASE = {"event": "phase", "phase": "planning", "text": "Составляю план"}
ANSWER = {"event": "answer", "text": SECRET_TEXT, "outcome": "partial"}


class TestStreamLine:
    def test_finished_stream(self, log: Logger) -> None:
        clock = Clock()
        stats = StreamStats("t-1", "v2", started=99.75, clock=clock)
        started, phase, answer = events(STARTED, PHASE, ANSWER)
        stats.event(started)
        clock.now = 101.0
        stats.ping()
        stats.event(phase)
        stats.event(answer)
        clock.now = 102.5
        stats.close("turn-1")
        assert stream_lines(log) == [
            {
                "trace_id": "t-1",
                "turn_id": "turn-1",
                "engine": "v2",
                "first_event_ms": 250,
                "duration_ms": 2750,
                "events": {"turn.started": 1, "phase": 1, "answer": 1},
                "pings": 1,
                "outcome": "partial",
                "disconnected": False,
            }
        ]
        assert SECRET_TEXT not in log.lines[0]
        assert "Составляю план" not in log.lines[0]

    def test_error_outcome(self, log: Logger) -> None:
        stats = StreamStats("t-1", "v2", started=0.0, clock=Clock())
        for event in events(STARTED, {"event": "error", "kind": "unavailable", "text": "Недоступно"}):
            stats.event(event)
        stats.close()
        (line,) = stream_lines(log)
        assert (line["outcome"], line["disconnected"]) == ("unavailable", False)

    def test_disconnect(self, log: Logger) -> None:
        stats = StreamStats("t-1", "v2", started=100.0, clock=Clock())
        for event in events(STARTED, PHASE):
            stats.event(event)
        stats.close("turn-1")
        (line,) = stream_lines(log)
        assert (line["outcome"], line["disconnected"]) == (None, True)
        assert line["events"] == {"turn.started": 1, "phase": 1}

    def test_no_events(self, log: Logger) -> None:
        stats = StreamStats("t-1", "v1", started=100.0, clock=Clock())
        stats.close()
        (line,) = stream_lines(log)
        assert (line["first_event_ms"], line["events"], line["turn_id"]) == (None, {}, None)

    def test_close_once(self, log: Logger) -> None:
        stats = StreamStats("t-1", "v2", started=100.0, clock=Clock())
        stats.close()
        stats.close()
        assert len(stream_lines(log)) == 1

    def test_failing_logger_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logger = Logger(fail=True)
        monkeypatch.setattr(APP_CTX, "get_logger", lambda: logger)
        stats = StreamStats("t-1", "v2", started=100.0, clock=Clock())
        stats.close()
        assert logger.errors == ["STREAM record failed: RuntimeError"]


class TestStreamMetrics:
    def test_answer(self, log: Logger) -> None:
        clock = Clock()
        stats = StreamStats("t-1", "v2", started=99.9, clock=clock)
        for event in events(STARTED, ANSWER):
            stats.event(event)
        stats.close()
        assert log.metrics == [(observe.STREAMS_METRIC, 1), (observe.FIRST_EVENT_METRIC, 100)]

    def test_disconnect(self, log: Logger) -> None:
        stats = StreamStats("t-1", "v2", started=100.0, clock=Clock())
        stats.event(events(STARTED)[0])
        stats.close()
        assert (observe.DISCONNECTS_METRIC, 1) in log.metrics
        assert all(name != observe.ERRORS_METRIC for name, _ in log.metrics)

    def test_error(self, log: Logger) -> None:
        stats = StreamStats("t-1", "v2", started=100.0, clock=Clock())
        for event in events(STARTED, {"event": "error", "kind": "failed", "text": "Сбой"}):
            stats.event(event)
        stats.close()
        assert (observe.ERRORS_METRIC, 1) in log.metrics
        assert all(name != observe.DISCONNECTS_METRIC for name, _ in log.metrics)

    def test_no_events_no_first_event_metric(self, log: Logger) -> None:
        StreamStats("t-1", "v2", started=100.0, clock=Clock()).close()
        assert [name for name, _ in log.metrics] == [observe.STREAMS_METRIC, observe.DISCONNECTS_METRIC]
