"""PreviewModelCheck (ADR-10, spec §8 №6, S4.4): background A/B check of the agent's model on the preview model.

After a successful model call a share ``AGENT_PREVIEW_CHECK_PERCENT`` of calls is copied into a bounded queue and
the main answer goes on unchanged: the turn never waits. One worker task repeats the exact request on the preview
model and compares the answers — tool calls by names and arguments, texts by an LLM judge — and writes one
``PREVIEW CHECK`` line. Placed right before ``ContextMeter`` (design D1): only there the request is the one the model
really gets, and inside ``ModelRetryMiddleware`` a retried call gives one check. Only the async path is checked.
"""

import asyncio
import contextvars
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cache
from random import random
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from aigw_service.api.v2.middleware.context_meter import call_number
from aigw_service.api.v2.middleware.stop_event import is_stop_event
from aigw_service.api.v2.middleware.telemetry import _answer as answer_message
from aigw_service.api.v2.prompts.middleware import PREVIEW_JUDGE_PROMPT
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

PREVIEW_QUEUE_SIZE = 50
"""Checks waiting for the worker; a new one over it is dropped (design D2)."""
TEXT_LIMIT = 1000
"""Characters of each answer's text in the ``PREVIEW CHECK`` line (design D6)."""
JUDGE_LIMIT = 500
"""Characters of the judge's analysis in the line."""
NO_CALLBACKS: dict[str, Any] = {"callbacks": []}
"""Preview and judge calls stay out of the turn's trace and ``LLM CALL`` lines (design D3)."""

QUEUE_FULL = "queue_full"
PREVIEW_FAILED = "preview_failed"
STOP_EVENT = "stop_event"
JUDGE_FAILED = "judge_failed"
JUDGE_UNPARSED = "judge_unparsed"

_SIMILARITY = re.compile(r"схожести\s*:?\s*(\d+(?:[.,]\d+)?)\s*%", re.IGNORECASE)


@dataclass(frozen=True)
class PreviewJob:  # pylint: disable=too-many-instance-attributes  # one field per part of the request
    """What the worker needs from one model call: the request as the model got it and the main answer."""

    system_message: SystemMessage | None
    messages: tuple[BaseMessage, ...]
    tools: tuple[Any, ...]
    tool_choice: Any
    model_settings: dict[str, Any]
    main: AIMessage
    trace_id: str = ""
    turn_id: str | None = None
    call: int = 0


@dataclass
class Outcome:
    """Result of one check; ``error`` is a code, ``exception`` the exception's type name — never its text."""

    preview: AIMessage | None = None
    similarity: int | None = None
    judge: str = ""
    error: str | None = None
    exception: str | None = None
    elapsed_s: float = 0.0


def snapshot(request: ModelRequest, main: AIMessage) -> PreviewJob:
    """Copies of the request's parts: ``state`` and ``runtime`` outlive the turn and change (design D3)."""
    state = request.state or {}
    context = getattr(request.runtime, "context", None)
    return PreviewJob(
        system_message=request.system_message,
        messages=tuple(request.messages),
        tools=tuple(request.tools or ()),
        tool_choice=request.tool_choice,
        model_settings=dict(request.model_settings or {}),
        main=main,
        trace_id=getattr(context, "trace_id", "") or "",
        turn_id=state.get("turn_id"),
        call=call_number(state),
    )


def _calls(message: AIMessage | None) -> list[dict[str, Any]]:
    return [{"name": c["name"], "args": c.get("args") or {}} for c in (message.tool_calls if message else [])]


def _text(message: AIMessage | None) -> str:
    if message is None:
        return ""
    if isinstance(message.content, str):
        return message.content
    parts = (p if isinstance(p, str) else p.get("text", "") for p in message.content if isinstance(p, str | dict))
    return "".join(parts)


