"""Opt-in local generation checks; no cloud calls or workbook mutations."""

import argparse
import json
import time
from collections.abc import Callable
from contextlib import nullcontext
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
from deepagents import create_deep_agent
from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from aigw_service.api.v2 import name_resolution as resolver
from aigw_service.api.v2.agent import create_v2_model
from aigw_service.api.v2.catalog_index import CatalogIndex
from aigw_service.api.v2.profiles import register_v2_harness_profile
from aigw_service.api.v2.prompts import CORE_SYSTEM_PROMPT
from aigw_service.config import APP_CONFIG
from aigw_service.config.v2 import V2Settings

CATALOGS = {
    "inputs": [{"id": "IN:2", "name": "Цена меди"}],
    "outputs": [{"id": "OUT:2", "name": "EBITDA"}],
}
REQUEST = "Set copper price to 450 and calculate EBITDA."


def timed_schema(label: str, check: Callable[[], dict[str, object]]) -> dict[str, object]:
    """Report each schema boundary and timing without exposing model responses."""
    print(f"RUN: {label}", flush=True)
    started = time.monotonic()
    result = check()
    print(f"PASS: {label}; {time.monotonic() - started:.2f}s", flush=True)
    return result


class FixtureEmbeddings(Embeddings):
    """Constant vectors for single-row catalogs; never call an embedding service."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Keep the fixture index searchable without testing semantic retrieval."""
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        """Return the same deterministic direction for queries."""
        return [1.0, 0.0]


def schema_checks(model: ChatOllama) -> None:
    """Use the actual resolver nodes and verify validated canonical matches."""
    state = {"messages": [HumanMessage(content=REQUEST)]}
    state.update(timed_schema("live extraction", lambda: resolver._extractor_node(state, llm=model)))
    mentions = state["mentions"]
    assert mentions.inputs, "Extraction omitted inputs"
    assert mentions.outputs, "Extraction omitted outputs"
    state["candidates"] = {
        "inputs": [CATALOGS["inputs"] for _ in mentions.inputs],
        "outputs": [CATALOGS["outputs"] for _ in mentions.outputs],
    }
    state["catalogs"] = CATALOGS
    state.update(timed_schema("live input matching", lambda: resolver._resolve_inputs_node(state, llm=model)))
    state.update(timed_schema("live output matching", lambda: resolver._resolve_outputs_node(state, llm=model)))
    result = resolver._finalize_node(state)["result"]
    assert result["inputs"][0]["resolved_name"] == "Цена меди", "Input matching did not resolve copper"
    assert result["outputs"][0]["resolved_name"] == "EBITDA", "Output matching did not resolve EBITDA"


def tool_round_trip(model: ChatOllama) -> None:
    """Require an actual tool call and a final answer after its result."""

    @tool
    def read_fixture_metric() -> str:
        """Read the fixture EBITDA value. Always call this to obtain its value."""
        return "EBITDA=1312.86"

    messages = [HumanMessage(content="Call read_fixture_metric to get EBITDA, then report its numeric value.")]
    answer = model.bind_tools([read_fixture_metric]).invoke(messages)
    assert answer.tool_calls, "Model did not emit a native tool call"
    messages.append(answer)
    for call in answer.tool_calls:
        assert call["name"] == read_fixture_metric.name, "Unexpected tool"
        messages.append(ToolMessage(content=read_fixture_metric.invoke(call["args"]), tool_call_id=call["id"]))
    final = model.invoke(messages)
    assert "1312.86" in final.content, "Tool result was not used in final answer"


