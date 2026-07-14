"""Unit tests for tool call deduplication in execute_tool."""

import json
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from aigw_service.api.v1.services import Agent


class TestToolDedup:
    """Tests for tool call deduplication in execute_tool."""

    @pytest.fixture
    def agent(self):
        """Bare Agent with mocked logger, no real tools."""
        a = Agent.__new__(Agent)
        a.logger = MagicMock()
        a.tools = []
        return a

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_state(tool_calls: list, executed: list | None = None) -> dict:
        return {
            "messages": [AIMessage(content="", tool_calls=tool_calls)],
            "executed_tool_calls": executed or [],
            "generated_graphs": [],
        }

    @staticmethod
    def _make_call(name: str, args: dict, call_id: str = "call_1") -> dict:
        return {"name": name, "args": args, "id": call_id, "type": "tool_call"}

    @staticmethod
    def _fingerprint(name: str, args: dict) -> str:
        return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_dedup_blocks_identical_call(self, agent):
        """Fingerprint совпадает → execute_tool пропускает вызов."""
        args = {"input_names": ["CPI"], "output_names": ["EBITDA"], "year": 2025}
        state = self._make_state(
            tool_calls=[self._make_call("analyze_excel_model", args)],
            executed=[self._fingerprint("analyze_excel_model", args)],
        )
        result = agent.execute_tool(state)
        last = result["messages"][-1]
        assert isinstance(last, ToolMessage)
        assert "уже был вызван" in last.content

    def test_dedup_allows_different_args(self, agent):
        """Другие аргументы → не блокируется."""
        prev = {"input_names": ["CPI"], "output_names": ["EBITDA"], "year": 2025}
        new = {"input_names": ["CPI", "FX"], "output_names": ["EBITDA"], "year": 2025}
        state = self._make_state(
            tool_calls=[self._make_call("analyze_excel_model", new)],
            executed=[self._fingerprint("analyze_excel_model", prev)],
        )
        result = agent.execute_tool(state)
        for msg in result["messages"]:
            if isinstance(msg, ToolMessage):
                assert "уже был вызван" not in msg.content

    def test_dedup_empty_history(self, agent):
        """Пустой executed_tool_calls → первый вызов проходит."""
        args = {"input_names": ["CPI"], "output_names": ["EBITDA"], "year": 2025}
        state = self._make_state(
            tool_calls=[self._make_call("analyze_excel_model", args)],
            executed=[],
        )
        result = agent.execute_tool(state)
        # tool not found (tools=[]) → error AIMessage, NOT dedup ToolMessage
        assert isinstance(result["messages"][-1], AIMessage)

    def test_dedup_no_key_in_state(self, agent):
        """Нет ключа executed_tool_calls → setdefault создаёт []."""
        args = {"input_names": ["CPI"], "output_names": ["EBITDA"], "year": 2025}
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[self._make_call("analyze_excel_model", args)],
                )
            ],
            "generated_graphs": [],
        }
        result = agent.execute_tool(state)
        assert isinstance(result["messages"][-1], AIMessage)

    def test_dedup_different_tool_same_args(self, agent):
        """Разные инструменты с одинаковыми args — разные fingerprints."""
        args = {"input_names": ["CPI"], "output_names": ["EBITDA"], "year": 2025}
        state = self._make_state(
            tool_calls=[self._make_call("modify_excel_input_value", args)],
            executed=[self._fingerprint("analyze_excel_model", args)],
        )
        result = agent.execute_tool(state)
        for msg in result["messages"]:
            if isinstance(msg, ToolMessage):
                assert "уже был вызван" not in msg.content

    def test_dedup_identical_calls_in_one_message(self, agent):
        """Два identical tool_call в одном сообщении → второй блокируется."""
        mock_result = MagicMock(result="done", content="ok")
        mock_result.image_path = None
        mock_tool = MagicMock()
        mock_tool.name = "analyze_excel_model"
        mock_tool.invoke.return_value = mock_result
        agent.tools = [mock_tool]

        args = {"input_names": ["CPI"], "output_names": ["EBITDA"], "year": 2025}
        state = self._make_state(
            tool_calls=[
                self._make_call("analyze_excel_model", args, call_id="call_1"),
                self._make_call("analyze_excel_model", args, call_id="call_2"),
            ],
            executed=[],
        )
        result = agent.execute_tool(state)

        # Last message should be the dedup skip
        last = result["messages"][-1]
        assert isinstance(last, ToolMessage)
        assert "уже был вызван" in last.content
