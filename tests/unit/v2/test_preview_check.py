"""S4.4 (ADR-10): background A/B check of the agent's model on the preview model — settings, comparison, worker,
middleware, factory wiring and whole turns on the scripted model."""

import asyncio
import io
import json
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from gigachat.exceptions import NotFoundError
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_api import Api, answer_text, v2
from tests.unit.v2.test_context_meter import full_request
from tests.unit.v2.test_memory import STEPS, plan
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_resilience import raising, response_error
from tests.unit.v2.test_telemetry import SCENARIOS, RunTree, stop_event

from aigw_service import api
from aigw_service.api.v2 import factory, models
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import preview_check as pc
from aigw_service.api.v2.middleware.context_meter import ContextMeter
from aigw_service.api.v2.middleware.preview_check import (
    PreviewChecker,
    PreviewJob,
    PreviewModelCheck,
    compare_calls,
    parse_similarity,
    preview_record,
    snapshot,
)
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent import AgentSettings

QUESTION = "EBITDA 2026 при метаноле 300–500?"
VERDICT = "Ответы совпадают по смыслу | Итоговый процент схожести: 90%"


class SlowModel(ScriptedChatModel):
    """A scripted model whose async call waits first, as a slow preview model would."""

    delay: float = 0.0

    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> Any:
        await asyncio.sleep(self.delay)
        return self._generate(messages, stop, run_manager, **kwargs)


class RecordingChecker(PreviewChecker):
    """Records submitted jobs instead of running them."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__(ScriptedChatModel(steps=[]), ScriptedChatModel(steps=[]), model="main")
        self.jobs: list[PreviewJob] = []
        self.fail = fail

    def submit(self, job: PreviewJob) -> None:
        if self.fail:
            raise RuntimeError("очередь сломана")
        self.jobs.append(job)


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """``PREVIEW CHECK`` records, ``LLM CALL`` records and warnings of the shared logger."""
    logged: dict[str, list[Any]] = {"PREVIEW CHECK": [], "LLM CALL": [], "warning": []}

    def info(message: str, *args: Any, **_kw: Any) -> None:
        for prefix in ("PREVIEW CHECK", "LLM CALL"):
            if message.startswith(prefix):
                logged[prefix].append(json.loads(args[0]))

    def warning(message: str, *args: Any, **_kw: Any) -> None:
        logged["warning"].append(message.format(*args))

    monkeypatch.setattr(pc.logger, "info", info)
    monkeypatch.setattr(pc.logger, "warning", warning)
    monkeypatch.setattr(pc.logger, "opt", lambda **_kw: pc.logger)  # opt(exception=True).warning → the same sink
    return logged


def checker(preview: ScriptedChatModel | None = None, judge_steps: list | None = None, **kw: Any) -> PreviewChecker:
    preview = preview if preview is not None else ScriptedChatModel(steps=[AIMessage("Ответ preview.")] * 20)
    judge = ScriptedChatModel(steps=judge_steps if judge_steps is not None else [AIMessage(VERDICT)] * 20)
    return PreviewChecker(preview, judge, model="GigaChat", **kw)


def job(main: AIMessage | None = None) -> PreviewJob:
    return snapshot(full_request(), main or AIMessage("Ответ основной модели."))


def call(name: str, **args: Any) -> AIMessage:
    return tool_call(name, "c1", **args)


# ---------------------------------------------------------------- 1. settings


class TestSettings:
    """AR-Q5 «Параметры A/B-проверки по умолчанию», «Недопустимая доля A/B-проверки»."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_PREVIEW_CHECK", raising=False)
        monkeypatch.delenv("AGENT_PREVIEW_CHECK_PERCENT", raising=False)
        settings = AgentSettings()
        assert (settings.preview_check, settings.preview_check_percent) == (False, 0.05)

    @pytest.mark.parametrize("value", ["-0.1", "1.5"])
    def test_invalid_percent(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("AGENT_PREVIEW_CHECK_PERCENT", value)
        with pytest.raises(ValidationError):
            AgentSettings()

    @pytest.mark.parametrize("value", ["0", "1"])
    def test_bounds_accepted(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("AGENT_PREVIEW_CHECK", "true")
        monkeypatch.setenv("AGENT_PREVIEW_CHECK_PERCENT", value)
        settings = AgentSettings()
        assert settings.preview_check is True
        assert settings.preview_check_percent == float(value)

    def test_v1_variables_do_not_switch_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_PREVIEW_CHECK", raising=False)
        monkeypatch.delenv("AGENT_PREVIEW_CHECK_PERCENT", raising=False)
        monkeypatch.setenv("ENABLE_PREVIEW_MODEL_CHECK", "true")
        monkeypatch.setenv("PREVIEW_MODEL_CHECK_PERCENT", "0.9")
        settings = AgentSettings()
        assert (settings.preview_check, settings.preview_check_percent) == (False, 0.05)


# ---------------------------------------------------------------- 2. comparison and the log line


class TestSnapshot:
    """ADR-10 «Тот же запрос, что у основной модели» (design D3)."""

    def test_parts_of_the_request(self) -> None:
        request = full_request()
        main = AIMessage("Ответ.")
        snap = snapshot(request, main)
        assert snap.system_message is request.system_message
        assert list(snap.messages) == request.messages
        assert list(snap.tools) == request.tools
        assert snap.tool_choice == request.tool_choice
        assert snap.main is main
        assert (snap.trace_id, snap.turn_id, snap.call) == ("tr-1", "t1", 6)  # as CONTEXT LAYERS

    def test_later_changes_do_not_leak(self) -> None:
        request = full_request()
        snap = snapshot(request, AIMessage("Ответ."))
        count = len(snap.messages)
        request.messages.append(HumanMessage("ещё"))
        request.model_settings["temperature"] = 0.9
        assert len(snap.messages) == count
        assert "temperature" not in snap.model_settings


class TestCompare:
    """ADR-10 «Совпавшие вызовы инструментов», «Разные вызовы инструментов» (design D5)."""

    @pytest.mark.parametrize(
        ("main", "preview", "expected"),
        [
            (call("resolve_terms", terms=["a"]), call("resolve_terms", terms=["a"]), (True, True)),
            (call("resolve_terms", terms=["a"]), call("resolve_terms", terms=["b"]), (True, False)),
            (call("run_scenarios", year=2026), call("resolve_terms", terms=["a"]), (False, False)),
            (call("resolve_terms", terms=["a"]), AIMessage("текст"), (False, False)),
        ],
        ids=["same", "other-args", "other-tool", "call-vs-text"],
    )
    def test_calls(self, main: AIMessage, preview: AIMessage, expected: tuple) -> None:
        assert compare_calls(main, preview) == expected

    def test_argument_order_does_not_matter(self) -> None:
        assert compare_calls(call("derive", a=1, b=2), call("derive", b=2, a=1)) == (True, True)


class TestSimilarity:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Различий нет | Итоговый процент схожести: 85%", 85),
            ("итоговый процент схожести: 150%", 100),
            ("Итоговый процент схожести: 72,6 %", 73),
            ("Ответы похожи", None),
            ("", None),
        ],
    )
    def test_parse(self, text: str, expected: int | None) -> None:
        assert parse_similarity(text) == expected


