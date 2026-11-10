"""Cloud adapter contracts without credentials or network."""

import json
from unittest.mock import Mock

import httpx
import pytest
from langchain_openai import OpenAIEmbeddings

from aigw_service.api.v2.embeddings import CloudEmbeddings, RetrievalSettings, validate_vectors


def test_raw_string_batch_and_query() -> None:
    """Assert the actual SDK HTTP payload retains strings and float encoding."""
    payloads: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Return valid vectors for every raw input string."""
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0, 2.0], "object": "embedding"}
                    for i, _ in enumerate(payload["input"])
                ],
                "model": "fake",
                "object": "list",
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )

    adapter = CloudEmbeddings(RetrievalSettings())
    adapter._client = OpenAIEmbeddings(
        api_key="fake",
        base_url="https://example.test/v1",
        check_embedding_ctx_length=False,
        chunk_size=2,
        model_kwargs={"encoding_format": "float"},
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    assert len(adapter.embed_documents(["Медь", "copper", "Выручка"])) == 3
    assert len(adapter.embed_query("profit")) == 2
    assert [p["input"] for p in payloads] == [["Медь", "copper"], ["Выручка"], ["profit"]]
    assert all(p["encoding_format"] == "float" for p in payloads)


@pytest.mark.parametrize("vectors", [[], [[0, 0]], [[float("nan"), 1]], [[float("inf"), 1]], [[1, 2], [3]]])
def test_invalid_vectors(vectors: list[list[float]]) -> None:
    """Invalid counts, dimensions and values cannot enter an index."""
    with pytest.raises(ValueError, match="embedding|Embeddings|inhomogeneous"):
        validate_vectors(vectors, 1)


def test_empty_calls_are_lazy() -> None:
    """Empty catalogs do not construct a credentialed client."""
    adapter = CloudEmbeddings(RetrievalSettings())
    adapter._get_client = Mock(side_effect=AssertionError("unexpected client"))
    assert adapter.embed_documents([]) == []


def test_dedicated_key_precedes_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the user's dedicated embedding secret while supporting older deployments."""
    monkeypatch.setenv("EMBEDDING_API_KEY", "dedicated-test-key")
    monkeypatch.setenv("API_KEY", "legacy-test-key")
    assert RetrievalSettings().api_key.get_secret_value() == "dedicated-test-key"
    monkeypatch.delenv("EMBEDDING_API_KEY")
    assert RetrievalSettings().api_key.get_secret_value() == "legacy-test-key"
