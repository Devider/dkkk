"""StreamStats (AR-Q18, S6.5 design D1, D2, D4): one ``STREAM`` log line and the stream metrics per stream.

``sse_body`` feeds it every frame and ping it hands to the client and closes it when the stream ends — after the
terminal frame or when the client goes away. The line describes what the client got, not how the turn ended: after
a disconnect the turn finishes on its own and its outcome is in ``TURN SUMMARY`` (joined on ``turn_id``).
Event texts never go into the line, only counts and the terminal outcome.
"""

import json
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from aigw_service.api.v2.stream.events import StreamEvent
from aigw_service.context import APP_CTX

STREAMS_METRIC = "aigw_service_streams_total"
FIRST_EVENT_METRIC = "aigw_service_stream_first_event_ms"
DISCONNECTS_METRIC = "aigw_service_stream_disconnects_total"
ERRORS_METRIC = "aigw_service_stream_errors_total"


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


class StreamStats:  # pylint: disable=too-many-instance-attributes  # the fields of one STREAM line
    """Counts one stream's frames; ``close`` writes the line and the metrics once and never raises."""

    def __init__(self, trace_id: str, engine: str, started: float, clock: Callable[[], float] = time.perf_counter):
        self.trace_id = trace_id
        self.engine = engine
        self._started = started
        self._clock = clock
        self._first_event: float | None = None
        self._events: Counter[str] = Counter()
        self._pings = 0
        self._outcome: str | None = None
        self._error = False
        self._closed = False

    def event(self, event: StreamEvent) -> None:
        if self._first_event is None:
            self._first_event = self._clock()
        self._events[event.event] += 1
        if event.event == "answer":
            self._outcome = getattr(event, "outcome", None)
        elif event.event == "error":
            self._outcome = getattr(event, "kind", None)
            self._error = True

    def ping(self) -> None:
        self._pings += 1

    def record(self, turn_id: str | None) -> dict[str, Any]:
        """The ``STREAM`` line's fields (design D1)."""
        first = None if self._first_event is None else _ms(self._first_event - self._started)
        return {
            "trace_id": self.trace_id,
            "turn_id": turn_id,
            "engine": self.engine,
            "first_event_ms": first,
            "duration_ms": _ms(self._clock() - self._started),
            "events": dict(self._events),
            "pings": self._pings,
            "outcome": self._outcome,
            "disconnected": self._outcome is None,
        }

    def close(self, turn_id: str | None = None) -> None:
        """Synchronous: a cancelled generator's ``finally`` runs it to the end."""
        if self._closed:
            return
        self._closed = True
        logger = APP_CTX.get_logger()
        try:
            record = self.record(turn_id)
            logger.info("STREAM {}", json.dumps(record, ensure_ascii=False))
            logger.metric(metric_name=STREAMS_METRIC, metric_value=1)
            if record["first_event_ms"] is not None:
                logger.metric(metric_name=FIRST_EVENT_METRIC, metric_value=record["first_event_ms"])
            if record["disconnected"]:
                logger.metric(metric_name=DISCONNECTS_METRIC, metric_value=1)
            if self._error:
                logger.metric(metric_name=ERRORS_METRIC, metric_value=1)
        except Exception as e:  # pylint: disable=broad-exception-caught  # the log must never break the stream
            logger.opt(exception=True).error("STREAM record failed: {}", type(e).__name__)