class TestRecord:
    """ADR-10 «Итог A/B-проверки в журнале» (design D6)."""

    def test_fields(self) -> None:
        main = call("resolve_terms", terms=["метанол"])
        outcome = pc.Outcome(preview=call("resolve_terms", terms=["метанол"]), elapsed_s=0.4)
        record = json.loads(preview_record(job(main), outcome, "GigaChat", "GigaChat-preview"))
        assert (record["trace_id"], record["turn_id"], record["call"]) == ("tr-1", "t1", 6)
        assert (record["model"], record["preview_model"], record["elapsed_s"]) == ("GigaChat", "GigaChat-preview", 0.4)
        assert record["main"]["tool_calls"] == [{"name": "resolve_terms", "args": {"terms": ["метанол"]}}]
        assert record["preview"] == record["main"]
        assert (record["same_tools"], record["same_args"], record["similarity"]) == (True, True, None)
        assert (record["error"], record["exception"]) == (None, None)

    def test_error_without_text(self) -> None:
        outcome = pc.Outcome(error=pc.PREVIEW_FAILED, exception="NotFoundError")
        record = json.loads(preview_record(job(), outcome, "m", "p"))
        assert (record["error"], record["exception"]) == ("preview_failed", "NotFoundError")
        assert record["preview"] is None
        assert (record["same_tools"], record["same_args"]) == (None, None)

    def test_long_texts_are_cut(self) -> None:
        outcome = pc.Outcome(preview=AIMessage("п" * 5000), judge="с" * 2000)
        record = json.loads(preview_record(job(AIMessage("о" * 5000)), outcome, "m", "p"))
        assert len(record["main"]["text"]) == pc.TEXT_LIMIT
        assert len(record["preview"]["text"]) == pc.TEXT_LIMIT
        assert len(record["judge"]) == pc.JUDGE_LIMIT


