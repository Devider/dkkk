"""S2.5 (FR-17): CalcCache — a repeated calculation of the session never starts the engine again."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.store.memory import InMemoryStore
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.calc_cache import CalcCache
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.catalog import default_catalog
from excel_engine import ExcelAnalysisToolResult, ModelInputAnalysisToolResult

SCENARIOS = {"inputs": [{"id": "IN1", "min": 300, "max": 500, "step": 100}], "outputs": ["OUT0"], "year": 2026}
TARGET = {"output_id": "OUT0", "year": 2026, "target": 1000.0, "input_ids": ["IN1"]}


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Tools whose calculation reached the domain (one entry per engine run)."""
    runs: list[str] = []

    def ema(**_kw: Any) -> ExcelAnalysisToolResult:
        runs.append("run_scenarios")
        return ExcelAnalysisToolResult(status="OK", result="3 сценария", content={})

    def ift(**_kw: Any) -> ModelInputAnalysisToolResult:
        runs.append("find_input_for_target")
        return ModelInputAnalysisToolResult(status="OK", result="Найдено", content={})

    monkeypatch.setattr(calc_tools, "analyze_excel_model", ema)
    monkeypatch.setattr(calc_tools, "analyze_model_inputs_for_target", ift)
    return runs


class Session:
    """One agent over one store and checkpointer, as the service runs it."""

    def __init__(self, tmp_path: Path, store: Any) -> None:
        self.tmp_path, self.store, self.saver = tmp_path, store, InMemorySaver()

    @classmethod
    async def create(cls, tmp_path: Path) -> "Session":
        return cls(tmp_path, await uploaded_store("u1", INDEX, str(tmp_path)))

    async def turn(self, steps: list, session_id: str = "s1") -> dict:
        model = ScriptedChatModel(steps=steps)
        agent = AgentFactory(
            model,
            default_catalog(),
            store=self.store,
            upload_dir=str(self.tmp_path),
            plan_mode="off",
            checkpointer=self.saver,
        ).build()
        return await agent.ainvoke(
            {"messages": [HumanMessage("EBITDA 2026?")]},
            config={"configurable": {"thread_id": f"u1:{session_id}"}},
            context=RequestContext(user_id="u1", session_id=session_id),
        )


def results(out: dict) -> list[str]:
    return [m.content for m in out["messages"] if isinstance(m, ToolMessage)]


class TestCalcCache:
    async def test_second_identical_call(self, tmp_path: Path, engine: list) -> None:
        session = await Session.create(tmp_path)
        steps = [
            tool_call("run_scenarios", "c1", **SCENARIOS),
            tool_call("run_scenarios", "c2", **SCENARIOS),
            AIMessage("ок"),
        ]
        out = await session.turn(steps)
        assert engine == ["run_scenarios"]
        first, second = results(out)
        assert first == second
        assert [m.tool_call_id for m in out["messages"] if isinstance(m, ToolMessage)] == ["c1", "c2"]

    async def test_key_order_and_explicit_defaults(self, tmp_path: Path, engine: list) -> None:
        session = await Session.create(tmp_path)
        reordered = {
            "year": 2026,
            "outputs": ["OUT0"],
            "inputs": [{"step": 100, "max": 500, "id": "IN1", "min": 300.0}],
        }
        steps = [
            tool_call("run_scenarios", "c1", **SCENARIOS),
            tool_call("run_scenarios", "c2", **reordered),
            tool_call("find_input_for_target", "c3", **TARGET),
            tool_call("find_input_for_target", "c4", **TARGET, tolerance=0.1),  # the default, passed explicitly
            AIMessage("ок"),
        ]
        await session.turn(steps)
        assert engine == ["run_scenarios", "find_input_for_target"]

    async def test_other_arguments(self, tmp_path: Path, engine: list) -> None:
        session = await Session.create(tmp_path)
        other = {**SCENARIOS, "inputs": [{"id": "IN1", "min": 300, "max": 600, "step": 100}]}
        steps = [
            tool_call("run_scenarios", "c1", **SCENARIOS),
            tool_call("run_scenarios", "c2", **other),
            AIMessage("ок"),
        ]
        await session.turn(steps)
        assert engine == ["run_scenarios", "run_scenarios"]

    async def test_next_turn_of_the_session(self, tmp_path: Path, engine: list) -> None:
        session = await Session.create(tmp_path)
        await session.turn([tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("ок")])
        await session.turn([tool_call("run_scenarios", "c2", **SCENARIOS), AIMessage("ок")])
        assert engine == ["run_scenarios"]

    async def test_other_session(self, tmp_path: Path, engine: list) -> None:
        session = await Session.create(tmp_path)
        await session.turn([tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("ок")], session_id="s1")
        await session.turn([tool_call("run_scenarios", "c2", **SCENARIOS), AIMessage("ок")], session_id="s2")
        assert engine == ["run_scenarios", "run_scenarios"]

    async def test_error_is_not_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        runs: list[int] = []

        def no_cell(**_kw: Any) -> ExcelAnalysisToolResult:
            runs.append(1)
            return ExcelAnalysisToolResult(status="ERROR", result="Не удалось найти ячейку для EBITDA", content={})

        monkeypatch.setattr(calc_tools, "analyze_excel_model", no_cell)
        session = await Session.create(tmp_path)
        steps = [
            tool_call("run_scenarios", "c1", **SCENARIOS),
            tool_call("run_scenarios", "c2", **SCENARIOS),
            AIMessage("ок"),
        ]
        out = await session.turn(steps)
        assert len(runs) == 2
        assert all('"no_values"' in r for r in results(out))


class TestOtherWorkbook:
    """FR-17 «Другая книга»: the workbook hash is part of the key."""

    @staticmethod
    def request(file_hash: str, call_id: str) -> ToolCallRequest:
        return ToolCallRequest(
            tool_call={"name": "run_scenarios", "args": SCENARIOS, "id": call_id},
            tool=default_catalog().tool("run_scenarios"),
            state={"model_ref": {"file_hash": file_hash}},
            runtime=SimpleNamespace(context=SimpleNamespace(thread_id="u1:s1")),  # type: ignore[arg-type]
        )

    async def test_replaced_workbook_misses(self) -> None:
        cache = CalcCache({"run_scenarios"}, store=InMemoryStore())
        runs: list[str] = []

        async def handler(request: ToolCallRequest) -> ToolMessage:
            runs.append(request.tool_call["id"])
            return ToolMessage('{"status": "ok", "result": "x"}', tool_call_id=request.tool_call["id"])

        await cache.awrap_tool_call(self.request("hash-a", "c1"), handler)
        hit = await cache.awrap_tool_call(self.request("hash-a", "c2"), handler)
        await cache.awrap_tool_call(self.request("hash-b", "c3"), handler)
        assert runs == ["c1", "c3"]
        assert hit.tool_call_id == "c2"

    async def test_tool_without_engine_is_not_cached(self) -> None:
        cache = CalcCache({"find_input_for_target"}, store=InMemoryStore())
        runs: list[str] = []

        async def handler(request: ToolCallRequest) -> ToolMessage:
            runs.append(request.tool_call["id"])
            return ToolMessage('{"status": "ok", "result": "x"}', tool_call_id=request.tool_call["id"])

        await cache.awrap_tool_call(self.request("hash-a", "c1"), handler)
        await cache.awrap_tool_call(self.request("hash-a", "c2"), handler)
        assert runs == ["c1", "c2"]
