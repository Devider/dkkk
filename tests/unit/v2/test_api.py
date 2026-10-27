"""S4.2 (FR-15, FR-20, AR-Q7): /invoke-agent at AGENT_ENGINE=v2 — ZIP, no workbook, 409, 503, user isolation."""

import asyncio
import contextlib
import io
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest
from gigachat.exceptions import ForbiddenError
from httpx import ASGITransport, AsyncClient
from langchain.agents.middleware import ModelRetryMiddleware, ToolErrorMiddleware, ToolRetryMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_turn_context import HEADERS

from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import cancellation
from aigw_service.api.v2.middleware.calc_cache import CalcCache
from aigw_service.api.v2.middleware.cancellation import Cancellation
from aigw_service.api.v2.middleware.context_meter import ContextMeter
from aigw_service.api.v2.middleware.language import LanguageGuard
from aigw_service.api.v2.middleware.stop_event import STOP_EVENT_MESSAGE, StopEventGuard, stop_event
from aigw_service.api.v2.middleware.summarization import CopilotSummarization
from aigw_service.api.v2.middleware.telemetry import Telemetry
from aigw_service.api.v2.middleware.turn_context import NO_MODEL_ANSWER
from aigw_service.api.v2.prompts.middleware import CANCELLED_ANSWER
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.turn.lock import TURN_BUSY_MESSAGE, TurnBusyError, TurnLocks
from aigw_service.config import APP_CONFIG
from aigw_service.exceptions import StopEventError
from excel_engine import ExcelAnalysisToolResult

STOP_BODY = b'{"status": 403, "message": "Model is temporarily unavailable due to technical reasons"}'


def forbidden(body: bytes = STOP_BODY) -> ForbiddenError:
    return ForbiddenError("https://gigachat/api/v1/chat/completions", 403, body, None)


