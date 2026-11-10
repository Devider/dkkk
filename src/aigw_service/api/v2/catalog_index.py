"""Exact category-separated cosine indexes with native FAISS/JSON persistence."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from aigw_service.api.v2.embeddings import validate_vectors

Catalog = list[dict[str, str]]


def sha256_file(path: Path) -> str:
    """Hash workbook or artifact bytes without reading a whole file into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def make_store(index: faiss.Index, rows: Catalog, embeddings: Embeddings) -> FAISS:
    """Reconstruct LangChain document mappings without pickle deserialization."""
    documents = {row["id"]: Document(page_content=row["name"], metadata={"id": row["id"]}) for row in rows}
    return FAISS(
        embeddings,
        index,
        InMemoryDocstore(documents),
        {i: row["id"] for i, row in enumerate(rows)},
        distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
    )


@dataclass
class CatalogIndex:
    """A generation's immutable catalogs and exact vector stores."""

    catalogs: dict[str, Catalog]
    stores: dict[str, FAISS | None]
    dimension: int

    @classmethod
    def build(cls, catalogs: dict[str, Catalog], embeddings: Embeddings) -> "CatalogIndex":
        """Embed each named row, retaining duplicate names as distinct documents."""
        stores: dict[str, FAISS | None] = {}
        dimension = 0
        for kind in ("inputs", "outputs"):
            rows = catalogs[kind]
            if not rows:
                stores[kind] = None
                continue
            vectors = validate_vectors(
                embeddings.embed_documents([row["name"] for row in rows]), len(rows), dimension or None
            )
            dimension = vectors.shape[1]
            index = faiss.IndexFlatIP(dimension)
            index.add(vectors)
            stores[kind] = make_store(index, rows, embeddings)
        return cls(catalogs, stores, dimension)

    def candidates(self, kind: str, mentions: list[str], embeddings: Embeddings, k: int) -> list[Catalog]:
        """Batch mentions once and bound each result to its category and positive k."""
        if k < 1:
            raise ValueError("retrieval k must be positive")
        store = self.stores[kind]
        if not mentions or store is None:
            return [[] for _ in mentions]
        nonempty = [mention for mention in mentions if mention.strip()]
        if not nonempty:
            return [[] for _ in mentions]
        vectors = validate_vectors(embeddings.embed_documents(nonempty), len(nonempty), self.dimension)
        ranked = iter(
            [
                [
                    {"id": doc.metadata["id"], "name": doc.page_content}
                    for doc, _score in store.similarity_search_with_score_by_vector(
                        vector.tolist(), k=min(k, len(self.catalogs[kind]))
                    )
                ]
                for vector in vectors
            ]
        )
        return [next(ranked) if mention.strip() else [] for mention in mentions]

    def save(self, directory: Path) -> dict[str, str]:
        """Write native indexes and JSON, returning checksums for the manifest."""
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "catalogs.json").write_text(json.dumps(self.catalogs, ensure_ascii=False), encoding="utf-8")
        artifacts = ["catalogs.json"]
        for kind, store in self.stores.items():
            if store is not None:
                filename = f"{kind}.faiss"
                faiss.write_index(store.index, str(directory / filename))
                artifacts.append(filename)
        return {name: sha256_file(directory / name) for name in artifacts}

    @classmethod
    def load(cls, directory: Path, manifest: dict, embeddings: Embeddings) -> "CatalogIndex":
        """Verify integrity and geometry before accepting persisted native indexes."""
        _verify_artifacts(directory, manifest["artifacts"])
        catalogs = json.loads((directory / "catalogs.json").read_text(encoding="utf-8"))
        stores: dict[str, FAISS | None] = {}
        dimension = manifest["dimension"]
        for kind, prefix in (("inputs", "IN:"), ("outputs", "OUT:")):
            stores[kind] = _load_category(directory, kind, prefix, catalogs[kind], manifest, embeddings)
        return cls(catalogs, stores, dimension)


def _verify_artifacts(directory: Path, artifacts: dict[str, str]) -> None:
    """Validate checksums before invoking the native FAISS parser."""
    for name, digest in artifacts.items():
        if name not in {"catalogs.json", "inputs.faiss", "outputs.faiss"}:
            raise ValueError("Unknown index artifact")
        if sha256_file(directory / name) != digest:
            raise ValueError("Corrupt index artifact")
    if "catalogs.json" not in artifacts:
        raise ValueError("Missing catalog metadata")


def _load_category(
    directory: Path,
    kind: str,
    prefix: str,
    rows: Catalog,
    manifest: dict,
    embeddings: Embeddings,
) -> FAISS | None:
    """Validate one catalog's row identities and reconstruct its normalized index."""
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)) or any(not value.startswith(prefix) for value in ids):
        raise ValueError("Invalid catalog row identities")
    if any(not isinstance(row["name"], str) or not row["name"].strip() for row in rows):
        raise ValueError("Invalid canonical name")
    if not rows:
        return None
    if f"{kind}.faiss" not in manifest["artifacts"]:
        raise ValueError("Missing category index")
    index = faiss.read_index(str(directory / f"{kind}.faiss"))
    dimension = manifest["dimension"]
    if not isinstance(index, faiss.IndexFlatIP) or index.d != dimension or index.ntotal != len(rows):
        raise ValueError("Incompatible index geometry")
    vectors = index.reconstruct_n(0, index.ntotal)
    normalized = validate_vectors(vectors.tolist(), len(rows), dimension)
    if not np.allclose(vectors, normalized, atol=1e-5):
        raise ValueError("Index vectors are not normalized")
    return make_store(index, rows, embeddings)
