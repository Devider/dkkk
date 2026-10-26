"""S1.5 on the real engine (needs LibreOffice): timeout leaves no soffice, pool recovers, event loop stays free."""

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("uno", reason="LibreOffice (python3-uno) is required; run inside the Docker image")

from aigw_service.api.v2.tools.engine_gate import EngineGate
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from excel_engine import analyze_excel_model

WORKBOOK = str(Path(__file__).resolve().parents[3] / "models" / "Methanex_FinModel.xlsx")
EMA = {
    "file_name": WORKBOOK,
    "input_names": ["Метанол"],
    "output_names": ["EBITDA"],
    "output_years": [2026],
    "ranges": [[300, 500]],
    "steps": [100],
}


def soffice_pids() -> list[int]:
    pids = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                cmdline = (proc / "cmdline").read_bytes()
            except OSError:
                continue
            if b"soffice" in cmdline:
                pids.append(int(proc.name))
    return pids


async def test_timeout_leaves_no_soffice_and_pool_recovers() -> None:
    gate = EngineGate(workers=1, timeout=0.3)  # far shorter than spawn + open + recalc
    try:
        with pytest.raises(ToolError) as exc_info:
            await gate.run("u1:s1", analyze_excel_model, **EMA)
        assert exc_info.value.code == ErrorCode.ENGINE_TIMEOUT
        deadline = time.monotonic() + 5
        while soffice_pids() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert soffice_pids() == [], "soffice survived the timeout"

        gate.timeout = 120
        result = await gate.run("u1:s1", analyze_excel_model, **EMA)
        assert result.status == "OK"
    finally:
        await gate.shutdown()
    await asyncio.sleep(1)
    assert soffice_pids() == [], "soffice survived the gate shutdown"


async def test_event_loop_stays_responsive_during_recalculation() -> None:
    gate = EngineGate(workers=1, timeout=120)
    worst = 0.0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal worst
        while not done.is_set():
            before = time.monotonic()
            await asyncio.sleep(0.05)
            worst = max(worst, time.monotonic() - before - 0.05)

    try:
        tick = asyncio.create_task(ticker())
        result = await gate.run("u1:s1", analyze_excel_model, **EMA)
        done.set()
        await tick
    finally:
        await gate.shutdown()
    assert result.status == "OK"
    assert worst < 1.0, f"event loop blocked for {worst:.2f} s"
