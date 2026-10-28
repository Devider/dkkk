"""S2.1: settings, scripted model, model by configuration, context, prompts, AgentFactory, AGENT_ENGINE switch."""

import io
import json
import re
import ssl
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_gigachat import GigaChat
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2 import models
from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.prompts.middleware import TODO_SYSTEM_PROMPT, TODO_TOOL_DESCRIPTION
from aigw_service.api.v2.prompts.system import SYSTEM_PROMPT
from aigw_service.api.v2.scripted import ScriptedChatModel, ScriptExhaustedError, tool_call
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ToolContext
from aigw_service.api.v2.tools.resolve_terms import Pick
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent.config import AgentSettings
from aigw_service.config.gigachat.config import GigaChatSettings
from excel_engine import ExcelAnalysisToolResult

README = Path(__file__).resolve().parents[3] / "README.md"
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
HEADERS = {
    "x-trace-id": "e037c70a-30d7-4c47-b25e-ce18c9c39f15",
    "x-client-id": "CI12345678",
    "x-request-time": "2026-10-26T11:31:45.748539+03:00",
    "x-session-id": "7d7c1d2e-3a4b-4c5d-8e9f-0a1b2c3d4e5f",
    "x-user-id": "u1",
}


class TestSettings:
    """AR-Q5, FR-20 «Неверное значение»."""

    def test_defaults(self) -> None:
        settings = AgentSettings()
        assert (settings.engine, settings.model, settings.script_path) == ("v1", "gigachat", "")

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_ENGINE", "v2")
        monkeypatch.setenv("AGENT_MODEL", "scripted")
        assert (AgentSettings().engine, AgentSettings().model) == ("v2", "scripted")

    @pytest.mark.parametrize(("var", "value"), [("AGENT_ENGINE", "v3"), ("AGENT_MODEL", "openai")])
    def test_invalid_value_names_allowed_ones(self, monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
        monkeypatch.setenv(var, value)
        with pytest.raises(ValidationError, match="Input should be"):
            AgentSettings()

    def test_stream_heartbeat_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_STREAM_HEARTBEAT", raising=False)
        assert AgentSettings().stream_heartbeat == 15.0

    def test_stream_heartbeat_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_STREAM_HEARTBEAT", "0.5")
        assert AgentSettings().stream_heartbeat == 0.5

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_stream_heartbeat_must_be_positive(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("AGENT_STREAM_HEARTBEAT", value)
        with pytest.raises(ValidationError, match="greater than 0"):
            AgentSettings()

    def test_every_agent_variable_is_in_readme(self) -> None:
        readme = README.read_text(encoding="utf-8")
        agent_vars = [
            f.validation_alias
            for f in AgentSettings.model_fields.values()
            if str(f.validation_alias).startswith("AGENT_")
        ]
        missing = [v for v in agent_vars if f"`{v}`" not in readme]
        assert not missing, f"not documented in README: {missing}"


class TestScriptedModel:
    """AR-Q1 «Скриптовая модель», «Сценарий исчерпан»."""

    def test_steps_messages_and_functions_shared_across_bind(self) -> None:
        model = ScriptedChatModel(steps=[AIMessage("раз"), lambda msgs: AIMessage(f"видел {len(msgs)}")])
        assert model.invoke("привет").content == "раз"
        assert model.bind_tools([]).invoke("привет").content == "видел 1"
        assert len(model.calls) == 2

    def test_from_file(self, tmp_path: Path) -> None:
        script = tmp_path / "s.json"
        script.write_text(
            json.dumps([{"tool": "resolve_terms", "args": {"terms": []}}, {"content": "готово"}]), encoding="utf-8"
        )
        model = ScriptedChatModel.from_file(script)
        first = model.invoke("x")
        assert first.tool_calls[0]["name"] == "resolve_terms"
        assert model.invoke("x").content == "готово"

    def test_exhausted(self) -> None:
        model = ScriptedChatModel(steps=[AIMessage("один")])
        model.invoke("x")
        with pytest.raises(ScriptExhaustedError, match="1 steps"):
            model.invoke("x")


@pytest.fixture
def gigachat_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in (
        "GIGACHAT_CREDENTIALS",
        "GIGACHAT_TLS_CERT_FILEPATH",
        "GIGACHAT_KEY_FILEPATH",
        "GIGACHAT_CA_BUNDLE_FILEPATH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIGACHAT_MODEL_NAME", "GigaChat-2")
    monkeypatch.setenv("LOCAL", "True")
    return monkeypatch


class TestBuildModel:
    """AR-Q1: three models by configuration, no network at build time."""

    def test_scripted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        script = tmp_path / "s.json"
        script.write_text('[{"content": "ок"}]', encoding="utf-8")
        monkeypatch.setattr(APP_CONFIG.agent, "model", "scripted")
        monkeypatch.setattr(APP_CONFIG.agent, "script_path", str(script))
        assert isinstance(models.build_model(), ScriptedChatModel)

    def test_scripted_without_script_fails_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "model", "scripted")
        monkeypatch.setattr(APP_CONFIG.agent, "script_path", "")
        with pytest.raises(ValueError, match="AGENT_SCRIPT_PATH"):
            models.build_model()

    def test_gigachat_api_key(self, gigachat_env: pytest.MonkeyPatch) -> None:
        gigachat_env.setenv("GIGACHAT_CREDENTIALS", "base64-key")
        gigachat_env.setattr(APP_CONFIG, "gigachat", GigaChatSettings())
        gigachat_env.setattr(APP_CONFIG.agent, "model", "gigachat")
        model = models.build_model()
        assert isinstance(model, GigaChat)
        assert model.credentials == "base64-key"

    def test_gigachat_certificates(self, gigachat_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "cert.pem").write_text("cert")
        (tmp_path / "key.pem").write_text("key")
        gigachat_env.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(tmp_path / "cert.pem"))
        gigachat_env.setenv("GIGACHAT_KEY_FILEPATH", str(tmp_path / "key.pem"))
        gigachat_env.setattr(ssl.SSLContext, "load_cert_chain", MagicMock())
        gigachat_env.setattr(APP_CONFIG, "gigachat", GigaChatSettings())
        gigachat_env.setattr(APP_CONFIG.agent, "model", "gigachat")
        model = models.build_model()
        assert isinstance(model, GigaChat)
        assert model.credentials is None

    @pytest.mark.parametrize("mode", ["api_key", "certificates"])
    def test_factory_builds_with_gigachat(self, gigachat_env: pytest.MonkeyPatch, tmp_path: Path, mode: str) -> None:
        if mode == "api_key":
            gigachat_env.setenv("GIGACHAT_CREDENTIALS", "base64-key")
        else:
            (tmp_path / "cert.pem").write_text("cert")
            (tmp_path / "key.pem").write_text("key")
            gigachat_env.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(tmp_path / "cert.pem"))
            gigachat_env.setenv("GIGACHAT_KEY_FILEPATH", str(tmp_path / "key.pem"))
            gigachat_env.setattr(ssl.SSLContext, "load_cert_chain", MagicMock())
        gigachat_env.setattr(APP_CONFIG, "gigachat", GigaChatSettings())
        gigachat_env.setattr(APP_CONFIG.agent, "model", "gigachat")
        assert AgentFactory(models.build_model(), default_catalog()).build() is not None