def compare_calls(main: AIMessage, preview: AIMessage) -> tuple[bool, bool]:
    """``(same_tools, same_args)``: ordered tool names, then (name, arguments) pairs (design D5)."""
    first, second = _calls(main), _calls(preview)
    same_tools = [c["name"] for c in first] == [c["name"] for c in second]

    def pairs(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
        return [(c["name"], json.dumps(c["args"], sort_keys=True, ensure_ascii=False)) for c in calls]

    return same_tools, same_tools and pairs(first) == pairs(second)


def parse_similarity(text: str) -> int | None:
    """«… | Итоговый процент схожести: 85%» → 85, clamped to 0–100; no number in that form → ``None``."""
    found = _SIMILARITY.search(text or "")
    if not found:
        return None
    return max(0, min(100, round(float(found.group(1).replace(",", ".")))))


def model_name(model: BaseChatModel) -> str:
    return str(getattr(model, "model", None) or getattr(model, "model_name", None) or type(model).__name__)


def _side(message: AIMessage | None) -> dict[str, Any] | None:
    if message is None:
        return None
    return {"tool_calls": _calls(message), "text": _text(message)[:TEXT_LIMIT]}


def preview_record(job: PreviewJob, outcome: Outcome, model: str, preview_model: str) -> str:
    """The JSON of one ``PREVIEW CHECK`` line (design D6)."""
    same_tools, same_args = compare_calls(job.main, outcome.preview) if outcome.preview else (None, None)
    record = {
        "trace_id": job.trace_id,
        "turn_id": job.turn_id,
        "call": job.call,
        "model": model,
        "preview_model": preview_model,
        "elapsed_s": outcome.elapsed_s,
        "main": _side(job.main),
        "preview": _side(outcome.preview),
        "same_tools": same_tools,
        "same_args": same_args,
        "similarity": outcome.similarity,
        "judge": outcome.judge[:JUDGE_LIMIT],
        "error": outcome.error,
        "exception": outcome.exception,
    }
    return json.dumps(record, ensure_ascii=False, default=str)


@dataclass
class _Worker:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[PreviewJob]
    task: asyncio.Task[None] = field(init=False)


class PreviewChecker:
    """Bounded queue and one worker task per event loop; a failed check never stops the worker (design D2)."""

    def __init__(
        self, preview: BaseChatModel, judge: BaseChatModel, model: str = "", queue_size: int = PREVIEW_QUEUE_SIZE
    ) -> None:
        self.preview = preview
        self.judge = judge
        self.model = model or model_name(judge)
        self.preview_model = model_name(preview)
        self.queue_size = queue_size
        self._worker: _Worker | None = None

    def _queue(self) -> asyncio.Queue[PreviewJob]:
        loop = asyncio.get_running_loop()
        worker = self._worker
        if worker is None or worker.loop is not loop or worker.task.done():
            worker = _Worker(loop, asyncio.Queue(maxsize=self.queue_size))
            # a clean context: the task must not inherit the run of the turn that created it (design D2)
            worker.task = loop.create_task(self._work(worker.queue), context=contextvars.Context())
            self._worker = worker
        return worker.queue

    def submit(self, job: PreviewJob) -> None:
        """Never waits: a full queue drops the check with a line."""
        try:
            self._queue().put_nowait(job)
        except asyncio.QueueFull:
            self._log(job, Outcome(error=QUEUE_FULL))

    async def join(self) -> None:
        """Waits until every queued check is done (tests and diagnostics)."""
        if self._worker is not None and self._worker.loop is asyncio.get_running_loop():
            await self._worker.queue.join()

    async def shutdown(self) -> None:
        """Cancels the worker and drops queued checks; a no-op before the first check."""
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.task.cancel()
        if worker.loop is asyncio.get_running_loop():
            try:
                await worker.task
            except asyncio.CancelledError:
                pass

    async def _work(self, queue: asyncio.Queue[PreviewJob]) -> None:
        while True:
            job = await queue.get()
            try:
                self._log(job, await self.check(job))
            except Exception as exc:  # pylint: disable=broad-exception-caught  # the worker must outlive any check
                logger.opt(exception=True).error("PREVIEW CHECK worker error: {}", type(exc).__name__)
            finally:
                queue.task_done()

    async def check(self, job: PreviewJob) -> Outcome:
        """Preview answer, then the judge only when both answers are text without tool calls (design D5)."""
        started = time.perf_counter()
        outcome = Outcome()
        try:
            outcome.preview = await self._ask_preview(job)
        except Exception as exc:  # pylint: disable=broad-exception-caught  # any failure is a code in the line
            outcome.error = STOP_EVENT if is_stop_event(exc) else PREVIEW_FAILED
            outcome.exception = type(exc).__name__
        if outcome.preview is not None and not job.main.tool_calls and not outcome.preview.tool_calls:
            await self._ask_judge(job, outcome)
        outcome.elapsed_s = round(time.perf_counter() - started, 3)
        return outcome

    async def _ask_preview(self, job: PreviewJob) -> AIMessage:
        """The binding ``create_agent`` makes, without callbacks and without retries (design D3)."""
        if job.tools:
            bound: Any = self.preview.bind_tools(list(job.tools), tool_choice=job.tool_choice, **job.model_settings)
        else:
            bound = self.preview.bind(**job.model_settings)
        messages = [job.system_message, *job.messages] if job.system_message else list(job.messages)
        answer = await bound.ainvoke(messages, config=NO_CALLBACKS)
        return answer if isinstance(answer, AIMessage) else AIMessage(str(getattr(answer, "content", answer)))

    async def _ask_judge(self, job: PreviewJob, outcome: Outcome) -> None:
        prompt = PREVIEW_JUDGE_PROMPT.format(main=_text(job.main), preview=_text(outcome.preview))
        try:
            verdict = await self.judge.ainvoke(prompt, config=NO_CALLBACKS)
        except Exception as exc:  # pylint: disable=broad-exception-caught  # a code in the line
            outcome.error, outcome.exception = JUDGE_FAILED, type(exc).__name__
            return
        outcome.judge = _text(verdict) if isinstance(verdict, AIMessage) else str(verdict)
        outcome.similarity = parse_similarity(outcome.judge)
        if outcome.similarity is None:
            outcome.error = JUDGE_UNPARSED

    def _log(self, job: PreviewJob, outcome: Outcome) -> None:
        logger.info("PREVIEW CHECK {}", preview_record(job, outcome, self.model, self.preview_model))


@cache
def default_preview_checker() -> PreviewChecker:
    """One checker per service process with the GigaChat preview model and judge (design D4)."""
    from aigw_service.api.v2.models import (  # pylint: disable=import-outside-toplevel  # GigaChat only when on
        build_judge_model,
        build_preview_model,
    )

    return PreviewChecker(build_preview_model(), build_judge_model())


async def shutdown_preview_checker() -> None:
    """``lifespan``: stops the worker of the service checker if one was ever built; no-op on v1 and when off."""
    if default_preview_checker.cache_info().currsize:
        await default_preview_checker().shutdown()


class PreviewModelCheck(AgentMiddleware):
    """Returns the main answer unchanged; a sampled successful call is queued for the worker."""

    def __init__(self, checker: PreviewChecker, percent: float, rng: Callable[[], float] = random) -> None:
        super().__init__()
        self.checker = checker
        self.percent = percent
        self.rng = rng

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        """Sync path: no background loop to run the worker in — no check (design D7)."""
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[Any]]) -> Any:
        response = await handler(request)  # an exception goes on without a check: a retry may still succeed
        if self.rng() < self.percent:
            try:
                if (main := answer_message(response)) is not None:
                    self.checker.submit(snapshot(request, main))
            except Exception as exc:  # pylint: disable=broad-exception-caught  # the check never touches the turn
                logger.opt(exception=True).warning("PREVIEW CHECK not queued: {}", type(exc).__name__)
        return response
