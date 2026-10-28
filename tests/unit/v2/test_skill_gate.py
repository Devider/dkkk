"""S3.4 (FR-18, §11 «Активация» п. 3; design D1): a hidden skill tool is refused with ``skill_required`` and a path."""

import json
from types import SimpleNamespace

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from aigw_service.api.v2.middleware.skill_gate import SkillToolGate
from aigw_service.api.v2.tools.catalog import default_catalog

GATE = SkillToolGate({"trace_dependencies": "sensitivity-drivers"})


def request(name: str, attached: bool) -> ToolCallRequest:
    tool = default_catalog().tool("get_values") if attached else None
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "c1"},
        tool=tool,
        state={},
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
    )


def passed(req: ToolCallRequest) -> ToolMessage:
    return ToolMessage("passed", tool_call_id=req.tool_call["id"])


async def apassed(req: ToolCallRequest) -> ToolMessage:
    return passed(req)


class TestSkillToolGate:
    def test_detached_skill_tool_refused(self) -> None:
        out = GATE.wrap_tool_call(request("trace_dependencies", attached=False), passed)
        assert isinstance(out, ToolMessage)
        assert out.status == "error"
        body = json.loads(str(out.content))
        assert body["code"] == "skill_required"
        assert "/skills/sensitivity-drivers/SKILL.md" in body["hint"]
        assert out.tool_call_id == "c1"
        assert out.name == "trace_dependencies"

    async def test_detached_skill_tool_refused_async(self) -> None:
        out = await GATE.awrap_tool_call(request("trace_dependencies", attached=False), apassed)
        assert json.loads(str(out.content))["code"] == "skill_required"  # type: ignore[union-attr]

    async def test_attached_skill_tool_runs(self) -> None:
        assert GATE.wrap_tool_call(request("trace_dependencies", attached=True), passed).content == "passed"
        out = await GATE.awrap_tool_call(request("trace_dependencies", attached=True), apassed)
        assert out.content == "passed"  # type: ignore[union-attr]

    async def test_name_outside_catalogue_passes_through(self) -> None:
        """``write_file`` keeps the tool node's unknown-tool error (AR-Q14)."""
        assert GATE.wrap_tool_call(request("write_file", attached=False), passed).content == "passed"
        out = await GATE.awrap_tool_call(request("write_file", attached=False), apassed)
        assert out.content == "passed"  # type: ignore[union-attr]


class TestCatalogToolSkills:
    def test_only_skill_tools_with_their_skill(self) -> None:
        catalog = default_catalog()
        mapping = catalog.tool_skills()
        assert all(catalog.spec(name).availability == skill for name, skill in mapping.items())
        assert not set(mapping) & {t.name for t in catalog.base_tools()}
