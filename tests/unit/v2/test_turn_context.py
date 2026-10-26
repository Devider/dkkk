"""S2.2 (FR-1, FR-2): model registry and card, /upload at v2, TurnContext, LanguageGuard, end-to-end turn."""

import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware.language import LANGUAGE_REFUSAL, LanguageGuard, detect_language
from aigw_service.api.v2.middleware.turn_context import NO_MODEL_ANSWER, TurnContext
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.resolve_terms import Pick
from aigw_service.api.v2.tools.state import RESET, merge_terms
from aigw_service.api.v2.turn import registry as registry_module
from aigw_service.api.v2.turn.card import build_card
from aigw_service.api.v2.turn.registry import NAMESPACE, ModelRegistry
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX
from excel_engine import ExcelAnalysisToolResult

WORKBOOK = Path(__file__).resolve().parents[3] / "models" / "Methanex_FinModel.xlsx"
HEADERS = {
    "x-trace-id": "e037c70a-30d7-4c47-b25e-ce18c9c39f15",
    "x-client-id": "CI12345678",
    "x-request-time": "2026-10-26T11:31:45.748539+03:00",
    "x-session-id": "7d7c1d2e-3a4b-4c5d-8e9f-0a1b2c3d4e5f",
}


def answer(steps: int = 1) -> ScriptedChatModel:
    return ScriptedChatModel(steps=[AIMessage("Ответ по модели.")] * steps)


def agent_for(model: ScriptedChatModel, store: Any, upload_dir: Path, checkpointer: Any = None) -> Any:
    return AgentFactory(
        model, default_catalog(), store=store, upload_dir=str(upload_dir), checkpointer=checkpointer
    ).build()


async def turn(agent: Any, text: str = "Какая EBITDA в 2026?", user: str = "u1", **config: Any) -> dict:
    return await agent.ainvoke(
        {"messages": [("user", text)]}, context=RequestContext(user_id=user, session_id="s1"), config=config
    )


def system_text(model: ScriptedChatModel) -> str:
    return model.calls[0]["messages"][0].text


class TestRegistryAndCard:
    @pytest.fixture
    def builds(self, monkeypatch: pytest.MonkeyPatch) -> list:
        calls: list = []
        real = registry_module.build_model_index
        monkeypatch.setattr(registry_module, "build_model_index", lambda path: calls.append(path) or real(path))
        return calls

    async def test_register_once_and_get(self, builds: list) -> None:
        registry = ModelRegistry(InMemoryStore())
        first = await registry.register(WORKBOOK)
        second = await registry.register(WORKBOOK)
        assert first == second
        assert len(builds) == 1
        index, card = await registry.get(first)
        methanol = next(k for k, v in index.inputs.items() if v == "Метанол")
        assert index.input_values[methanol][2026] == pytest.approx(386.7, abs=0.1)
        assert card["years"].startswith("2018–")
        assert "EBITDA" in card["key_metrics"]
        assert any("DSCR" in m for m in card["key_metrics"])

    def test_card_balances_metrics(self) -> None:
        names = [f"DSCR вариант {i}" for i in range(10)] + ["EBITDA", "Выручка"]
        index = type(INDEX)(
            {}, {f"OUT{i}": n for i, n in enumerate(names)}, {}, {f"OUT{i}": (2026,) for i in range(len(names))}
        )
        card = build_card("m.xlsx", index)
        assert "EBITDA" in card["key_metrics"]
        assert "Выручка" in card["key_metrics"]