# ---------------------------------------------------------------- 3. background worker


class TestChecker:
    """ADR-10 «Сбой preview-модели», «Очередь переполнена», «Остановка сервиса» (design D2, D3, D5)."""

    async def test_one_submit_one_check(self, checks: dict) -> None:
        check = checker()
        check.submit(job())
        await check.join()
        assert len(check.preview.calls) == 1  # type: ignore[attr-defined]
        (record,) = checks["PREVIEW CHECK"]
        assert record["preview"]["text"] == "Ответ preview."
        assert (record["similarity"], record["error"]) == (90, None)
        await check.shutdown()

    async def test_same_request_as_main(self, checks: dict) -> None:
        preview = ScriptedChatModel(steps=[AIMessage("Ответ preview.")])
        check = checker(preview)
        request = full_request().override(tool_choice="run_scenarios")
        check.submit(snapshot(request, AIMessage("Ответ.")))
        await check.join()
        (seen,) = preview.calls
        assert seen["messages"] == [request.system_message, *request.messages]
        assert seen["tools"] == ["run_scenarios"]
        assert seen["tool_choice"] == "run_scenarios"
        await check.shutdown()

    async def test_without_tools_binds_settings_only(self, checks: dict) -> None:
        preview = ScriptedChatModel(steps=[AIMessage("Ответ preview.")])
        check = checker(preview)
        request = full_request().override(tools=[], system_message=None)
        check.submit(snapshot(request, AIMessage("Ответ.")))
        await check.join()
        assert preview.calls[0]["messages"] == request.messages
        await check.shutdown()

    async def test_judge_only_for_texts(self, checks: dict) -> None:
        preview = ScriptedChatModel(steps=[call("resolve_terms", terms=["a"]), AIMessage("Текст preview.")])
        judge = [AIMessage(VERDICT)]
        check = checker(preview, judge)
        check.submit(job(call("resolve_terms", terms=["a"])))
        check.submit(job(AIMessage("Текст основной.")))
        await check.join()
        assert len(check.judge.calls) == 1  # type: ignore[attr-defined]
        tools, text = checks["PREVIEW CHECK"]
        assert (tools["same_tools"], tools["similarity"]) == (True, None)
        assert (text["similarity"], text["judge"]) == (90, VERDICT)
        await check.shutdown()

    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (stop_event(), "stop_event"),
            (response_error(NotFoundError, 404), "preview_failed"),
            (httpx.ConnectError("refused"), "preview_failed"),
        ],
        ids=["stop-event", "404", "network"],
    )
    async def test_preview_failure_keeps_worker(self, checks: dict, exc: Exception, code: str) -> None:
        preview = ScriptedChatModel(steps=[raising(exc), AIMessage("Ответ preview.")])
        check = checker(preview)
        check.submit(job())
        check.submit(job())
        await check.join()
        failed, ok = checks["PREVIEW CHECK"]
        assert (failed["error"], failed["exception"]) == (code, type(exc).__name__)
        assert "секрет" not in json.dumps(failed, ensure_ascii=False)
        assert ok["error"] is None
        await check.shutdown()

    async def test_judge_failed_and_unparsed(self, checks: dict) -> None:
        check = checker(judge_steps=[raising(RuntimeError("{судья}")), AIMessage("Похоже.")])
        check.submit(job())
        check.submit(job())
        await check.join()
        failed, unparsed = checks["PREVIEW CHECK"]
        assert (failed["error"], failed["exception"], failed["similarity"]) == ("judge_failed", "RuntimeError", None)
        assert failed["preview"]["text"] == "Ответ preview."
        assert (unparsed["error"], unparsed["similarity"], unparsed["judge"]) == ("judge_unparsed", None, "Похоже.")
        await check.shutdown()

    async def test_full_queue_drops_without_waiting(self, checks: dict) -> None:
        check = checker(SlowModel(steps=[AIMessage("Ответ preview.")] * 3, delay=10), queue_size=1)
        check.submit(job())
        for _ in range(3):
            await asyncio.sleep(0)  # the worker takes the first check and waits on the slow model
        started = time.perf_counter()
        check.submit(job())  # fills the queue
        check.submit(job())  # dropped
        assert time.perf_counter() - started < 0.05
        assert [r["error"] for r in checks["PREVIEW CHECK"]] == ["queue_full"]
        await check.shutdown()

    def test_new_event_loop_new_worker(self, checks: dict) -> None:
        check = checker()
        workers = []

        async def once() -> None:
            check.submit(job())
            await check.join()
            workers.append(check._worker)  # pylint: disable=protected-access

        asyncio.run(once())
        asyncio.run(once())
        assert len(check.preview.calls) == 2  # type: ignore[attr-defined]
        assert workers[0] is not workers[1]

    async def test_shutdown_drops_queue(self, checks: dict) -> None:
        preview = SlowModel(steps=[AIMessage("Ответ preview.")] * 3, delay=10)
        check = checker(preview)
        check.submit(job())
        check.submit(job())
        await asyncio.sleep(0)
        await check.shutdown()
        await asyncio.sleep(0.05)
        assert preview.calls == []
        assert check._worker is None  # pylint: disable=protected-access

    async def test_shutdown_before_any_check(self) -> None:
        await checker().shutdown()  # no worker — nothing to stop

    async def test_worker_survives_unexpected_error(self, checks: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        check = checker()
        calls = []

        async def broken(_job: PreviewJob) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("сбой")
            return pc.Outcome()

        monkeypatch.setattr(check, "check", broken)
        check.submit(job())
        check.submit(job())
        await check.join()
        assert len(calls) == 2
        assert len(checks["PREVIEW CHECK"]) == 1
        await check.shutdown()


class TestLifespan:
    """ADR-10 «Остановка сервиса»: the service checker is stopped in lifespan; nothing is built when off."""

    @pytest.fixture
    def quiet(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        async def nothing() -> None:
            return None

        monkeypatch.setattr(api.APP_CTX, "on_startup", nothing)
        monkeypatch.setattr(api.APP_CTX, "on_shutdown", nothing)
        monkeypatch.setattr(models, "build_preview_model", lambda: SlowModel(steps=[AIMessage("p")] * 3, delay=10))
        monkeypatch.setattr(models, "build_judge_model", lambda: ScriptedChatModel(steps=[]))
        pc.default_preview_checker.cache_clear()
        yield
        pc.default_preview_checker.cache_clear()

    async def test_v1_start_and_stop(self, quiet: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v1")
        async with api.lifespan(None):
            pass
        assert pc.default_preview_checker.cache_info().currsize == 0  # no GigaChat preview model built

    async def test_stops_service_checker(self, quiet: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v1")
        service = pc.default_preview_checker()
        async with api.lifespan(None):
            service.submit(job())
            await asyncio.sleep(0)
        assert service._worker is None  # pylint: disable=protected-access
        assert service.preview.calls == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 4. middleware and factory


class TestMiddleware:
    """ADR-10 «Проверяется каждый вызов», «Доля ноль», «Повтор после сбоя основной модели» (design D7)."""

    @pytest.mark.parametrize(("percent", "submitted"), [(0.0, 0), (1.0, 1)])
    async def test_sampling(self, percent: float, submitted: int) -> None:
        recorder = RecordingChecker()
        response = ModelResponse(result=[AIMessage("Ответ.")])

        async def handler(_request: Any) -> Any:
            return response

        middleware = PreviewModelCheck(recorder, percent)
        assert await middleware.awrap_model_call(full_request(), handler) is response
        assert len(recorder.jobs) == submitted

    async def test_rng_decides(self) -> None:
        recorder = RecordingChecker()

        async def handler(_request: Any) -> Any:
            return AIMessage("Ответ.")

        await PreviewModelCheck(recorder, 0.5, rng=lambda: 0.49).awrap_model_call(full_request(), handler)
        await PreviewModelCheck(recorder, 0.5, rng=lambda: 0.5).awrap_model_call(full_request(), handler)
        assert len(recorder.jobs) == 1

    async def test_failed_call_is_not_checked(self) -> None:
        recorder = RecordingChecker()

        async def handler(_request: Any) -> Any:
            raise httpx.ConnectError("refused")

        with pytest.raises(httpx.ConnectError):
            await PreviewModelCheck(recorder, 1.0).awrap_model_call(full_request(), handler)
        assert recorder.jobs == []

    async def test_broken_queue_does_not_touch_the_call(self, checks: dict) -> None:
        response = AIMessage("Ответ.")

        async def handler(_request: Any) -> Any:
            return response

        assert await PreviewModelCheck(RecordingChecker(fail=True), 1.0).awrap_model_call(full_request(), handler) is (
            response
        )
        assert checks["warning"]

    def test_sync_path_without_check(self) -> None:
        recorder = RecordingChecker()
        response = AIMessage("Ответ.")
        assert PreviewModelCheck(recorder, 1.0).wrap_model_call(full_request(), lambda _r: response) is response
        assert recorder.jobs == []


class TestModels:
    """Design D4: preview — ``<GIGACHAT_MODEL>-preview``, judge — the agent's model; no network to build them."""

    def test_names(self) -> None:
        assert pc.model_name(models.build_preview_model()) == f"{APP_CONFIG.gigachat.model}-preview"
        assert pc.model_name(models.build_judge_model()) == APP_CONFIG.gigachat.model


class TestFactory:
    """ADR-10 «Проверка выключена по умолчанию», «Скриптовая модель» (design D1, D4)."""

    @staticmethod
    def stack(**kw: Any) -> list[Any]:
        return AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), **kw).middleware()

    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "preview_check", False)
        off = self.stack()
        on = self.stack(preview_check=PreviewModelCheck(RecordingChecker(), 1.0))
        assert not any(isinstance(m, PreviewModelCheck) for m in off)
        assert [type(m) for m in off] == [type(m) for m in on if not isinstance(m, PreviewModelCheck)]

    def test_right_before_context_meter(self) -> None:
        check = PreviewModelCheck(RecordingChecker(), 1.0)
        stack = self.stack(preview_check=check)
        assert stack[-2] is check
        assert isinstance(stack[-1], ContextMeter)

    def test_scripted_model_warns(self, monkeypatch: pytest.MonkeyPatch, checks: dict) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "preview_check", True)
        monkeypatch.setattr(APP_CONFIG.agent, "model", "scripted")
        assert not any(isinstance(m, PreviewModelCheck) for m in self.stack())
        assert any("AGENT_PREVIEW_CHECK" in w for w in checks["warning"])

    def test_gigachat_installs_service_checker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = RecordingChecker()
        monkeypatch.setattr(APP_CONFIG.agent, "preview_check", True)
        monkeypatch.setattr(APP_CONFIG.agent, "preview_check_percent", 0.2)
        monkeypatch.setattr(APP_CONFIG.agent, "model", "gigachat")
        monkeypatch.setattr(factory, "default_preview_checker", lambda: recorder)
        check = self.stack()[-2]
        assert isinstance(check, PreviewModelCheck)
        assert (check.checker, check.percent) == (recorder, 0.2)


