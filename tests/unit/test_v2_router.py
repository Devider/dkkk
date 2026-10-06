"""Unit-тесты для `/api/v2/upload` и `/api/v2/invoke-agent` (`api/v2/router.py`).

Не поднимают реальный сервер и не вызывают `APP_CTX.on_startup()` (не нужен GigaChat/
LibreOffice) — используют `httpx.ASGITransport` напрямую поверх `app_main` и подменяют
`DeepAgentGraph` через `app.dependency_overrides[get_agent]`, как рекомендует FastAPI для
тестов роутеров. `APP_CTX.agent_memory.store` — настоящий `InMemoryStore` (создаётся в
`AsyncAgentMemory.__init__`, без обращения к Pangolin), поэтому тест на "общий store" проверяет
реальное поведение `("memories", user_id)`-неймспейса, которым пользуются оба роутера.
"""

import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import httpx
import pytest
from httpx import ASGITransport
from langchain_core.messages import AIMessage

from aigw_service.api import app_main
from aigw_service.api.v2.router import get_agent as v2_get_agent
from aigw_service.api.v2.router import get_cb_handler as v2_get_cb_handler
from aigw_service.context import APP_CTX


def _headers(user_id: str, **overrides) -> dict:
    headers = {
        "x-trace-id": str(uuid.uuid4()),
        "x-client-id": "CI12345678",
        "x-request-time": datetime.now(timezone.utc).isoformat(),
        "x-user-id": user_id,
    }
    headers.update(overrides)
    return headers


def _new_user_id() -> str:
    """8-char user id (header max_length=8), unique per test to avoid store cross-talk."""
    return uuid.uuid4().hex[:8]


class _FakeDeepAgentGraph:
    """Stand-in for `DeepAgentGraph` with the same store-lookup/raise contract as the real
    `extract_excel_data` node (`main_graph_deep.py:get_available_inputs_outputs`), so the
    missing-upload and shared-store scenarios exercise real `APP_CTX.agent_memory.store`
    behaviour without needing GigaChat/LibreOffice.
    """

    def __init__(self, response_text: str = "ok"):
        self.logger = None
        self._response_text = response_text
        self.graph = self
        self.last_config: dict | None = None

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        self.last_config = config
        user_id = state.get("user_id")
        namespace = ("memories", user_id)
        stored = await APP_CTX.agent_memory.store.aget(namespace, user_id)
        if not stored or not getattr(stored, "value", {}).get("filename"):
            raise ValueError(f"Файл не найден в store для user_id={user_id}.")
        return {"messages": [AIMessage(content=self._response_text)]}


@pytest.fixture
async def async_client():
    transport = ASGITransport(app=app_main)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def fake_agent():
    """Overrides `get_agent` (a fake graph, no GigaChat/LibreOffice) and `get_cb_handler`'s
    inner callable (Langfuse is not initialized without `APP_CTX.on_startup()`, which this
    router-level unit test does not call — see module docstring).
    """
    agent = _FakeDeepAgentGraph()
    app_main.dependency_overrides[v2_get_agent] = lambda: agent
    app_main.dependency_overrides[v2_get_cb_handler()] = lambda: None
    yield agent
    app_main.dependency_overrides.pop(v2_get_agent, None)
    app_main.dependency_overrides.pop(v2_get_cb_handler(), None)


def _zip_text(content: bytes) -> str:
    with zipfile.ZipFile(BytesIO(content)) as zf:
        return zf.read("txt_response.txt").decode("utf-8")


class TestUploadHeaderValidation:
    async def test_malformed_client_id_rejected(self, async_client):
        response = await async_client.post(
            "/api/v2/upload",
            headers=_headers(_new_user_id(), **{"x-client-id": "not-a-valid-client-id"}),
            files={"file": ("model.xlsx", b"fake-bytes", "application/octet-stream")},
        )
        assert response.status_code == 422

    async def test_malformed_request_time_rejected(self, async_client):
        response = await async_client.post(
            "/api/v2/upload",
            headers=_headers(_new_user_id(), **{"x-request-time": "not-a-timestamp"}),
            files={"file": ("model.xlsx", b"fake-bytes", "application/octet-stream")},
        )
        assert response.status_code == 422


