"""H1–H4 prototype server: the service's ``log_requests`` (as is, or with a ``text/event-stream`` branch) + SSE.

python scripts/spikes/s6_1_stream/http_proto.py --port 8931 --log current|patched

Endpoints (POST with the service's x-* headers, so ``log_requests`` takes its logging branch):
- ``/ticks?n=5&interval=1`` — n frames, one per interval;
- ``/agent?mode=task|generator&delay=3&heartbeat=15`` — a scripted v2 turn (engine call sleeps ``delay``);
- ``GET /state`` — what the server saw (no x-trace-id: not logged, not buffered).
"""

import argparse
import asyncio
import contextlib
import json
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT), str(ROOT / "src")]

import uvicorn
from common import SpikeGate, build_agent, install_fakes, u2_steps
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from tests.unit.v2.fakes import ScriptedChatModel

from aigw_service.api import middleware as service_middleware
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.turn.disconnect import disconnect_watch
from aigw_service.api.v2.turn.lock import TurnLocks
from aigw_service.context import APP_CTX

STATE: dict[str, Any] = {"requests": {}, "log": []}
LOCKS = TurnLocks()
T0 = time.monotonic()


def now() -> float:
    return round(time.monotonic() - T0, 3)


# --- what log_requests writes: a recording proxy over the service logger ------------------------------------


class RecordingLogger:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if name not in ("info", "metric", "audit", "warning", "debug"):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            first = args[0] if args else kwargs.get("metric_name") or kwargs.get("event_name")
            entry = {"t": now(), "kind": name, "what": str(first)[:90]}
            if name == "metric":
                entry["value"] = kwargs.get("metric_value")
            if kwargs.get("message_type") == "response":
                entry["body"] = str(kwargs.get("args", {}).get("message"))[:60]
            STATE["log"].append(entry)
            return target(*args, **kwargs)

        return call


_service_logger = APP_CTX.get_logger()
APP_CTX.get_logger = lambda: RecordingLogger(_service_logger)  # type: ignore[method-assign]


# --- the patched log_requests: a text/event-stream branch (sketch for S6.3) -----------------------------------


async def log_requests_patched(request: Request, call_next: Any) -> Any:
    """Everything as in the service, except a ``text/event-stream`` response: its body is passed through as it
    comes, and the response record, audit, metrics and duration are written after the last frame."""
    if not request.headers.get("x-trace-id"):
        return await service_middleware.log_requests(request, call_next)
    holder: dict[str, Any] = {}

    async def call_next_peek(req: Request) -> Any:
        response = await call_next(req)
        holder["response"] = response
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            original = response.body_iterator

            async def empty() -> AsyncIterator[bytes]:
                return
                yield b""  # pragma: no cover

            holder["stream"] = original
            response.body_iterator = empty()  # log_requests collects nothing
        return response

    started = time.time()
    response = await service_middleware.log_requests(request, call_next_peek)
    if "stream" not in holder:
        return response
    # log_requests above has already written the request records and — wrongly for a stream — the response ones;
    # in the real patch the stream branch skips them there and writes them here, after the stream
    logger = APP_CTX.get_logger()
    original = holder["stream"]

    async def passthrough() -> AsyncIterator[bytes]:
        sent = 0
        outcome = "completed"
        try:
            async for chunk in original:
                sent += 1
                yield chunk
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            ms = int((time.time() - started) * 1000)
            logger.info("Outgoing response to client system (stream)", frames=sent, outcome=outcome)
            logger.metric(metric_name="aigw_service_responses_total", metric_value=1)
            logger.audit(event_name="BusinessRequestFinished", event_params=json.dumps({"frames": sent}))
            logger.metric(metric_name="aigw_service_process_duration_ms", metric_value=ms)

    response.body_iterator = passthrough()
    return response


# --- endpoints ------------------------------------------------------------------------------------------------

app = FastAPI()
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def frame(seq: int, event: str, data: Any) -> bytes:
    body = json.dumps({"v": 1, "seq": seq, **data}, ensure_ascii=False, default=str)
    return f"event: {event}\nid: {seq}\ndata: {body}\n\n".encode()


@app.get("/state")
async def state() -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(STATE, default=str)))


@app.post("/reset")
async def reset() -> JSONResponse:
    STATE["requests"].clear()
    STATE["log"].clear()
    return JSONResponse({"ok": True})


