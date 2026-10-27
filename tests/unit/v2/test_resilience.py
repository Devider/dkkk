"""S2.5 (AR-Q11, FR-7): model retries, tool errors as envelopes, one engine retry, StopEvent never retried."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from gigachat.exceptions import AuthenticationError, BadRequestError, ForbiddenError, RateLimitError, ServerError
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware._retry import calculate_delay
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_api import STOP_BODY
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import resilience
from aigw_service.api.v2.middleware.resilience import (
    is_transient_llm_error,
    is_transient_tool_error,
    model_retry,
    tool_error_content,
    tool_errors,
)
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from aigw_service.config import APP_CONFIG
from aigw_service.exceptions import StopEventError
from excel_engine import ExcelAnalysisToolResult

URL = "https://gigachat/api/v1/chat/completions"
SCENARIOS = {"inputs": [{"id": "IN1", "min": 300, "max": 300, "step": 1}], "outputs": ["OUT0"], "year": 2026}


def response_error(cls: type, status: int, body: bytes = b"{}") -> Exception:
    return cls(URL, status, body, None)


def raising(exc: Exception) -> Any:
    def step(_messages: list[BaseMessage]) -> AIMessage:
        raise exc

    return step


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(APP_CONFIG.agent, "llm_retry_wait", 0.0)
    monkeypatch.setattr(resilience, "ENGINE_RETRY_DELAY", 0.0)


@pytest.fixture
def domain(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def run(**kw: Any) -> ExcelAnalysisToolResult:
        calls.append(kw)
        return ExcelAnalysisToolResult(status="OK", result="1 сценарий", content={})

    monkeypatch.setattr(calc_tools, "analyze_excel_model", run)
    return calls


async def run_turn(tmp_path: Path, steps: list) -> tuple[dict, ScriptedChatModel]:
    model = ScriptedChatModel(steps=steps)
    store = await uploaded_store("u1", INDEX, str(tmp_path))
    agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode="off").build()
    out = await agent.ainvoke(
        {"messages": [HumanMessage("EBITDA 2026 при цене метанола 300?")]},
        context=RequestContext(user_id="u1", session_id="s1"),
    )
    return out, model


def envelopes(out: dict) -> list[dict]:
    return [json.loads(m.content) for m in out["messages"] if isinstance(m, ToolMessage)]


class TestPredicates:
    @pytest.mark.parametrize(
        "exc",
        [
            response_error(ServerError, 502),
            response_error(RateLimitError, 429),
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            ConnectionError("reset"),
            TimeoutError(),
        ],
        ids=["5xx", "429", "connect", "read-timeout", "connection", "timeout"],
    )
    def test_transient(self, exc: Exception) -> None:
        assert is_transient_llm_error(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            response_error(ForbiddenError, 403, STOP_BODY),
            response_error(ForbiddenError, 403),
            StopEventError(user_message="x", url=URL, status_code=403, reason="r"),
            response_error(BadRequestError, 400),
            response_error(AuthenticationError, 401),
            ValueError("x"),
        ],
        ids=["stop-event", "403", "stop-event-error", "400", "401", "other"],
    )
    def test_not_transient(self, exc: Exception) -> None:
        assert not is_transient_llm_error(exc)

    def test_only_engine_unavailable_is_transient_for_tools(self) -> None:
        assert is_transient_tool_error(ToolError(ErrorCode.ENGINE_UNAVAILABLE))
        assert not is_transient_tool_error(ToolError(ErrorCode.ENGINE_TIMEOUT))
        assert not is_transient_tool_error(RuntimeError("engine_unavailable"))

    def test_error_content(self) -> None:
        assert tool_error_content(response_error(ForbiddenError, 403, STOP_BODY), None) is None  # type: ignore[arg-type]
        content = tool_error_content(ZeroDivisionError("секрет /etc/passwd"), None)  # type: ignore[arg-type]
        envelope = json.loads(content or "")
        assert envelope["code"] == "internal_error"
        assert "секрет" not in content
        assert "passwd" not in content

    def test_model_retry_settings(self) -> None:
        middleware = model_retry(3, 2.0)
        assert isinstance(middleware, ModelRetryMiddleware)
        assert (middleware.max_retries, middleware.on_failure, middleware.jitter) == (3, "error", False)
        delays = [
            calculate_delay(
                n,
                backoff_factor=middleware.backoff_factor,
                initial_delay=middleware.initial_delay,
                max_delay=middleware.max_delay,
                jitter=middleware.jitter,
            )
            for n in range(3)
        ]
        assert delays == [2.0, 4.0, 8.0]  # «паузы растут вдвое»


class TestToolExceptionReachesMiddleware:
    """Design risk: ToolNode must not swallow a tool's exception before ToolErrorMiddleware sees it."""

    async def test_exception_becomes_internal_error(self) -> None:
        @tool
        def broken(x: int) -> str:
            """Падает."""
            raise ZeroDivisionError(f"деление {x}")

        agent = create_agent(
            ScriptedChatModel(steps=[tool_call("broken", x=1), AIMessage("ок")]), [broken], middleware=[tool_errors()]
        )
        out = await agent.ainvoke({"messages": [HumanMessage("ход")]})
        message = next(m for m in out["messages"] if isinstance(m, ToolMessage))
        assert message.status == "error"
        assert json.loads(message.content)["code"] == "internal_error"
        assert out["messages"][-1].content == "ок"