def graph_check(model: ChatOllama) -> None:
    """Exercise real scoped delegation with an in-memory repository and safe tool."""
    calls: list[dict[str, str]] = []

    @tool
    def calculate_fixture(resolved_input: str, resolved_output: str) -> str:
        """Calculate the fixture only after workbook-name-resolver supplies canonical names."""
        assert resolved_input == "Цена меди", "Noncanonical input"
        assert resolved_output == "EBITDA", "Noncanonical output"
        calls.append({"input": resolved_input, "output": resolved_output})
        return "EBITDA=1312.86"

    embeddings = FixtureEmbeddings()
    index = CatalogIndex.build(CATALOGS, embeddings)
    repo = SimpleNamespace(
        lock=lambda user_id: nullcontext(),
        load=lambda user_id: SimpleNamespace(identity="smoke", index=index),
        embeddings=embeddings,
        settings=SimpleNamespace(retrieval_k=1),
    )
    register_v2_harness_profile()
    graph = create_deep_agent(
        model=model,
        tools=[calculate_fixture],
        subagents=[resolver.build_name_resolver(model)],
        system_prompt=CORE_SYSTEM_PROMPT
        + "\nSmoke fixture: first delegate names to workbook-name-resolver through task, then use "
        "calculate_fixture with its canonical names. No Excel tools are installed in this fixture.",
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"user_id": "smoke", "thread_id": uuid4().hex}, "recursion_limit": 30}
    with patch.object(resolver, "get_repository", return_value=repo):
        result = graph.invoke({"messages": [HumanMessage(content=REQUEST)]}, config)
        if result.get("__interrupt__"):
            raise AssertionError("Unexpected live ambiguity in the single-row fixture")
    tool_calls = [call for msg in result["messages"] for call in getattr(msg, "tool_calls", [])]
    names = [call["name"] for call in tool_calls]
    resolver_calls = [
        index
        for index, call in enumerate(tool_calls)
        if call["name"] == "task" and call["args"].get("subagent_type") == "workbook-name-resolver"
    ]
    assert resolver_calls, "Delegation to the specialized workbook resolver was omitted"
    assert "calculate_fixture" in names, "Calculation was omitted"
    assert resolver_calls[0] < names.index("calculate_fixture"), "Calculation preceded name resolution"
    assert calls, "Calculation was not called"
    assert "1312.86" in result["messages"][-1].content, "Calculation result was not reported"


def controlled_clarification() -> None:
    """Verify deterministic clarification/resume; this is not a live-model assertion."""
    workflow = StateGraph(resolver._ResolverState)
    workflow.add_node("clarify", resolver._clarify_node)
    workflow.add_node("finalize", resolver._finalize_node)
    workflow.add_edge(START, "clarify")
    workflow.add_edge("clarify", "finalize")
    workflow.add_edge("finalize", END)
    graph = workflow.compile(checkpointer=MemorySaver())
    state = {
        "catalogs": CATALOGS,
        "input_matches": [
            {
                "mentioned_name": "copper",
                "matched_names": ["Цена меди"],
                "candidate_names": ["Цена меди"],
                "candidate_ids": ["IN:2"],
                "is_unambiguous": False,
                "reason": "Controlled ambiguity fixture",
                "llm_matches": [],
            }
        ],
        "output_matches": [],
    }
    config = {"configurable": {"thread_id": uuid4().hex}}
    assert graph.invoke(state, config)["__interrupt__"]
    assert graph.invoke(Command(resume="invalid"), config)["__interrupt__"]
    final = graph.invoke(Command(resume="1"), config)
    assert final["result"]["inputs"][0]["resolution_source"] == "user"


def runtime_metadata(url: str, model: str) -> dict[str, Any]:
    """Identify the runtime and exact installed model without printing secrets."""
    with httpx.Client(base_url=url, timeout=10) as client:
        server = client.get("/api/version")
        server.raise_for_status()
        models = client.get("/api/tags")
        models.raise_for_status()
    installed = next((item for item in models.json()["models"] if item["name"] == model), None)
    if installed is None:
        raise ValueError(f"Requested model is not installed: {model}")
    return {
        "ollama": server.json(),
        "model": model,
        "digest": installed["digest"],
        "details": installed["details"],
        "langchain-ollama": version("langchain-ollama"),
        "deepagents": version("deepagents"),
        "reasoning": False,
    }


def main() -> None:
    """Run only when explicitly invoked and stop on any failed compatibility check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.5:9b-mlx")
    parser.add_argument("--url", default=APP_CONFIG.ollama.base_url)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    print(json.dumps(runtime_metadata(args.url, args.model), ensure_ascii=False), flush=True)
    settings = APP_CONFIG.ollama.model_copy(
        update={"model_name": args.model, "base_url": args.url, "timeout": args.timeout}
    )
    with (
        patch.object(APP_CONFIG, "v2", V2Settings(V2_MODEL_TO_USE="OLLAMA")),
        patch.object(APP_CONFIG, "ollama", settings),
    ):
        model = create_v2_model()
    for label, check in (
        ("live schemas", lambda: schema_checks(model)),
        ("live tool round trip", lambda: tool_round_trip(model)),
        ("live scoped delegation", lambda: graph_check(model)),
        ("controlled clarification (no inference)", controlled_clarification),
    ):
        started = time.monotonic()
        print(f"RUN: {label}", flush=True)
        try:
            check()
        except Exception as exc:
            print(f"FAIL: {label}; {type(exc).__name__}; {time.monotonic() - started:.2f}s", flush=True)
            raise SystemExit(1) from None
        print(f"PASS: {label}; {time.monotonic() - started:.2f}s", flush=True)


if __name__ == "__main__":
    main()
