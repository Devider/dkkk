"""Candidate membership and durable generation guards in a checkpointed resolver."""

import re

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from tests.api.v2.test_catalog_generations import prepare

from aigw_service.api.v2 import name_resolution as resolver
from aigw_service.api.v2.catalog_generations import GenerationRepository
from aigw_service.api.v2.catalog_index import CatalogIndex


class MatchingModel:
    """Extract one ambiguous mention and select only prompt-visible rows."""

    def __init__(self) -> None:
        """Record requests and matching prompts for isolation assertions."""
        self.requests: list[str] = []
        self.prompts: list[str] = []

    def with_structured_output(self, schema: type) -> object:
        """Return a schema-aware deterministic structured adapter."""
        owner = self

        class Structured:
            """Synthesize responses using the candidate prompt."""

            def invoke(self, messages: list) -> object:
                """Keep all visible rows ambiguous so clarification must interrupt."""
                if schema is resolver.ExtractedNames:
                    owner.requests.append(messages[-1].content)
                    return resolver.ExtractedNames(inputs=["metal price"], outputs=[])
                text = messages[0].content
                owner.prompts.append(text)
                entries = re.findall(r"(IN:\d+) \| ([^\n]+)", text)
                return resolver.InputResolutionAnalysis(
                    mentioned_inputs=[
                        resolver.InputItem(
                            mentioned_input_name="metal price",
                            equivalent_input_id=entries[0][0],
                            equivalent_input_name=entries[0][1],
                            candidate_names=[name for _, name in entries],
                            is_unambiguous=False,
                            explanation="Choose a row",
                        )
                    ]
                )

        return Structured()


def make_graph(llm: MatchingModel) -> object:
    """Compile the real subagent inside a parent with persistent checkpoints."""
    parent = StateGraph(resolver._ResolverState)
    parent.add_node("resolver", resolver.build_name_resolver(llm)["runnable"])
    parent.add_edge(START, "resolver")
    parent.add_edge("resolver", END)
    return parent.compile(checkpointer=MemorySaver())


def publish_rows(repo: GenerationRepository, names: list[str]) -> str:
    """Publish a complete generation with the supplied named input rows."""
    generation = prepare(repo, "user0001")
    index = CatalogIndex.build(
        {"inputs": [{"id": f"IN:{i}", "name": name} for i, name in enumerate(names, start=2)], "outputs": []},
        repo.embeddings,
    )
    generation = repo.prepare("user0001", generation.identity, generation.workbook, "hash", index)
    repo.publish("user0001", generation)
    return generation.identity


async def test_replacement_restarts_original_request_without_applying_old_number(
    repository: GenerationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old option number cannot silently select a new workbook's same row ID."""
    monkeypatch.setattr(resolver, "get_repository", lambda: repository)
    publish_rows(repository, ["Цена меди", "Цена алюминия"])
    llm = MatchingModel()
    graph = make_graph(llm)
    config = {"configurable": {"user_id": "user0001", "thread_id": "replacement"}}
    first = await graph.ainvoke({"messages": [HumanMessage(content="Original financial request")]}, config)
    assert first["__interrupt__"]
    publish_rows(repository, ["Новая медь", "Новый алюминий"])
    resumed = await graph.ainvoke(Command(resume="2"), config)
    assert resumed.get("__interrupt__"), resumed
    assert "Новая медь" in resumed["__interrupt__"][0].value["question"]
    assert llm.requests == ["Original financial request", "Original financial request"]
    final = await graph.ainvoke(Command(resume="1"), config)
    assert (
        final["result"]["inputs"][0]["resolved_name"]
        == resumed["__interrupt__"][0].value["pending"][0]["candidates"][0]["name"]
    )


async def test_same_generation_preserves_pending_choices(
    repository: GenerationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stable active identity preserves valid pending clarification."""
    monkeypatch.setattr(resolver, "get_repository", lambda: repository)
    publish_rows(repository, ["Цена меди", "Цена алюминия"])
    graph = make_graph(MatchingModel())
    config = {"configurable": {"user_id": "user0001", "thread_id": "reuse"}}
    first = await graph.ainvoke({"messages": [HumanMessage(content="Original request")]}, config)
    active = repository.load("user0001")
    repository.publish("user0001", active)
    result = await graph.ainvoke(Command(resume="2"), config)
    assert (
        result["result"]["inputs"][0]["resolved_name"]
        == first["__interrupt__"][0].value["pending"][0]["candidates"][1]["name"]
    )


def test_per_mention_membership_rejects_cross_list_id() -> None:
    """Even an ID valid for another mention cannot resolve this mention."""

    class LLM:
        """Always propose the second mention's candidate for both mentions."""

        def with_structured_output(self, schema: type) -> object:
            """Supply structured but intentionally invalid membership."""

            class Structured:
                """Ignore mention isolation in the model response."""

                def invoke(self, messages: list) -> resolver.InputResolutionAnalysis:
                    """Return the same out-of-list ID for the first mention."""
                    assert "Скрытая строка" not in messages[0].content
                    return resolver.InputResolutionAnalysis(
                        mentioned_inputs=[
                            resolver.InputItem(
                                mentioned_input_name=mention,
                                equivalent_input_id="IN:3",
                                equivalent_input_name="Выручка",
                                candidate_names=["Выручка"],
                                is_unambiguous=True,
                                explanation="model suggestion",
                            )
                            for mention in ("copper", "revenue")
                        ]
                    )

            return Structured()

    lists = [[{"id": "IN:2", "name": "Цена меди"}], [{"id": "IN:3", "name": "Выручка"}]]
    matches = resolver._resolve_catalog(
        ["copper", "revenue"],
        [],
        resolver.InputResolutionAnalysis,
        "mentioned_inputs",
        "{catalog}",
        llm=LLM(),
        candidate_lists=lists,
    )
    assert matches[0]["candidate_ids"] == []
    assert matches[1]["candidate_ids"] == ["IN:3"]


def test_duplicate_names_can_be_resolved_by_row_id() -> None:
    """Explicit IDs distinguish equal canonical names in either category."""
    entries = [{"id": "IN:2", "name": "Медь"}, {"id": "IN:3", "name": "Медь"}]
    item = resolver.InputItem(
        mentioned_input_name="copper",
        equivalent_input_id="IN:3",
        equivalent_input_name="Медь",
        candidate_names=["Медь"],
        is_unambiguous=True,
        explanation="specific row",
    )
    assert resolver._candidate_ids_from_items([item], entries, "mentioned_inputs", "equivalent_input_name") == ["IN:3"]
    output = resolver.OutputItem(
        mentioned_output_name="profit",
        equivalent_output_id="OUT:3",
        equivalent_output_name="Прибыль",
        candidate_names=["Прибыль"],
        is_unambiguous=True,
        explanation="specific row",
    )
    outputs = [{"id": "OUT:2", "name": "Прибыль"}, {"id": "OUT:3", "name": "Прибыль"}]
    assert resolver._candidate_ids_from_items([output], outputs, "mentioned_outputs", "equivalent_output_name") == [
        "OUT:3"
    ]
