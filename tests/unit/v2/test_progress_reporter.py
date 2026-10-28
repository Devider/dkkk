"""S6.4 (FR-22, spec §8 №17а, design D1–D3): ProgressReporter — phases and the engine-retry outcome."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.limits import LIMIT_HIT, TOOL_CALLS
from aigw_service.api.v2.middleware.progress import ProgressReporter, phase_of
from aigw_service.api.v2.middleware.resilience import engine_retry
from aigw_service.api.v2.scripted import ScriptedChatModel
from aigw_service.api.v2.stream.translator import PROGRESS_KEY
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError

ENGINE = "run_scenarios"


@dataclass
class Writer:
    parts: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, part: dict[str, Any]) -> None:
        self.parts.append(part)


def todos(*statuses: str) -> list[dict[str, Any]]:
    return [
        {"content": f"шаг {i}", "status": s, **({"reason": "r"} if s in ("skipped", "blocked") else {})}
        for i, s in enumerate(statuses)
    ]


def state(plan: list[dict[str, Any]] | None = None, tool_calls: int = 0, **counters: int) -> dict[str, Any]:
    return {"messages": [], "todos": plan or [], "guard_counters": {TOOL_CALLS: tool_calls, **counters}}


class TestPhase:
    @pytest.mark.parametrize(
        ("st", "phase"),
        [
            (state(todos("pending"), tool_calls=3, **{LIMIT_HIT: 1}), "answering"),  # 1: limit hit
            (state(todos("completed", "in_progress"), tool_calls=2), "executing"),  # 2: an open step
            (state(todos("pending")), "executing"),  # 2: a plan written, nothing else yet
            (state(todos("completed", "skipped", "blocked"), tool_calls=4), "answering"),  # 3: plan closed
            (state(), "planning"),  # 4: first call of the turn
            (state(todos("completed", "blocked")), "planning"),  # 4: a closed plan of the previous turn
            (state(tool_calls=2), "executing"),  # 5: tools without a plan (plan mode off)
            ({"messages": []}, "planning"),  # no counters at all
        ],
    )
    def test_rules(self, st: dict[str, Any], phase: str) -> None:
        assert phase_of(st) == phase

    async def test_writes_phase_and_no_update(self) -> None:
        writer = Writer()
        runtime = SimpleNamespace(stream_writer=writer)
        reporter = ProgressReporter([ENGINE])
        assert await reporter.abefore_model(state(), runtime) is None
        assert reporter.before_model(state(todos("in_progress"), tool_calls=1), runtime) is None
        assert writer.parts == [
            {PROGRESS_KEY: "phase", "phase": "planning"},
            {PROGRESS_KEY: "phase", "phase": "executing"},
        ]


def request(writer: Writer, name: str = ENGINE, call_id: str = "c4", thread: str = "u1:s1") -> ToolCallRequest:
    runtime = SimpleNamespace(stream_writer=writer, context=SimpleNamespace(thread_id=thread))
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": call_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=runtime,  # type: ignore[arg-type]
    )


class Failing:
    """A tool that raises the given errors one per attempt, then succeeds."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    def __call__(self, req: ToolCallRequest) -> ToolMessage:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return ToolMessage("ok", tool_call_id=req.tool_call["id"])

    async def acall(self, req: ToolCallRequest) -> ToolMessage:
        return self(req)


def retry_stack() -> tuple[ToolRetryMiddleware, ProgressReporter]:
    retry = engine_retry([ENGINE])
    retry.initial_delay = 0.0  # no pause between attempts in unit tests
    return retry, ProgressReporter([ENGINE])


def unavailable() -> ToolError:
    return ToolError(ErrorCode.ENGINE_UNAVAILABLE)


class TestEngineRetry:
    async def test_one_failure_then_ok(self) -> None:
        writer, (retry, reporter), tool = Writer(), retry_stack(), Failing(unavailable())

        async def inner(req: ToolCallRequest) -> Any:
            return await reporter.awrap_tool_call(req, tool.acall)

        result = await retry.awrap_tool_call(request(writer), inner)
        assert isinstance(result, ToolMessage)
        assert tool.calls == 2
        assert writer.parts == [{PROGRESS_KEY: "retry", "tool_call_id": "c4", "code": "engine_unavailable"}]
        assert reporter._attempts == {}  # pylint: disable=protected-access

    async def test_two_failures_one_retry_part(self) -> None:
        writer, (retry, reporter), tool = Writer(), retry_stack(), Failing(unavailable(), unavailable())

        async def inner(req: ToolCallRequest) -> Any:
            return await reporter.awrap_tool_call(req, tool.acall)

        with pytest.raises(ToolError):  # on_failure="error": ToolErrorMiddleware makes the envelope
            await retry.awrap_tool_call(request(writer), inner)
        assert len(writer.parts) == 1
        assert reporter._attempts == {}  # pylint: disable=protected-access

    def test_sync_path(self) -> None:
        writer, reporter, tool = Writer(), ProgressReporter([ENGINE]), Failing(unavailable())
        with pytest.raises(ToolError):
            reporter.wrap_tool_call(request(writer), tool)
        assert reporter.wrap_tool_call(request(writer), tool).content == "ok"
        assert len(writer.parts) == 1
        assert reporter._attempts == {}  # pylint: disable=protected-access

    async def test_not_retried_error(self) -> None:
        writer, (retry, reporter), tool = Writer(), retry_stack(), Failing(ToolError(ErrorCode.AMBIGUOUS_TERM))

        async def inner(req: ToolCallRequest) -> Any:
            return await reporter.awrap_tool_call(req, tool.acall)

        with pytest.raises(ToolError):
            await retry.awrap_tool_call(request(writer), inner)
        assert writer.parts == []
        assert reporter._attempts == {}  # pylint: disable=protected-access

    async def test_other_tool_untouched(self) -> None:
        writer, reporter, tool = Writer(), ProgressReporter([ENGINE]), Failing(unavailable())
        with pytest.raises(ToolError):
            await reporter.awrap_tool_call(request(writer, name="get_values"), tool.acall)
        assert writer.parts == []
        assert reporter._attempts == {}  # pylint: disable=protected-access

    async def test_same_call_id_in_two_sessions(self) -> None:
        writer, reporter = Writer(), ProgressReporter([ENGINE])
        first, second = Failing(unavailable()), Failing(unavailable())
        with pytest.raises(ToolError):
            await reporter.awrap_tool_call(request(writer, thread="u1:s1"), first.acall)
        with pytest.raises(ToolError):
            await reporter.awrap_tool_call(request(writer, thread="u2:s1"), second.acall)
        assert len(writer.parts) == 2  # each session's first attempt still has a retry left
        await reporter.awrap_tool_call(request(writer, thread="u1:s1"), first.acall)
        await reporter.awrap_tool_call(request(writer, thread="u2:s1"), second.acall)
        assert reporter._attempts == {}  # pylint: disable=protected-access


class TestStackPlace:
    def test_right_after_engine_retry(self) -> None:
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog()).middleware()
        at = next(i for i, m in enumerate(stack) if isinstance(m, ToolRetryMiddleware))
        reporter = stack[at + 1]
        assert isinstance(reporter, ProgressReporter)
        assert reporter.engine_tools == default_catalog().engine_tool_names()
        assert reporter.max_retries == stack[at].max_retries