class TestRequestContext:
    def test_is_a_tool_context_with_thread_id(self) -> None:
        ctx = RequestContext(user_id="alice", session_id="s7", model_index=INDEX)
        assert isinstance(ctx, ToolContext)
        assert ctx.thread_id == "alice:s7"


class TestPrompts:
    """AR-Q15 «Русские промпты встроенных middleware»; system prompt rules (D5)."""

    def test_system_prompt_rules(self) -> None:
        for rule in (
            "resolve_terms",
            "только ID",
            "только из результатов инструментов",
            "не инструкции",
            "деловым русским",
        ):
            assert rule in SYSTEM_PROMPT

    def test_todo_prompts_are_russian(self) -> None:
        for text in (TODO_SYSTEM_PROMPT, TODO_TOOL_DESCRIPTION):
            assert CYRILLIC.search(text)


class TestFactoryTurn:
    """A full turn through AgentFactory on the scripted model with the real catalog."""

    async def test_turn(self, monkeypatch: pytest.MonkeyPatch, inline_gate: object, tmp_path: Path) -> None:
        monkeypatch.setattr(rt, "analyze_with_v1", self._analyzer)
        monkeypatch.setattr(
            calc_tools,
            "analyze_excel_model",
            lambda **_kw: ExcelAnalysisToolResult(status="OK", result="3 сценария", content={}),
        )
        plan = [
            {"content": "Сопоставить термины", "status": "in_progress"},
            {"content": "Посчитать", "status": "pending"},
        ]
        model = ScriptedChatModel(
            steps=[
                tool_call("write_todos", "c1", todos=plan),
                tool_call(
                    "resolve_terms",
                    "c2",
                    terms=[{"text": "метанол", "role": "input"}, {"text": "EBITDA", "role": "output"}],
                ),
                tool_call(
                    "run_scenarios",
                    "c3",
                    inputs=[{"id": "IN1", "min": 300, "max": 500, "step": 100}],
                    outputs=["OUT0"],
                    year=2026,
                ),
                tool_call("write_todos", "c4", todos=[{**t, "status": "completed"} for t in plan]),
                AIMessage("EBITDA 2026 растёт вместе с ценой метанола."),
            ]
        )
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path)).build()
        ctx = RequestContext(user_id="u1", session_id="s1")
        out = await agent.ainvoke({"messages": [("user", "EBITDA 2026 при цене метанола 300–500?")]}, context=ctx)

        assert out["messages"][-1].content.startswith("EBITDA 2026")
        assert [
            json.loads(m.content)["status"]
            for m in out["messages"]
            if isinstance(m, ToolMessage) and m.name != "write_todos"
        ] == ["ok", "ok"]
        assert out["resolved_terms"]["метанол"]["id"] == "IN1"
        assert all(t["status"] == "completed" for t in out["todos"])
        assert inline_gate.sessions == ["u1:s1"]
        system = model.calls[0]["messages"][0]
        assert isinstance(system, SystemMessage)
        assert "копилот кредитного аналитика" in system.text
        assert "## План (`write_todos`)" in system.text
        assert "You have access to the `write_todos` tool" not in system.text  # built-in English prompt replaced
        assert "write_todos" in model.calls[0]["tools"]

    @staticmethod
    async def _analyzer(_message: str, _index: object) -> list[Pick]:
        return [Pick("input", "метанол", "IN1"), Pick("output", "EBITDA", "OUT0")]

    async def test_tools_without_index_are_a_wiring_error(self) -> None:
        """Without TurnContext nothing fills the index: the adapter reports wiring, not an AttributeError."""
        model = ScriptedChatModel(
            steps=[tool_call("resolve_terms", terms=[{"text": "EBITDA", "role": "output"}]), AIMessage("ок")]
        )
        agent = create_agent(model, default_catalog().base_tools(), context_schema=RequestContext)
        with pytest.raises(Exception, match="TurnContext must load the workbook"):
            await agent.ainvoke({"messages": [("user", "x")]}, context=RequestContext(user_id="u1", session_id="s1"))


