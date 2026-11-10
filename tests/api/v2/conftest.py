"""Isolated repositories for offline V2 tests."""

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.api.v2.test_catalog_index import FakeEmbeddings

from aigw_service.api.v2.catalog_generations import GenerationRepository
from aigw_service.api.v2.embeddings import RetrievalSettings


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[GenerationRepository]:
    """Create a repository and clean up its uniquely named temporary workbooks."""
    repo = GenerationRepository(RetrievalSettings(index_dir=tmp_path), FakeEmbeddings())
    yield repo
    for manifest in tmp_path.rglob("manifest.json"):
        data = json.loads(manifest.read_text())
        (Path(tempfile.gettempdir()) / data["filename"]).unlink(missing_ok=True)
