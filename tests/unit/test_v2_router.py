"""HTTP contract tests for the Deep Agent API v2."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx
import openpyxl
import pytest
from httpx import ASGITransport
from langchain_core.messages import AIMessage
from langgraph.types import Command
from ollama import ResponseError
from pydantic import ValidationError

from aigw_service.api import app_main
from aigw_service.api.v2 import router as v2_router


class StubDeepAgent:
    """Capture invocation inputs and return a stable final response."""

    def __init__(
        self,
        response: str = "V2 result",
        error: Exception | None = None,
        interruptions: list | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.interruptions = interruptions
        self.calls: list[tuple[dict, dict]] = []

    async def ainvoke(self, payload: dict, *, config: dict) -> dict:
        self.calls.append((payload, config))
        if self.error is not None:
            raise self.error
        result = {"messages": [AIMessage(content=self.response)]}
        if self.interruptions is not None:
            result["__interrupt__"] = self.interruptions
        return result


class PendingStubDeepAgent(StubDeepAgent):
    """Stand-in for a checkpoint with a pending name-resolution interrupt."""

    async def aget_state(self, config: dict) -> object:
        return type("Snapshot", (), {"tasks": [type("Task", (), {"interrupts": [object()]})()]})()


def _valid_headers(session_id: str | None = None) -> dict[str, str]:
    headers = {
        "x-trace-id": str(uuid4()),
        "x-client-id": "CI12345678",
        "x-request-time": "2026-11-07T12:00:00Z",
        "x-user-id": "user0001",
    }
    if session_id is not None:
        headers["x-session-id"] = session_id
    return headers


@pytest.fixture(autouse=True)
def disable_uninitialized_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep request tests independent from Langfuse application startup."""
    monkeypatch.setattr(v2_router.APP_CTX, "tracing", None)
    generation = SimpleNamespace(identity="fixture")
    monkeypatch.setattr(v2_router, "get_repository", lambda: SimpleNamespace(load=lambda user_id: generation))
    monkeypatch.setattr(v2_router, "restore_workbook", AsyncMock())

    @asynccontextmanager
    async def lock(repo: object, user_id: str) -> AsyncIterator[None]:
        """Skip persistence infrastructure in existing HTTP response contract tests."""
        yield

    monkeypatch.setattr(v2_router, "user_lock", lock)


@pytest.mark.asyncio
async def test_invoke_agent_returns_zip_and_passes_request_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = str(uuid4())
    agent = StubDeepAgent("The model result")
    monkeypatch.setattr(v2_router, "get_deep_agent", lambda: agent)

    async with httpx.AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/invoke-agent",
            json={"message": "Analyze EBITDA"},
            headers=_valid_headers(session_id),
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert f"agent_response_{session_id}.zip" in response.headers["content-disposition"]

    with ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"txt_response.txt", "generated/agent_output.xlsx"}
        assert archive.read("txt_response.txt").decode() == "The model result"
        workbook = openpyxl.load_workbook(BytesIO(archive.read("generated/agent_output.xlsx")))
        assert workbook.sheetnames == ["Sheet1"]

    payload, config = agent.calls[0]
    assert payload["messages"][0].content == "Analyze EBITDA"
    assert config["configurable"] == {
        "thread_id": f"user0001:{session_id}",
        "user_id": "user0001",
        "catalog_generation": "fixture",
    }
    assert config["metadata"]["client_id"] == "CI12345678"
    assert config["metadata"]["langfuse_session_id"] == f"user0001:{session_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["", "x" * 2001])
async def test_invoke_agent_rejects_invalid_message_before_invocation(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    agent = StubDeepAgent()
    monkeypatch.setattr(v2_router, "get_deep_agent", lambda: agent)

    async with httpx.AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/invoke-agent",
            json={"message": message},
            headers=_valid_headers(),
        )

    assert response.status_code == 422
    assert agent.calls == []


