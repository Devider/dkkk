"""``log_requests``: characterization of the current records (S6.3 task 1, written before the stream branch).

``TestCharacterization`` pins what the middleware writes for every non-stream response — records, their order,
audit bodies, metrics and the ``x-*`` response headers. It must keep passing unchanged after the
``text/event-stream`` branch is added (§13 «Журнал запросов и поток», item 1).
"""

import asyncio
import io
import json
import re
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import ASGITransport, AsyncClient

from aigw_service.api import middleware
from aigw_service.api.middleware import log_requests
from aigw_service.api.v1.utils import common_headers
from aigw_service.context import APP_CTX

TRACE = "e037c70a-30d7-4c47-b25e-ce18c9c39f15"
HEADERS = {
    "x-trace-id": TRACE,
    "x-client-id": "CI12345678",
    "x-request-time": "2026-10-26T11:31:45.748539+03:00",
    "x-session-id": "7d7c1d2e-3a4b-4c5d-8e9f-0a1b2c3d4e5f",
    "x-user-id": "u1",
}
BODY = {"message": "Привет"}


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("txt_response.txt", "Ответ")
    return buffer.getvalue()


ZIP_BYTES = _zip()


class RecordingLogger:
    """Stands in for the service logger: what ``log_requests`` writes, in order, with the request's trace id."""

    def __init__(self) -> None:
        self.records: list[tuple] = []
        self.durations: list[int] = []

    def _trace(self) -> Any:
        return APP_CTX.get_context_vars_container().context_vars.rqUId

    def _text(self, kind: str, message: Any, **kwargs: Any) -> None:
        args = kwargs.get("args")
        extra: dict[str, Any] = {}
        if args is not None:
            extra = {
                "headers": _norm_headers(args.get("headers", {})),
                "message": _norm_body(args.get("message")),
                "message_type": kwargs.get("message_type"),
                "path": kwargs.get("path"),
            }
        self.records.append((kind, _norm_text(str(message)), extra, self._trace()))

    def info(self, message: Any, *_a: Any, **kwargs: Any) -> None:
        self._text("info", message, **kwargs)

    def debug(self, message: Any, *_a: Any, **kwargs: Any) -> None:
        self._text("debug", message, **kwargs)

    def warning(self, message: Any, *_a: Any, **kwargs: Any) -> None:
        self._text("warning", message, **kwargs)

    def metric(self, metric_name: str, metric_value: Any) -> None:
        if metric_name == "aigw_service_process_duration_ms":
            self.durations.append(metric_value)
        value = (
            "<int>"
            if metric_name == "aigw_service_process_duration_ms" and isinstance(metric_value, int)
            else metric_value
        )
        self.records.append(("metric", metric_name, value, self._trace()))

    def audit(self, event_name: str, event_params: str) -> None:
        self.records.append(("audit", event_name, _norm_body(json.loads(event_params)), self._trace()))


def _norm_text(text: str) -> str:
    text = re.sub(r": \d+ ms$", ": <ms> ms", text)
    return re.sub(r"processing time:\s+[\d.]+ s$", "processing time: <s> s", text)


def _norm_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<time>" if k == "x-response-time" else v) for k, v in headers.items()}


def _norm_body(body: Any) -> Any:
    if isinstance(body, dict) and isinstance(body.get("detail"), list):  # a 422 body: pydantic wording varies
        return {"detail": "<validation>"}
    return body


