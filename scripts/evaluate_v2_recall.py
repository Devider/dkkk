"""Opt-in candidate recall evaluation, independent of matching LLM choices."""

import argparse
import json
from pathlib import Path

from langchain_core.embeddings import Embeddings

from aigw_service.api.v2.catalog_index import CatalogIndex
from aigw_service.api.v2.embeddings import CloudEmbeddings, RetrievalSettings

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests/api/v2/fixtures/recall.json"


def evaluate(fixture: dict, embeddings: Embeddings, k: int) -> dict:
    """Measure row-ID candidate inclusion with category-specific batched retrieval."""
    index = CatalogIndex.build(fixture["catalogs"], embeddings)
    results = []
    for kind in ("inputs", "outputs"):
        cases = [case for case in fixture["cases"] if case["kind"] == kind]
        candidates = index.candidates(kind, [case["mention"] for case in cases], embeddings, k)
        for case, rows in zip(cases, candidates, strict=True):
            ids = [row["id"] for row in rows]
            results.append({**case, "candidates": ids, "hit": case["expected_id"] in ids})
    hits = sum(result["hit"] for result in results)
    return {
        "k": k,
        "hits": hits,
        "total": len(results),
        "recall": hits / len(results) if results else None,
        "cases": results,
    }


def main() -> None:
    """Evaluate Cloud.ru only when credentials are configured and explicitly invoked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = RetrievalSettings()
    if not settings.api_key.get_secret_value():
        parser.exit(2, "EMBEDDING_API_KEY is required for live candidate recall evaluation.\n")
    result = evaluate(json.loads(args.fixture.read_text()), CloudEmbeddings(settings), settings.retrieval_k)
    result["embedding_fingerprint"] = settings.fingerprint()
    report = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(report + "\n")
    print(report)


if __name__ == "__main__":
    main()