@app.post("/ticks")
async def ticks(request: Request, n: int = 5, interval: float = 1.0) -> StreamingResponse:
    rid = request.headers.get("x-trace-id", "?")
    rec = STATE["requests"].setdefault(rid, {"events": []})

    async def gen() -> AsyncIterator[bytes]:
        try:
            for i in range(1, n + 1):
                rec["events"].append((now(), f"send {i}"))
                yield frame(i, "tick", {"i": i})
                await asyncio.sleep(interval)
        except BaseException as exc:
            rec["events"].append((now(), f"gen {type(exc).__name__}"))
            raise
        finally:
            rec["events"].append((now(), "gen finally"))

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/agent")
async def agent_sse(  # noqa: C901  # one prototype endpoint, three variants
    request: Request, mode: str = "task", delay: float = 3.0, heartbeat: float = 15.0, watch: int = 0
) -> Any:
    rid = request.headers.get("x-trace-id", "?")
    rec: dict[str, Any] = STATE["requests"].setdefault(rid, {"events": []})
    ev = rec["events"]
    gate = SpikeGate(delay=delay)
    install_fakes(gate)
    rec["gate"] = gate.__dict__
    agent = await build_agent(ScriptedChatModel(steps=u2_steps()))
    context = RequestContext(user_id="u1", session_id=rid, trace_id=rid)
    inp = {"messages": [("user", "EBITDA 2026?")]}
    modes = ["updates", "custom"]

    def to_frame(seq: int, part: Any) -> bytes:
        nodes = list(part["data"]) if part["type"] == "updates" and isinstance(part["data"], dict) else []
        return frame(seq, part["type"], {"nodes": nodes})

    if mode == "generator":  # rejected variant H3(a): the turn runs inside the response generator

        async def gen_inline() -> AsyncIterator[bytes]:
            seq = 0
            try:
                with LOCKS.hold(context.thread_id):
                    async for part in agent.astream(inp, context=context, stream_mode=modes, version="v2"):
                        seq += 1
                        yield to_frame(seq, part)
                    ev.append((now(), "turn end"))
                    yield frame(seq + 1, "answer", {})
            except BaseException as exc:
                ev.append((now(), f"gen {type(exc).__name__}"))
                raise
            finally:
                ev.append((now(), "gen finally; lock released"))

        return StreamingResponse(gen_inline(), media_type="text/event-stream", headers=SSE_HEADERS)

    # variant H3(b): the turn is its own task; the generator only reads its queue
    queue: asyncio.Queue = asyncio.Queue()

    async def watched() -> None:
        if not watch:
            return await turn()
        async with disconnect_watch(request, context.cancel):  # S2.8: the receive channel sees http.disconnect
            await turn()
            return None

    async def turn() -> None:
        try:
            with LOCKS.hold(context.thread_id):
                async for part in agent.astream(inp, context=context, stream_mode=modes, version="v2"):
                    queue.put_nowait(part)
                ev.append((now(), f"turn end (cancel flag={context.cancel.is_set()})"))
        except BaseException as exc:
            ev.append((now(), f"turn {type(exc).__name__}: {str(exc)[:80]}"))
            raise
        finally:
            ev.append((now(), "lock released"))
            queue.put_nowait(None)

    task = asyncio.create_task(watched())
    rec["task"] = task

    async def gen_task() -> AsyncIterator[bytes]:
        seq = 0
        try:
            while True:
                try:
                    part = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except TimeoutError:
                    ev.append((now(), "ping"))
                    yield b": ping\n\n"
                    continue
                if part is None:
                    seq += 1
                    yield frame(seq, "answer", {})
                    return
                seq += 1
                yield to_frame(seq, part)
        except BaseException as exc:
            ev.append((now(), f"gen {type(exc).__name__}"))
            raise
        finally:
            if not task.done():  # AR-Q17: a gone client only sets the flag; Cancellation stops after the call
                context.cancel.set()
                ev.append((now(), "gen finally: cancel flag set"))
            else:
                ev.append((now(), "gen finally"))

    return StreamingResponse(gen_task(), media_type="text/event-stream", headers=SSE_HEADERS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument("--log", choices=["current", "patched", "none"], default="current")
    args = parser.parse_args()
    if args.log == "current":
        app.middleware("http")(service_middleware.log_requests)
    elif args.log == "patched":
        app.middleware("http")(log_requests_patched)
    STATE["log_variant"] = args.log
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
