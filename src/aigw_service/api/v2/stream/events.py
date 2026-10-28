"""Stream event schema ``v = 1`` (spec §13 «Каталог событий», S6.2 design D2).

Changes are additive only (AR-Q18): new optional fields and new event types. The translator produces drafts
(``Draft``: the event type plus its own fields); ``EventFactory`` adds the common fields and numbers them, so
``seq`` is monotonic across every source of events, including the terminal ``error`` of ``TurnStream``.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from aigw_service.api.v2.middleware.todos import Status

SCHEMA_VERSION = 1

Phase = Literal["preparing", "planning", "executing", "checking", "answering"]
ToolOutcome = Literal["ok", "retry", "failed"]
AnswerOutcome = Literal["full", "partial", "clarify", "refusal"]
ErrorKind = Literal["unavailable", "failed"]


class StreamEvent(BaseModel):
    """Common fields of every event; ``event`` becomes the SSE ``event:`` line, not a ``data`` field (S6.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: str
    v: Literal[1] = SCHEMA_VERSION
    seq: int
    ts: str
    trace_id: str

    def data(self) -> dict[str, Any]:
        """Fields of the SSE ``data`` JSON."""
        return self.model_dump(exclude={"event"})


class TurnStarted(StreamEvent):
    event: Literal["turn.started"] = "turn.started"
    session_id: str


class PhaseEvent(StreamEvent):
    event: Literal["phase"] = "phase"
    phase: Phase
    text: str


class PlanStepView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    n: int
    title: str
    status: Status
    reason: str | None = None


class PlanEvent(StreamEvent):
    event: Literal["plan"] = "plan"
    steps: list[PlanStepView]


class ToolStarted(StreamEvent):
    event: Literal["tool.started"] = "tool.started"
    call: int
    step: int | None
    label: str


class ToolFinished(StreamEvent):
    event: Literal["tool.finished"] = "tool.finished"
    call: int
    label: str
    outcome: ToolOutcome
    elapsed_s: float
    summary: str | None = None
    """Step summary from the envelope (FR-23, S6.4); only with ``ok``."""
    text: str | None = None
    """What happens next; only with ``retry`` / ``failed``."""


class TurnSummaryEvent(StreamEvent):
    """Before ``answer`` when the turn had a plan (FR-23); emitted from S6.4 on."""

    event: Literal["summary"] = "summary"
    completed: int
    skipped: int
    blocked: int
    text: str


class AnswerEvent(StreamEvent):
    """Terminal: the final answer accepted by the guard middleware (FR-26)."""

    event: Literal["answer"] = "answer"
    text: str
    outcome: AnswerOutcome


class ErrorEvent(StreamEvent):
    """Terminal: the turn gave no answer."""

    event: Literal["error"] = "error"
    kind: ErrorKind
    text: str


EVENT_TYPES: dict[str, type[StreamEvent]] = {
    cls.model_fields["event"].default: cls
    for cls in (
        TurnStarted,
        PhaseEvent,
        PlanEvent,
        ToolStarted,
        ToolFinished,
        TurnSummaryEvent,
        AnswerEvent,
        ErrorEvent,
    )
}
TERMINAL = frozenset({"answer", "error"})

Draft = dict[str, Any]
"""An event without the common fields: ``{"event": <type>, **fields}``."""


def _now() -> datetime:
    return datetime.now(UTC)


class EventFactory:
    """Adds ``v``, ``seq`` (from 1), ``ts`` (RFC-3339 UTC, milliseconds) and ``trace_id`` to drafts."""

    def __init__(self, trace_id: str, clock: Callable[[], datetime] = _now) -> None:
        self.trace_id = trace_id
        self._clock = clock
        self._seq = 0

    def make(self, draft: Draft) -> StreamEvent:
        fields = dict(draft)
        cls = EVENT_TYPES[fields.pop("event")]
        self._seq += 1
        ts = self._clock().astimezone(UTC).isoformat(timespec="milliseconds")
        return cls(seq=self._seq, ts=ts, trace_id=self.trace_id, **fields)
