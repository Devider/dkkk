"""G5: chunk timing of a long text answer straight from GigaChat — LangChain wrapper and the raw SDK."""

import time
from typing import Any

from gigachat_checks import gigachat
from langchain_core.messages import HumanMessage

from aigw_service.config import APP_CONFIG

PROMPT = "Напиши связный текст из 250 слов о том, как цена сырья влияет на EBITDA производителя метанола."
LONG = "Напиши подробный текст из 900 слов о влиянии цены метанола на денежный поток проекта."


async def g5() -> dict[str, Any]:
    out: dict[str, Any] = {"check": "G5", "model": APP_CONFIG.gigachat.model}
    t0 = time.monotonic()
    stream = gigachat(streaming=True).astream([HumanMessage(PROMPT)])
    times = [(round(time.monotonic() - t0, 3), len(chunk.content)) async for chunk in stream]
    out["langchain"] = {
        "chunks": len(times),
        "first": times[0],
        "last": times[-1],
        "sample": times[:: max(1, len(times) // 8)],
    }
    client = gigachat()._client  # pylint: disable=protected-access
    from gigachat.models import Chat, Messages, MessagesRole  # pylint: disable=import-outside-toplevel

    chat = Chat(messages=[Messages(role=MessagesRole.USER, content=PROMPT)], model=APP_CONFIG.gigachat.model)
    t0 = time.monotonic()
    raw = [
        (round(time.monotonic() - t0, 3), len(chunk.choices[0].delta.content or "") if chunk.choices else 0)
        async for chunk in client.astream(chat)
    ]
    out["sdk"] = {"chunks": len(raw), "first": raw[0], "last": raw[-1], "sample": raw[:: max(1, len(raw) // 8)]}
    for upd in (0.1,):
        chat2 = Chat(
            messages=[Messages(role=MessagesRole.USER, content=PROMPT)],
            model=APP_CONFIG.gigachat.model,
            update_interval=upd,
        )
        t0 = time.monotonic()
        raw2 = [round(time.monotonic() - t0, 3) async for _ in client.astream(chat2)]
        out[f"sdk_update_interval_{upd}"] = {"chunks": len(raw2), "first": raw2[0], "last": raw2[-1]}
    for name in (APP_CONFIG.gigachat.model, "GigaChat-2"):  # a long answer: in how many bursts it arrives
        model = gigachat(streaming=True)
        model.model = name
        t0 = time.monotonic()
        stamps = [round(time.monotonic() - t0, 2) async for _ in model.astream([HumanMessage(LONG)])]
        out[f"long_{name}"] = {"chunks": len(stamps), "bursts_at_s": sorted(set(stamps)), "total_s": stamps[-1]}
    return out


CHECKS = {"G5": g5}
