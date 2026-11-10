"""Transactional V2 workbook preparation and active-reference restoration."""

import hashlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import Depends, File, UploadFile
from fastapi.responses import JSONResponse

from aigw_service.api.v1.excel_handler import STATUS_ERROR, ExcelWorkbook, correct_workbook_on_disk
from aigw_service.api.v1.router import validate_workbook_extension
from aigw_service.api.v1.schemas_file import FailedDependencyResponse, FileLoaderResponse
from aigw_service.api.v1.utils import common_headers
from aigw_service.api.v2.catalog_generations import Generation, GenerationRepository, get_repository
from aigw_service.api.v2.catalog_index import CatalogIndex
from aigw_service.context import APP_CTX


@asynccontextmanager
async def user_lock(repo: GenerationRepository, user_id: str, *, exclusive: bool = False) -> AsyncIterator[None]:
    """Acquire blocking OS locks off the event loop and release after protected work."""
    lock = repo.lock(user_id, exclusive=exclusive)
    await anyio.to_thread.run_sync(lock.__enter__)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def convert_legacy_workbook(source: Path, destination: Path) -> None:
    """Convert an accepted legacy XLS to readable OOXML using the existing backend."""
    with ExcelWorkbook(str(source), logger=APP_CTX.get_logger()) as workbook:
        workbook.save_as_xlsx(str(destination))


def prepare_upload(repo: GenerationRepository, user_id: str, content: bytes, extension: str) -> tuple[Generation, str]:
    """Reuse a verified active identity or build complete artifacts before publication."""
    from aigw_service.api.v2.name_resolution import build_workbook_catalog

    digest = hashlib.sha256(content).hexdigest()
    try:
        active = repo.load(user_id)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        active = None
    if active is not None and active.manifest["original_sha256"] == digest:
        return active, "Файл загружен. Поисковые каталоги переиспользованы."
    identity = uuid4().hex
    workbook = Path(tempfile.gettempdir()) / f"v2_{identity}.xlsx"
    generation_dir = repo.user_directory(user_id) / identity
    try:
        with tempfile.TemporaryDirectory(prefix="v2_stage_") as stage:
            source = Path(stage) / f"original.{extension}"
            source.write_bytes(content)
            if extension == "xls":
                converted = Path(stage) / "converted.xlsx"
                convert_legacy_workbook(source, converted)
                source = converted
            correction = correct_workbook_on_disk(str(source))
            if correction["status"] == STATUS_ERROR:
                raise ValueError(correction["message"])
            catalogs = build_workbook_catalog(str(source))
            index = CatalogIndex.build(catalogs, repo.embeddings)
            shutil.copyfile(source, workbook)
            generation = repo.prepare(user_id, identity, workbook, digest, index)
            repo.publish(user_id, generation)
        return generation, correction["message"]
    except BaseException:
        # Never remove a generation if its active pointer was already committed.
        pointer = repo.user_directory(user_id) / "active.json"
        committed = pointer.exists() and f'"generation": "{identity}"' in pointer.read_text()
        if not committed:
            workbook.unlink(missing_ok=True)
            shutil.rmtree(generation_dir, ignore_errors=True)
        raise


async def restore_workbook(user_id: str, generation: Generation) -> None:
    """Repair the existing tool store from the persisted authoritative manifest."""
    await APP_CTX.agent_memory.store.aput(("memories", user_id), user_id, {"filename": generation.workbook.name})


async def upload_file_v2(
    file: UploadFile = File(...),
    headers: dict[str, str] = Depends(common_headers),
) -> FileLoaderResponse | JSONResponse:
    """Prepare searchable catalogs before reporting the existing upload response."""
    try:
        extension = validate_workbook_extension(file.filename or "")
        user_id = headers["x-user-id"]
        content = await file.read()
        repo = get_repository()
        async with user_lock(repo, user_id, exclusive=True):
            generation, message = await anyio.to_thread.run_sync(prepare_upload, repo, user_id, content, extension)
            await restore_workbook(user_id, generation)
            await anyio.to_thread.run_sync(repo.cleanup, user_id, generation.identity)
        return FileLoaderResponse(
            content=message, filename=generation.workbook.name, save_dir=str(generation.workbook.parent)
        )
    except Exception as exc:
        APP_CTX.get_logger().opt(exception=True).error("V2 upload preparation failed: {}", str(exc))
        return JSONResponse(status_code=424, content=FailedDependencyResponse(error_description=str(exc)).model_dump())