class TestUpload:
    async def test_successful_upload(self, async_client):
        user_id = _new_user_id()
        response = await async_client.post(
            "/api/v2/upload",
            headers=_headers(user_id),
            files={"file": ("model.xlsx", b"fake-bytes", "application/octet-stream")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == f"user_file_{user_id}.xlsx"

        stored = await APP_CTX.agent_memory.store.aget(("memories", user_id), user_id)
        assert stored.value["filename"] == f"user_file_{user_id}.xlsx"

    async def test_non_excel_file_rejected(self, async_client):
        user_id = _new_user_id()
        response = await async_client.post(
            "/api/v2/upload",
            headers=_headers(user_id),
            files={"file": ("model.txt", b"fake-bytes", "text/plain")},
        )
        assert response.status_code == 424


class TestInvokeAgentHeaderValidation:
    async def test_malformed_header_rejected(self, async_client, fake_agent):
        response = await async_client.post(
            "/api/v2/invoke-agent",
            headers=_headers(_new_user_id(), **{"x-client-id": "not-a-valid-client-id"}),
            json={"message": "Привет"},
        )
        assert response.status_code == 422


class TestInvokeAgentRequiresPriorUpload:
    async def test_no_prior_upload_returns_424(self, async_client, fake_agent):
        user_id = _new_user_id()  # never uploaded anything
        response = await async_client.post(
            "/api/v2/invoke-agent",
            headers=_headers(user_id),
            json={"message": "Привет"},
        )
        assert response.status_code == 424


class TestInvokeAgentSuccess:
    async def test_successful_answer_returns_zip(self, async_client, fake_agent):
        user_id = _new_user_id()
        fake_agent._response_text = "Ответ агента для теста"
        await async_client.post(
            "/api/v2/upload",
            headers=_headers(user_id),
            files={"file": ("model.xlsx", b"fake-bytes", "application/octet-stream")},
        )

        response = await async_client.post(
            "/api/v2/invoke-agent",
            headers=_headers(user_id),
            json={"message": "Привет"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert _zip_text(response.content) == "Ответ агента для теста"


class TestSharedStoreAcrossApiVersions:
    """`deep-agent-api` spec: "Upload via the other version is still usable"."""

    async def test_upload_via_v1_then_invoke_via_v2(self, async_client, fake_agent):
        user_id = _new_user_id()
        fake_agent._response_text = "v2 видит файл, загруженный через v1"

        upload_response = await async_client.post(
            "/api/v1/upload",
            headers=_headers(user_id),
            files={"file": ("model.xlsx", b"fake-bytes", "application/octet-stream")},
        )
        assert upload_response.status_code == 200

        invoke_response = await async_client.post(
            "/api/v2/invoke-agent",
            headers=_headers(user_id),
            json={"message": "Привет"},
        )

        assert invoke_response.status_code == 200
        assert _zip_text(invoke_response.content) == "v2 видит файл, загруженный через v1"


class TestThreadIdIsolationFromV1:
    """`DeepAgentGraph` and `AgentGraph` compile against the same
    `APP_CTX.agent_memory.checkpointer` singleton (see `main_graph.py`/`main_graph_deep.py`),
    so without a disjoint `thread_id` namespace a `/api/v1` and a `/api/v2` call sharing
    `user_id`/`session_id` would resolve to the same LangGraph thread and corrupt each other's
    state (`agent-graph`/`deep-agent-api` spec: "v1 and v2 histories do not mix")."""

    async def test_v2_thread_id_is_not_v1s_raw_user_session_key(self, async_client, fake_agent):
        user_id = _new_user_id()
        session_id = str(uuid.uuid4())
        await async_client.post(
            "/api/v2/upload",
            headers=_headers(user_id),
            files={"file": ("model.xlsx", b"fake-bytes", "application/octet-stream")},
        )

        response = await async_client.post(
            "/api/v2/invoke-agent",
            headers=_headers(user_id, **{"x-session-id": session_id}),
            json={"message": "Привет"},
        )

        assert response.status_code == 200
        v1_style_thread_id = f"{user_id}:{session_id}"
        v2_thread_id = fake_agent.last_config["configurable"]["thread_id"]
        assert v2_thread_id != v1_style_thread_id
        assert v2_thread_id == f"v2:{v1_style_thread_id}"
