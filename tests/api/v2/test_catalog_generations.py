"""Durable identities, compatibility and reader/replacement isolation."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from tests.api.v2.test_catalog_index import FakeEmbeddings

from aigw_service.api.v2.catalog_generations import Generation, GenerationRepository
from aigw_service.api.v2.catalog_index import CatalogIndex


def prepare(repo: GenerationRepository, user: str, original: str = "hash") -> Generation:
    """Prepare complete immutable test artifacts with no external backend."""
    identity = uuid4().hex
    workbook = Path(tempfile.gettempdir()) / f"v2_{identity}.xlsx"
    workbook.write_bytes(identity.encode())
    index = CatalogIndex.build({"inputs": [{"id": "IN:2", "name": "Цена меди"}], "outputs": []}, repo.embeddings)
    return repo.prepare(user, identity, workbook, original, index)


def test_restart_integrity_and_fingerprint(repository: GenerationRepository) -> None:
    """Fresh instances reuse vectors; corruption and model changes cannot reuse them."""
    generation = prepare(repository, "alice")
    repository.publish("alice", generation)
    fresh = GenerationRepository(repository.settings, FakeEmbeddings())
    assert fresh.load("alice").identity == generation.identity
    assert fresh.embeddings.calls == []
    fresh.settings = fresh.settings.model_copy(update={"embedding_revision": "2"})
    with pytest.raises(ValueError, match="Incompatible embeddings"):
        fresh.load("alice")
    generation.workbook.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="corrupt workbook"):
        repository.load("alice")


def test_failed_publication_preserves_old_and_other_user(
    repository: GenerationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed atomic replace never exposes the prepared generation."""
    first, other, replacement = prepare(repository, "alice"), prepare(repository, "bob"), prepare(repository, "alice")
    repository.publish("alice", first)
    repository.publish("bob", other)

    def fail_replace(source: Path, target: Path) -> None:
        """Simulate filesystem publication failure."""
        raise OSError("publication failed")

    monkeypatch.setattr("aigw_service.api.v2.catalog_generations.os.replace", fail_replace)
    with pytest.raises(OSError, match="publication"):
        repository.publish("alice", replacement)
    assert repository.load("alice").identity == first.identity
    assert repository.load("bob").identity == other.identity


def test_replacement_waits_for_pinned_reader(repository: GenerationRepository) -> None:
    """A different worker cannot delete or mix a generation during invocation."""
    first, second = prepare(repository, "alice"), prepare(repository, "alice")
    repository.publish("alice", first)
    started, finished = Event(), Event()

    def replace() -> None:
        """Publish only after the reader's shared lock is released."""
        started.set()
        with repository.lock("alice", exclusive=True):
            repository.publish("alice", second)
            repository.cleanup("alice", second.identity)
        finished.set()

    with ThreadPoolExecutor() as pool:
        with repository.lock("alice"):
            future = pool.submit(replace)
            assert started.wait(2)
            assert not finished.wait(0.05)
            assert repository.load("alice").identity == first.identity
            assert first.workbook.exists()
        future.result(timeout=3)
    assert finished.is_set()
    assert repository.load("alice").identity == second.identity
    assert not first.workbook.exists()
