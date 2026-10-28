"""U10 (spec §14; S6.5 design D8): U2 through ``/invoke-agent/stream``, then the same with a disconnect mid-grid.

Scripted model, no network, no LibreOffice: the calculation goes through ``TimedGate``, and a ``sleep`` child
process stands in for ``soffice``. Pass 1 goes through ASGI, pass 2 through a live uvicorn (the disconnect must be
a real one).
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from tests.unit.v2.stream_stands import StreamRecorder
from tests.unit.v2.test_api import answer_text
from tests.unit.v2.test_stream_api import (
    SESSION,
    STREAM,
    StreamApi,
    TimedGate,
    alive,
    assert_stream_contract,
    counts,
    fresh,
    headers_for,
    parse_sse,
    wait_free,
)
from tests.unit.v2.test_stream_turn import ANSWER_U2, QUESTION, u2_steps

GRID_CALL = 2

from aigw_service.api.v2.stream.events import StreamEvent
from aigw_service.api.v2.stream.hygiene import violations
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.turn.lock import TurnLocks


def clean(events: list[StreamEvent]) -> None:
    assert [v for e in events for v in violations(e)] == []


async def read_until_grid(base: str, gate: TimedGate) -> list[StreamEvent]:
    """Events up to ``tool.started`` of the grid call (``run_scenarios``, call 2 of U2); then the client leaves
    while the calculation and its child process run."""
    got: list[StreamEvent] = []
    buffer = ""
    async with (
        httpx.AsyncClient(base_url=base, timeout=30) as client,
        client.stream("POST", STREAM, json={"message": QUESTION}, headers=headers_for()) as response,
    ):
        assert response.status_code == 200
        async for text in response.aiter_text():
            buffer += text
            *frames, buffer = buffer.split("\n\n")
            for frame in frames:
                if frame == ": ping":
                    continue
                got += parse_sse(frame + "\n\n")[0]
                if got[-1].event == "tool.started" and got[-1].call == GRID_CALL:  # type: ignore[attr-defined]
                    while not gate.pids:
                        await asyncio.sleep(0.01)
                    return got
    return got


class TestU10:
    async def test_pass_1_stream_equals_zip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, domain: list, recorder: StreamRecorder
    ) -> None:
        agent, _ = await fresh(u2_steps, tmp_path, "stream")
        response = await StreamApi(monkeypatch, agent).stream()
        assert response.status_code == 200
        events, _ = parse_sse(response.text)
        assert_stream_contract(events)
        clean(events)
        kinds = [e.event for e in events]
        assert "plan" in kinds
        finished = [e for e in events if e.event == "tool.finished"]
        assert finished
        assert all(e.outcome == "ok" and e.summary for e in finished)  # type: ignore[attr-defined]
        assert kinds.count("tool.started") == len(finished)
        assert kinds[-2:] == ["summary", "answer"]
        assert events[-1].outcome == "full"  # type: ignore[attr-defined]

        agent, _ = await fresh(u2_steps, tmp_path, "zip")
        zipped = await StreamApi(monkeypatch, agent).ask(QUESTION, session=SESSION)
        assert events[-1].text == answer_text(zipped) == ANSWER_U2  # type: ignore[attr-defined]

        (line,) = recorder.streams  # the ZIP request writes no STREAM line
        assert (line["outcome"], line["disconnected"], line["engine"]) == ("full", False, "v2")
        assert line["events"] == counts(events)

    async def test_pass_2_disconnect_mid_grid(  # pylint: disable=too-many-locals
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        domain: list,
        live: str,
        recorder: StreamRecorder,
    ) -> None:
        started = time.monotonic()
        gate = TimedGate(0.5, child_s=1.0)
        monkeypatch.setattr(calc_tools, "_gate", lambda: gate)
        locks = TurnLocks()
        agent, model = await fresh(u2_steps, tmp_path, "a", locks=locks, checkpointer=InMemorySaver())
        StreamApi(monkeypatch, agent)
        before = await read_until_grid(live, gate)
        assert gate.pids  # the client leaves while the grid calculation and its child process run
        calls_at_disconnect = len(model.calls)
        clean(before)
        assert before[-1].event == "tool.started"

        async with httpx.AsyncClient(base_url=live, timeout=30) as client:
            busy = await client.post(STREAM, json={"message": "Ещё вопрос"}, headers=headers_for())
            assert busy.status_code == 409  # the turn is finishing
            await wait_free(locks)
            assert len(gate.finished) == 1  # the grid calculation was not cut
            assert not alive(gate.pids[0])
            assert len(model.calls) == calls_at_disconnect  # no model call after the disconnect
            nxt = await client.post(STREAM, json={"message": "Что дальше?"}, headers=headers_for())
        events, _ = parse_sse(nxt.text)
        assert events[-1].event == "answer"

        first, second = recorder.streams
        assert (first["outcome"], first["disconnected"]) == (None, True)
        # frames already handed to the socket count too: at least what the client read, never a terminal one
        assert all(first["events"].get(kind, 0) >= n for kind, n in counts(before).items())
        assert "answer" not in first["events"]
        assert second["disconnected"] is False
        assert "Ещё вопрос" not in json.dumps(recorder.streams, ensure_ascii=False)
        assert time.monotonic() - started < 15
