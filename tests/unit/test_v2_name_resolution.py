"""Focused tests for V2 workbook catalogs and deterministic choices."""

from contextlib import nullcontext
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import openpyxl
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from aigw_service.api.v2.name_resolution import (
    ExtractedNames,
    InputItem,
    InputResolutionAnalysis,
    OutputItem,
    OutputResolutionAnalysis,
    _choices,
    _finalize_node,
    _parse_selection,
    _resolve_catalog,
    _resolve_inputs_node,
    _ResolverState,
    build_name_resolver,
    build_workbook_catalog,
    get_user_workbook_catalog,
)


@pytest.fixture(autouse=True)
def legacy_graph_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep existing graph/clarification tests focused on their catalog stubs."""
    from aigw_service.api.v2 import name_resolution as resolver

    class Repository:
        """Adapt legacy catalog fixtures to the persistent repository interface."""

        embeddings = None
        settings = SimpleNamespace(retrieval_k=10)

        def lock(self, user_id: str) -> object:
            """Provide a no-op lock for immutable test fixtures."""
            return nullcontext()

        def load(self, user_id: str) -> object:
            """Expose the existing test's catalog without a cloud client."""
            catalogs = resolver.get_user_workbook_catalog(user_id)
            index = SimpleNamespace(
                catalogs=catalogs, candidates=lambda kind, mentions, embeddings, k: [catalogs[kind] for _ in mentions]
            )
            return SimpleNamespace(identity="fixture", index=index)

    monkeypatch.setattr(resolver, "get_repository", Repository)


def _make_workbook(path: Path) -> None:
    """Create representative Inputs and Outputs catalog sheets."""
    workbook = openpyxl.Workbook()
    inputs = workbook.active
    inputs.title = "Inputs"
    inputs.append(["Наименование", 2025])
    inputs.append(["", None])
    inputs.append(["Цена меди (USD/т)", None])
    inputs.append(["Цена меди (USD/т)", 100])
    outputs = workbook.create_sheet("Outputs")
    outputs.append(["Наименование", 2025])
    outputs.append([None, None])
    outputs.append(["EBITDA", None])
    workbook.save(path)
    workbook.close()


def test_catalog_uses_sheet_prefixed_excel_rows_and_keeps_duplicate_names(tmp_path: Path) -> None:
    path = tmp_path / "model.xlsx"
    _make_workbook(path)

    catalogs = build_workbook_catalog(str(path))

    assert catalogs["inputs"] == [
        {"id": "IN:3", "name": "Цена меди (USD/т)"},
        {"id": "IN:4", "name": "Цена меди (USD/т)"},
    ]
    assert catalogs["outputs"] == [{"id": "OUT:3", "name": "EBITDA"}]


def test_catalog_includes_names_without_values_and_does_not_use_libreoffice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "model.xlsx"
    _make_workbook(path)
    monkeypatch.setattr("aigw_service.api.v2.name_resolution.get_store_file", lambda user_id: path.name)

    monkeypatch.setattr(
        "aigw_service.api.v2.name_resolution.tempfile.gettempdir",
        lambda: str(tmp_path),
    )
    catalogs = get_user_workbook_catalog("user0001")

    assert catalogs["inputs"][0]["name"] == "Цена меди (USD/т)"
    assert catalogs["outputs"][0]["name"] == "EBITDA"


@pytest.mark.parametrize("user_id", [None, ""])
def test_catalog_requires_trusted_user_identity(user_id: str | None) -> None:
    with pytest.raises(ValueError, match="пользователя"):
        get_user_workbook_catalog(user_id)


def test_catalog_reports_missing_user_workbook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aigw_service.api.v2.name_resolution.get_store_file", lambda user_id: None)

    with pytest.raises(FileNotFoundError, match="загрузите Excel-модель"):
        get_user_workbook_catalog("user0001")