class TestModelRetry:
    """AR-Q11 «Повтор вызова модели»."""

    async def test_transient_failure(self, tmp_path: Path) -> None:
        fail = raising(response_error(ServerError, 503))
        out, model = await run_turn(tmp_path, [fail, fail, AIMessage("Готово.")])
        assert out["messages"][-1].content == "Готово."
        assert len(model.calls) == 3

    async def test_rate_limit(self, tmp_path: Path) -> None:
        out, model = await run_turn(tmp_path, [raising(response_error(RateLimitError, 429)), AIMessage("Готово.")])
        assert out["messages"][-1].content == "Готово."
        assert len(model.calls) == 2

    async def test_retries_exhausted(self, tmp_path: Path) -> None:
        fail = raising(response_error(ServerError, 500, b"internal trace"))
        with pytest.raises(ServerError):
            await run_turn(tmp_path, [fail] * 5)

    async def test_attempts_from_settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "llm_retry_attempts", 1)
        fail = raising(response_error(ServerError, 500))
        model = ScriptedChatModel(steps=[fail, fail, AIMessage("не дойдёт")])
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode="off").build()
        with pytest.raises(ServerError):
            await agent.ainvoke(
                {"messages": [HumanMessage("ход")]}, context=RequestContext(user_id="u1", session_id="s1")
            )
        assert len(model.calls) == 2

    @pytest.mark.parametrize(
        "exc", [response_error(BadRequestError, 400), response_error(ForbiddenError, 403)], ids=["400", "403"]
    )
    async def test_not_retried(self, tmp_path: Path, exc: Exception) -> None:
        model_steps = [raising(exc), AIMessage("не дойдёт")]
        with pytest.raises(type(exc)):
            await run_turn(tmp_path, model_steps)

    async def test_stop_event_once(self, tmp_path: Path) -> None:
        steps = [raising(response_error(ForbiddenError, 403, STOP_BODY)), AIMessage("не дойдёт")]
        model = ScriptedChatModel(steps=steps)
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode="off").build()
        with pytest.raises(StopEventError):
            await agent.ainvoke(
                {"messages": [HumanMessage("ход")]}, context=RequestContext(user_id="u1", session_id="s1")
            )
        assert len(model.calls) == 1


class FlakyGate:
    """Engine gate that fails with ``error`` for the first ``failures`` runs."""

    def __init__(self, error: Exception, failures: int) -> None:
        self.error, self.failures, self.runs = error, failures, 0

    async def run(self, _session_key: str, func: Any, /, **kwargs: Any) -> Any:
        self.runs += 1
        if self.runs <= self.failures:
            raise self.error
        return func(**kwargs)


class TestEngineRetry:
    """AR-Q11 «Повтор движка»."""

    @staticmethod
    def gate(monkeypatch: pytest.MonkeyPatch, code: ErrorCode, failures: int) -> FlakyGate:
        flaky = FlakyGate(ToolError(code), failures)
        monkeypatch.setattr(calc_tools, "_gate", lambda: flaky)
        return flaky

    async def test_fails_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: list) -> None:
        flaky = self.gate(monkeypatch, ErrorCode.ENGINE_UNAVAILABLE, 1)
        out, _ = await run_turn(tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("Готово.")])
        assert flaky.runs == 2
        assert [e["status"] for e in envelopes(out)] == ["ok"]

    async def test_fails_twice(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: list) -> None:
        flaky = self.gate(monkeypatch, ErrorCode.ENGINE_UNAVAILABLE, 5)
        out, _ = await run_turn(tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("Шаг пропущен.")])
        assert flaky.runs == 2
        (envelope,) = envelopes(out)
        assert envelope["code"] == "engine_unavailable"
        assert envelope["hint"]
        assert out["messages"][-1].content == "Шаг пропущен."  # the turn goes on

    async def test_domain_engine_failure_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline_gate: Any
    ) -> None:
        results = [
            ExcelAnalysisToolResult(status="ERROR", result="Ошибка при анализе: soffice did not start", content={}),
            ExcelAnalysisToolResult(status="OK", result="1 сценарий", content={}),
        ]
        monkeypatch.setattr(calc_tools, "analyze_excel_model", lambda **_kw: results.pop(0))
        out, _ = await run_turn(tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("Готово.")])
        assert len(inline_gate.sessions) == 2
        assert [e["status"] for e in envelopes(out)] == ["ok"]

    async def test_model_fixable_error_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: list
    ) -> None:
        flaky = self.gate(monkeypatch, ErrorCode.ENGINE_TIMEOUT, 5)
        out, _ = await run_turn(tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("Сужу сетку.")])
        assert flaky.runs == 1
        assert envelopes(out)[0]["code"] == "engine_timeout"


class TestToolExceptions:
    """FR-7 «Исключение в ходе агента»."""

    async def test_unexpected_exception(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def crash(**_kw: Any) -> Any:
            raise ZeroDivisionError("внутренний путь /srv/model.xlsx")

        monkeypatch.setattr(calc_tools, "analyze_excel_model", crash)
        out, _ = await run_turn(tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("Шаг пропущен.")])
        (envelope,) = envelopes(out)
        assert envelope["code"] == "internal_error"
        assert "/srv" not in json.dumps(envelope, ensure_ascii=False)
        assert out["messages"][-1].content == "Шаг пропущен."
