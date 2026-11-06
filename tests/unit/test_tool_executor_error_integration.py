"""Unit tests for tool_executor error path integration."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from aigw_service.api.v1.main_graph import AgentGraph

# Minimal runnable config forwarded to OrchestratorTools.dispatch (RunnableConfig).
_CONFIG = {"configurable": {"thread_id": "u:s", "user_id": "user_1"}}


@pytest.fixture
def agent_graph():
    """AgentGraph with mocked dependencies."""
    ag = AgentGraph.__new__(AgentGraph)
    ag.orchestrator_tools = MagicMock()
    ag.logger = MagicMock()
    return ag


class TestToolExecutorErrorPath:
    """Tests for tool_executor error path integration."""

    @pytest.mark.asyncio
    async def test_all_calls_fail(self, agent_graph):
        """When all tool_calls fail, tool_executor returns ToolMessages for ALL calls."""
        agent_graph.orchestrator_tools.dispatch = AsyncMock(
            side_effect=Exception("HTTP 424")
        )

        ai_msg = AIMessage(
            content="decision",
            tool_calls=[
                {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"},
                {"name": "analyze_excel_model", "args": {}, "id": "call_2"},
            ],
        )
        state = {
            "messages": [ai_msg],
            "executed_tool_fingerprints": [],
            "tool_results": [],
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }

        result = await agent_graph.tool_executor(state, _CONFIG)

        assert len(result["messages"]) == 2
        for msg in result["messages"]:
            assert isinstance(msg, ToolMessage)
        assert len(result["tool_results"]) == 2
        for tr in result["tool_results"]:
            assert tr["status"] == "error"

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self, agent_graph):
        """When one call succeeds and one fails, tool_executor returns both results."""
        success_result = {
            "task_description": "test task",
            "status": "success",
            "result_text": "calculation done",
        }
        agent_graph.orchestrator_tools.dispatch = AsyncMock(side_effect=[success_result, Exception("HTTP 424")])

        ai_msg = AIMessage(
            content="decision",
            tool_calls=[
                {"name": "analyze_excel_model", "args": {}, "id": "call_1"},
                {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_2"},
            ],
        )
        state = {
            "messages": [ai_msg],
            "executed_tool_fingerprints": [],
            "tool_results": [],
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }

        result = await agent_graph.tool_executor(state, _CONFIG)

        assert len(result["messages"]) == 2
        assert isinstance(result["messages"][0], ToolMessage)
        assert isinstance(result["messages"][1], ToolMessage)
        assert "calculation done" in result["messages"][0].content
        assert "Ошибка выполнения" in result["messages"][1].content
        assert len(result["tool_results"]) == 2
        assert result["tool_results"][0]["status"] == "success"
        assert result["tool_results"][1]["status"] == "error"

    @pytest.mark.asyncio
    async def test_dedup_regression(self, agent_graph):
        """Deduplication still works correctly — regression test for existing behavior."""
        success_result = {
            "task_description": "test task",
            "status": "success",
            "result_text": "done",
        }
        agent_graph.orchestrator_tools.dispatch = AsyncMock(return_value=success_result)

        ai_msg = AIMessage(
            content="decision",
            tool_calls=[
                {
                    "name": "analyze_excel_model",
                    "args": {"input_names": ["CPI"], "output_names": ["EBITDA"]},
                    "id": "call_1",
                },
                {
                    "name": "analyze_excel_model",
                    "args": {"input_names": ["CPI"], "output_names": ["EBITDA"]},
                    "id": "call_2",
                },
            ],
        )
        state = {
            "messages": [ai_msg],
            "executed_tool_fingerprints": [],
            "tool_results": [],
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }

        result = await agent_graph.tool_executor(state, _CONFIG)

        assert len(result["messages"]) == 2
        # Second call should be dedup skip
        assert "уже был выполнен" in result["messages"][1].content
        assert result["messages"][1].tool_call_id == "call_2"

    @pytest.mark.asyncio
    async def test_logger_exception_called_on_error(self, agent_graph, monkeypatch):
        """When dispatch raises, logger.exception is called."""
        agent_graph.orchestrator_tools.dispatch = AsyncMock(
            side_effect=RuntimeError("subagent timeout")
        )

        ai_msg = AIMessage(
            content="decision",
            tool_calls=[
                {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"},
            ],
        )
        state = {
            "messages": [ai_msg],
            "executed_tool_fingerprints": [],
            "tool_results": [],
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }

        mock_logger_exception = MagicMock()
        monkeypatch.setattr("aigw_service.api.v1.main_graph.logger.exception", mock_logger_exception)

        await agent_graph.tool_executor(state, _CONFIG)

        mock_logger_exception.assert_called_once()
        assert "subagent timeout" in str(mock_logger_exception.call_args)