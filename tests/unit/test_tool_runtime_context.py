"""Tests for identity injection into Excel tools through runtime config."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool

from aigw_service.api.v1 import tools as excel_tools
from aigw_service.api.v1.services import Agent


def test_identity_is_not_part_of_model_facing_tool_schemas() -> None:
    for definition in excel_tools.TOOLS:
        langchain_tool_instance = definition if isinstance(definition, BaseTool) else langchain_tool(definition)

        assert "user_id" not in langchain_tool_instance.args
        assert "thread_id" not in langchain_tool_instance.args
        assert "config" not in langchain_tool_instance.args


def test_identity_dependent_tool_reads_user_id_from_invocation_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_user_ids: list[str | None] = []

    def get_missing_file(user_id: str | None) -> str:
        observed_user_ids.append(user_id)
        return "not-present-in-test.xlsx"

    monkeypatch.setattr(excel_tools, "get_store_file", get_missing_file)

    result = excel_tools.describe_outputs_sheet.invoke(
        {},
        config={"configurable": {"thread_id": "user0001:session", "user_id": "user0001"}},
    )

    assert observed_user_ids == ["user0001"]
    assert result.status == "ERROR"


def test_v1_manual_tool_runner_passes_identity_in_runtime_config() -> None:
    agent = Agent.__new__(Agent)
    agent.logger = MagicMock()
    mock_tool = MagicMock()
    mock_tool.name = "describe_outputs_sheet"
    mock_tool.invoke.return_value = SimpleNamespace(result="ok", content="model summary")
    agent.tools = [mock_tool]
    business_args: dict = {}
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "describe_outputs_sheet",
                        "args": business_args,
                        "id": "call-1",
                    }
                ],
            )
        ],
        "thread_id": "user0001:session",
        "user_id": "user0001",
        "generated_graphs": [],
    }

    agent.execute_tool(state)

    mock_tool.invoke.assert_called_once_with(
        {},
        config={
            "configurable": {
                "thread_id": "user0001:session",
                "user_id": "user0001",
            }
        },
    )
