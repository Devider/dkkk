"""Client disconnect during a turn (AR-Q12, design D2): Starlette does not cancel the handler, so we watch.

The watcher waits on the request's ``receive``: FastAPI has already read the body, so the next message can only
be ``http.disconnect``. ``Request.is_disconnected()`` is not used — behind the service's ``BaseHTTPMiddleware``
(``log_requests``) its instant-cancel read never sees the disconnect (checked on a live uvicorn, S2.8 task 3.3).
The watcher sets ``RequestContext.cancel``; ``Cancellation`` then stops the turn after the current call.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

DISCONNECT = "http.disconnect"


class Receivable(Protocol):
    @property
    def receive(self) -> Callable[[], Awaitable[dict[str, Any]]]: ...


async def watch_disconnect(request: Receivable, event: asyncio.Event) -> None:
    """Return once the client is gone, after setting ``event``."""
    while (await request.receive()).get("type") != DISCONNECT:
        pass
    logger.warning("Client disconnected: the turn stops after the current call")
    event.set()


@contextlib.asynccontextmanager
async def disconnect_watch(request: Receivable, event: asyncio.Event) -> AsyncIterator[None]:
    """Watch for the duration of a turn; the watcher never outlives it."""
    task = asyncio.create_task(watch_disconnect(request, event))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