def test_input_resolver_prompt_contains_only_retrieved_rows() -> None:
    class FakeStructuredModel:
        """Capture the system prompt and return no LLM matches."""

        def __init__(self) -> None:
            self.system_prompt = ""

        def invoke(self, messages: list) -> InputResolutionAnalysis:
            self.system_prompt = str(messages[0].content)
            return InputResolutionAnalysis(mentioned_inputs=[])

    class FakeLLM:
        """Return a structured model stub for the input schema."""

        def __init__(self) -> None:
            self.structured_model = FakeStructuredModel()

        def with_structured_output(self, schema: type) -> FakeStructuredModel:
            return self.structured_model

    llm = FakeLLM()
    catalog = [
        {"id": "IN:12", "name": "Метанол"},
        {"id": "IN:13", "name": "Цена бензина"},
        {"id": "IN:14", "name": "Объём МЕТАНОЛА"},
    ]
    state = {
        "mentions": ExtractedNames(inputs=["цена метанола"], outputs=[]),
        "catalogs": {"inputs": catalog, "outputs": []},
        "candidates": {"inputs": [[catalog[0], catalog[2]]], "outputs": []},
    }

    result = _resolve_inputs_node(state, llm=llm)

    assert "IN:12 | Метанол" in llm.structured_model.system_prompt
    assert "IN:14 | Объём МЕТАНОЛА" in llm.structured_model.system_prompt
    assert "Цена бензина" not in llm.structured_model.system_prompt
    assert result["input_matches"][0]["candidate_ids"] == []


@pytest.mark.parametrize(
    ("answer", "count", "expected"),
    [("1, 2", 2, [1, 2]), ("1", 2, None), ("0", 1, None), ("one", 1, None), ("1,1", 1, None)],
)
def test_numeric_choice_parser(answer: str, count: int, expected: list[int] | None) -> None:
    assert _parse_selection(answer, count) == expected


def test_bilingual_match_is_validated_and_only_canonical_name_is_returned() -> None:
    class FakeStructuredModel:
        """Return a catalog ID selected for an English alias."""

        def invoke(self, messages: list) -> InputResolutionAnalysis:
            return InputResolutionAnalysis(
                analysis="Copper price alias.",
                mentioned_inputs=[
                    InputItem(
                        mentioned_input_name="copper price",
                        equivalent_input_name="Цена меди (USD/т)",
                        equivalent_input_id="IN:8",
                        candidate_names=["Цена меди (USD/т)"],
                        is_unambiguous=True,
                        explanation="Copper price alias.",
                    )
                ],
            )

    class FakeLLM:
        """Supply the structured model used by the catalog resolver."""

        def with_structured_output(self, schema: type) -> FakeStructuredModel:
            return FakeStructuredModel()

    catalog = [{"id": "IN:8", "name": "Цена меди (USD/т)"}]
    matches = _resolve_catalog(
        ["copper price"], catalog, InputResolutionAnalysis, "mentioned_inputs", "{catalog}", llm=FakeLLM()
    )
    assert matches[0]["candidate_ids"] == ["IN:8"]

    state = {
        "catalogs": {"inputs": catalog, "outputs": []},
        "input_matches": matches,
        "output_matches": [],
        "pending": [],
    }
    result = _finalize_node(state)["result"]
    assert result["inputs"] == [
        {
            "mentioned_name": "copper price",
            "resolved_name": "Цена меди (USD/т)",
            "status": "resolved",
            "reason": "Copper price alias.",
        }
    ]
    assert "IN:8" not in str(result)


def test_missing_match_in_empty_catalog_stays_unresolved() -> None:
    class FakeStructuredModel:
        """Return no candidate for an empty catalog."""

        def invoke(self, messages: list) -> InputResolutionAnalysis:
            return InputResolutionAnalysis(
                analysis="No supported match.",
                mentioned_inputs=[
                    InputItem(
                        mentioned_input_name="unknown parameter",
                        equivalent_input_name="Неизвестный параметр",
                        equivalent_input_id="IN:404",
                        candidate_names=["Неизвестный параметр"],
                        is_unambiguous=True,
                        explanation="No supported match.",
                    )
                ],
            )

    class FakeLLM:
        """Provide structured output for catalog candidate selection."""

        def with_structured_output(self, schema: type) -> FakeStructuredModel:
            return FakeStructuredModel()

    matches = _resolve_catalog(
        ["unknown parameter"], [], InputResolutionAnalysis, "mentioned_inputs", "{catalog}", llm=FakeLLM()
    )
    assert matches[0]["candidate_ids"] == []


