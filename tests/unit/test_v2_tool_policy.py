"""Tests for the GigaChat-specific Deep Agents tool exclusions."""

from dataclasses import dataclass
from types import SimpleNamespace

from deepagents._models import get_model_provider
from deepagents.middleware._tool_exclusion import _ToolExclusionMiddleware
from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
from langchain_core.messages import ToolMessage

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


def test_resolved_gigachat_profile_excludes_defaults_and_keeps_excel_tools() -> None:
    register_v2_harness_profile()
    model = FakeGigaChatModel()
    profile = _harness_profile_for_model(model, None)

    assert get_model_provider(model) == "giga"
    assert profile.excluded_tools == V2_EXCLUDED_BUILTIN_TOOLS

    application_tool_names = {
        definition.name if hasattr(definition, "name") else definition.__name__ for definition in TOOLS
    }
    assert application_tool_names
    assert application_tool_names.isdisjoint(profile.excluded_tools)


def test_exclusion_middleware_hides_defaults_but_keeps_application_tools() -> None:
    register_v2_harness_profile()
    profile = _harness_profile_for_model(FakeGigaChatModel(), None)
    middleware = _ToolExclusionMiddleware(excluded=profile.excluded_tools)
    offered_tools = [
        {"name": "ls"},
        {"name": "glob"},
        {"name": "task"},
        {"name": "analyze_excel_model"},
        {"name": "get_output_info"},
    ]
    request = FakeModelRequest(tools=offered_tools)

    visible_tools = middleware.wrap_model_call(request, lambda filtered_request: filtered_request.tools)

    assert {tool["name"] for tool in visible_tools} == {
        "analyze_excel_model",
        "get_output_info",
    }


def test_excluded_tool_call_is_rejected() -> None:
    register_v2_harness_profile()
    profile = _harness_profile_for_model(FakeGigaChatModel(), None)
    middleware = _ToolExclusionMiddleware(excluded=profile.excluded_tools)
    tool_call_request = SimpleNamespace(tool_call={"name": "ls", "id": "call-1"})

    result = middleware.wrap_tool_call(tool_call_request, lambda _: None)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
