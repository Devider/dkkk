"""HTTP-level tests for run-latest-wins on ``/api/v1/invoke-agent`` (tasks 2.3/2.4).

Unit tests in ``tests/test_thread_runs_isolation.py`` cover the registry in isolation.
These go through the real ASGI app, the real router and a real ``MemorySaver``-compiled
LangGraph graph, stubbing only the LLM call — so they assert the client-visible contract:

* the winning request gets ``200`` and a ZIP with ``txt_response.txt``;
* the superseded request gets **no HTTP response at all** (its ASGI task is cancelled —
  no 409/424, no success payload);
* the winner's own failures still map to ``StopEventError`` -> 503 and generic -> 424.

The graph is driven through a scripted LLM so each turn is deterministic: the scripted
response either issues a tool call (creating an extra checkpoint and a delay) or answers
immediately.
"""

import asyncio
import zipfile
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from langchain_core.messages import AIMessage

from aigw_service.api.v1 import router as router_module
from aigw_service.api.v1.main_graph import AgentGraph
from aigw_service.api.v1.thread_runs import get_thread_run_registry
from aigw_service.exceptions import StopEventError


class _ScriptedLLM:
    """``bind_tools``-able LLM stand-in that yields ``responses`` in order."""

    def __init__(self, responses: list[AIMessage], delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.calls = 0

    def bind_tools(self, _tools) -> "_ScriptedLLM":
        return self

    async def ainvoke(self, _prompt) -> AIMessage:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


class _FailingStream:
    """Graph stand-in whose ``astream`` raises ``error`` — exercises the winner's error mapping."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def astream(self, *_args: Any, **_kwargs: Any):
        async def _gen():
            raise self.error
            yield  # pragma: no cover — makes this an async generator

        return _gen()


def _tool_call_agent(dispatch_speed: float) -> AgentGraph:
    """Agent whose LLM issues one tool call, keeping the run in flight during dispatch."""
    agent = AgentGraph.__new__(AgentGraph)
    agent.llm = _ScriptedLLM(
        [AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "call_1"}])],
    )
    agent.llm_with_tools = agent.llm
    agent.orchestrator_tools = MagicMock()
    agent.orchestrator_tools.TOOLS = []
    agent.MAX_ORCHESTRATOR_ITERATIONS = 4

    async def _dispatch(**_kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(dispatch_speed)
        return {"task_description": "stub", "status": "success", "result_text": "stub tool result"}

    agent.orchestrator_tools.dispatch = _dispatch
    agent.get_available_inputs_outputs = _stub_extract
    agent.graph = agent._build_graph()
    return agent


def _answering_agent(answer: str) -> AgentGraph:
    """Agent whose LLM answers immediately (no tool calls)."""
    agent = AgentGraph.__new__(AgentGraph)
    agent.llm = _ScriptedLLM([AIMessage(content=answer)])
    agent.llm_with_tools = agent.llm
    agent.orchestrator_tools = MagicMock()
    agent.orchestrator_tools.TOOLS = []
    agent.MAX_ORCHESTRATOR_ITERATIONS = 4
    agent.get_available_inputs_outputs = _stub_extract
    agent.graph = agent._build_graph()
    return agent


async def _stub_extract(_state) -> dict[str, Any]:
    return {
        "messages": [],
        "filename": "stub.xlsx",
        "available_inputs": {},
        "available_outputs": {},
        "tool_call_count": 0,
        "executed_tool_fingerprints": [],
        "tool_results": [],
    }


_HEADERS = {
    "x-trace-id": "e037c70a-30d7-4c47-b25e-ce18c9c39f15",
    "x-client-id": "CI12345678",
    "x-request-time": "2025-04-08T11:31:45.748539+03:00",
    "x-user-id": "12345678",
}


@pytest.fixture
def api_app(monkeypatch):
    """ASGI app whose ``get_agent`` dependency each test overrides.

    Langfuse is not initialised in tests; ``APP_CTX.tracing`` is stubbed without a
    ``callback_handler`` attribute, so the endpoint skips tracing cleanly.

    The logging middleware is stubbed for the duration of a test: a superseded request
    is deliberately aborted without a response, and ``BaseHTTPMiddleware`` (which sits
    under ``@app.middleware("http")``) turns that into ``RuntimeError("No response
    returned.")``. That is Starlette machinery, not the app contract under test here.
    """
    from aigw_service.api import app_main
    from aigw_service.context import APP_CTX

    monkeypatch.setattr(APP_CTX, "tracing", object())
    original_middleware = app_main.user_middleware[:]
    app_main.user_middleware.clear()
    app_main.middleware_stack = None
    original = dict(app_main.dependency_overrides)
    yield app_main
    app_main.dependency_overrides.clear()
    app_main.dependency_overrides.update(original)
    app_main.user_middleware[:] = original_middleware
    app_main.middleware_stack = None
    get_thread_run_registry.cache_clear()


def _client(api_app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://testserver")


async def _post(client: httpx.AsyncClient, session_id: str, message: str) -> httpx.Response:
    headers = {**_HEADERS, "x-session-id": session_id}
    return await client.post("/api/v1/invoke-agent", json={"message": message}, headers=headers)


async def _committed_contents(agent: AgentGraph, thread_id: str) -> list[str]:
    """Conversation as persisted by the thread's latest checkpoint."""
    snapshot = await agent.graph.aget_state({"configurable": {"thread_id": thread_id}})
    return [getattr(m, "content", "") for m in snapshot.values.get("messages", [])]


class TestSupersededRequestGetsNoResponse:
    """Task 2.3: a superseded run must not answer its client at all."""

    async def test_second_request_wins_and_first_gets_no_response(self, api_app):
        session_id = "a1b2c3d4-0000-4000-8000-000000000001"
        agent_a = _tool_call_agent(dispatch_speed=0.6)
        agent_b = _answering_agent("B answer")

        async with _client(api_app) as client:
            api_app.dependency_overrides[router_module.get_agent] = lambda: agent_a
            task_a = asyncio.create_task(_post(client, session_id, "request A"))

            await asyncio.sleep(0.2)  # A is now blocked inside the slow tool dispatch
            api_app.dependency_overrides[router_module.get_agent] = lambda: agent_b
            response_b = await _post(client, session_id, "request B")

            with pytest.raises(asyncio.CancelledError):
                await task_a

        # A produced no HTTP response at all: neither 409/424 nor a success payload.
        assert task_a.cancelled(), "superseded request must be aborted, not answered"
        assert response_b.status_code == 200
        with zipfile.ZipFile(BytesIO(response_b.content)) as archive:
            assert "txt_response.txt" in archive.namelist()
            assert "B answer" in archive.read("txt_response.txt").decode("utf-8")

    async def test_superseded_run_does_not_reach_the_client(self, api_app):
        """The client of the superseded request gets nothing; the winner's answer is delivered.

        Regression guard for the cancellation being swallowed inside ``tool_executor``: in
        that case run A survived the cancel, produced "Ошибка выполнения" and answered its
        client with a 200 ZIP.
        """
        session_id = "a1b2c3d4-0000-4000-8000-000000000002"
        agent_a = _tool_call_agent(dispatch_speed=0.6)
        agent_b = _answering_agent("B answer")

        async with _client(api_app) as client:
            api_app.dependency_overrides[router_module.get_agent] = lambda: agent_a
            task_a = asyncio.create_task(_post(client, session_id, "request A"))
            await asyncio.sleep(0.2)

            api_app.dependency_overrides[router_module.get_agent] = lambda: agent_b
            response_b = await _post(client, session_id, "request B")
            with pytest.raises(asyncio.CancelledError):
                await task_a

        thread_id = f"{_HEADERS['x-user-id']}:{session_id}"
        assert response_b.status_code == 200, "the winning request must be answered"

        contents = await _committed_contents(agent_b, thread_id)
        assert "B answer" in contents
        assert not any("Ошибка выполнения" in c for c in contents), (
            "a cancelled tool call must not be recorded as an error in the conversation"
        )


class TestWinnerErrorMapping:
    """Task 2.4: StopEventError -> 503 and generic -> 424 still apply to the winner."""

    async def test_stop_event_maps_to_503(self, api_app):
        agent = _answering_agent("unused")
        agent.graph = _FailingStream(StopEventError(user_message="stopped", url="http://x"))

        async with _client(api_app) as client:
            api_app.dependency_overrides[router_module.get_agent] = lambda: agent
            response = await _post(client, "a1b2c3d4-0000-4000-8000-000000000003", "boom")

        assert response.status_code == 503
        assert response.json()["error_description"] == "stopped"

    async def test_generic_error_maps_to_424(self, api_app):
        agent = _answering_agent("unused")
        agent.graph = _FailingStream(RuntimeError("agent exploded"))

        async with _client(api_app) as client:
            api_app.dependency_overrides[router_module.get_agent] = lambda: agent
            response = await _post(client, "a1b2c3d4-0000-4000-8000-000000000004", "boom")

        assert response.status_code == 424
