"""Unit-тесты для экспериментального deepagents-графа (`main_graph_deep.py`).

Проверяют: извлечение каталога входов/выходов, диспетчеризацию + дедуп дубликатов в
subagent-обёртках (IFT/EMA), и полный цикл model -> task -> model через реальный
`create_deep_agent` с замоканным LLM (без обращения к GigaChat/LibreOffice).
"""

from typing import ClassVar
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aigw_service.api.v1 import main_graph_deep as mgd

FAKE_INPUTS = {"IN0": "Цена меди"}
FAKE_OUTPUTS = {"OUT0": "EBITDA"}


class _FakeStoredValue:
    value: ClassVar[dict] = {"filename": "fake.xlsx"}


class _FakeStore:
    async def aget(self, namespace, user_id):
        return _FakeStoredValue()


class _FakeAgentMemory:
    store = _FakeStore()
    checkpointer = None


@pytest.fixture
def deep_agent_graph():
    with (
        patch.object(mgd.APP_CTX, "agent_memory", _FakeAgentMemory()),
        patch.object(mgd, "_build_catalog_with_ids", return_value=(FAKE_INPUTS, FAKE_OUTPUTS)),
    ):
        yield mgd.DeepAgentGraph()


class TestExtractExcelData:
    async def test_populates_catalog_and_filename(self, deep_agent_graph):
        result = await deep_agent_graph.get_available_inputs_outputs({"user_id": "u1"})
        assert result["filename"] == "fake.xlsx"
        assert result["available_inputs"] == FAKE_INPUTS
        assert result["available_outputs"] == FAKE_OUTPUTS
        assert result["executed_tool_fingerprints"] == []

    async def test_raises_without_user_id(self, deep_agent_graph):
        with pytest.raises(ValueError, match="user_id"):
            await deep_agent_graph.get_available_inputs_outputs({})


class TestSubAgentWrapperDispatchAndDedup:
    @pytest.fixture
    def patched_dispatch(self, deep_agent_graph):
        calls = []

        async def fake_dispatch(tool_name, reformulated_query, **kwargs):
            calls.append((tool_name, reformulated_query))
            return {
                "tool_name": tool_name,
                "task_description": "x",
                "status": "OK",
                "result_text": f"calc result #{len(calls)}",
                "content": {},
            }

        deep_agent_graph.orchestrator_tools.dispatch = fake_dispatch
        return deep_agent_graph, calls

    async def test_dispatches_and_records_fingerprint(self, patched_dispatch):
        graph, calls = patched_dispatch
        wrapper = graph._build_subagent_wrapper_graph("analyze_model_inputs_for_target")

        out = await wrapper.ainvoke(
            {
                "messages": [HumanMessage(content="query A")],
                "available_inputs": FAKE_INPUTS,
                "available_outputs": FAKE_OUTPUTS,
                "filename": "fake.xlsx",
                "user_id": "u1",
                "executed_tool_fingerprints": [],
            }
        )

        assert len(calls) == 1
        assert out["messages"][-1].content == "calc result #1"
        assert out["executed_tool_fingerprints"] == ["analyze_model_inputs_for_target:query A"]

    async def test_duplicate_call_is_skipped(self, patched_dispatch):
        graph, calls = patched_dispatch
        wrapper = graph._build_subagent_wrapper_graph("analyze_model_inputs_for_target")

        fingerprint = "analyze_model_inputs_for_target:query A"
        out = await wrapper.ainvoke(
            {
                "messages": [HumanMessage(content="query A")],
                "available_inputs": FAKE_INPUTS,
                "available_outputs": FAKE_OUTPUTS,
                "filename": "fake.xlsx",
                "user_id": "u1",
                "executed_tool_fingerprints": [fingerprint],
            }
        )

        assert len(calls) == 0
        assert out["messages"][-1].content == mgd.DUPLICATE_CALL_MESSAGE

    async def test_different_query_is_not_deduped(self, patched_dispatch):
        graph, calls = patched_dispatch
        wrapper = graph._build_subagent_wrapper_graph("analyze_model_inputs_for_target")

        out = await wrapper.ainvoke(
            {
                "messages": [HumanMessage(content="query B")],
                "available_inputs": FAKE_INPUTS,
                "available_outputs": FAKE_OUTPUTS,
                "filename": "fake.xlsx",
                "user_id": "u1",
                "executed_tool_fingerprints": ["analyze_model_inputs_for_target:query A"],
            }
        )

        assert len(calls) == 1
        assert out["messages"][-1].content != mgd.DUPLICATE_CALL_MESSAGE


class TestFullDeepAgentLoop:
    async def test_tool_call_then_final_answer(self, deep_agent_graph):
        async def fake_dispatch(tool_name, reformulated_query, **kwargs):
            return {
                "tool_name": tool_name,
                "task_description": "x",
                "status": "OK",
                "result_text": "EBITDA=1000 at copper price=500",
                "content": {},
            }

        deep_agent_graph.orchestrator_tools.dispatch = fake_dispatch

        responses = iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "subagent_type": "analyze_model_inputs_for_target",
                                "description": "какой должна быть цена меди, чтобы EBITDA 2025 = 1000",
                            },
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Цена меди должна составить 500."),
            ]
        )

        async def fake_ainvoke(*args, **kwargs):
            return next(responses)

        object.__setattr__(deep_agent_graph.llm, "ainvoke", fake_ainvoke)

        result = await deep_agent_graph.graph.ainvoke(
            {
                "messages": [HumanMessage(content="какой должна быть цена меди, чтобы EBITDA 2025 = 1000")],
                "thread_id": "t1",
                "user_id": "u1",
            }
        )

        last = result["messages"][-1]
        assert isinstance(last, AIMessage)
        assert last.content == "Цена меди должна составить 500."

    async def test_recursion_limit_returns_graceful_message(self, deep_agent_graph):
        """Модель бесконечно вызывает task -> должен сработать recursion_limit, а не упасть."""

        async def fake_dispatch(tool_name, reformulated_query, **kwargs):
            return {
                "tool_name": tool_name,
                "task_description": "x",
                "status": "OK",
                "result_text": "ok",
                "content": {},
            }

        deep_agent_graph.orchestrator_tools.dispatch = fake_dispatch

        call_n = {"n": 0}

        async def always_call_tool(*args, **kwargs):
            call_n["n"] += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "subagent_type": "analyze_model_inputs_for_target",
                            "description": f"query {call_n['n']}",
                        },
                        "id": f"call_{call_n['n']}",
                        "type": "tool_call",
                    }
                ],
            )

        object.__setattr__(deep_agent_graph.llm, "ainvoke", always_call_tool)

        result = await deep_agent_graph.graph.ainvoke(
            {
                "messages": [HumanMessage(content="query 0")],
                "thread_id": "t1",
                "user_id": "u1",
            }
        )

        last = result["messages"][-1]
        assert isinstance(last, AIMessage)
        assert last.content == mgd.RECURSION_LIMIT_MESSAGE
