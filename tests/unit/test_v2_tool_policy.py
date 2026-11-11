"""Tests for the provider-independent Deep Agents tool exclusions."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from deepagents._models import get_model_provider
from deepagents.middleware._tool_exclusion import _ToolExclusionMiddleware
from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
from langchain_core.messages import ToolMessage
from langchain_ollama import ChatOllama

from aigw_service.api.v1.tools import TOOLS
from aigw_service.api.v2.profiles import (
    V2_EXCLUDED_BUILTIN_TOOLS,
    register_v2_harness_profile,
)


class FakeGigaChatModel:
    """Small model stand-in exposing the provider metadata Deep Agents reads."""

    model = "GigaChat-test"

    def _get_ls_params(self) -> dict[str, str]:
        return {"ls_provider": "giga"}


@dataclass
class FakeModelRequest:
    tools: list[dict[str, str]]

    def override(self, *, tools: list[dict[str, str]]) -> "FakeModelRequest":
        return FakeModelRequest(tools=tools)


@pytest.mark.parametrize("provider", ["giga", "ollama"])
def test_resolved_profile_excludes_defaults_and_keeps_excel_tools(provider: str) -> None:
    register_v2_harness_profile()
    model = FakeGigaChatModel() if provider == "giga" else ChatOllama(model="qwen3.5:9b-mlx")
    profile = _harness_profile_for_model(model, None)

    assert get_model_provider(model) == provider
    assert profile.excluded_tools == V2_EXCLUDED_BUILTIN_TOOLS
    assert profile.general_purpose_subagent.enabled is True
    assert "task" not in profile.excluded_tools

    application_tool_names = {
        definition.name if hasattr(definition, "name") else definition.__name__ for definition in TOOLS
    }
    assert application_tool_names
    assert application_tool_names.isdisjoint(profile.excluded_tools)


@pytest.mark.parametrize("provider", ["giga", "ollama"])
def test_exclusion_middleware_hides_defaults_but_keeps_application_tools(provider: str) -> None:
    register_v2_harness_profile()
    profile = _harness_profile_for_model(
        FakeGigaChatModel() if provider == "giga" else ChatOllama(model="qwen3.5:9b-mlx"), None
    )
    middleware = _ToolExclusionMiddleware(excluded=profile.excluded_tools)
    offered_tools = [
        {"name": "ls"},
        {"name": "glob"},
        {"name": "analyze_excel_model"},
        {"name": "get_output_info"},
    ]
    request = FakeModelRequest(tools=offered_tools)

    visible_tools = middleware.wrap_model_call(request, lambda filtered_request: filtered_request.tools)

    assert {tool["name"] for tool in visible_tools} == {
        "analyze_excel_model",
        "get_output_info",
    }


@pytest.mark.parametrize("provider", ["giga", "ollama"])
def test_task_tool_remains_available_for_registered_resolver(provider: str) -> None:
    register_v2_harness_profile()
    profile = _harness_profile_for_model(
        FakeGigaChatModel() if provider == "giga" else ChatOllama(model="qwen3.5:9b-mlx"), None
    )
    middleware = _ToolExclusionMiddleware(excluded=profile.excluded_tools)
    offered_tools = [{"name": "task"}, {"name": "analyze_excel_model"}]
    request = FakeModelRequest(tools=offered_tools)

    visible_tools = middleware.wrap_model_call(request, lambda filtered_request: filtered_request.tools)

    assert {tool["name"] for tool in visible_tools} == {"task", "analyze_excel_model"}


def test_v2_agent_registers_specialized_resolver_and_excel_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from aigw_service.api.v2 import agent as v2_agent

    captured: dict[str, object] = {}

    class FakeGigaChat:
        """Model placeholder used to inspect V2 agent wiring."""

        def __init__(self, **kwargs: object) -> None:
            pass

    def capture_agent(**kwargs: object) -> dict[str, object]:
        """Capture the agent factory inputs without invoking a model."""
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(v2_agent, "create_v2_model", FakeGigaChat)
    monkeypatch.setattr(v2_agent, "create_deep_agent", capture_agent)

    v2_agent.create_v2_agent()

    assert [subagent["name"] for subagent in captured["subagents"]] == ["workbook-name-resolver"]
    assert captured["tools"] == TOOLS
    assert captured["subagents"][0]["runnable"] is not None


@pytest.mark.parametrize("provider", ["giga", "ollama"])
def test_excluded_tool_call_is_rejected(provider: str) -> None:
    register_v2_harness_profile()
    profile = _harness_profile_for_model(
        FakeGigaChatModel() if provider == "giga" else ChatOllama(model="qwen3.5:9b-mlx"), None
    )
    middleware = _ToolExclusionMiddleware(excluded=profile.excluded_tools)
    tool_call_request = SimpleNamespace(tool_call={"name": "ls", "id": "call-1"})

    result = middleware.wrap_tool_call(tool_call_request, lambda _: None)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
