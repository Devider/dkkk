"""Unit tests for config propagation in _execute_single_call.

Ensures that the RunnableConfig from the tool_executor node is correctly
passed down to OrchestratorTools.dispatch() — without it, Langfuse tracing
spans are not created because the callback manager remains empty.

This test prevents regressions like the one introduced in commit 77d62c4,
where `config=config` was accidentally removed from the dispatch() call.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from aigw_service.api.v1.main_graph import AgentGraph


@pytest.fixture
def agent_graph():
    """AgentGraph with mocked dependencies."""
    ag = AgentGraph.__new__(AgentGraph)
    ag.orchestrator_tools = MagicMock()
    ag.logger = MagicMock()
    return ag


class TestExecuteSingleCallConfigPropagation:
    """Tests verifying that config is propagated to OrchestratorTools.dispatch()."""

    @pytest.mark.asyncio
    async def test_execute_single_call_passes_config_to_dispatch(self, agent_graph):
        """When _execute_single_call receives config, it must pass it to dispatch().

        The RunnableConfig carries the callback manager with the Langfuse
        CallbackHandler — without it, tracing spans are never created
        because on_tool_start with no handlers is a no-op.
        """
        fake_config = {"config_key": "test_value", "callbacks": []}
        agent_graph.orchestrator_tools.dispatch = AsyncMock(
            return_value={
                "task_description": "test task",
                "status": "success",
                "result_text": "done",
                "content": {},
            }
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
            call, state, fingerprints, tool_results, config=fake_config
        )

        # ToolMessage should be returned successfully
        assert isinstance(result, ToolMessage)

        # Verify dispatch was called with config
        agent_graph.orchestrator_tools.dispatch.assert_called_once()
        call_kwargs = agent_graph.orchestrator_tools.dispatch.call_args.kwargs
        assert "config" in call_kwargs, (
            "dispatch() must receive 'config' argument — without it, "
            "Langfuse tracing spans cannot be created."
        )
        assert call_kwargs["config"] is fake_config

class TestToolExecutorConfigPropagation:
    """Tests for config propagation through the tool_executor node."""

    @pytest.mark.asyncio
    async def test_tool_executor_passes_config_to_dispatch(self, agent_graph):
        """When tool_executor receives config, it must pass it to dispatch()."""
        success_result = {
            "task_description": "test task",
            "status": "success",
            "result_text": "done",
            "content": {},
        }
        agent_graph.orchestrator_tools.dispatch = AsyncMock(return_value=success_result)

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

        fake_config = {"config_key": "test_value", "callbacks": []}
        result = await agent_graph.tool_executor(state, config=fake_config)

        assert len(result["messages"]) == 1
        assert isinstance(result["messages"][0], ToolMessage)

        # Verify dispatch received the config
        agent_graph.orchestrator_tools.dispatch.assert_called_once()
        call_kwargs = agent_graph.orchestrator_tools.dispatch.call_args.kwargs
        assert "config" in call_kwargs, (
            "dispatch() must receive 'config' from tool_executor — "
            "this is how Langfuse callback manager reaches OrchestratorTools."
        )
        assert call_kwargs["config"] is fake_config

    @pytest.mark.asyncio
    async def test_tool_executor_passes_config_to_all_dispatch_calls(self, agent_graph):
        """When multiple tool_calls are made, each dispatch receives the same config."""
        success_result = {
            "task_description": "test task",
            "status": "success",
            "result_text": "done",
            "content": {},
        }
        agent_graph.orchestrator_tools.dispatch = AsyncMock(return_value=success_result)

        ai_msg = AIMessage(
            content="decision",
            tool_calls=[
                {
                    "name": "analyze_model_inputs_for_target",
                    "args": {},
                    "id": "call_1",
                },
                {
                    "name": "analyze_excel_model",
                    "args": {},
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

        fake_config = {"config_key": "test_value", "callbacks": []}
        result = await agent_graph.tool_executor(state, config=fake_config)

        assert len(result["messages"]) == 2

        # Both dispatch calls must have received config
        assert agent_graph.orchestrator_tools.dispatch.call_count == 2
        for call_args in agent_graph.orchestrator_tools.dispatch.call_args_list:
            assert "config" in call_args.kwargs
            assert call_args.kwargs["config"] is fake_config