# ---------------------------------------------------------------- 5. whole turns on the scripted model


def turn_steps() -> list[AIMessage]:
    return [
        tool_call("write_todos", "p1", todos=STEPS),
        tool_call("resolve_terms", "r1", terms=[{"text": "метанол", "role": "input"}]),
        tool_call("run_scenarios", "c1", **SCENARIOS),
        tool_call("write_todos", "p2", todos=plan("completed")),
        AIMessage("EBITDA 2026: 1350.5."),
    ]


def preview_steps(count: int = 20) -> list[AIMessage]:
    return [AIMessage("Ответ preview.") for _ in range(count)]


async def turn(
    tmp_path: Path, steps: list, check: PreviewModelCheck | None = None, **kw: Any
) -> tuple[dict, ScriptedChatModel]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = await uploaded_store("u1", INDEX, str(tmp_path))
    model = ScriptedChatModel(steps=steps)
    config = kw.pop("config", None)
    agent = AgentFactory(
        model, default_catalog(), store=store, upload_dir=str(tmp_path), preview_check=check, **kw
    ).build()
    out = await agent.ainvoke(
        {"messages": [HumanMessage(QUESTION)]},
        context=RequestContext(user_id="u1", session_id="s1", trace_id="tr-5"),
        config=config,
    )
    return out, model


