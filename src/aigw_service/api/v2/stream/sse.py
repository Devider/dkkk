"""SSE delivery of stream events (S6.3 design D1–D2, D4; §13 «Кадр»; AR-Q16, AR-Q17).

Frames are written by hand: ``event: <type>``, ``id: <seq>``, ``data: <one-line JSON>`` and an empty line.
The heartbeat is the SSE comment ``: ping``. Nothing here knows about FastAPI or agent v1.
"""

import asyncio
import json
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable

from aigw_service.api.v2.stream.events import TERMINAL, EventFactory, StreamEvent
from aigw_service.api.v2.stream.turn import FAILED_TEXT
from aigw_service.context import APP_CTX
from aigw_service.exceptions import StopEventError

logger = APP_CTX.get_logger()

HEARTBEAT_S = 15.0
"""AR-Q16: a comment while no event is due. A constant until S6.5 moves it to ``AGENT_STREAM_HEARTBEAT``."""
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
PING = b": ping\n\n"

_RUNNING: set[asyncio.Task[tuple[str, str]]] = set()
"""Strong references to one-shot turns whose consumer may be gone."""


def encode(event: StreamEvent) -> bytes:
    """One SSE frame; ``data`` is single-line JSON, so a multi-line answer stays one ``data:`` line."""
    data = json.dumps(event.data(), ensure_ascii=False)
    return f"event: {event.event}\nid: {event.seq}\ndata: {data}\n\n".encode()


async def sse_body(events: AsyncIterable[StreamEvent], heartbeat_s: float | None = None) -> AsyncIterator[bytes]:
    """Frames for ``events`` with ``PING`` while waiting; ends right after the terminal event.

    The pending read is never cancelled by the heartbeat: a cancelled read of ``TurnStream`` would set
    ``RequestContext.cancel`` and stop the turn (design D2). When the consumer goes away, the pending read is
    cancelled (or the iterator closed), which is what stops the turn after its current call (AR-Q17).
    """
    interval = HEARTBEAT_S if heartbeat_s is None else heartbeat_s
    iterator = aiter(events)
    pending: asyncio.Future[StreamEvent] | None = None
    try:
        while True:
            pending = asyncio.ensure_future(anext(iterator))
            while not (await asyncio.wait({pending}, timeout=interval))[0]:
                yield PING
            read, pending = pending, None
            try:
                event = read.result()
            except StopAsyncIteration:
                return
            yield encode(event)
            if event.event in TERMINAL:
                return
    finally:
        if pending is not None and not pending.done():
            pending.cancel()  # no await here: the cancellation reaches the iterator on the next loop step
        else:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                await aclose()


async def _guarded(run: Callable[[], Awaitable[str]]) -> tuple[str, str]:
    """The turn's outcome as ``(kind, text)``; never raises, so a task nobody awaits leaves no warning."""
    try:
        return "answer", await run()
    except StopEventError as e:
        logger.warning("One-shot stream turn: StopEvent")
        return "unavailable", e.user_message
    except Exception as e:  # pylint: disable=broad-exception-caught  # the stream must end with one terminal event
        logger.opt(exception=True).error("One-shot stream turn failed: {}", type(e).__name__)
        return "failed", FAILED_TEXT


async def one_shot(trace_id: str, session_id: str, run: Callable[[], Awaitable[str]]) -> AsyncIterator[StreamEvent]:
    """FR-21 at ``AGENT_ENGINE=v1``: ``turn.started``, then ``answer`` (``full``) or ``error``; no progress.

    The turn runs as its own task: a client that goes away does not cut it in the middle of a calculation.
    """
    factory = EventFactory(trace_id)
    task = asyncio.ensure_future(_guarded(run))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    yield factory.make({"event": "turn.started", "session_id": session_id})
    await asyncio.wait({task})  # unlike ``await task``, leaving here does not cancel the turn
    kind, text = task.result()
    if kind == "answer":
        yield factory.make({"event": "answer", "text": text, "outcome": "full"})
    else:
        yield factory.make({"event": "error", "kind": kind, "text": text})