def build_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(log_requests)

    @app.post("/zip")
    async def zip_route() -> StreamingResponse:
        return StreamingResponse(
            io.BytesIO(ZIP_BYTES),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=agent_response.zip"},
        )

    @app.post("/json")
    async def json_route() -> JSONResponse:
        return JSONResponse({"content": "Файл был успешно сохранен."})

    @app.post("/status/{code}")
    async def status_route(code: int) -> JSONResponse:
        return JSONResponse(status_code=code, content={"error_description": f"ошибка {code}"})

    @app.post("/headers")
    async def headers_route(headers: dict = Depends(common_headers)) -> JSONResponse:
        return JSONResponse({"user": headers["x-user-id"]})

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.post("/sse")
    async def sse_route(pause: float = 0.0, terminal: str = "answer", split: int = 0) -> StreamingResponse:
        async def frames() -> AsyncIterator[bytes]:
            APP_CTX.get_logger().info("frame 1")
            yield b'event: turn.started\nid: 1\ndata: {"v": 1, "seq": 1}\n\n'
            await asyncio.sleep(pause)
            yield b": ping\n\n"
            await asyncio.sleep(pause)
            APP_CTX.get_logger().info("frame last")
            if terminal:
                last = f'event: {terminal}\nid: 2\ndata: {{"v": 1, "seq": 2, "text": "Ответ"}}\n\n'.encode()
                if split:
                    yield last[:split]
                    yield last[split:]
                else:
                    yield last

        return StreamingResponse(frames(), media_type="text/event-stream")

    return app


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    rec = RecordingLogger()
    monkeypatch.setattr(APP_CTX, "get_logger", lambda: rec)
    return rec


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=build_app()), base_url="http://test") as ac:
        yield ac


def request_records(path: str, body: Any = BODY, headers: dict[str, str] = HEADERS) -> list[tuple]:
    sent = {k: v for k, v in headers.items() if v}
    records: list[tuple] = []
    records.append(
        (
            "info",
            f"Incoming POST-request for {path}",
            {"headers": sent, "message": body, "message_type": "request", "path": path},
            TRACE,
        )
    )
    if sent.get("x-user-id"):
        records.append(("metric", f"aigw_service_user_{sent['x-user-id']}", 1, TRACE))
    records.append(("metric", "aigw_service_requests_total", 1, TRACE))
    records.append(("audit", "BusinessRequestReceived", body, TRACE))
    return records


def response_records(path: str, body: Any, success: bool, headers: dict[str, str] = HEADERS) -> list[tuple]:
    sent = {k: v for k, v in headers.items() if v} | {"x-response-time": "<time>"}
    status_metric = (
        "aigw_service_request_status_success_total" if success else "aigw_service_request_status_failure_total"
    )
    return [
        (
            "info",
            "Outgoing response to client system",
            {"headers": sent, "message": body, "message_type": "response", "path": path},
            TRACE,
        ),
        ("metric", "aigw_service_responses_total", 1, TRACE),
        ("audit", "BusinessRequestFinished", body, TRACE),
        ("info", f"Request processing time for {path}: <ms> ms", {}, TRACE),
        ("metric", "aigw_service_process_duration_ms", "<int>", TRACE),
        ("metric", status_metric, 1, TRACE),
    ]


def x_headers(response: Any) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.startswith("x-")}


def assert_x_headers(response: Any, headers: dict[str, str] = HEADERS) -> None:
    got = x_headers(response)
    assert got.pop("x-response-time")
    assert got == {k: v for k, v in headers.items() if v}


