"""End-to-end tests for session continuity (GigaChat 422 constraint).

These tests verify the key behaviors:
- force_execute_remaining closes unexecuted tool_calls at iteration cap
- route_after_orchestrator returns correct routing at cap
- error ToolMessages are created for failed dispatch calls
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from aigw_service.api.v1.main_graph import AgentGraph as RealAgentGraph

# Minimal runnable config forwarded to OrchestratorTools.dispatch (RunnableConfig).
_CONFIG = {"configurable": {"thread_id": "u:s", "user_id": "user_1"}}


class TestSessionContinuity:
    """E2E-level tests for session continuity."""

    # ---- Task 8.1: Session continuation after tool failure ----
    @pytest.mark.asyncio
    async def test_session_continuation_after_tool_failure(self):
        """dispatch raises → ToolMessage created → no 422 on session continuation.

        Tests the core property: every tool_call_id gets a ToolMessage even on failure.
        """
        graph = MagicMock()
        graph.orchestrator_tools = MagicMock()

        async def failing_dispatch(*args, **kwargs):
            raise Exception("HTTP 424 from IFT")

        graph.orchestrator_tools.dispatch = AsyncMock(side_effect=failing_dispatch)

        call = {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"}
        state = {
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }
        fingerprints: set[str] = set()
        tool_results: list[dict] = []

        result = await RealAgentGraph._execute_single_call(
            graph, call, state, fingerprints, tool_results, _CONFIG
        )

        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == "call_1"
        assert "Ошибка выполнения" in result.content
        assert "HTTP 424" in result.content
        assert len(tool_results) == 1
        assert tool_results[0]["status"] == "error"

    # ---- Task 8.2: Cap routing through force_execute_remaining ----
    @pytest.mark.asyncio
    async def test_cap_routing_through_force_execute_remaining(self):
        """Cap reached → force_execute_remaining creates ToolMessage stubs → routes to synthesizer."""
        graph = MagicMock()
        graph.MAX_ORCHESTRATOR_ITERATIONS = 4

        # Test force_execute_remaining creates ToolMessage stubs
        ai_msg = AIMessage(
            content="decision",
            tool_calls=[
                {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"},
            ],
        )
        state = {"messages": [ai_msg], "tool_call_count": 4}

        result = await RealAgentGraph.force_execute_remaining(graph, state)

        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert "Достигнут лимит" in msg.content
        assert "analyze_model_inputs_for_target" in msg.content

        # Test routing at cap
        route = RealAgentGraph.route_after_orchestrator(graph, state)
        assert route == "force_execute_remaining"

    # ---- Task 8.3: Coherent response after failure ----
    @pytest.mark.asyncio
    async def test_coherent_response_after_failure(self):
        """First call has tool failure (ToolMessage created), second call continues normally."""
        graph = MagicMock()
        graph.MAX_ORCHESTRATOR_ITERATIONS = 4

        call_count = [0]

        async def failing_dispatch(*args, **kwargs):
            call_count[0] += 1
            raise Exception("HTTP 424")

        graph.orchestrator_tools = MagicMock()
        graph.orchestrator_tools.dispatch = AsyncMock(side_effect=failing_dispatch)

        # First call: simulate tool failure
        call = {"name": "analyze_model_inputs_for_target", "args": {}, "id": "call_1"}
        state1 = {
            "available_inputs": {},
            "available_outputs": {},
            "filename": "test.xlsx",
            "user_id": "user_1",
        }
        fingerprints: set[str] = set()
        tool_results: list[dict] = []

        result1 = await RealAgentGraph._execute_single_call(
            graph, call, state1, fingerprints, tool_results, _CONFIG
        )

        # Verify ToolMessage exists for the failed call
        assert isinstance(result1, ToolMessage)
        assert result1.tool_call_id == "call_1"
        assert "Ошибка выполнения" in result1.content
        assert tool_results[0]["status"] == "error"

        # Second call: simulate route_after_orchestrator at cap with no tool_calls
        state2 = {
            "messages": [AIMessage(content="answer", tool_calls=[])],
            "tool_call_count": 4,
        }
        route = RealAgentGraph.route_after_orchestrator(graph, state2)
        assert route == "force_execute_remaining"