@pytest.mark.asyncio
async def test_invoke_agent_generates_session_id_when_header_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_session_id = UUID("12345678-1234-4234-8234-123456789abc")
    agent = StubDeepAgent()
    monkeypatch.setattr(v2_router, "get_deep_agent", lambda: agent)
    monkeypatch.setattr(v2_router, "uuid4", lambda: generated_session_id)

    async with httpx.AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/invoke-agent",
            json={"message": "Hello"},
            headers=_valid_headers(),
        )

    assert response.status_code == 200
    assert f"agent_response_{generated_session_id}.zip" in response.headers["content-disposition"]
    assert agent.calls[0][1]["configurable"]["thread_id"] == f"user0001:{generated_session_id}"


@pytest.mark.asyncio
async def test_invoke_agent_resumes_pending_clarification_on_same_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = str(uuid4())
    agent = PendingStubDeepAgent("Resolved response")
    monkeypatch.setattr(v2_router, "get_deep_agent", lambda: agent)

    async with httpx.AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/invoke-agent",
            json={"message": "2"},
            headers=_valid_headers(session_id),
        )

    assert response.status_code == 200
    payload, config = agent.calls[0]
    assert isinstance(payload, Command)
    assert payload.resume == "2"
    assert config["configurable"]["thread_id"] == f"user0001:{session_id}"


@pytest.mark.asyncio
async def test_clarification_interrupt_is_delivered_in_existing_zip_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = StubDeepAgent(
        interruptions=[
            type(
                "Interrupt",
                (),
                {
                    "value": {
                        "question": "Не удалось однозначно сопоставить показатель. "
                        "Возможно, вы имеете в виду:\n1) EBITDA\n2) Revenue\n3) Ни один из перечисленных вариантов"
                    }
                },
            )()
        ]
    )
    monkeypatch.setattr(v2_router, "get_deep_agent", lambda: agent)

    async with httpx.AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/invoke-agent",
            json={"message": "Analyze operating profit"},
            headers=_valid_headers(),
        )

    with ZipFile(BytesIO(response.content)) as archive:
        body = archive.read("txt_response.txt").decode()
    assert response.status_code == 200
    assert "Возможно, вы имеете в виду" in body
    assert "3) Ни один из перечисленных вариантов" in body


@pytest.mark.asyncio
async def test_invoke_agent_maps_agent_exception_to_424(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = StubDeepAgent(error=RuntimeError("agent failed"))
    monkeypatch.setattr(v2_router, "get_deep_agent", lambda: agent)

    async with httpx.AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/invoke-agent",
            json={"message": "Hello"},
            headers=_valid_headers(),
        )

    assert response.status_code == 424
    assert response.json()["detail"] == "agent failed"


def test_v2_upload_route_is_hidden_from_openapi() -> None:
    paths = app_main.openapi()["paths"]

    assert "/api/v2/invoke-agent" in paths
    assert "/api/v2/upload" not in paths


@pytest.mark.parametrize(
    "error",
    [
        ResponseError("model not found", status_code=404),
        httpx.ConnectError("Ollama unreachable"),
        httpx.ReadTimeout("Ollama timeout"),
        ValidationError.from_exception_data(
            "InputResolutionAnalysis",
            [{"type": "missing", "loc": ("mentioned_inputs",), "input": {}}],
        ),
    ],
)
async def test_ollama_failures_return_424_without_fallback(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Keep provider/parse failures on the existing failure contract."""
    from aigw_service.api.v2 import agent as v2_agent

    def forbid_fallback(**kwargs: object) -> None:
        """Any attempt to construct another provider fails the test."""
        raise AssertionError("Unexpected GigaChat fallback")

    monkeypatch.setattr(v2_agent, "GigaChat", forbid_fallback)
    agent = StubDeepAgent(error=error)
    monkeypatch.setattr(v2_router, "get_deep_agent", lambda: agent)
    async with httpx.AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/invoke-agent", json={"message": "Analyze EBITDA"}, headers=_valid_headers()
        )
    assert response.status_code == 424
    assert response.json()["detail"] == str(error)