class TestCharacterization:
    """§13 «Журнал запросов и поток», item 1: records of non-stream responses as they were before S6.3."""

    async def test_zip(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        response = await client.post("/zip", json=BODY, headers=HEADERS)
        assert response.status_code == 200
        assert zipfile.ZipFile(io.BytesIO(response.content)).read("txt_response.txt").decode() == "Ответ"
        assert recorder.records == [
            *request_records("/zip"),
            ("warning", "response body is not json", {}, TRACE),
            *response_records("/zip", {}, success=True),
        ]
        assert_x_headers(response)

    async def test_json(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        response = await client.post("/json", json=BODY, headers=HEADERS)
        body = {"content": "Файл был успешно сохранен."}
        assert response.json() == body
        assert recorder.records == [*request_records("/json"), *response_records("/json", body, success=True)]
        assert_x_headers(response)

    @pytest.mark.parametrize("code", [409, 424, 503])
    async def test_error_json(self, client: AsyncClient, recorder: RecordingLogger, code: int) -> None:
        path = f"/status/{code}"
        response = await client.post(path, json=BODY, headers=HEADERS)
        body = {"error_description": f"ошибка {code}"}
        assert response.status_code == code
        assert response.json() == body
        assert recorder.records == [*request_records(path), *response_records(path, body, success=False)]
        assert_x_headers(response)

    async def test_422(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        headers = {**HEADERS, "x-client-id": "bad"}
        response = await client.post("/headers", json=BODY, headers=headers)
        assert response.status_code == 422
        body = {"detail": "<validation>"}
        assert recorder.records == [
            *request_records("/headers", headers=headers),
            *response_records("/headers", body, success=False, headers=headers),
        ]
        assert_x_headers(response, headers)

    async def test_non_logged_endpoint(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        response = await client.get("/health", headers=HEADERS)
        assert response.json() == {"status": "ok"}
        assert recorder.records == [("debug", "Processed request for /health with code 200", {}, TRACE)]
        assert x_headers(response) == {}

    async def test_no_trace_id(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        headers = {k: v for k, v in HEADERS.items() if k != "x-trace-id"}
        response = await client.post("/json", json=BODY, headers=headers)
        assert response.json() == {"content": "Файл был успешно сохранен."}
        assert recorder.records == [
            ("info", "Incoming POST-request with no id for /json", {}, ""),
            ("info", "Request with no id for /json processing time: <s> s", {}, ""),
        ]
        assert x_headers(response) == {}

    async def test_request_body_not_json(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        response = await client.post("/json", content=b"not json", headers=HEADERS)
        assert response.status_code == 200
        assert recorder.records[0] == ("warning", "request body is not json", {}, TRACE)
        assert recorder.records[1:] == [
            *request_records("/json", body={}),
            *response_records("/json", {"content": "Файл был успешно сохранен."}, success=True),
        ]

    def test_module_surface(self) -> None:
        assert middleware.NON_LOGGED_ENDPOINTS == ("/like", "/dislike", "/health", "/info", "/openapi.json", "/docs")


def stream_tail(body: Any) -> list[tuple]:
    return response_records("/sse", body, success=True)


class TestEventStreamBranch:
    """§13 «Журнал запросов и поток», items 2–6 (S6.3 design D5)."""

    async def test_records_after_the_stream(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        response = await client.post("/sse?pause=0.2", json=BODY, headers=HEADERS)
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.endswith('data: {"v": 1, "seq": 2, "text": "Ответ"}\n\n')
        assert recorder.records == [
            *request_records("/sse"),
            ("info", "frame 1", {}, TRACE),
            ("info", "frame last", {}, TRACE),
            *stream_tail({"v": 1, "seq": 2, "text": "Ответ"}),
        ]
        assert recorder.durations[0] >= 400  # the whole stream, not up to the headers
        assert_x_headers(response)

    async def test_error_is_the_audit_body(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        await client.post("/sse?terminal=error", json=BODY, headers=HEADERS)
        assert recorder.records[-6:] == stream_tail({"v": 1, "seq": 2, "text": "Ответ"})

    async def test_split_terminal_frame(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        await client.post("/sse?split=20", json=BODY, headers=HEADERS)
        assert recorder.records[-6:] == stream_tail({"v": 1, "seq": 2, "text": "Ответ"})

    async def test_no_terminal_frame(self, client: AsyncClient, recorder: RecordingLogger) -> None:
        await client.post("/sse?terminal=", json=BODY, headers=HEADERS)
        assert recorder.records[-6:] == stream_tail({})

    async def test_disconnect(self, recorder: RecordingLogger) -> None:
        ended: list[dict] = []

        async def endless() -> AsyncIterator[bytes]:
            yield b'event: turn.started\nid: 1\ndata: {"v": 1}\n\n'
            while True:
                await asyncio.sleep(0.01)
                yield b": ping\n\n"

        stream = middleware._logged_stream(endless(), ended.append)  # pylint: disable=protected-access
        assert (await anext(stream)).startswith(b"event: turn.started")
        await stream.aclose()  # Starlette closes the body iterator when the client goes away
        assert ended == [{}]
        assert recorder.records == []

    def test_terminal_data(self) -> None:
        terminal = middleware._terminal_data  # pylint: disable=protected-access
        assert terminal('event: answer\nid: 3\ndata: {"text": "a"}') == {"text": "a"}
        assert terminal('event: plan\nid: 2\ndata: {"steps": []}') is None
        assert terminal("event: error\nid: 3\ndata: not json") is None
        assert terminal(": ping") is None
