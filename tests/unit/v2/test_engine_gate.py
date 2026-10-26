"""S1.5 (AR-Q8, ADR-6): EngineGate on stand-in jobs — session queue, pool limit, timeout without orphans."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from tests.unit.v2 import engine_jobs
from tests.unit.v2.fakes import FakeContext, ScriptedChatModel, tool_call
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.engine_gate import EngineGate
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from aigw_service.config.agent.config import AgentSettings
from excel_engine import ExcelAnalysisToolResult

JOB = 0.6  # seconds of "recalculation"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@pytest.fixture
async def gate() -> EngineGate:
    g = EngineGate(workers=2, timeout=10)
    await g.run("warmup", engine_jobs.echo, value=1)  # spawn workers before timing
    await g.run("warmup2", engine_jobs.echo, value=1)
    yield g
    await g.shutdown()


class TestSettings:
    def test_defaults_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = AgentSettings()
        assert (settings.calc_workers, settings.calc_timeout) == (2, 120.0)
        monkeypatch.setenv("AGENT_CALC_WORKERS", "4")
        monkeypatch.setenv("AGENT_CALC_TIMEOUT", "30")
        assert (AgentSettings().calc_workers, AgentSettings().calc_timeout) == (4, 30.0)


class TestConcurrency:
    """AR-Q8 «Конкурентность движка»."""

    async def test_same_session_runs_one_at_a_time(self, gate: EngineGate) -> None:
        a, b = await asyncio.gather(
            gate.run("u1:s1", engine_jobs.interval, seconds=JOB), gate.run("u1:s1", engine_jobs.interval, seconds=JOB)
        )
        assert not overlap(a, b)

    async def test_different_sessions_run_in_parallel(self, gate: EngineGate) -> None:
        a, b = await asyncio.gather(
            gate.run("u1:s1", engine_jobs.interval, seconds=JOB), gate.run("u2:s1", engine_jobs.interval, seconds=JOB)
        )
        assert overlap(a, b)

    async def test_pool_limits_concurrency(self, gate: EngineGate) -> None:
        results = await asyncio.gather(*(gate.run(f"u{i}:s", engine_jobs.interval, seconds=JOB) for i in range(4)))
        events = sorted([(r[0], 1) for r in results] + [(r[1], -1) for r in results], key=lambda e: (e[0], e[1]))
        running = peak = 0
        for _, delta in events:  # sweep line: how many jobs run at the same moment
            running += delta
            peak = max(peak, running)
        assert peak == 2

    async def test_locks_are_cleaned_up(self, gate: EngineGate) -> None:
        await asyncio.gather(*(gate.run("u1:s1", engine_jobs.echo, value=i) for i in range(3)))
        assert gate._locks == {}  # pylint: disable=protected-access


class TestFailures:
    """ADR-6 «Таймаут расчёта без сирот»."""

    async def test_timeout_kills_child_and_pool_recovers(self, tmp_path: Path) -> None:
        gate = EngineGate(workers=1, timeout=3)
        pid_file = tmp_path / "child.pid"
        try:
            with pytest.raises(ToolError) as exc_info:
                await gate.run("u1:s1", engine_jobs.hang_with_child, pid_file=str(pid_file))
            assert exc_info.value.code == ErrorCode.ENGINE_TIMEOUT
            child = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 5
            while alive(child) and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert not alive(child), "the stand-in soffice survived the timeout"
            assert await gate.run("u1:s1", engine_jobs.echo, value="ok") == "ok"
        finally:
            await gate.shutdown()

    async def test_crashed_worker_is_engine_unavailable(self) -> None:
        gate = EngineGate(workers=1, timeout=10)
        try:
            with pytest.raises(ToolError) as exc_info:
                await gate.run("u1:s1", engine_jobs.crash)
            assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
            assert await gate.run("u1:s1", engine_jobs.echo, value="ok") == "ok"
        finally:
            await gate.shutdown()

    async def test_worker_pids_are_visible(self, gate: EngineGate) -> None:
        assert gate._worker_pids(gate._pool)  # pylint: disable=protected-access


class TestCalcToolsThroughGate:
    """Design D3: prechecks before the gate, the thread is the lock key, gate failures reach the model as codes."""

    @staticmethod
    async def envelope(call: object, thread_id: str = "alice:s7") -> dict:
        agent = create_agent(
            ScriptedChatModel(steps=[call, AIMessage("ок")]),
            default_catalog().base_tools(),
            context_schema=FakeContext,
        )
        out = await agent.ainvoke(
            {"messages": [HumanMessage("ход")]}, context=FakeContext(thread_id=thread_id, model_index=INDEX)
        )
        return json.loads(next(m for m in out["messages"] if isinstance(m, ToolMessage)).content)

    @staticmethod
    def scenarios_call(**overrides: object) -> object:
        args = {
            "inputs": [{"id": "IN1", "min": 300, "max": 500, "step": 100}],
            "outputs": ["OUT0"],
            "year": 2026,
        } | overrides
        return tool_call("run_scenarios", **args)

    async def test_lock_key_is_thread(self, inline_gate: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            calc_tools,
            "analyze_excel_model",
            lambda **_kw: ExcelAnalysisToolResult(status="OK", result="", content={}),
        )
        await self.envelope(self.scenarios_call(), thread_id="alice:s7")
        assert inline_gate.sessions == ["alice:s7"]

    async def test_precheck_failure_never_reaches_gate(self, inline_gate: object) -> None:
        env = await self.envelope(self.scenarios_call(year=None))
        assert env["code"] == "missing_input"
        assert inline_gate.sessions == []

    @pytest.mark.parametrize("code", [ErrorCode.ENGINE_TIMEOUT, ErrorCode.ENGINE_UNAVAILABLE])
    async def test_gate_failures_become_envelopes(self, inline_gate: object, code: ErrorCode) -> None:
        inline_gate.error = ToolError(code)
        env = await self.envelope(self.scenarios_call())
        assert env["code"] == code.value
        assert env["hint"]
