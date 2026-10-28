"""G1–G4: GigaChat in streaming mode with functions, then a full v2 turn on GigaChat through ``astream``.

Live GigaChat with the service's connection (``.env``); the engine and ``resolve_terms`` are fakes.
"""

import time
from typing import Any

from common import QUESTION, SpikeGate, build_agent, install_fakes
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_gigachat import GigaChat

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.middleware import telemetry
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.config import APP_CONFIG

SYSTEM = "Ты агент. Прежде чем отвечать, всегда составь план вызовом инструмента write_todos (два шага)."


def gigachat(streaming: bool | None = None) -> GigaChat:
    """``streaming=None`` — as the service builds it today: the class sets ``streaming=False`` explicitly."""
    extra = {} if streaming is None else {"streaming": streaming}
    return GigaChat(**APP_CONFIG.gigachat.base_params, model=APP_CONFIG.gigachat.model, **extra)


def tools() -> list[Any]:
    return default_catalog().base_tools()


async def _stream_call(model: Any, messages: list[Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    chunks: list[AIMessageChunk] = []
    first: float | None = None
    async for chunk in model.astream(messages):
        if first is None:
            first = time.monotonic() - t0
        chunks.append(chunk)
    merged = chunks[0]
    for c in chunks[1:]:
        merged = merged + c
    return {
        "merged": merged,
        "chunks": len(chunks),
        "first_chunk_s": round(first or 0, 3),
        "total_s": round(time.monotonic() - t0, 3),
        "usage_per_chunk": [bool(c.usage_metadata) for c in chunks],
        "fc_per_chunk": [bool(c.additional_kwargs.get("function_call")) for c in chunks],
    }


def _view(m: AIMessageChunk | AIMessage) -> dict[str, Any]:
    return {
        "content": m.content[:120] if isinstance(m.content, str) else str(m.content)[:120],
        "tool_calls": m.tool_calls,
        "invalid_tool_calls": getattr(m, "invalid_tool_calls", None),
        "additional_kwargs_keys": sorted(m.additional_kwargs),
        "functions_state_id": m.additional_kwargs.get("functions_state_id"),
        "usage_metadata": m.usage_metadata,
    }


FIRST: dict[str, Any] = {}


async def g1() -> dict[str, Any]:
    """Streaming with bound functions: tool_calls survive the merge; the non-streaming call for comparison."""
    model = gigachat().bind_tools(tools())
    messages = [SystemMessage(SYSTEM), HumanMessage(QUESTION)]
    streamed = await _stream_call(model, messages)
    plain = await model.ainvoke(messages)
    FIRST["messages"], FIRST["ai"] = messages, streamed["merged"]
    return {
        "check": "G1",
        "streamed": {k: v for k, v in streamed.items() if k != "merged"} | {"message": _view(streamed["merged"])},
        "ainvoke": _view(plain),
        "ok": bool(streamed["merged"].tool_calls) and not streamed["merged"].invalid_tool_calls,
    }


async def g2() -> dict[str, Any]:
    """functions_state_id of the merged chunk goes into the next request and the model continues."""
    if "ai" not in FIRST:
        await g1()
    ai = FIRST["ai"]
    as_message = AIMessage(
        content=ai.content, tool_calls=ai.tool_calls, additional_kwargs=ai.additional_kwargs, id=ai.id
    )
    call = ai.tool_calls[0] if ai.tool_calls else {"id": "x", "name": "write_todos"}
    history = [
        *FIRST["messages"],
        as_message,
        ToolMessage("План записан.", tool_call_id=call["id"], name=call["name"]),
    ]
    model = gigachat()
    payload = model._build_payload(history, functions=None)  # pylint: disable=protected-access
    sent = [m.model_dump(exclude_none=True) for m in payload.messages]
    assistant = next((m for m in sent if m.get("role") == "assistant"), {})
    second = await _stream_call(model.bind_tools(tools()), history)
    return {
        "check": "G2",
        "state_id_in_merged": ai.additional_kwargs.get("functions_state_id"),
        "assistant_in_payload": {k: v for k, v in assistant.items() if k != "content"},
        "second": {k: v for k, v in second.items() if k != "merged"} | {"message": _view(second["merged"])},
        "ok": bool(ai.additional_kwargs.get("functions_state_id"))
        and assistant.get("functions_state_id") == ai.additional_kwargs.get("functions_state_id"),
    }


async def g3() -> dict[str, Any]:
    """usage_metadata in streaming: text answer and a function call."""
    model = gigachat()
    text = await _stream_call(model, [HumanMessage("Назови три цвета радуги.")])
    fn = await _stream_call(model.bind_tools(tools()), [SystemMessage(SYSTEM), HumanMessage(QUESTION)])
    return {
        "check": "G3",
        "text": {
            "chunks": text["chunks"],
            "usage_per_chunk": text["usage_per_chunk"],
            "usage": text["merged"].usage_metadata,
        },
        "function": {
            "chunks": fn["chunks"],
            "usage_per_chunk": fn["usage_per_chunk"],
            "usage": fn["merged"].usage_metadata,
        },
        "ok": bool(text["merged"].usage_metadata) and bool(fn["merged"].usage_metadata),
    }


async def _turn(n: int, streaming: bool | None = None) -> dict[str, Any]:  # noqa: C901  # one bench run, kept flat
    install_fakes(SpikeGate(delay=1.0))
    agent = await build_agent(gigachat(streaming))
    lines: list[str] = []
    orig = telemetry.logger.info

    def capture(message: str, *args: Any, **kw: Any) -> None:
        if message.startswith("LLM CALL"):
            lines.append(str(args[0])[:300])
        orig(message, *args, **kw)

    telemetry.logger.info = capture
    t0 = time.monotonic()
    calls: list[str] = []
    model_chunks: list[dict[str, Any]] = []  # AIMessageChunk of node "model" in order, with time
    final_update: str | None = None
    custom: list[Any] = []
    try:
        async for part in agent.astream(
            {"messages": [("user", QUESTION)]},
            context=RequestContext(user_id="u1", session_id=f"g4-{n}", trace_id=f"g4-{n}"),
            stream_mode=["updates", "custom", "messages"],
            version="v2",
        ):
            at = round(time.monotonic() - t0, 3)
            kind, data = part["type"], part["data"]
            if kind == "messages":
                msg, meta = data
                if meta.get("langgraph_node") == "model" and isinstance(msg, AIMessageChunk):
                    model_chunks.append(
                        {
                            "t": at,
                            "id": msg.id,
                            "step": meta.get("langgraph_step"),
                            "text": msg.content,
                            "tool": bool(msg.tool_call_chunks or msg.tool_calls),
                        }
                    )
            elif kind == "updates":
                for node, delta in (data or {}).items():
                    for m in (delta or {}).get("messages", []) if isinstance(delta, dict) else []:
                        if node == "model" and isinstance(m, AIMessage):
                            if m.tool_calls:
                                calls.extend(c["name"] for c in m.tool_calls)
                            else:
                                final_update = m.content
            else:
                custom.append(data)
    except Exception as exc:  # pylint: disable=broad-exception-caught  # a failed live run is a result
        return {"run": n, "error": f"{type(exc).__name__}: {str(exc)[:300]}", "calls": calls, "llm_call_lines": lines}
    finally:
        telemetry.logger.info = orig
    # the answer's chunks: those of the last model call (one graph step per call; GigaChat ids differ per chunk)
    last_step = model_chunks[-1]["step"] if model_chunks else None
    answer = [c for c in model_chunks if c["step"] == last_step and c["text"]]
    per_call: dict[Any, int] = {}
    ids: dict[Any, set] = {}
    for c in model_chunks:
        per_call[c["step"]] = per_call.get(c["step"], 0) + 1
        ids.setdefault(c["step"], set()).add(c["id"])
    streamed_text = "".join(c["text"] for c in answer)
    return {
        "run": n,
        "seconds": round(time.monotonic() - t0, 2),
        "calls": calls,
        "model_chunks": len(model_chunks),
        "chunks_per_call": list(per_call.values()),
        "text_calls": sum(1 for i in per_call if any(c["text"] for c in model_chunks if c["step"] == i)),
        "ids_per_call": [len(v) for v in ids.values()],
        "text_call_windows": [
            [min(c["t"] for c in model_chunks if c["step"] == i), max(c["t"] for c in model_chunks if c["step"] == i)]
            for i in per_call
            if any(c["text"] for c in model_chunks if c["step"] == i)
        ],
        "answer_chunks": len(answer),
        "answer_first_s": answer[0]["t"] if answer else None,
        "answer_last_s": answer[-1]["t"] if answer else None,
        "streamed_equals_final": streamed_text.strip() == (final_update or "").strip(),
        "final": (final_update or "")[:300],
        "llm_call_lines": lines,
    }


async def g4() -> dict[str, Any]:
    model = gigachat()
    as_built = {"streaming_in_fields_set": "streaming" in model.model_fields_set, "streaming": model.streaming}
    runs = [await _turn(n, streaming=True) for n in range(1, 4)]
    return {"check": "G4", "as_built": as_built, "runs": runs}


CHECKS = {"G1": g1, "G2": g2, "G3": g3, "G4": g4}
