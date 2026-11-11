"""Exercise the real Ollama schema adapter and resolver with offline HTTP."""

import json
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from tests.api.v2.test_catalog_generations import prepare

from aigw_service.api.v2 import name_resolution as resolver
from aigw_service.api.v2.catalog_generations import GenerationRepository
from aigw_service.api.v2.catalog_index import CatalogIndex
from aigw_service.api.v2.embeddings import RetrievalSettings


def mock_model(*, ambiguous: bool = False, invalid: bool = False) -> ChatOllama:
    """Return schema-specific JSON through the adapter's actual HTTP/parser path."""

    def respond(request: httpx.Request) -> httpx.Response:
        """Inspect the native schema and synthesize a validated completion."""
        payload = json.loads(request.content)
        schema = payload["format"]["title"]
        assert payload["model"] == "qwen3.5:9b-mlx"
        if schema == "ExtractedNames":
            content = {"inputs": ["copper"], "outputs": ["EBITDA"]}
        else:
            is_input = schema == "InputResolutionAnalysis"
            kind = "input" if is_input else "output"
            name = "Цена меди" if is_input else "EBITDA"
            candidates = [name, "Цена алюминия"] if is_input and ambiguous else [name]
            content = {
                f"mentioned_{kind}s": [
                    {
                        f"mentioned_{kind}_name": "copper" if is_input else "EBITDA",
                        f"equivalent_{kind}_name": "Not in retrieved catalog" if invalid else name,
                        f"equivalent_{kind}_id": "IN:999" if invalid else "IN:2" if is_input else "OUT:2",
                        "candidate_names": candidates,
                        "is_unambiguous": not (is_input and ambiguous),
                        "explanation": "Schema fixture explanation",
                    }
                ]
            }
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "message": {"role": "assistant", "content": json.dumps(content)},
                "done": True,
                "done_reason": "stop",
            },
        )

    return ChatOllama(model="qwen3.5:9b-mlx", client_kwargs={"transport": httpx.MockTransport(respond)})


def resolver_graph(model: ChatOllama) -> CompiledStateGraph:
    """Give the production subgraph checkpointing for interrupt/resume tests."""
    parent = StateGraph(resolver._ResolverState)
    parent.add_node("resolver", resolver.build_name_resolver(model)["runnable"])
    parent.add_edge(START, "resolver")
    parent.add_edge("resolver", END)
    return parent.compile(checkpointer=MemorySaver())


def publish_catalogs(repository: GenerationRepository) -> None:
    """Prepare the complete candidate fixture without cloud embeddings."""
    generation = prepare(repository, "user0001")
    index = CatalogIndex.build(
        {
            "inputs": [{"id": "IN:2", "name": "Цена меди"}, {"id": "IN:3", "name": "Цена алюминия"}],
            "outputs": [{"id": "OUT:2", "name": "EBITDA"}],
        },
        repository.embeddings,
    )
    generation = repository.prepare("user0001", generation.identity, generation.workbook, "hash", index)
    repository.publish("user0001", generation)


@pytest.mark.parametrize("invalid", [False, True])
async def test_schema_adapter_preserves_membership_validation(
    repository: GenerationRepository, monkeypatch: pytest.MonkeyPatch, invalid: bool
) -> None:
    """Native JSON Schema responses pass through production validation unchanged."""
    publish_catalogs(repository)
    monkeypatch.setattr(resolver, "get_repository", lambda: repository)
    graph = resolver_graph(mock_model(invalid=invalid))
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="Set copper and calculate EBITDA")]},
        {"configurable": {"user_id": "user0001", "thread_id": "schema"}},
    )
    assert "__interrupt__" not in result
    for kind, expected in (("inputs", "Цена меди"), ("outputs", "EBITDA")):
        item = result["result"][kind][0]
        assert item["status"] == ("unresolved" if invalid else "resolved")
        assert item["resolved_name"] == (None if invalid else expected)
        assert item["explanation"] == "Schema fixture explanation"
        assert item["resolution_source"] == (None if invalid else "llm")


async def test_ollama_schema_adapter_clarification_and_resume(
    repository: GenerationRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambiguous structured output still uses deterministic numeric selection."""
    publish_catalogs(repository)
    monkeypatch.setattr(resolver, "get_repository", lambda: repository)
    graph = resolver_graph(mock_model(ambiguous=True))
    config = {"configurable": {"user_id": "user0001", "thread_id": "choice"}}
    first = await graph.ainvoke({"messages": [HumanMessage(content="copper and EBITDA")]}, config)
    assert "Цена алюминия" in first["__interrupt__"][0].value["question"]
    bad = await graph.ainvoke(Command(resume="wrong"), config)
    assert bad["__interrupt__"]
    final = await graph.ainvoke(Command(resume="2"), config)
    item = final["result"]["inputs"][0]
    assert item["resolved_name"] == "Цена алюминия"
    assert item["resolution_source"] == "user"
    assert item["explanation"] == "Schema fixture explanation"


def test_generation_settings_preserve_persisted_embedding_identity(
    repository: GenerationRepository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider/model switch loads the same vectors without re-embedding."""
    generation = prepare(repository, "alice")
    repository.publish("alice", generation)
    before = RetrievalSettings(index_dir=tmp_path)
    monkeypatch.setenv("V2_MODEL_TO_USE", "OLLAMA")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.5:9b-mlx")
    after = RetrievalSettings(index_dir=tmp_path)
    assert after.model_dump() == before.model_dump()
    assert after.fingerprint() == before.fingerprint()
    calls = list(repository.embeddings.calls)
    fresh = GenerationRepository(after, repository.embeddings)
    assert fresh.load("alice").identity == generation.identity
    assert repository.embeddings.calls == calls
