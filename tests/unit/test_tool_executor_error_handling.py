"""Unit tests for exception handling in _execute_single_call."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage

from aigw_service.api.v1.main_graph import AgentGraph

# Minimal runnable config forwarded to OrchestratorTools.dispatch (RunnableConfig).
_CONFIG = {"configurable": {"thread_id": "u:s", "user_id": "user_1"}}


@pytest.fixture
def agent_graph():
    """AgentGraph with mocked dispatch and logger."""
    ag = AgentGraph.__new__(AgentGraph)
    ag.orchestrator_tools = MagicMock()
    ag.logger = MagicMock()
    return ag


class TestExecuteSingleCallExceptionHandling:
    """Tests for exception handling in _execute_single_call."""

    @pytest.mark.asyncio
    async def test_dispatch_raises_generic_exception(self, agent_graph):
        """When dispatch raises Exception, _execute_single_call returns ToolMessage with error."""
        agent_graph.orchestrator_tools.dispatch = AsyncMock(
            side_effect=Exception("HTTP 424 from IFT")
        )

        call = {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"}
        state = {
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }
        fingerprints: set[str] = set()
        tool_results: list[dict] = []

        result = await agent_graph._execute_single_call(
            call, state, fingerprints, tool_results, _CONFIG
        )

        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == "call_1"
        assert result.name == "analyze_model_inputs_for_target"
        assert "Ошибка выполнения" in result.content
        assert "HTTP 424" in result.content
        assert len(tool_results) == 1
        assert tool_results[0]["status"] == "error"
        assert tool_results[0]["result_text"] == result.content
        # The task 3.2 invariant: the runnable config is forwarded down to dispatch intact.
        assert agent_graph.orchestrator_tools.dispatch.await_args.kwargs["config"] is _CONFIG
        assert agent_graph.orchestrator_tools.dispatch.await_args.kwargs["config"]["configurable"]["thread_id"] == "u:s"

    @pytest.mark.asyncio
    async def test_dispatch_raises_cancelled_error_propagates(self, agent_graph):
        """When dispatch raises CancelledError, it propagates: a superseded run must not answer.

        A cancelled run has to reach ``ThreadRunRegistry.run_latest_wins`` (which aborts the
        request without any HTTP response). Converting the cancellation into a ToolMessage
        here made the run continue to synthesizer and answer the losing client.
        """
        agent_graph.orchestrator_tools.dispatch = AsyncMock(
            side_effect=asyncio.CancelledError("cancelled")
        )

        call = {"name": "analyze_excel_model", "args": {}, "id": "call_2"}
        state = {
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }
        fingerprints: set[str] = set()
        tool_results: list[dict] = []

        with pytest.raises(asyncio.CancelledError):
            await agent_graph._execute_single_call(
                call, state, fingerprints, tool_results, _CONFIG
            )

        assert tool_results == [], "a cancelled call must not be recorded as a tool error"

    @pytest.mark.asyncio
    async def test_dispatch_raises_runtime_error(self, agent_graph, monkeypatch):
        """When dispatch raises RuntimeError, exception is logged via logger.exception."""
        agent_graph.orchestrator_tools.dispatch = AsyncMock(
            side_effect=RuntimeError("subagent timeout")
        )

        call = {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_3"}
        state = {
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }
        fingerprints: set[str] = set()
        tool_results: list[dict] = []

        mock_logger_exception = MagicMock()
        monkeypatch.setattr("aigw_service.api.v1.main_graph.logger.exception", mock_logger_exception)

        await agent_graph._execute_single_call(
            call, state, fingerprints, tool_results, _CONFIG
        )

        mock_logger_exception.assert_called_once()
        call_args = mock_logger_exception.call_args
        assert "subagent timeout" in str(call_args)