def test_empty_llm_candidate_list_is_unresolved_without_catalog_suggestions() -> None:
    class FakeStructuredModel:
        """Return a valid structured response without proposing candidates."""

        def invoke(self, messages: list) -> InputResolutionAnalysis:
            return InputResolutionAnalysis(
                analysis="No direct match.",
                mentioned_inputs=[
                    InputItem(
                        mentioned_input_name="цена метанола",
                        equivalent_input_name="",
                        equivalent_input_id="",
                        candidate_names=[],
                        is_unambiguous=False,
                        explanation="Прямое соответствие не определено.",
                    )
                ],
            )

    class FakeLLM:
        """Provide the structured response stub."""

        def with_structured_output(self, schema: type) -> FakeStructuredModel:
            return FakeStructuredModel()

    catalog = [
        {"id": "IN:22", "name": "Цена метанола, руб./т"},
        {"id": "IN:23", "name": "Объём производства метанола"},
    ]
    matches = _resolve_catalog(
        ["цена метанола"], catalog, InputResolutionAnalysis, "mentioned_inputs", "{catalog}", llm=FakeLLM()
    )

    assert matches[0]["candidate_ids"] == []
    assert matches[0]["is_unambiguous"] is False
    state = {
        "catalogs": {"inputs": catalog, "outputs": []},
        "input_matches": matches,
        "output_matches": [],
    }
    assert _choices(state) == []
    result = _finalize_node(state)["result"]
    assert result["inputs"][0]["status"] == "unresolved"
    assert result["inputs"][0]["reason"] == "Для «цена метанола» подходящий аналог в каталоге не найден."


def test_repeated_mentions_keep_separate_candidate_selections() -> None:
    catalog = [
        {"id": "IN:8", "name": "Цена меди"},
        {"id": "IN:9", "name": "Цена алюминия"},
    ]
    state = {
        "catalogs": {"inputs": catalog, "outputs": []},
        "input_matches": [
            {
                "mentioned_name": "metal price",
                "candidate_ids": ["IN:8", "IN:9"],
                "is_unambiguous": False,
                "reason": "Ambiguous.",
            },
            {
                "mentioned_name": "metal price",
                "candidate_ids": ["IN:8", "IN:9"],
                "is_unambiguous": False,
                "reason": "Ambiguous.",
            },
        ],
        "output_matches": [],
        "pending": [
            {"kind": "inputs", "match_index": 0, "mentioned_name": "metal price", "selected_id": "IN:8"},
            {"kind": "inputs", "match_index": 1, "mentioned_name": "metal price", "selected_id": "IN:9"},
        ],
    }

    result = _finalize_node(state)["result"]["inputs"]

    assert [item["resolved_name"] for item in result] == ["Цена меди", "Цена алюминия"]


def test_resolver_graph_has_parallel_input_and_output_branches() -> None:
    class FakeLLM:
        """Only graph construction is relevant to this test."""

    resolver = build_name_resolver(FakeLLM())
    nodes = resolver["runnable"].get_graph().nodes

    assert "resolve_inputs" in nodes
    assert "resolve_outputs" in nodes
    assert "clarify" in nodes


@pytest.mark.asyncio
async def test_resolver_runs_both_branches_and_returns_canonical_names(monkeypatch: pytest.MonkeyPatch) -> None:
    catalogs = {
        "inputs": [{"id": "IN:8", "name": "Цена меди (USD/т)"}],
        "outputs": [{"id": "OUT:13", "name": "EBITDA"}],
    }
    monkeypatch.setattr(
        "aigw_service.api.v2.name_resolution.get_user_workbook_catalog",
        lambda user_id: catalogs,
    )
    resolved_branches: set[str] = set()
    branch_barrier = Barrier(2)

    class FakeStructuredModel:
        """Return input/output extraction and catalog IDs without a remote LLM."""

        def __init__(self, schema: type) -> None:
            self.schema = schema

        def invoke(self, messages: list) -> object:
            if self.schema is ExtractedNames:
                return ExtractedNames(inputs=["copper price"], outputs=["operating profit"])
            branch_barrier.wait(timeout=2)
            if self.schema is InputResolutionAnalysis:
                resolved_branches.add("inputs")
                return InputResolutionAnalysis(
                    analysis="Inputs matched.",
                    mentioned_inputs=[
                        InputItem(
                            mentioned_input_name="copper price",
                            equivalent_input_name="Цена меди (USD/т)",
                            equivalent_input_id="IN:8",
                            candidate_names=["Цена меди (USD/т)"],
                            is_unambiguous=True,
                            explanation="Copper price alias.",
                        )
                    ],
                )
            resolved_branches.add("outputs")
            return OutputResolutionAnalysis(
                analysis="Outputs matched.",
                mentioned_outputs=[
                    OutputItem(
                        mentioned_output_name="operating profit",
                        equivalent_output_name="EBITDA",
                        candidate_names=["EBITDA"],
                        is_unambiguous=True,
                        explanation="Operating profit alias.",
                    )
                ],
            )

    class FakeLLM:
        """Factory for schema-aware structured output stubs."""

        def with_structured_output(self, schema: type) -> FakeStructuredModel:
            return FakeStructuredModel(schema)

    resolver = build_name_resolver(FakeLLM())["runnable"]
    response = await resolver.ainvoke(
        {"messages": [HumanMessage(content="Analyze copper price and operating profit")]},
        config={"configurable": {"thread_id": "user0001:session", "user_id": "user0001"}},
    )

    assert resolved_branches == {"inputs", "outputs"}
    assert '"resolved_name": "Цена меди (USD/т)"' in response["messages"][-1].content
    assert '"resolved_name": "EBITDA"' in response["messages"][-1].content
    assert "IN:8" not in response["messages"][-1].content
    assert "OUT:13" not in response["messages"][-1].content


