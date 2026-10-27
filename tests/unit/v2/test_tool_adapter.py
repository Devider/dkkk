"""S1.1: tool adapter, catalog and calculation tools (ADR-5) — scripted model, fake domain, no LibreOffice."""

import json
from dataclasses import dataclass
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel
from tests.unit.v2.fakes import FakeContext, ScriptedChatModel, tool_call

from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.adapter import ToolContextError, ToolSpec, build_tool
from aigw_service.api.v2.tools.catalog import ToolCatalog, default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError, ToolSuccess, failure
from excel_engine import ExcelAnalysisToolResult, ModelInputAnalysisToolResult

SERVICE_FIELDS = {"user_id", "session_id", "thread_id", "file_name", "file_path", "model_index"}
INDEX = ModelIndex({"IN1": "Метанол", "IN2": "Нефть марки Brent"}, {"OUT0": "EBITDA", "OUT7": "Чистая прибыль"})


async def run_turn(tools: list, steps: list, context: Any) -> list:
    agent = create_agent(ScriptedChatModel(steps=steps), tools, context_schema=type(context))
    out = await agent.ainvoke({"messages": [HumanMessage("ход")]}, context=context)
    return out["messages"]


def tool_messages(messages: list) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


class ProbeArgs(BaseModel):
    """Model-facing arguments of the probe tool."""

    output_id: str