class TestUpload:
    """FR-1 «Загрузка при включённом v2»."""

    @pytest.fixture
    def env(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from aigw_service.api import app_main
        from aigw_service.api.v1 import router

        store = InMemoryStore()
        monkeypatch.setattr(APP_CTX.agent_memory, "store", store)
        user = "t" + uuid4().hex[:7]
        yield app_main, router, store, user
        for ext in ("xlsx",):
            (Path(tempfile.gettempdir()) / f"user_file_{user}.{ext}").unlink(missing_ok=True)

    async def upload(self, app: Any, user: str) -> int:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            files = {"file": ("Methanex_FinModel.xlsx", WORKBOOK.read_bytes())}
            response = await client.post("/api/v1/upload", files=files, headers={**HEADERS, "x-user-id": user})
        return response.status_code

    async def test_v2_registers_model(self, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        app, _router, store, user = env
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        assert await self.upload(app, user) == 200
        record = (await store.aget(("memories", user), user)).value
        assert record["file_hash"]
        assert (await ModelRegistry(store).get(record["file_hash"]))[1]["file_name"] == "Methanex_FinModel.xlsx"

    async def test_v1_upload_unchanged(self, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        app, _router, store, user = env
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v1")
        assert await self.upload(app, user) == 200
        assert (await store.aget(("memories", user), user)).value == {"filename": f"user_file_{user}.xlsx"}
        assert await store.asearch(NAMESPACE) == []

    async def test_registration_failure_keeps_upload(self, env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        app, router, store, user = env
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")

        async def broken(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("openpyxl failed")

        monkeypatch.setattr(router.ModelRegistry, "register", broken)
        assert await self.upload(app, user) == 200
        assert (await store.aget(("memories", user), user)).value["filename"] == f"user_file_{user}.xlsx"


class TestTermsReset:
    def test_merge_and_reset(self) -> None:
        assert merge_terms({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
        assert merge_terms({"a": 1}, {RESET: True}) == {}
        assert merge_terms({"a": 1}, {RESET: True, "c": 3}) == {"c": 3}


class TestTurnContext:
    """FR-1 «Ход начинается с книги и индекса модели»."""

    async def test_no_workbook_answers_without_llm(self, tmp_path: Path) -> None:
        model = answer()
        out = await turn(agent_for(model, InMemoryStore(), tmp_path))
        assert out["messages"][-1].content == NO_MODEL_ANSWER
        assert model.calls == []

    async def test_index_from_registry_without_opening_workbook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(registry_module, "build_model_index", lambda _p: pytest.fail("workbook opened"))
        seen: dict = {}

        def capture(messages: list[BaseMessage]) -> AIMessage:
            return AIMessage("ок")

        model = ScriptedChatModel(steps=[capture])
        store = await uploaded_store("u1", INDEX, str(tmp_path), file_name="Methanex_FinModel.xlsx")
        agent = agent_for(model, store, tmp_path)
        ctx = RequestContext(user_id="u1", session_id="s1")
        await agent.ainvoke({"messages": [("user", "EBITDA?")]}, context=ctx)
        seen.update(path=ctx.file_path, index=ctx.model_index)
        assert seen["path"] == str(tmp_path / "user_file_u1.xlsx")
        assert seen["index"].output_name("OUT0") == "EBITDA"
        assert "## Загруженная модель" in system_text(model)
        assert "Methanex_FinModel.xlsx" in system_text(model)

    async def test_workbook_uploaded_before_v2_is_indexed_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builds: list = []
        real = registry_module.build_model_index
        monkeypatch.setattr(registry_module, "build_model_index", lambda p: builds.append(p) or real(p))
        shutil.copy(WORKBOOK, tmp_path / "user_file_u1.xlsx")
        store = InMemoryStore()
        await store.aput(("memories", "u1"), "u1", {"filename": "user_file_u1.xlsx"})  # v1 upload: no hash
        agent = agent_for(answer(2), store, tmp_path)
        await turn(agent)
        await turn(agent)
        assert len(builds) == 1
        assert (await store.aget(("memories", "u1"), "u1")).value["file_hash"]

    async def test_new_turn_resets_turn_fields(self, tmp_path: Path) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        saver = InMemorySaver()
        cfg = {"configurable": {"thread_id": "u1:s1"}}
        steps = [
            tool_call("write_todos", todos=[{"content": "Посчитать", "status": "completed"}]),
            AIMessage("ок"),
            AIMessage("ок"),
        ]
        agent = agent_for(ScriptedChatModel(steps=steps), store, tmp_path, checkpointer=saver)
        first = await turn(agent, **cfg)
        assert first["todos"]
        second = await turn(agent, **cfg)
        assert second["todos"] == []
        assert second["step_results"] == []
        assert second["guard_counters"] == {}
        assert second["turn_id"] != first["turn_id"]

    def test_blocked_plan_survives(self) -> None:
        state = {"todos": [{"content": "Год?", "status": "blocked"}], "model_ref": {"file_hash": "h"}}
        update = TurnContext._turn_update(state, "h", {"file_name": "m.xlsx"})  # pylint: disable=protected-access
        assert "todos" not in update

    async def test_replaced_workbook_resets_terms_and_plan(self, tmp_path: Path) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        saver = InMemorySaver()
        cfg = {"configurable": {"thread_id": "u1:s1"}}
        model = ScriptedChatModel(steps=[AIMessage("ок"), AIMessage("ок")])
        agent = agent_for(model, store, tmp_path, checkpointer=saver)
        await turn(agent, **cfg)
        await agent.aupdate_state(cfg, {"resolved_terms": {"метанол": {"id": "IN1"}}})
        # the user uploads another workbook into the same session
        other = (await store.aget(NAMESPACE, "hash-u1")).value
        await store.aput(NAMESPACE, "hash-other", other)
        await store.aput(("memories", "u1"), "u1", {"filename": "user_file_u1.xlsx", "file_hash": "hash-other"})
        out = await turn(agent, **cfg)
        assert out["resolved_terms"] == {}
        assert "Модель заменена" in model.calls[1]["messages"][0].text
        assert "Модель заменена" not in model.calls[0]["messages"][0].text


LANGUAGE_CASES = [
    ("Какая EBITDA в 2026 году?", "ru"),
    ("Посчитай DSCR при цене copper (LME) 9000", "ru"),
    ("What is EBITDA in 2026 if methanol price drops by 20%?", "en"),
    ("Show DSCR and Debt/EBITDA for 2027", "en"),
    ("Wie hoch ist das EBITDA im Jahr 2026 und was ist der DSCR?", "other"),
    ("Quelle est la marge EBITDA pour 2026 avec le prix du méthanol?", "other"),
    ("¿Cuál es el EBITDA para 2026 con el precio del metanol?", "other"),
    ("Яка EBITDA у 2026 році?", "other"),
    ("2026 年的 EBITDA 是多少？", "other"),
    ("2026", "unknown"),
    ("ok", "unknown"),
    ("DSCR?", "unknown"),
]


class TestLanguage:
    """FR-2 «Ограничения языка и темы»."""

    @pytest.mark.parametrize(
        ("text", "expected"), LANGUAGE_CASES, ids=[c[1] + ":" + c[0][:20] for c in LANGUAGE_CASES]
    )
    def test_detect(self, text: str, expected: str) -> None:
        assert detect_language(text) == expected

    async def test_foreign_request_refused_without_llm(self, tmp_path: Path) -> None:
        model = answer()
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        out = await turn(agent_for(model, store, tmp_path), "Wie hoch ist das EBITDA im Jahr 2026?")
        assert out["messages"][-1].content == LANGUAGE_REFUSAL
        assert model.calls == []
        assert not [m for m in out["messages"] if isinstance(m, ToolMessage)]

    def test_order_and_no_workbook_wins(self) -> None:
        names = [type(m).__name__ for m in AgentFactory(answer(), default_catalog()).middleware()]
        assert names[:2] == ["TurnContext", "LanguageGuard"]
        assert isinstance(AgentFactory(answer(), default_catalog()).middleware()[1], LanguageGuard)

    async def test_no_workbook_and_foreign_language(self, tmp_path: Path) -> None:
        out = await turn(agent_for(answer(), InMemoryStore(), tmp_path), "Wie hoch ist das EBITDA im Jahr 2026?")
        assert out["messages"][-1].content == NO_MODEL_ANSWER


class TestEndToEnd:
    """Upload at v2 → /invoke-agent at v2: the tools get the Methanex index from the registry."""

    async def test_upload_then_turn(self, monkeypatch: pytest.MonkeyPatch, inline_gate: object) -> None:
        from aigw_service.api import app_main
        from aigw_service.api.v1 import router

        user = "t" + uuid4().hex[:7]
        monkeypatch.setattr(APP_CTX.agent_memory, "store", InMemoryStore())
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        monkeypatch.setattr(rt, "analyze_with_v1", self._analyzer)
        monkeypatch.setattr(
            calc_tools, "analyze_excel_model", lambda **kw: ExcelAnalysisToolResult(status="OK", result="", content={})
        )

        def scenarios(messages: list[BaseMessage]) -> AIMessage:
            data = json.loads(next(m for m in reversed(messages) if isinstance(m, ToolMessage)).content)["data"]
            ids = {t["text"]: t["candidates"][0]["id"] for t in data}
            return tool_call(
                "run_scenarios",
                "c2",
                inputs=[{"id": ids["метанол"], "min": 300, "max": 500, "step": 100}],
                outputs=[ids["EBITDA"]],
                year=2026,
            )

        steps = [
            tool_call("write_todos", "c0", todos=[{"content": "Сценарии EBITDA 2026", "status": "in_progress"}]),
            tool_call(
                "resolve_terms",
                "c1",
                terms=[{"text": "метанол", "role": "input"}, {"text": "EBITDA", "role": "output"}],
            ),
            scenarios,
            tool_call("write_todos", "c3", todos=[{"content": "Сценарии EBITDA 2026", "status": "completed"}]),
            AIMessage("EBITDA 2026 растёт с ценой метанола."),
        ]
        v2 = AgentV2(AgentFactory(ScriptedChatModel(steps=steps), default_catalog()).build())
        monkeypatch.setitem(app_main.dependency_overrides, router.get_agent, lambda: v2)
        monkeypatch.setitem(app_main.dependency_overrides, router.get_cb_handler(), lambda: None)
        headers = {**HEADERS, "x-user-id": user}
        try:
            async with AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
                missing = await client.post("/api/v1/invoke-agent", json={"message": "EBITDA?"}, headers=headers)
                files = {"file": ("Methanex_FinModel.xlsx", WORKBOOK.read_bytes())}
                assert (await client.post("/api/v1/upload", files=files, headers=headers)).status_code == 200
                response = await client.post(
                    "/api/v1/invoke-agent", json={"message": "EBITDA 2026 при цене метанола 300–500?"}, headers=headers
                )
        finally:
            (Path(tempfile.gettempdir()) / f"user_file_{user}.xlsx").unlink(missing_ok=True)
        assert zipfile.ZipFile(io.BytesIO(missing.content)).read("txt_response.txt").decode() == NO_MODEL_ANSWER
        assert (
            zipfile.ZipFile(io.BytesIO(response.content)).read("txt_response.txt").decode()
            == "EBITDA 2026 растёт с ценой метанола."
        )
        assert inline_gate.sessions == [f"{user}:{HEADERS['x-session-id']}"]

    @staticmethod
    async def _analyzer(_message: str, index: Any) -> list[Pick]:
        methanol = next(k for k, v in index.inputs.items() if v == "Метанол")
        ebitda = next(k for k, v in index.outputs.items() if v == "EBITDA")
        return [Pick("input", "метанол", methanol), Pick("output", "EBITDA", ebitda)]