@pytest.mark.asyncio
async def test_multiple_ambiguous_choices_resume_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    catalogs = {
        "inputs": [
            {"id": "IN:8", "name": "Цена меди (USD/т)"},
            {"id": "IN:9", "name": "Цена алюминия (USD/т)"},
        ],
        "outputs": [
            {"id": "OUT:13", "name": "EBITDA"},
            {"id": "OUT:14", "name": "Выручка"},
        ],
    }
    monkeypatch.setattr(
        "aigw_service.api.v2.name_resolution.get_user_workbook_catalog",
        lambda user_id: catalogs,
    )

    class FakeStructuredModel:
        """Return two valid options for each requested name."""

        def __init__(self, schema: type) -> None:
            self.schema = schema

        def invoke(self, messages: list) -> object:
            if self.schema is ExtractedNames:
                return ExtractedNames(inputs=["metal price"], outputs=["profit"])
            if self.schema is InputResolutionAnalysis:
                return InputResolutionAnalysis(
                    analysis="Two possible inputs.",
                    mentioned_inputs=[
                        InputItem(
                            mentioned_input_name="metal price",
                            equivalent_input_name="Цена меди (USD/т)",
                            equivalent_input_id="IN:8",
                            candidate_names=["Цена меди (USD/т)", "Цена алюминия (USD/т)"],
                            is_unambiguous=False,
                            explanation="Two possible inputs.",
                        )
                    ],
                )
            return OutputResolutionAnalysis(
                analysis="Two possible outputs.",
                mentioned_outputs=[
                    OutputItem(
                        mentioned_output_name="profit",
                        equivalent_output_name="EBITDA",
                        candidate_names=["EBITDA", "Выручка"],
                        is_unambiguous=False,
                        explanation="Two possible outputs.",
                    )
                ],
            )

    class FakeLLM:
        """Provide deterministic structured model responses."""

        def with_structured_output(self, schema: type) -> FakeStructuredModel:
            return FakeStructuredModel(schema)

    parent = StateGraph(_ResolverState)
    parent.add_node("resolver", build_name_resolver(FakeLLM())["runnable"])
    parent.add_edge(START, "resolver")
    parent.add_edge("resolver", END)
    graph = parent.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "user0001:clarification", "user_id": "user0001"}}

    first = await graph.ainvoke({"messages": [HumanMessage(content="Compare metal price and profit")]}, config=config)
    assert first["__interrupt__"]
    assert "Возможно, вы имеете в виду:" in first["__interrupt__"][0].value["question"]
    assert "1) Цена меди (USD/т)" in first["__interrupt__"][0].value["question"]
    assert "1) EBITDA" in first["__interrupt__"][0].value["question"]

    incomplete = await graph.ainvoke(Command(resume="1"), config=config)
    assert incomplete["__interrupt__"]

    resumed = await graph.ainvoke(Command(resume="2, 1"), config=config)
    final_text = resumed["messages"][-1].content
    assert '"resolved_name": "Цена алюминия (USD/т)"' in final_text
    assert '"resolved_name": "EBITDA"' in final_text
    assert (
        '"reason": "Пользователь выбрал вариант «Цена алюминия (USD/т)». Используем его для дальнейших расчётов."'
        in final_text
    )
    assert '"reason": "Пользователь выбрал вариант «EBITDA». Используем его для дальнейших расчётов."' in final_text
    assert '"status": "resolved"' in final_text
    assert "IN:9" not in final_text
    assert "OUT:13" not in final_text

    none_config = {"configurable": {"thread_id": "user0001:none-of-these", "user_id": "user0001"}}
    await graph.ainvoke(
        {"messages": [HumanMessage(content="Compare metal price and profit")]},
        config=none_config,
    )
    none_result = await graph.ainvoke(Command(resume="3, 1"), config=none_config)
    none_text = none_result["messages"][-1].content
    assert '"resolved_name": null' in none_text
    assert '"status": "unresolved"' in none_text
