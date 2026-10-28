"""H1–H4: SSE on a live uvicorn behind the service's ``log_requests``; the client stamps the arrival of every line."""

import asyncio
import contextlib
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def headers() -> dict[str, str]:
    return {
        "x-trace-id": str(uuid.uuid4()),
        "x-client-id": "AB12345678",
        "x-request-time": datetime.now(UTC).isoformat(),
        "x-session-id": str(uuid.uuid4()),
        "x-user-id": "u1",
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def server(log: str) -> Any:
    port = free_port()
    proc = subprocess.Popen(  # noqa: ASYNC220  # starts our own prototype once per check; Popen does not wait
        [sys.executable, str(HERE / "http_proto.py"), "--port", str(port), "--log", log],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=60) as client:
            for _ in range(100):
                with contextlib.suppress(httpx.TransportError):
                    if (await client.get("/state")).status_code == 200:
                        break
                await asyncio.sleep(0.2)
            yield client
    finally:
        proc.terminate()
        proc.wait(10)


async def read_sse(client: httpx.AsyncClient, url: str, hdrs: dict[str, str], stop_after: float | None = None) -> dict:
    """Arrival time of every non-empty line; ``stop_after`` drops the connection by a timer (a gone client)."""
    t0 = time.monotonic()
    lines: list[tuple[float, str]] = []
    resp_headers: dict[str, str] = {}

    async def consume() -> None:
        async with client.stream("POST", url, headers=hdrs, json={}) as resp:
            resp_headers.update(
                {k: v for k, v in resp.headers.items() if k.startswith(("content-type", "cache", "x-"))}
            )
            lines.extend([(round(time.monotonic() - t0, 3), line[:80]) async for line in resp.aiter_lines() if line])

    reader = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(reader, timeout=stop_after)
    except TimeoutError:
        lines.append((round(time.monotonic() - t0, 3), "client dropped the connection"))
    except httpx.HTTPError as exc:
        lines.append((round(time.monotonic() - t0, 3), f"client error {type(exc).__name__}"))
    frames = [t for t, line in lines if line.startswith("event:")]
    return {
        "status": resp_headers,
        "first_frame_s": frames[0] if frames else None,
        "last_frame_s": frames[-1] if frames else None,
        "distinct_arrivals": len({round(t, 1) for t in frames}),
        "frames": len(frames),
        "pings": sum(1 for _, line in lines if line.startswith(": ping")),
        "lines": lines[:60],
        "seconds": round(time.monotonic() - t0, 3),
    }


async def server_state(client: httpx.AsyncClient, trace_id: str) -> dict:
    state = (await client.get("/state")).json()
    return {"request": state["requests"].get(trace_id), "log": state["log"]}


async def _ticks(log: str) -> dict:
    async with server(log) as client:
        h = headers()
        got = await read_sse(client, "/ticks?n=5&interval=1", h)
        return {"client": got, "server": await server_state(client, h["x-trace-id"])}


async def h1() -> dict:
    """The service's log_requests as is."""
    return {"check": "H1", **await _ticks("current")}


async def h2() -> dict:
    """The text/event-stream branch prototype; plus a non-stream request through it, to see nothing else changed."""
    out = {"check": "H2", **await _ticks("patched")}
    async with server("patched") as client:
        h = headers()
        resp = await client.get("/state", headers=h)  # a JSON response through the logging branch
        out["json_response_status"] = resp.status_code
        out["json_response_log"] = (await client.get("/state")).json()["log"]
    return out


async def _agent(
    log: str, mode: str, stop_after: float | None, delay: float = 3.0, heartbeat: float = 15.0, watch: int = 0
) -> dict:
    async with server(log) as client:
        h = headers()
        got = await read_sse(
            client, f"/agent?mode={mode}&delay={delay}&heartbeat={heartbeat}&watch={watch}", h, stop_after=stop_after
        )
        await asyncio.sleep(delay + 2)  # let the turn finish (or not) after the client is gone
        return {"client": got, "server": await server_state(client, h["x-trace-id"])}


async def h3() -> dict:
    """Disconnect mid-calculation: (a) turn inside the generator, (b) turn in its own task; both behind both logs."""
    out: dict[str, Any] = {"check": "H3"}
    for log in ("patched", "current"):
        for mode in ("generator", "task"):
            out[f"{mode}_{log}"] = await _agent(log, mode, stop_after=0.5, delay=3.0, heartbeat=1.0)
    out["task_patched_watch"] = await _agent("patched", "task", stop_after=0.5, delay=3.0, heartbeat=15.0, watch=1)
    out["task_patched_no_disconnect"] = await _agent("patched", "task", stop_after=None, delay=1.0, heartbeat=15.0)
    return out


async def h4() -> dict:
    """Heartbeat: a 6 s calculation with a 2 s heartbeat (15 s in the service), then frames after the pings."""
    return {"check": "H4", **await _agent("patched", "task", stop_after=None, delay=6.0, heartbeat=2.0)}


CHECKS = {"H1": h1, "H2": h2, "H3": h3, "H4": h4}
