"""Turn lock (spec §12 п. 3, FR-15): one turn per session at a time; a second request gets HTTP 409 at once.

A set of busy ``thread_id`` instead of an ``asyncio.Lock`` per session: check and take happen with no ``await``
in between, so there is no race inside one event loop, and a free session leaves nothing behind. Process memory,
like ``MemorySaver``: one uvicorn process (§12 п. 7). Separate from the engine's per-session lock (§12 п. 2).
"""

import contextlib
from collections.abc import Callable, Iterator

TURN_BUSY_MESSAGE = "Предыдущий запрос ещё обрабатывается. Направьте следующий вопрос позже."


class TurnBusyError(RuntimeError):
    """A turn of this session is already running; no queue (§16)."""


class TurnLocks:
    def __init__(self) -> None:
        self._busy: set[str] = set()

    @property
    def busy(self) -> frozenset[str]:
        return frozenset(self._busy)

    def acquire(self, thread_id: str) -> Callable[[], None]:
        """Takes the session and returns its release; the streaming turn releases it from its own task (S6.2 D9).

        The release frees only this hold: called again, it does nothing, even if the session was taken anew.
        """
        if thread_id in self._busy:
            raise TurnBusyError(thread_id)
        self._busy.add(thread_id)
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._busy.discard(thread_id)

        return release

    @contextlib.contextmanager
    def hold(self, thread_id: str) -> Iterator[None]:
        release = self.acquire(thread_id)
        try:
            yield
        finally:
            release()


TURN_LOCKS = TurnLocks()
