"""S4.3 (AR-Q13): Telemetry log lines and the Langfuse run tree of a v2 turn."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest
from langchain.agents.middleware import ModelResponse
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_api import Api, answer_text
from tests.unit.v2.test_context_meter import full_request
from tests.unit.v2.test_memory import STEPS, plan
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_turn_context import HEADERS

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import telemetry
from aigw_service.api.v2.middleware.telemetry import TOOL_ERROR, Telemetry, token_usage, tool_outcome
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.service import AgentV2
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.turn.lock import TurnLocks
from aigw_service.config import APP_CONFIG
from aigw_service.exceptions import StopEventError
from excel_engine import ExcelAnalysisToolResult

SCENARIOS = {"inputs": [{"id": "IN1", "min": 300, "max": 500, "step": 100}], "outputs": ["OUT0"], "year": 2026}
USAGE = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}


@pytest.fixture
def lines(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    """``LLM CALL`` and ``TOOL CALL`` records written by Telemetry."""
    logged: dict[str, list[dict]] = {"LLM CALL": [], "TOOL CALL": []}

    def info(message: str, *args: Any, **_kw: Any) -> None:  # the logger is shared, other callers pass kwargs
        for prefix, records in logged.items():
            if message.startswith(prefix):
                records.append(json.loads(args[0]))

    monkeypatch.setattr(telemetry.logger, "info", info)
    return logged


def stop_event() -> StopEventError:
    return StopEventError(user_message="недоступна {секрет}", url="https://giga", status_code=403, reason="тело")


def tool_request(name: str = "run_scenarios") -> ToolCallRequest:
    runtime = SimpleNamespace(context=RequestContext(user_id="u1", session_id="s1", trace_id="tr-1"))
    return ToolCallRequest(
        tool_call={"name": name, "id": "c1", "args": {}}, tool=None, state={"turn_id": "t1"}, runtime=runtime
    )


def envelope(status: str, **fields: Any) -> str:
    return json.dumps({"status": status, **fields}, ensure_ascii=False)


async def run_turn(tmp_path: Path, steps: list, trace_id: str = "tr-7", **invoke: Any) -> tuple[dict, Any]:
    store = await uploaded_store("u1", INDEX, str(tmp_path))
    model = ScriptedChatModel(steps=steps)
    agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path), plan_mode="off").build()
    out = await agent.ainvoke(
        {"messages": [HumanMessage("EBITDA 2026 при метаноле 300–500?")]},
        context=RequestContext(user_id="u1", session_id="s1", trace_id=trace_id),
        **invoke,
    )
    return out, model


class TestModelLine:
    """AR-Q13 «Строка на каждый вызов модели», «Ошибка вызова модели»."""

    def test_answer_with_tokens_and_tools(self, lines: dict) -> None:
        answer = AIMessage("", tool_calls=[{"name": "resolve_terms", "args": {}, "id": "r1"}], usage_metadata=USAGE)
        response = ModelResponse(result=[answer])
        assert Telemetry().wrap_model_call(full_request(), lambda _r: response) is response
        (record,) = lines["LLM CALL"]
        assert (record["trace_id"], record["turn_id"], record["call"]) == ("tr-1", "t1", 6)  # as CONTEXT LAYERS
        assert record["token_usage"] == USAGE
        assert record["tool_calls"] == ["resolve_terms"]
        assert record["error"] is None
        assert record["elapsed_s"] >= 0

    def test_no_usage_metadata(self) -> None:
        assert token_usage(AIMessage("ответ")) == {}
        assert token_usage(None) == {}

    async def test_error_code_without_text(self, lines: dict) -> None:
        async def fail(_request: Any) -> Any:
            raise stop_event()

        with pytest.raises(StopEventError):
            await Telemetry().awrap_model_call(full_request(), fail)
        (record,) = lines["LLM CALL"]
        assert record["error"] == "StopEventError"
        assert "секрет" not in json.dumps(record, ensure_ascii=False)

    def test_sync_error(self, lines: dict) -> None:
        def fail(_request: Any) -> Any:
            raise ValueError("текст {ошибки}")

        with pytest.raises(ValueError, match="ошибки"):
            Telemetry().wrap_model_call(full_request(), fail)
        assert lines["LLM CALL"][0]["error"] == "ValueError"


class TestToolLine:
    """AR-Q13 «Строка на каждый вызов инструмента»."""

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (ToolMessage(envelope("ok", result="3 сценария"), tool_call_id="c1"), ("ok", None)),
            (ToolMessage(envelope("error", code="unknown_id"), tool_call_id="c1"), ("error", "unknown_id")),
            (ToolMessage("План обновлён", tool_call_id="c1"), ("ok", None)),  # write_todos: plain text
            (ToolMessage("Error: invalid", tool_call_id="c1", status="error"), ("error", TOOL_ERROR)),
            (
                Command(update={"messages": [ToolMessage(envelope("error", code="no_values"), tool_call_id="c1")]}),
                ("error", "no_values"),
            ),
            (Command(update={"todos": []}), ("ok", None)),
        ],
    )
    def test_outcome(self, result: Any, expected: tuple) -> None:
        assert tool_outcome(result) == expected

    def test_sync_line(self, lines: dict) -> None:
        message = ToolMessage(envelope("error", code="unknown_id"), tool_call_id="c1")
        assert Telemetry().wrap_tool_call(tool_request(), lambda _r: message) is message
        (record,) = lines["TOOL CALL"]
        assert record == {**record, "trace_id": "tr-1", "turn_id": "t1", "tool": "run_scenarios"}
        assert (record["status"], record["code"]) == ("error", "unknown_id")

    async def test_exception_passes_through(self, lines: dict) -> None:
        async def fail(_request: Any) -> Any:
            raise stop_event()

        with pytest.raises(StopEventError):
            await Telemetry().awrap_tool_call(tool_request(), fail)
        (record,) = lines["TOOL CALL"]
        assert (record["status"], record["code"]) == ("error", "StopEventError")
        assert "секрет" not in json.dumps(record, ensure_ascii=False)

    def test_sync_exception(self, lines: dict) -> None:
        def fail(_request: Any) -> Any:
            raise RuntimeError("текст")

        with pytest.raises(RuntimeError):
            Telemetry().wrap_tool_call(tool_request(), fail)
        assert lines["TOOL CALL"][0]["code"] == "RuntimeError"


class TestOnAgent:
    """AR-Q13 on a whole v2 turn: one line per call, envelope codes, cache hits, no lines without LLM."""

    async def test_turn_lines(self, tmp_path: Path, domain: list, lines: dict) -> None:
        steps = [
            tool_call("resolve_terms", "r1", terms=[{"text": "метанол", "role": "input"}]),
            tool_call("run_scenarios", "c1", **SCENARIOS),
            tool_call("run_scenarios", "c2", **SCENARIOS),  # CalcCache hit
            tool_call("run_scenarios", "c3", **{**SCENARIOS, "outputs": ["OUT99"]}),  # unknown_id
            AIMessage("EBITDA 2026: 1350.5.", usage_metadata=USAGE),
        ]
        out, _ = await run_turn(tmp_path, steps)
        models = lines["LLM CALL"]
        assert [r["call"] for r in models] == [1, 2, 3, 4, 5]
        assert {r["turn_id"] for r in models} == {out["turn_id"]}
        assert {r["trace_id"] for r in models} == {"tr-7"}
        assert models[0]["tool_calls"] == ["resolve_terms"]
        assert models[-1]["token_usage"] == USAGE
        tools = [(r["tool"], r["status"], r["code"]) for r in lines["TOOL CALL"]]
        assert tools == [
            ("resolve_terms", "ok", None),
            ("run_scenarios", "ok", None),
            ("run_scenarios", "ok", None),
            ("run_scenarios", "error", "unknown_id"),
        ]
        assert len(domain) == 1  # the second call came from the cache

    @pytest.mark.parametrize("case", ["no_workbook", "language"])
    async def test_no_llm_no_lines(self, tmp_path: Path, lines: dict, case: str) -> None:
        model = ScriptedChatModel(steps=[])
        store = InMemoryStore() if case == "no_workbook" else await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path)).build()
        text = "Вопрос" if case == "no_workbook" else "Wie hoch ist das EBITDA im Jahr 2026 bei diesem Preis?"
        await agent.ainvoke({"messages": [HumanMessage(text)]}, context=RequestContext(user_id="u1", session_id="s1"))
        assert model.calls == []
        assert lines == {"LLM CALL": [], "TOOL CALL": []}


# ---------------------------------------------------------------- Langfuse (S4.3 design D2, D3)


class RunTree(BaseCallbackHandler):
    """Records every run the Langfuse handler would get: id, parent, name, kind and metadata."""

    run_inline = True

    def __init__(self) -> None:
        self.runs: dict[UUID, dict[str, Any]] = {}

    def _add(self, kind: str, label: str | None, run_id: UUID, parent: UUID | None, kw: dict[str, Any]) -> None:
        self.runs[run_id] = {"kind": kind, "name": label, "parent": parent, "metadata": kw.get("metadata") or {}}

    def on_chain_start(
        self, serialized: Any, inputs: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kw: Any
    ) -> None:
        self._add("chain", kw.get("name"), run_id, parent_run_id, kw)

    def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kw: Any
    ) -> None:
        self._add("llm", kw.get("name") or (serialized or {}).get("name"), run_id, parent_run_id, kw)

    def on_tool_start(
        self, serialized: Any, input_str: str, *, run_id: UUID, parent_run_id: UUID | None = None, **kw: Any
    ) -> None:
        self._add("tool", kw.get("name") or (serialized or {}).get("name"), run_id, parent_run_id, kw)

    def root(self) -> dict[str, Any]:
        (root,) = [r for r in self.runs.values() if r["parent"] is None]
        return root

    def ancestors(self, run_id: UUID) -> list[dict[str, Any]]:
        chain, parent = [], self.runs[run_id]["parent"]
        while parent is not None:
            chain.append(self.runs[parent])
            parent = self.runs[parent]["parent"]
        return chain

    def named(self, kind: str, name: str) -> list[UUID]:
        return [i for i, r in self.runs.items() if r["kind"] == kind and r["name"] == name]


class FakeSemanticLayer:
    """Stands for the v1 analyzer: calls a chat model without an explicit config, as the real one does."""

    def __init__(self) -> None:
        self.llm = FakeListChatModel(responses=["{}"], name="semantic_llm")

    async def ainvoke(self, messages: list, _inputs: dict, _outputs: dict) -> dict:
        await self.llm.ainvoke(messages)
        return {"q_analysis": None, "resolved_inputs": []}


STREAM_URL = "/api/v1/invoke-agent/stream"


class FakeV1:
    """The v1 branch of the router: ``agent.graph.ainvoke`` with the config it got."""

    def __init__(self) -> None:
        self.configs: list[dict] = []
        self.graph = self

    async def ainvoke(self, _state: dict, config: dict) -> dict:
        self.configs.append(config)
        return {"messages": [AIMessage("Ответ v1.")]}


class RecordingV2(AgentV2):
    def __init__(self, agent: Any) -> None:
        super().__init__(agent, locks=TurnLocks())
        self.configs: list[dict] = []

    async def answer(self, message: str, context: RequestContext, config: dict[str, Any]) -> str:
        self.configs.append(config)
        return await super().answer(message, context, config)


def fake_engine(**_kw: Any) -> ExcelAnalysisToolResult:
    frame = pd.DataFrame({"Метанол": [300, 400, 500], "EBITDA": [1200.0, 1350.5, 1501.0]})
    return ExcelAnalysisToolResult(status="OK", result="", content={"result_df": frame})


def with_handler(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    from aigw_service.api import app_main  # pylint: disable=import-outside-toplevel
    from aigw_service.api.v1 import router  # pylint: disable=import-outside-toplevel

    monkeypatch.setitem(app_main.dependency_overrides, router.get_cb_handler(), lambda: handler)


def assert_langfuse_metadata(metadata: dict, engine: str) -> None:
    assert metadata["langfuse_session_id"] == "u1:" + HEADERS["x-session-id"]
    assert metadata["langfuse_user_id"] == "u1"
    assert metadata["langfuse_tags"] == [f"agent_engine={engine}"]
    assert not any("tmp" in str(v) or ".xlsx" in str(v) for v in metadata.values())  # no file path


class TestLangfuse:
    """AR-Q13 «Метаданные трассы», «Вложенный вызов смыслового слоя», «Дерево хода», «Langfuse выключен»."""

    async def test_v1_metadata(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        agent = FakeV1()
        api = Api(monkeypatch, agent)  # type: ignore[arg-type]  # the router takes v1 by duck typing
        monkeypatch.setattr(APP_CONFIG.agent, "engine", "v1")
        response = await api.ask()
        assert response.status_code == 200
        assert_langfuse_metadata(agent.configs[0]["metadata"], "v1")

    async def test_v1_stream_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AR-Q18 (S6.5 D5): a turn started through the stream endpoint carries ``stream=true``."""
        agent = FakeV1()
        api = Api(monkeypatch, agent)  # type: ignore[arg-type]
        response = await api.client.post(STREAM_URL, json={"message": "Привет"}, headers=HEADERS)
        assert response.status_code == 200
        assert agent.configs[0]["metadata"]["langfuse_tags"] == ["agent_engine=v1", "stream=true"]

    async def test_v2_stream_tag(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        configs: list[dict] = []

        class StreamRecordingV2(AgentV2):
            def stream(self, message: str, context: RequestContext, config: dict[str, Any]) -> Any:
                configs.append(config)
                return super().stream(message, context, config)

        store = await uploaded_store("u1", INDEX, str(tmp_path))
        model = ScriptedChatModel(steps=[AIMessage("Привет!")])
        agent = StreamRecordingV2(
            AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path)).build(), TurnLocks()
        )
        response = await Api(monkeypatch, agent).client.post(STREAM_URL, json={"message": "Привет"}, headers=HEADERS)
        assert response.status_code == 200
        assert configs[0]["metadata"]["langfuse_tags"] == ["agent_engine=v2", "stream=true"]

    async def test_v2_tree(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: dict) -> None:
        monkeypatch.setattr(rt, "_ema_analyzer", FakeSemanticLayer)
        monkeypatch.setattr(calc_tools, "analyze_excel_model", fake_engine)
        steps = [
            tool_call("write_todos", "p1", todos=STEPS),
            tool_call("resolve_terms", "r1", terms=[{"text": "метанол", "role": "input"}]),
            tool_call("run_scenarios", "c1", **SCENARIOS),
            tool_call("write_todos", "p2", todos=plan("completed")),
            AIMessage("EBITDA 2026: 1350.5."),
        ]
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = RecordingV2(
            AgentFactory(
                ScriptedChatModel(steps=steps), default_catalog(), store=store, upload_dir=str(tmp_path)
            ).build()
        )
        tree = RunTree()
        api = Api(monkeypatch, agent)
        with_handler(monkeypatch, tree)
        response = await api.ask("EBITDA 2026 при метаноле 300?")
        assert response.status_code == 200
        assert agent.configs[0]["callbacks"] == [tree]
        assert_langfuse_metadata(tree.root()["metadata"], "v2")
        for tool in ("write_todos", "resolve_terms", "run_scenarios"):
            assert tree.named("tool", tool), tool
        assert any(r["kind"] == "chain" and "TurnSummary" in (r["name"] or "") for r in tree.runs.values())
        model_runs = [r for r in tree.runs.values() if r["kind"] == "llm" and r["name"] != "semantic_llm"]
        assert len(model_runs) == len(steps)
        (semantic,) = tree.named("llm", "semantic_llm")
        assert "resolve_terms" in [r["name"] for r in tree.ancestors(semantic) if r["kind"] == "tool"]
        assert len(lines["LLM CALL"]) == len(steps)

    async def test_disabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lines: dict) -> None:
        monkeypatch.setattr(calc_tools, "analyze_excel_model", fake_engine)
        steps = [
            tool_call("run_scenarios", "c1", **SCENARIOS),
            AIMessage("EBITDA 2026: 1350.5."),
        ]
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = RecordingV2(
            AgentFactory(
                ScriptedChatModel(steps=steps),
                default_catalog(),
                store=store,
                upload_dir=str(tmp_path),
                plan_mode="off",
            ).build()
        )
        response = await Api(monkeypatch, agent).ask()  # Api turns the handler off
        assert response.status_code == 200
        assert answer_text(response) == "EBITDA 2026: 1350.5."
        assert "callbacks" not in agent.configs[0]
        assert len(lines["LLM CALL"]) == 2
        assert [r["tool"] for r in lines["TOOL CALL"]] == ["run_scenarios"]