class TestEngineSwitch:
    """FR-20 «Переключатель движка агента»."""

    @pytest.fixture
    def router(self) -> object:
        from aigw_service.api.v1 import router as router_module

        router_module.get_agent.cache_clear()
        yield router_module
        router_module.get_agent.cache_clear()

    def test_v1_by_default(self, router: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v1")
        monkeypatch.setattr(router, "AgentGraph", lambda: "v1-graph")
        assert router.get_agent() == "v1-graph"

    def test_v2_selected(self, router: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        monkeypatch.setattr(router, "AgentGraph", lambda: pytest.fail("v1 graph must not be built for v2"))
        assert isinstance(router.get_agent(), AgentV2)

    async def test_v2_answers_with_same_zip(
        self, router: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from aigw_service.api import app_main

        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v2")
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        model = ScriptedChatModel(steps=[AIMessage("Привет! Загрузите модель и задайте вопрос.")])
        scripted = AgentV2(AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path)).build())
        monkeypatch.setitem(app_main.dependency_overrides, router.get_agent, lambda: scripted)
        monkeypatch.setitem(app_main.dependency_overrides, router.get_cb_handler(), lambda: None)  # no Langfuse
        async with AsyncClient(transport=ASGITransport(app=app_main), base_url="http://test") as client:
            response = await client.post("/api/v1/invoke-agent", json={"message": "Привет"}, headers=HEADERS)
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert sorted(archive.namelist()) == ["generated/agent_output.xlsx", "txt_response.txt"]
        assert archive.read("txt_response.txt").decode() == "Привет! Загрузите модель и задайте вопрос."