class TestModelIndex:
    def test_translates_ids(self) -> None:
        assert INDEX.input_name("IN1") == "Метанол"
        assert INDEX.output_name("OUT7") == "Чистая прибыль"

    def test_unknown_id_raises_unknown_id(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            INDEX.input_name("IN999")
        assert exc_info.value.code == "unknown_id"

    def test_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            INDEX.inputs["IN3"] = "x"  # type: ignore[index]


class TestAdapter:
    @pytest.fixture
    def probe(self) -> tuple[Any, dict]:
        seen: dict = {}

        def call(args: ProbeArgs, ctx: Any) -> dict:
            seen.update(
                args=args.model_dump(),
                user=ctx.user_id,
                file=ctx.file_path,
                name=ctx.model_index.output_name(args.output_id),
            )
            return ToolSuccess(result="EBITDA 2026 = 1265.9")

        return build_tool(ToolSpec("probe", "Проба", ProbeArgs, call)), seen

    def test_schema_has_only_model_arguments(self, probe: tuple) -> None:
        tool, _ = probe
        assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {"output_id"}

    async def test_context_comes_from_runtime(self, probe: tuple) -> None:
        tool, seen = probe
        ctx = FakeContext(user_id="alice", file_path="/tmp/user_file_alice.xlsx", model_index=INDEX)
        messages = await run_turn([tool], [tool_call("probe", output_id="OUT0"), AIMessage("ок")], ctx)
        assert seen == {
            "args": {"output_id": "OUT0"},
            "user": "alice",
            "file": "/tmp/user_file_alice.xlsx",
            "name": "EBITDA",
        }
        assert json.loads(tool_messages(messages)[0].content) == {"status": "ok", "result": "EBITDA 2026 = 1265.9"}

    async def test_forged_service_arguments_are_dropped(self, probe: tuple) -> None:
        tool, seen = probe
        forged = tool_call("probe", output_id="OUT0", user_id="mallory", file_name="/etc/passwd")
        await run_turn([tool], [forged, AIMessage("ок")], FakeContext(model_index=INDEX))
        assert seen["args"] == {"output_id": "OUT0"}
        assert seen["user"] == "u1"
        assert seen["file"] == "/tmp/user_file_u1.xlsx"

    async def test_missing_turn_context_is_a_wiring_error(self, probe: tuple) -> None:
        tool, _ = probe

        @dataclass
        class NoIndex:
            user_id: str = "u1"

        with pytest.raises(ToolContextError):
            await run_turn([tool], [tool_call("probe", output_id="OUT0"), AIMessage("ок")], NoIndex())


class TestTransientFailures:
    """S2.5 design D4: engine_unavailable leaves the adapter as an exception, so ToolRetryMiddleware sees it."""

    @staticmethod
    def failing(code: ErrorCode, raised: bool) -> Any:
        def call(_args: ProbeArgs, _ctx: Any) -> Any:
            if raised:
                raise ToolError(code)
            return failure(code)

        return build_tool(ToolSpec("probe", "Проба", ProbeArgs, call))

    @pytest.mark.parametrize("raised", [True, False], ids=["raised", "returned"])
    async def test_engine_unavailable_is_raised(self, raised: bool) -> None:
        tool = self.failing(ErrorCode.ENGINE_UNAVAILABLE, raised)
        with pytest.raises(ToolError) as exc_info:
            await run_turn(
                [tool], [tool_call("probe", output_id="OUT0"), AIMessage("ок")], FakeContext(model_index=INDEX)
            )
        assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE

    @pytest.mark.parametrize("raised", [True, False], ids=["raised", "returned"])
    async def test_other_codes_stay_envelopes(self, raised: bool) -> None:
        tool = self.failing(ErrorCode.ENGINE_TIMEOUT, raised)
        steps = [tool_call("probe", output_id="OUT0"), AIMessage("ок")]
        messages = await run_turn([tool], steps, FakeContext(model_index=INDEX))
        assert json.loads(tool_messages(messages)[0].content)["code"] == "engine_timeout"


class TestCatalog:
    def test_skill_tool_is_not_in_base_set(self) -> None:
        catalog = ToolCatalog()
        catalog.register(ToolSpec("probe", "Проба", ProbeArgs, lambda a, c: ToolSuccess(result="")))
        catalog.register(
            ToolSpec(
                "calc_headroom",
                "Запас",
                ProbeArgs,
                lambda a, c: ToolSuccess(result=""),
                availability="covenant-headroom",
            )
        )
        assert [t.name for t in catalog.base_tools()] == ["probe"]
        assert [t.name for t in catalog.skill_tools()] == ["calc_headroom"]

    def test_duplicate_name_rejected(self) -> None:
        catalog = ToolCatalog()
        catalog.register(ToolSpec("probe", "Проба", ProbeArgs, lambda a, c: ToolSuccess(result="")))
        with pytest.raises(ValueError, match="already registered"):
            catalog.register(ToolSpec("probe", "Проба", ProbeArgs, lambda a, c: ToolSuccess(result="")))

    def test_default_catalog_has_engine_calc_tools(self) -> None:
        catalog = default_catalog()
        assert {t.name for t in catalog.base_tools()} >= {"run_scenarios", "find_input_for_target"}
        assert catalog.engine_tool_names() >= {"run_scenarios", "find_input_for_target"}

    def test_no_service_fields_in_any_schema(self) -> None:
        catalog = default_catalog()
        for tool in catalog.base_tools() + catalog.skill_tools():
            schema = json.dumps(tool.tool_call_schema.model_json_schema())
            leaked = [f for f in SERVICE_FIELDS if f'"{f}"' in schema]
            assert not leaked, f"{tool.name} exposes {leaked}"


class TestCalcTools:
    @pytest.fixture
    def domain(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        calls: dict = {}

        def fake_ema(**kwargs: Any) -> ExcelAnalysisToolResult:
            calls["ema"] = kwargs
            return ExcelAnalysisToolResult(status="OK", result="3 сценария", content={})

        def fake_ift(**kwargs: Any) -> ModelInputAnalysisToolResult:
            calls["ift"] = kwargs
            return ModelInputAnalysisToolResult(status="OK", result="найдено", content={})

        monkeypatch.setattr(calc_tools, "analyze_excel_model", fake_ema)
        monkeypatch.setattr(calc_tools, "analyze_model_inputs_for_target", fake_ift)
        return calls

    async def test_run_scenarios_translates_ids(self, domain: dict) -> None:
        call = tool_call(
            "run_scenarios",
            inputs=[{"id": "IN1", "min": 300, "max": 500, "step": 100}],
            outputs=["OUT0", "OUT7"],
            year=2026,
        )
        ctx = FakeContext(file_path="/tmp/user_file_u1.xlsx", model_index=INDEX)
        messages = await run_turn(default_catalog().base_tools(), [call, AIMessage("ок")], ctx)
        assert domain["ema"] == {
            "file_name": "/tmp/user_file_u1.xlsx",
            "input_names": ["Метанол"],
            "output_names": ["EBITDA", "Чистая прибыль"],
            "output_years": [2026, 2026],
            "ranges": [[300.0, 500.0]],
            "steps": [100.0],
            "user_id": None,
        }
        assert json.loads(tool_messages(messages)[0].content)["status"] == "ok"

    async def test_find_input_for_target_uses_v1_settings(self, domain: dict) -> None:
        call = tool_call("find_input_for_target", output_id="OUT0", year=2026, target=1000, input_ids=["IN1", "IN2"])
        await run_turn(default_catalog().base_tools(), [call, AIMessage("ок")], FakeContext(model_index=INDEX))
        assert domain["ift"] == {
            "file_name": "/tmp/user_file_u1.xlsx",
            "output_name": "EBITDA",
            "output_year": 2026,
            "target_value": 1000.0,
            "input_names": ["Метанол", "Нефть марки Brent"],
            "tolerance": 0.1,
            "max_scenarios": 1000,
        }
