"""TurnStream (ADR-14, AR-Q17, S6.2 design D9–D11): one streaming turn as its own task, events through a queue.

The turn runs in an ``asyncio`` task, not inside the consumer: Starlette cancels the response generator about
0.2 s after a disconnect (spike S6.1, H3), and a turn inside it would be cut in the middle of a calculation.
The queue is unbounded, so a slow consumer never holds the turn back (AR-Q16). A consumer that goes away only
sets ``RequestContext.cancel``: ``Cancellation`` stops the turn after the current call, and the session lock is
released when the turn ends, not when the stream does.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.stream.events import TERMINAL, Draft, EventFactory, StreamEvent
from aigw_service.api.v2.stream.translator import StreamTranslator
from aigw_service.context import APP_CTX
from aigw_service.exceptions import StopEventError

logger = APP_CTX.get_logger()

STREAM_MODE = ["updates", "custom"]
"""ADR-14: ``"messages"`` is not used — the answer comes whole (ADR-15)."""
STREAM_VERSION = "v2"
FAILED_TEXT = "Не удалось подготовить ответ из-за внутренней ошибки. Повторите вопрос позже."

_RUNNING: set[asyncio.Task[None]] = set()
"""Strong references to turn tasks: a task whose consumer went away must not be garbage-collected."""
_END = object()


class TurnStream:  # pylint: disable=too-many-instance-attributes  # one turn: agent, input, queue, task
    """Async iterable of ``StreamEvent``; ends right after the single terminal ``answer`` / ``error``."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        agent: Any,
        agent_input: dict[str, Any],
        context: RequestContext,
        config: dict[str, Any],
        release: Callable[[], None],
        *,
        factory: EventFactory | None = None,
        translator: StreamTranslator | None = None,
    ) -> None:
        self.context = context
        self._agent = agent
        self._input = agent_input
        self._config = config
        self._release = release
        self._factory = factory or EventFactory(context.trace_id)
        self._translator = translator or StreamTranslator(lambda: context.model_index)
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._put({"event": "turn.started", "session_id": context.session_id})
        self.task: asyncio.Task[None] = asyncio.create_task(self._run())
        _RUNNING.add(self.task)
        self.task.add_done_callback(_RUNNING.discard)

    def _put(self, draft: Draft) -> None:
        self._queue.put_nowait(self._factory.make(draft))

    async def _run(self) -> None:
        try:
            stream = self._agent.astream(
                self._input,
                config=self._config,
                context=self.context,
                stream_mode=STREAM_MODE,
                version=STREAM_VERSION,
            )
            async for part in stream:
                for draft in self._translator.feed(part):
                    self._put(draft)
            self._put(self._translator.finish())
        except StopEventError as e:  # FR-25: GigaChat is temporarily unavailable
            logger.warning("Stream turn of {}: StopEvent", self.context.thread_id)
            self._put({"event": "error", "kind": "unavailable", "text": e.user_message})
        except Exception as e:  # pylint: disable=broad-exception-caught  # the stream must end with one terminal event
            logger.opt(exception=True).error("Stream turn of {} failed: {}", self.context.thread_id, type(e).__name__)
            self._put({"event": "error", "kind": "failed", "text": FAILED_TEXT})
        finally:
            self._release()  # AR-Q17: the lock lives as long as the turn
            self._queue.put_nowait(_END)

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[StreamEvent]:
        try:
            while (item := await self._queue.get()) is not _END:
                yield item
                if item.event in TERMINAL:
                    return
        finally:
            self.stop()

    def stop(self) -> None:
        """The consumer went away (or finished): a turn still running stops after its current call (AR-Q12)."""
        if not self.task.done():
            self.context.cancel.set()

    async def aclose(self) -> None:
        self.stop()
