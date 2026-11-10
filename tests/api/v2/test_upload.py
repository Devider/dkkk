"""Offline V2 upload lifecycle and restoration integration contracts."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openpyxl
import pytest
from tests.unit.test_v2_router import StubDeepAgent, _valid_headers

from aigw_service.api import app_main
from aigw_service.api.v2 import router, upload
from aigw_service.api.v2.catalog_generations import GenerationRepository


def workbook_bytes(name: str = "Цена меди") -> bytes:
    """Make named rows with and without values in both workbook catalogs."""
    book = openpyxl.Workbook()
    book.active.title = "Inputs"
    book.active.append(["Наименование", 2025])
    book.active.append([name, None])
    sheet = book.create_sheet("Outputs")
    sheet.append(["Наименование", 2025])
    sheet.append(["Выручка", None])
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


@pytest.fixture
def setup_upload(repository: GenerationRepository, monkeypatch: pytest.MonkeyPatch) -> GenerationRepository:
    """Stub only cloud/correction infrastructure, leaving ASGI routes real."""
    monkeypatch.setattr(upload, "get_repository", lambda: repository)
    monkeypatch.setattr(router, "get_repository", lambda: repository)
    monkeypatch.setattr(upload, "correct_workbook_on_disk", lambda path: {"status": "OK", "message": "Corrected"})
    monkeypatch.setattr(upload.APP_CTX, "agent_memory", SimpleNamespace(store=SimpleNamespace(aput=AsyncMock())))
    monkeypatch.setattr(router.APP_CTX, "tracing", None)
    return repository


@pytest.mark.parametrize("extension", ["xlsx", "xls"])
async def test_upload_reuse_replace_failure_and_restart(
    setup_upload: GenerationRepository,
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
) -> None:
    """Only successful preparation changes active workbook; restart embeds no documents."""
    repo = setup_upload

    def convert(source: Path, destination: Path) -> None:
        """Stub LibreOffice conversion while asserting original extension reaches it."""
        assert source.suffix == ".xls"
        destination.write_bytes(source.read_bytes())

    monkeypatch.setattr(upload, "convert_legacy_workbook", convert)
    original = workbook_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_main), base_url="http://test") as client:

        async def send(content: bytes) -> httpx.Response:
            """Upload one supported original file through the hidden V2 route."""
            return await client.post(
                "/api/v2/upload", files={"file": (f"book.{extension}", content)}, headers=_valid_headers()
            )

        first = await send(original)
        assert first.status_code == 200, first.text
        assert set(first.json()) == {"content", "filename", "save_dir"}
        old = repo.load("user0001")
        assert len(old.index.catalogs["inputs"]) == len(old.index.catalogs["outputs"]) == 1
        assert old.index.catalogs["inputs"][0]["name"] == "Цена меди"
        repo.embeddings.calls.clear()
        repeated = await send(original)
        assert repeated.json()["filename"] == first.json()["filename"]
        assert repo.embeddings.calls == []
        changed = await send(workbook_bytes("Объём меди"))
        assert changed.status_code == 200
        assert changed.json()["filename"] != first.json()["filename"]
        assert not old.workbook.exists()
        active = repo.load("user0001")
        original_embed = repo.embeddings.embed_documents

        def fail(texts: list[str]) -> list[list[float]]:
            """Simulate cloud outage during changed-file preparation."""
            raise RuntimeError("embedding outage")

        monkeypatch.setattr(repo.embeddings, "embed_documents", fail)
        failed = await send(workbook_bytes("Другой параметр"))
        assert failed.status_code == 424
        assert "error_description" in failed.json()
        assert repo.load("user0001").identity == active.identity
        monkeypatch.setattr(repo.embeddings, "embed_documents", original_embed)
        repo.embeddings.calls.clear()
        fresh = GenerationRepository(repo.settings, repo.embeddings)
        monkeypatch.setattr(router, "get_repository", lambda: fresh)
        store = SimpleNamespace(aput=AsyncMock())
        monkeypatch.setattr(upload.APP_CTX, "agent_memory", SimpleNamespace(store=store))
        agent = StubDeepAgent()
        monkeypatch.setattr(router, "get_deep_agent", lambda: agent)
        result = await client.post(
            "/api/v2/invoke-agent", json={"message": "Analyze copper"}, headers=_valid_headers()
        )
        assert result.status_code == 200, result.text
        store.aput.assert_awaited_once_with(("memories", "user0001"), "user0001", {"filename": active.workbook.name})
        assert agent.calls[0][1]["configurable"]["catalog_generation"] == active.identity
        assert repo.embeddings.calls == []


async def test_v1_upload_retains_contract(setup_upload: GenerationRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shared extension validation leaves V1 correction and filenames intact."""
    from aigw_service.api.v1 import router as v1

    store = SimpleNamespace(aput=AsyncMock(), aget=AsyncMock())
    monkeypatch.setattr(v1.APP_CTX, "agent_memory", SimpleNamespace(store=store))
    monkeypatch.setattr(v1, "correct_workbook_on_disk", lambda path: {"status": "OK", "message": "V1 corrected"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/upload", files={"file": ("book.xlsx", workbook_bytes())}, headers=_valid_headers()
        )
        assert response.status_code == 200
        assert response.json()["filename"] == "user_file_user0001.xlsx"
        assert response.json()["content"] == "V1 corrected"
        failed = await client.post(
            "/api/v1/upload", files={"file": ("book.txt", b"invalid")}, headers=_valid_headers()
        )
        assert failed.status_code == 424
        assert "error_description" in failed.json()


async def test_restart_restores_real_workbook_tool_lookup(
    setup_upload: GenerationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh memory store can invoke the actual cached-value workbook tool."""
    from langgraph.store.memory import InMemoryStore

    from aigw_service.api.v1.tools import get_input_info

    repo = setup_upload
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_main), base_url="http://test") as client:
        response = await client.post(
            "/api/v2/upload", files={"file": ("book.xlsx", workbook_bytes())}, headers=_valid_headers()
        )
        assert response.status_code == 200
        repo.embeddings.calls.clear()
        monkeypatch.setattr(upload.APP_CTX, "agent_memory", SimpleNamespace(store=InMemoryStore()))

        class WorkbookAgent(StubDeepAgent):
            """Use the real workbook tool after router restoration."""

            async def ainvoke(self, payload: dict, *, config: dict) -> dict:
                """Read a named row without a remote model or LibreOffice."""
                result = get_input_info.invoke({"input_name": "Цена меди", "year_range": [2025]}, config=config)
                assert result.status == "PARTIAL", result
                assert result.content.input_name == "Цена меди"
                return await super().ainvoke(payload, config=config)

        monkeypatch.setattr(router, "get_deep_agent", WorkbookAgent)
        monkeypatch.setattr(router, "get_repository", lambda: GenerationRepository(repo.settings, repo.embeddings))
        invoked = await client.post(
            "/api/v2/invoke-agent", json={"message": "Read copper price"}, headers=_valid_headers()
        )
        assert invoked.status_code == 200, invoked.text
        assert repo.embeddings.calls == []


def test_simultaneous_same_user_uploads_reuse_complete_generation(setup_upload: GenerationRepository) -> None:
    """Separate worker threads publish one matching pair for identical original bytes."""
    from concurrent.futures import ThreadPoolExecutor

    repo = setup_upload
    content = workbook_bytes()

    def run_upload() -> str:
        """Use the same exclusive OS lock as each upload worker."""
        with repo.lock("user0001", exclusive=True):
            generation, _ = upload.prepare_upload(repo, "user0001", content, "xlsx")
            repo.cleanup("user0001", generation.identity)
            return generation.identity

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_upload) for _ in range(2)]
        identities = [future.result(timeout=5) for future in futures]
    assert identities[0] == identities[1] == repo.load("user0001").identity
    assert len(repo.embeddings.calls) == 2  # one Inputs batch and one Outputs batch


def test_corrupt_artifacts_rebuild_on_identical_reupload(setup_upload: GenerationRepository) -> None:
    """Same bytes never reuse incomplete or incompatible persisted vectors."""
    repo = setup_upload
    content = workbook_bytes()
    with repo.lock("user0001", exclusive=True):
        first, _ = upload.prepare_upload(repo, "user0001", content, "xlsx")
        (repo.user_directory("user0001") / first.identity / "inputs.faiss").unlink()
        repo.embeddings.calls.clear()
        rebuilt, _ = upload.prepare_upload(repo, "user0001", content, "xlsx")
        assert rebuilt.identity != first.identity
        assert len(repo.embeddings.calls) == 2
        repo.settings = repo.settings.model_copy(update={"embedding_revision": "next"})
        changed, _ = upload.prepare_upload(repo, "user0001", content, "xlsx")
        assert changed.identity != rebuilt.identity
        assert repo.load("user0001").identity == changed.identity