def outline(out: dict) -> tuple[str, list, list]:
    answers = [m for m in out["messages"] if isinstance(m, AIMessage)]
    return answers[-1].content, [[c["name"] for c in m.tool_calls] for m in answers], out.get("todos")


class TestTurns:
    """ADR-10 «Ответ не меняется», «Тот же запрос», «Медленная preview-модель», «Лимиты хода», контракт API."""

    async def test_answer_unchanged(self, tmp_path: Path, domain: list, checks: dict) -> None:
        plain, _ = await turn(tmp_path / "a", turn_steps())
        check = checker(ScriptedChatModel(steps=preview_steps()))
        checked, model = await turn(tmp_path / "b", turn_steps(), PreviewModelCheck(check, 1.0))
        await check.join()
        assert outline(checked) == outline(plain)
        assert len(checks["PREVIEW CHECK"]) == len(model.calls)
        assert {r["trace_id"] for r in checks["PREVIEW CHECK"]} == {"tr-5"}
        assert [r["call"] for r in checks["PREVIEW CHECK"]] == list(range(1, len(model.calls) + 1))
        await check.shutdown()

    async def test_same_request_with_narrowed_tools(self, tmp_path: Path, checks: dict) -> None:
        preview = ScriptedChatModel(steps=preview_steps())
        check = checker(preview)
        steps = [
            tool_call("write_todos", "p1", todos=[{"content": "Ответить", "status": "completed"}]),
            AIMessage("Готово."),
        ]
        _, model = await turn(tmp_path, steps, PreviewModelCheck(check, 1.0), plan_mode="always")
        await check.join()
        main, seen = model.calls[0], preview.calls[0]
        assert seen["tools"] == main["tools"]
        assert seen["tool_choice"] == main["tool_choice"]
        assert seen["messages"] == main["messages"]
        assert "write_todos" in main["tools"]
        assert "run_scenarios" not in main["tools"]  # narrowed by ForcePlanFirst
        await check.shutdown()

    async def test_retried_call_one_check(self, tmp_path: Path, checks: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "llm_retry_wait", 0.0)
        check = checker(ScriptedChatModel(steps=preview_steps()))
        steps = [raising(httpx.ConnectError("refused")), AIMessage("Ответ.")]
        out, _ = await turn(tmp_path, steps, PreviewModelCheck(check, 1.0), plan_mode="off")
        await check.join()
        assert outline(out)[0] == "Ответ."
        assert len(checks["PREVIEW CHECK"]) == 1
        await check.shutdown()

    async def test_slow_failing_preview_does_not_delay(self, tmp_path: Path, checks: dict) -> None:
        preview = SlowModel(steps=[raising(response_error(NotFoundError, 404))], delay=2)
        check = checker(preview)
        started = time.perf_counter()
        out, _ = await turn(tmp_path, [AIMessage("Ответ.")], PreviewModelCheck(check, 1.0), plan_mode="off")
        assert time.perf_counter() - started < 0.5
        assert outline(out)[0] == "Ответ."
        await check.join()
        (record,) = checks["PREVIEW CHECK"]
        assert (record["error"], record["exception"]) == ("preview_failed", "NotFoundError")
        await check.shutdown()

    async def test_limits_and_trace(
        self, tmp_path: Path, domain: list, checks: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "max_model_calls", 2)
        steps = [
            tool_call("resolve_terms", "r1", terms=[{"text": "метанол", "role": "input"}]),
            AIMessage("Частичный ответ."),
        ]
        plain, _ = await turn(tmp_path / "a", list(steps), plan_mode="off")
        preview = ScriptedChatModel(steps=preview_steps(), name="preview")
        check = checker(preview)
        tree = RunTree()
        checked, model = await turn(
            tmp_path / "b", list(steps), PreviewModelCheck(check, 1.0), plan_mode="off", config={"callbacks": [tree]}
        )
        await check.join()
        assert outline(checked) == outline(plain)
        assert len(preview.calls) == len(model.calls) == 2
        assert not tree.named("llm", "preview")
        assert len([r for r in tree.runs.values() if r["kind"] == "llm"]) == len(model.calls)
        await check.shutdown()

    async def test_api_contract(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, checks: dict) -> None:
        responses = []
        check = checker(ScriptedChatModel(steps=preview_steps()))
        for folder, preview in (("a", None), ("b", PreviewModelCheck(check, 1.0))):
            (tmp_path / folder).mkdir()
            store = await uploaded_store("u1", INDEX, str(tmp_path / folder))
            agent = v2(
                ScriptedChatModel(steps=[AIMessage("Ответ по модели.")]),
                store,
                tmp_path / folder,
                preview_check=preview,
            )
            responses.append(await Api(monkeypatch, agent).ask())
        await check.join()
        plain, checked = responses
        assert checked.status_code == plain.status_code == 200
        names = [sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) for r in responses]
        assert names[0] == names[1]
        assert answer_text(checked) == answer_text(plain) == "Ответ по модели."
        assert len(checks["PREVIEW CHECK"]) == 1
        await check.shutdown()
