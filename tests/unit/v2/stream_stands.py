"""Stream test stands shared by the HTTP stream tests (S6.3) and U10 (S6.5); fixtures live in ``conftest.py``."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from tests.unit.test_log_requests import RecordingLogger


class StreamRecorder(RecordingLogger):
    """``RecordingLogger`` that also keeps the ``STREAM`` lines (S6.5): their JSON is a positional argument."""

    def __init__(self) -> None:
        super().__init__()
        self.streams: list[dict[str, Any]] = []

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        if message == "STREAM {}" and args:
            self.streams.append(json.loads(args[0]))
        super().info(message, *args, **kwargs)

    def opt(self, **_kw: Any) -> "StreamRecorder":
        return self

    def error(self, message: Any, *_a: Any, **kwargs: Any) -> None:
        self._text("error", message, **kwargs)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def live_server() -> AsyncIterator[str]:
    """``app_main`` on a real uvicorn in this event loop; dependency overrides come from the test's ``Api``."""
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
