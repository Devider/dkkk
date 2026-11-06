"""Unit tests for route_after_orchestrator routing logic."""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from aigw_service.api.v1.main_graph import AgentGraph


@pytest.fixture
def agent_graph():
    """AgentGraph with mocked dependencies."""
    ag = AgentGraph.__new__(AgentGraph)
    ag.MAX_ORCHESTRATOR_ITERATIONS = 4
    ag.orchestrator_tools = MagicMock()
    return ag


class TestRouteAfterOrchestrator:
    """Tests for route_after_orchestrator method."""

    def test_below_cap_with_tool_calls(self, agent_graph):
        """When tool_call_count < MAX, route to tool_executor."""
        ai_msg = AIMessage(
            content="decision",
            tool_calls=[{"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"}],
        )
        state = {"messages": [ai_msg], "tool_call_count": 3}

        result = agent_graph.route_after_orchestrator(state)

        assert result == "tool_executor"

    def test_at_cap_with_tool_calls(self, agent_graph):
        """When tool_call_count >= MAX and tool_calls present, route to force_execute_remaining."""
        ai_msg = AIMessage(
            content="decision",
            tool_calls=[{"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"}],
        )
        state = {"messages": [ai_msg], "tool_call_count": 4}

        result = agent_graph.route_after_orchestrator(state)

        assert result == "force_execute_remaining"

    def test_no_tool_calls(self, agent_graph):
        """When no tool_calls, route to synthesizer."""
        ai_msg = AIMessage(content="answer", tool_calls=[])
        state = {"messages": [ai_msg], "tool_call_count": 0}

        result = agent_graph.route_after_orchestrator(state)

        assert result == "synthesizer"

    def test_at_cap_without_tool_calls(self, agent_graph):
        """When cap reached but no tool_calls, route to force_execute_remaining."""
        ai_msg = AIMessage(content="answer", tool_calls=[])
        state = {"messages": [ai_msg], "tool_call_count": 4}

        result = agent_graph.route_after_orchestrator(state)

        assert result == "force_execute_remaining"