class Api:
    """The FastAPI app with agent v2 swapped in, as the service runs it at AGENT_ENGINE=v2."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, agent: AgentV2) -> None:
        from aigw_service.api import app_main  # pylint: disable=import-outside-toplevel
        from aigw_service.api.v1 import router  # pylint: disable=import-outside-toplevel

        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        monkeypatch.setitem(app_main.dependency_overrides, router.get_agent, lambda: agent)
        monkeypatch.setitem(app_main.dependency_overrides, router.get_cb_handler(), lambda: None)
        self.client = AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test")

    async def ask(self, text: str = "Привет", user: str = "u1", session: str | None = None) -> Any:
        headers = {**HEADERS, "x-user-id": user} | ({"x-session-id": session} if session else {})
        return await self.client.post("/api/v1/invoke-agent", json={"message": text}, headers=headers)


def answer_text(response: Any) -> str:
    return zipfile.ZipFile(io.BytesIO(response.content)).read("txt_response.txt").decode()


def v2(model: ScriptedChatModel, store: Any, upload_dir: Path, locks: TurnLocks | None = None, **kw: Any) -> AgentV2:
    agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(upload_dir), **kw).build()
    return AgentV2(agent, locks=locks or TurnLocks())


class TestContract:
    """FR-15 «ZIP того же состава», «Нет загруженного файла»."""

    async def test_zip(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        api = Api(monkeypatch, v2(ScriptedChatModel(steps=[AIMessage("Ответ по модели.")]), store, tmp_path))
        response = await api.ask()
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert sorted(archive.namelist()) == ["generated/agent_output.xlsx", "txt_response.txt"]
        assert answer_text(response) == "Ответ по модели."

    async def test_no_workbook(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        model = ScriptedChatModel(steps=[])
        api = Api(monkeypatch, v2(model, InMemoryStore(), tmp_path))
        response = await api.ask()
        assert response.status_code == 200
        assert answer_text(response) == NO_MODEL_ANSWER
        assert model.calls == []


class TestTurnLock:
    """FR-15 «Занятая сессия»."""

    def test_hold(self) -> None:
        locks = TurnLocks()
        with locks.hold("u1:s1"):
            assert locks.busy == {"u1:s1"}
            with pytest.raises(TurnBusyError), locks.hold("u1:s1"):
                pass
            with locks.hold("u1:s2"):
                assert locks.busy == {"u1:s1", "u1:s2"}
        assert locks.busy == frozenset()

    def test_released_on_error(self) -> None:
        locks = TurnLocks()
        with pytest.raises(ValueError, match="ход упал"), locks.hold("u1:s1"):
            raise ValueError("ход упал")
        assert locks.busy == frozenset()

    async def test_second_request_gets_409(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        started, release = threading.Event(), threading.Event()

        def slow(_messages: list[BaseMessage]) -> AIMessage:
            started.set()
            release.wait(10)
            return AIMessage("Первый ответ.")

        model = ScriptedChatModel(steps=[slow, AIMessage("Другая сессия."), AIMessage("Снова первая.")])
        locks = TurnLocks()
        api = Api(monkeypatch, v2(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, locks))
        first = asyncio.create_task(api.ask(session="11111111-1111-4111-8111-111111111111"))
        await asyncio.to_thread(started.wait, 10)

        busy = await api.ask(session="11111111-1111-4111-8111-111111111111")
        assert busy.status_code == 409
        assert busy.json()["error_description"] == TURN_BUSY_MESSAGE
        other = await api.ask(session="22222222-2222-4222-8222-222222222222")
        assert (other.status_code, answer_text(other)) == (200, "Другая сессия.")

        release.set()
        done = await first
        assert (done.status_code, answer_text(done)) == (200, "Первый ответ.")
        again = await api.ask(session="11111111-1111-4111-8111-111111111111")
        assert (again.status_code, answer_text(again)) == (200, "Снова первая.")
        assert locks.busy == frozenset()

    async def test_failed_turn_frees_the_session(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def boom(_messages: list[BaseMessage]) -> AIMessage:
            raise RuntimeError("сбой")

        locks = TurnLocks()
        model = ScriptedChatModel(steps=[boom, AIMessage("После сбоя.")])
        api = Api(monkeypatch, v2(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, locks))
        assert (await api.ask()).status_code == 424
        assert answer_text(await api.ask()) == "После сбоя."
        assert locks.busy == frozenset()


class TestStopEvent:
    """FR-15 «StopEvent GigaChat»."""

    async def test_model_stop_event_is_503(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def stop(_messages: list[BaseMessage]) -> AIMessage:
            raise forbidden()

        model = ScriptedChatModel(steps=[stop, AIMessage("не должен вызываться")])
        api = Api(monkeypatch, v2(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path))
        response = await api.ask()
        assert response.status_code == 503
        assert response.json()["error_description"] == STOP_EVENT_MESSAGE
        assert len(model.calls) == 1  # no retry

    async def test_semantic_layer_stop_event_is_503(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        async def analyzer(_message: str, _index: Any) -> list:
            raise forbidden()

        monkeypatch.setattr(rt, "analyze_with_v1", analyzer)
        model = ScriptedChatModel(
            steps=[tool_call("resolve_terms", "r1", terms=[{"text": "EBITDA", "role": "output"}]), AIMessage("нет")]
        )
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        api = Api(monkeypatch, v2(model, store, tmp_path, plan_mode="off"))
        response = await api.ask("EBITDA 2026?")
        assert response.status_code == 503
        assert len(model.calls) == 1

    async def test_403_without_marker_is_not_503(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def denied(_messages: list[BaseMessage]) -> AIMessage:
            raise forbidden(b'{"message": "Permission denied"}')

        api = Api(
            monkeypatch,
            v2(ScriptedChatModel(steps=[denied]), await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path),
        )
        assert (await api.ask()).status_code == 424

    def test_classification(self) -> None:
        assert isinstance(stop_event(forbidden()), StopEventError)
        already = StopEventError(user_message="x", url="u", status_code=403, reason="r")
        assert stop_event(already) is already
        assert stop_event(forbidden(b"denied")) is None
        assert stop_event(forbidden(None)) is None
        assert stop_event(ValueError("x")) is None

    def test_sync_paths(self) -> None:
        guard = StopEventGuard()

        def raise_stop(_request: Any) -> Any:
            raise forbidden()

        def raise_other(_request: Any) -> Any:
            raise ValueError("другое")

        for hook in (guard.wrap_model_call, guard.wrap_tool_call):
            with pytest.raises(StopEventError):
                hook(None, raise_stop)
            with pytest.raises(ValueError, match="другое"):
                hook(None, raise_other)
            assert hook(None, lambda _r: "ok") == "ok"

    def test_place_in_stack(self, tmp_path: Path) -> None:
        stack = AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), store=InMemoryStore()).middleware()
        names = [type(m) for m in stack]
        # S2.8: Cancellation (§8 №3) sits between LanguageGuard and StopEventGuard
        assert names.index(Cancellation) == names.index(LanguageGuard) + 1
        assert names.index(StopEventGuard) == names.index(Cancellation) + 1
        # S2.5: retries and tool errors sit inside the guard, so a StopEvent is neither retried nor an envelope
        assert names.index(StopEventGuard) < names.index(ModelRetryMiddleware)
        assert names.index(StopEventGuard) < names.index(ToolErrorMiddleware) < names.index(ToolRetryMiddleware)
        assert names.index(CalcCache) < names.index(ToolErrorMiddleware)
        # S4.3: Telemetry right inside StopEventGuard (§8 №5), outside retries, cache and tool errors
        assert names.index(Telemetry) == names.index(StopEventGuard) + 1
        assert names.index(Telemetry) < names.index(ModelRetryMiddleware)
        assert names.index(Telemetry) < names.index(CalcCache) < names.index(ToolErrorMiddleware)
        # S2.7: compression right inside the outer guards (§8 №7), the meter innermost (design D5)
        assert names.index(CopilotSummarization) == names.index(Telemetry) + 1
        assert names[-1] is ContextMeter


class TestTurnLimitsAtApi:
    """FR-9 at the API: a looping model ends with HTTP 200 and a partial answer."""

    async def test_loop_gives_partial_answer(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "max_tool_calls", 3)
        monkeypatch.setattr(
            calc_tools,
            "analyze_excel_model",
            lambda **_kw: ExcelAnalysisToolResult(status="OK", result="1 сценарий", content={}),
        )
        args = {"inputs": [{"id": "IN1", "min": 300, "max": 300, "step": 1}], "outputs": ["OUT0"], "year": 2026}
        loop = [tool_call("run_scenarios", f"c{i}", **args) for i in range(5)]
        model = ScriptedChatModel(steps=[*loop[:4], AIMessage("Частичный ответ по полученному сценарию.")])
        api = Api(monkeypatch, v2(model, await uploaded_store("u1", INDEX, str(tmp_path)), tmp_path, plan_mode="off"))
        response = await api.ask("EBITDA 2026 при цене метанола 300?")
        assert response.status_code == 200
        assert answer_text(response) == "Частичный ответ по полученному сценарию."
        assert model.calls[-1]["tools"] == []


class TestIsolation:
    """AR-Q7 at the API: two users with the same x-session-id never share history."""

    async def test_same_session_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        (tmp_path / "user_file_u2.xlsx").write_bytes(b"workbook of u2")
        await store.aput(("memories", "u2"), "u2", {"filename": "user_file_u2.xlsx", "file_hash": "hash-u1"})
        seen: list[list[str]] = []

        def remember(messages: list[BaseMessage]) -> AIMessage:
            seen.append([m.text for m in messages if isinstance(m, HumanMessage) and not m.additional_kwargs])
            return AIMessage("ок")

        model = ScriptedChatModel(steps=[remember, remember, remember])
        api = Api(monkeypatch, v2(model, store, tmp_path, checkpointer=InMemorySaver()))
        await api.ask("Секрет первого пользователя", user="u1")
        await api.ask("Вопрос второго", user="u2")
        await api.ask("Снова первый", user="u1")
        assert seen == [
            ["Секрет первого пользователя"],
            ["Вопрос второго"],
            ["Секрет первого пользователя", "Снова первый"],
        ]


class TestCancellationAtApi:
    """AR-Q12 «Разрыв соединения», «Чекпоинт цел»; AR-Q10 «Ответ клиенту при таймауте» — at the API."""

    @staticmethod
    def engine(monkeypatch: pytest.MonkeyPatch, during_calc: Any) -> list[int]:
        runs: list[int] = []

        def run(**_kw: Any) -> ExcelAnalysisToolResult:
            runs.append(1)
            during_calc()
            return ExcelAnalysisToolResult(status="OK", result="1 сценарий", content={})

        monkeypatch.setattr(calc_tools, "analyze_excel_model", run)
        return runs

    async def test_disconnect_ends_turn_and_frees_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from aigw_service.api.v1 import router  # pylint: disable=import-outside-toplevel

        events: list[asyncio.Event] = []

        @contextlib.asynccontextmanager
        async def watch(_request: Any, event: asyncio.Event) -> Any:
            events.append(event)
            yield

        monkeypatch.setattr(router, "disconnect_watch", watch)
        runs = self.engine(monkeypatch, lambda: events[-1].set())  # the client leaves during the calculation
        args = {"inputs": [{"id": "IN1", "min": 300, "max": 300, "step": 1}], "outputs": ["OUT0"], "year": 2026}
        model = ScriptedChatModel(
            steps=[
                tool_call("run_scenarios", "c1", **args),
                tool_call("run_scenarios", "c2", **args),
                AIMessage("Привет!"),
            ]
        )
        locks = TurnLocks()
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        api = Api(monkeypatch, v2(model, store, tmp_path, locks=locks, plan_mode="off", checkpointer=InMemorySaver()))
        first = await api.ask("EBITDA 2026 при 300?", session="9b2f6c1e-1d2a-4b3c-8d4e-5f6a7b8c9d0e")
        assert first.status_code == 200
        assert answer_text(first) == CANCELLED_ANSWER
        assert len(runs) == 1
        assert len(model.calls) == 1
        assert locks.busy == frozenset()
        second = await api.ask(
            "Привет", session="9b2f6c1e-1d2a-4b3c-8d4e-5f6a7b8c9d0e"
        )  # the scripted model answers the next turn
        assert second.status_code == 200
        assert answer_text(second) == "Привет!"

    async def test_timeout_is_200_with_partial_answer(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.engine(monkeypatch, lambda: monkeypatch.setattr(cancellation, "_now", lambda: float("inf")))
        args = {"inputs": [{"id": "IN1", "min": 300, "max": 300, "step": 1}], "outputs": ["OUT0"], "year": 2026}
        model = ScriptedChatModel(steps=[tool_call("run_scenarios", "c1", **args), AIMessage("Частичный ответ.")])
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        api = Api(monkeypatch, v2(model, store, tmp_path, plan_mode="off"))
        response = await api.ask("EBITDA 2026 при 300?")
        assert response.status_code == 200
        assert answer_text(response) == "Частичный ответ."
        assert model.calls[-1]["tools"] == []
