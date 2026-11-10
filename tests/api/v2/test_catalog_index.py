"""Deterministic vector-store persistence and category contracts."""

from pathlib import Path

import pytest
from langchain_core.embeddings import Embeddings

from aigw_service.api.v2.catalog_index import CatalogIndex


class FakeEmbeddings(Embeddings):
    """A small bilingual oracle for mechanics, not a production quality model."""

    def __init__(self) -> None:
        """Record document/query batches for cost assertions."""
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Assign aliases to deterministic semantic axes."""
        self.calls.append(texts)
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """Map representative Russian/English names to identical directions."""
        lower = text.lower()
        if any(word in lower for word in ("мед", "медь", "copper")):
            return [1, 0, 0]
        if any(word in lower for word in ("выруч", "revenue")):
            return [0, 1, 0]
        return [0, 0, 1]


def test_duplicate_rows_category_separation_and_empty_queries(tmp_path: Path) -> None:
    """Named rows with no values survive and categories never cross."""
    embeddings = FakeEmbeddings()
    catalogs = {
        "inputs": [
            {"id": "IN:2", "name": "Цена меди"},
            {"id": "IN:3", "name": "Цена меди"},
        ],
        "outputs": [{"id": "OUT:4", "name": "Выручка"}],
    }
    index = CatalogIndex.build(catalogs, embeddings)
    assert len(index.candidates("inputs", ["copper price"], embeddings, 10)[0]) == 2
    assert index.candidates("outputs", ["copper price"], embeddings, 1) == [catalogs["outputs"]]
    before = len(embeddings.calls)
    assert index.candidates("inputs", [], embeddings, 10) == []
    empty = CatalogIndex.build({"inputs": [], "outputs": []}, embeddings)
    assert empty.candidates("inputs", ["copper"], embeddings, 10) == [[]]
    assert len(embeddings.calls) == before


def test_native_roundtrip_without_document_embeddings(tmp_path: Path) -> None:
    """Loading JSON/native vectors reproduces candidates without embedding rows."""
    embeddings = FakeEmbeddings()
    index = CatalogIndex.build({"inputs": [{"id": "IN:2", "name": "Цена меди"}], "outputs": []}, embeddings)
    manifest = {"artifacts": index.save(tmp_path), "dimension": index.dimension}
    embeddings.calls.clear()
    loaded = CatalogIndex.load(tmp_path, manifest, embeddings)
    assert embeddings.calls == []
    assert loaded.candidates("inputs", ["copper"], embeddings, 1) == [[{"id": "IN:2", "name": "Цена меди"}]]
    (tmp_path / "inputs.faiss").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Corrupt"):
        CatalogIndex.load(tmp_path, manifest, embeddings)


def test_mentions_are_batched_and_top_k_is_bounded() -> None:
    """Full-catalog retrieval includes non-gold inputs and skips blank mentions."""
    embeddings = FakeEmbeddings()
    rows = [{"id": f"IN:{i}", "name": name} for i, name in enumerate(["Цена меди", "Выручка", "Цена золота"], start=2)]
    index = CatalogIndex.build({"inputs": rows, "outputs": []}, embeddings)
    embeddings.calls.clear()
    candidates = index.candidates("inputs", ["copper", "", "revenue"], embeddings, 1)
    assert embeddings.calls == [["copper", "revenue"]]
    assert candidates == [[rows[0]], [], [rows[1]]]
    with pytest.raises(ValueError, match="positive"):
        index.candidates("inputs", ["copper"], embeddings, 0)


def test_bilingual_recall_harness_with_deterministic_embeddings() -> None:
    """Check recall computation separately from an LLM or production embedding quality."""
    import json

    from scripts.evaluate_v2_recall import DEFAULT_FIXTURE, evaluate

    result = evaluate(json.loads(DEFAULT_FIXTURE.read_text()), FakeEmbeddings(), 10)
    assert result["hits"] == 4
    assert result["total"] == 8
    assert result["recall"] == 0.5
    assert all(len(case["candidates"]) == 10 for case in result["cases"])
