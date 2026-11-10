"""Single-host durable generations, with readers coordinated by OS file locks."""

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from langchain_core.embeddings import Embeddings

from aigw_service.api.v2.catalog_index import CatalogIndex, sha256_file
from aigw_service.api.v2.embeddings import RetrievalSettings, get_embeddings

FORMAT_VERSION = 1


@dataclass
class Generation:
    """A verified workbook and matching immutable catalogs."""

    manifest: dict
    index: CatalogIndex

    @property
    def identity(self) -> str:
        """Return the stable ID used by checkpointed clarification."""
        return self.manifest["generation"]

    @property
    def workbook(self) -> Path:
        """Resolve the unique workbook using existing calculation-tool conventions."""
        return Path(tempfile.gettempdir()) / self.manifest["filename"]


class GenerationRepository:
    """Keep each user's active pointer authoritative across workers and restarts."""

    def __init__(self, settings: RetrievalSettings, embeddings: Embeddings) -> None:
        """Inject paths and embeddings so persistence tests remain offline."""
        self.settings = settings
        self.embeddings = embeddings

    def user_directory(self, user_id: str) -> Path:
        """Derive a safe isolated namespace from trusted authenticated identity."""
        if not user_id:
            raise ValueError("Не удалось определить пользователя для поиска загруженной модели.")
        return self.settings.index_dir / hashlib.sha256(user_id.encode()).hexdigest()

    @contextmanager
    def lock(self, user_id: str, *, exclusive: bool = False) -> Iterator[None]:
        """Pin readers or serialize replacement across processes on one host."""
        directory = self.user_directory(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".lock").open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def load(self, user_id: str) -> Generation:
        """Load and verify the authoritative active generation under a caller's lock."""
        directory = self.user_directory(user_id)
        try:
            active = json.loads((directory / "active.json").read_text())
        except FileNotFoundError as exc:
            raise FileNotFoundError("Сначала загрузите Excel-модель через /api/v2/upload.") from exc
        identity = active["generation"]
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Invalid generation identity; reupload workbook")
        generation_dir = directory / identity
        manifest = json.loads((generation_dir / "manifest.json").read_text())
        if active != manifest or manifest["format_version"] != FORMAT_VERSION:
            raise ValueError("Incompatible generation manifest; reupload workbook")
        if manifest["fingerprint"] != self.settings.fingerprint() or manifest["normalization"] != "l2":
            raise ValueError("Incompatible embeddings; reupload workbook")
        filename = manifest["filename"]
        if filename != f"v2_{identity}.xlsx":
            raise ValueError("Invalid workbook reference")
        generation = Generation(manifest, CatalogIndex.load(generation_dir, manifest, self.embeddings))
        if sha256_file(generation.workbook) != manifest["workbook_sha256"]:
            raise ValueError("Missing or corrupt workbook; reupload workbook")
        return generation

    def publish(self, user_id: str, generation: Generation) -> None:
        """Atomically select complete artifacts; failed replacement keeps the old pointer."""
        directory = self.user_directory(user_id)
        staged = directory / "active.pending.json"
        try:
            with staged.open("w") as stream:
                json.dump(generation.manifest, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, directory / "active.json")
        finally:
            staged.unlink(missing_ok=True)

    def prepare(
        self, user_id: str, identity: str, workbook: Path, original_hash: str, index: CatalogIndex
    ) -> Generation:
        """Persist all artifacts and verify them before the active pointer can change."""
        directory = self.user_directory(user_id) / identity
        manifest = {
            "format_version": FORMAT_VERSION,
            "generation": identity,
            "original_sha256": original_hash,
            "workbook_sha256": sha256_file(workbook),
            "filename": workbook.name,
            "fingerprint": self.settings.fingerprint(),
            "dimension": index.dimension,
            "normalization": "l2",
            "artifacts": index.save(directory),
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        verified = CatalogIndex.load(directory, manifest, self.embeddings)
        return Generation(manifest, verified)

    def cleanup(self, user_id: str, keep: str) -> None:
        """Retire old artifacts only with the exclusive lock, after all readers exit."""
        for directory in self.user_directory(user_id).iterdir():
            if directory.is_dir() and directory.name != keep:
                workbook = Path(tempfile.gettempdir()) / f"v2_{directory.name}.xlsx"
                workbook.unlink(missing_ok=True)
                shutil.rmtree(directory)


def get_repository() -> GenerationRepository:
    """Construct cheap repository access without initializing a cloud client."""
    return GenerationRepository(RetrievalSettings(), get_embeddings())
