"""Unit tests for force_execute_remaining node."""

import asyncio
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from aigw_service.api.v1.main_graph import AgentGraph


@pytest.fixture
def agent_graph():
    """AgentGraph with mocked dependencies."""
    ag = AgentGraph.__new__(AgentGraph)
    ag.MAX_ORCHESTRATOR_ITERATIONS = 4
    ag.orchestrator_tools = MagicMock()
    return ag


class TestForceExecuteRemaining:
    """Tests for force_execute_remaining node."""

    @pytest.mark.asyncio
    async def test_force_execute_remaining_with_pending_calls(self, agent_graph):
        """When there are 2 unexecuted tool_calls, force_execute_remaining returns 2 ToolMessages."""
        ai_msg = AIMessage(
            content="decision",
            tool_calls=[
                {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"},
                {"name": "analyze_excel_model", "args": {}, "id": "call_2"},
            ],
        )
        state = {
            "messages": [ai_msg],
            "tool_call_count": 4,
        }

        result = await agent_graph.force_execute_remaining(state)

        assert "messages" in result
        assert len(result["messages"]) == 2
        for msg in result["messages"]:
            assert isinstance(msg, ToolMessage)
        assert result["messages"][0].tool_call_id == "call_1"
        assert result["messages"][0].name == "analyze_model_inputs_for_target"
        assert "Достигнут лимит итераций" in result["messages"][0].content
        assert "analyze_model_inputs_for_target" in result["messages"][0].content
        assert result["messages"][1].tool_call_id == "call_2"
        assert result["messages"][1].name == "analyze_excel_model"
        assert "analyze_excel_model" in result["messages"][1].content

    @pytest.mark.asyncio
    async def test_force_execute_remaining_no_tool_calls(self, agent_graph):
        """When tool_calls=None, force_execute_remaining returns empty messages."""
        ai_msg = AIMessage(content="answer", tool_calls=[])
        # Simulate no tool calls by removing the attribute or setting to empty
        state = {"messages": [ai_msg], "tool_call_count": 4}

        result = await agent_graph.force_execute_remaining(state)

        assert result == {"messages": []}

    @pytest.mark.asyncio
    async def test_force_execute_remaining_single_call(self, agent_graph):
        """When there is 1 pending tool_call, content includes tool name and cap message."""
        ai_msg = AIMessage(
            content="decision",
            tool_calls=[{"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_3"}],
        )
        state = {"messages": [ai_msg], "tool_call_count": 4}

        result = await agent_graph.force_execute_remaining(state)

        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert msg.tool_call_id == "call_3"
        assert msg.name == "analyze_model_inputs_for_target"
        assert "Достигнут лимит итераций" in msg.content
        assert "analyze_model_inputs_for_target" in msg.content
