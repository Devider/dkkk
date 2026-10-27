"""S1.6 (AR-Q6, AR-Q7, ADR-7): long results go to /results/, the model gets a compact envelope and reads the rest."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_prechecks import INDEX

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools import calc_tools
from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.adapter import step_entry
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.envelope import ErrorCode, Number, ToolSuccess, failure, to_json
from aigw_service.api.v2.tools.offload import NOT_SAVED_NOTE, compact, offload, result_path, results_namespace
from aigw_service.api.v2.tools.resolve_terms import Pick
from aigw_service.api.v2.tools.state import RESET, STEPS_RESET, merge_steps
from aigw_service.config import APP_CONFIG
from aigw_service.config.agent import AgentSettings
from excel_engine import ExcelAnalysisToolResult, ModelInputAnalysisToolResult

LIMIT = APP_CONFIG.agent.result_max_chars
GRID = [{"id": "IN1", "min": 300, "max": 500, "step": 1}]  # 201 scenarios
SCENARIOS = {"inputs": GRID, "outputs": ["OUT0"], "year": 2026}
TERMS = [{"text": "метанол", "role": "input"}, {"text": "EBITDA", "role": "output"}]


def big(count: int = 400, notes: list[str] | None = None) -> ToolSuccess:
    numbers = [Number(value=float(i), label=f"EBITDA при Метанол = {i}") for i in range(count)]
    return ToolSuccess(
        result=f"Сценариев: {count}.",
        data={"rows": [{"Метанол": i, "EBITDA": float(i)} for i in range(count)]},
        numbers=numbers,
        notes=notes or [],
    )


class FailingStore:
    async def aget(self, *_a: Any, **_kw: Any) -> None:
        raise OSError("store is down")

    async def aput(self, *_a: Any, **_kw: Any) -> None:
        raise OSError("store is down")


class TestSettings:
    """AR-Q5 «Недопустимый порог выноса»."""

    def test_default(self) -> None:
        assert AgentSettings().result_max_chars == 1500

    def test_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_RESULT_MAX_CHARS", "4000")
        assert AgentSettings().result_max_chars == 4000

    def test_too_small_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_RESULT_MAX_CHARS", "499")
        with pytest.raises(ValidationError, match="greater than or equal to 500"):
            AgentSettings()


class TestPathAndNamespace:
    def test_path(self) -> None:
        assert result_path("t1", "call_1") == "/results/t1/call_1.json"
        assert result_path("../x", "a/b") == "/results/___x/a_b.json"
        assert result_path(None, "") == "/results/turn/call.json"

    def test_namespace_is_per_user(self) -> None:
        assert results_namespace("u1") == ("results", "u1")


class TestCompact:
    @pytest.mark.parametrize("limit", [500, 1500, 5000])
    def test_never_longer_than_limit(self, limit: int) -> None:
        small = compact(big(), "/results/t1/c1.json", limit)
        assert len(to_json(small)) <= limit
        assert small.data is None
        assert small.ref == "/results/t1/c1.json"
        assert small.numbers == big().numbers[: len(small.numbers)]
        assert small.notes[-1] == f"Показаны первые {len(small.numbers)} из 400 чисел; полный список — в ref."

    def test_fits_without_cut(self) -> None:
        small = compact(big(5), "/results/t1/c1.json", 1500)
        assert small.numbers == big(5).numbers
        assert small.notes == []

    def test_long_summary_is_truncated(self) -> None:
        envelope = big().model_copy(update={"result": "Очень длинная сводка. " * 200})
        small = compact(envelope, "/results/t1/c1.json", 500)
        assert len(to_json(small)) <= 500
        assert small.numbers == []
        assert small.result.endswith("…")
        assert small.ref == "/results/t1/c1.json"

    def test_long_notes_keep_ref(self) -> None:
        small = compact(big(notes=["оговорка " * 300]), "/results/t1/c1.json", 500)
        assert len(to_json(small)) <= 500
        assert small.ref == "/results/t1/c1.json"


class TestOffload:
    async def test_short_is_unchanged_and_not_written(self) -> None:
        store = InMemoryStore()
        envelope = big(3)
        assert await offload(envelope, store=store, user_id="u1", turn_id="t1", call_id="c1", limit=LIMIT) is envelope
        assert await store.asearch(("results", "u1")) == []

    async def test_long_is_written_in_full(self) -> None:
        store = InMemoryStore()
        small = await offload(big(), store=store, user_id="u1", turn_id="t1", call_id="c1", limit=LIMIT)
        assert small.ref == "/results/t1/c1.json"
        assert len(to_json(small)) <= LIMIT
        item = await store.aget(("results", "u1"), "/t1/c1.json")
        saved = json.loads(item.value["content"])
        assert len(saved["data"]["rows"]) == 400
        assert len(saved["numbers"]) == 400
        assert item.value["content"].count("\n") > 400  # one value per line: read_file pages through it

    @pytest.mark.parametrize("store", [None, FailingStore()], ids=["no store", "store fails"])
    async def test_store_failure_keeps_the_call(self, store: Any) -> None:
        small = await offload(big(), store=store, user_id="u1", turn_id="t1", call_id="c1", limit=LIMIT)
        assert small.ref is None
        assert NOT_SAVED_NOTE in small.notes
        assert len(to_json(small)) <= LIMIT


class TestSteps:
    def test_parallel_updates_append(self) -> None:
        assert merge_steps([{"call_id": "a"}], [{"call_id": "b"}]) == [{"call_id": "a"}, {"call_id": "b"}]
        assert merge_steps(None, [{"call_id": "a"}]) == [{"call_id": "a"}]

    def test_reset(self) -> None:
        assert merge_steps([{"call_id": "a"}], list(STEPS_RESET)) == []
        assert merge_steps([{"call_id": "a"}], [{RESET: True}, {"call_id": "b"}]) == [{"call_id": "b"}]

    def test_entry_keeps_all_numbers(self) -> None:
        runtime = SimpleNamespace(tool_call_id="c1")
        envelope = big()
        shown = compact(envelope, "/results/t1/c1.json", LIMIT)
        entry = step_entry("run_scenarios", runtime, envelope, shown)  # type: ignore[arg-type]
        assert entry["ref"] == "/results/t1/c1.json"
        assert len(entry["numbers"]) == 400
        assert entry["status"] == "ok"
        assert entry["code"] is None

    def test_entry_of_failure(self) -> None:
        runtime = SimpleNamespace(tool_call_id="c1")
        failed = failure(ErrorCode.MISSING_INPUT)
        entry = step_entry("run_scenarios", runtime, failed, failed)  # type: ignore[arg-type]
        assert entry["code"] == "missing_input"
        assert entry["status"] == "error"
        assert entry["ref"] is None


# ---------------------------------------------------------------- the assembled agent (scripted model, fake engine)


@pytest.fixture
def domain(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """resolve_terms and a 201-point grid without LLM and LibreOffice; returns the engine calls."""
    calls: list[dict] = []

    async def analyzer(_message: str, _index: Any) -> list[Pick]:
        return [Pick("input", "метанол", "IN1"), Pick("output", "EBITDA", "OUT0")]

    def engine(**kw: Any) -> ExcelAnalysisToolResult:
        calls.append(kw)
        prices = list(range(300, 501))
        frame = pd.DataFrame({"Метанол": prices, "EBITDA": [1000.0 + 1.5 * p for p in prices]})
        return ExcelAnalysisToolResult(status="OK", result="", content={"result_df": frame})

    def target(**_kw: Any) -> ModelInputAnalysisToolResult:
        scenario = {"input_values": {"Метанол": 420.0}, "output_value": 1630.0, "deviation_percent": 0.0}
        content = {"matching_scenarios": [scenario], "current_input_values": {"Метанол": 386.7}, "target_value": 1630}
        return ModelInputAnalysisToolResult(status="OK", result="Найдено", content=content)

    monkeypatch.setattr(rt, "analyze_with_v1", analyzer)
    monkeypatch.setattr(calc_tools, "analyze_excel_model", engine)
    monkeypatch.setattr(calc_tools, "analyze_model_inputs_for_target", target)
    return calls


async def add_user(store: Any, user_id: str, upload_dir: Path) -> None:
    """Second user's upload in the same service store."""
    other = await uploaded_store(user_id, INDEX, str(upload_dir))
    for namespace in (("memories", user_id),):
        for item in await other.asearch(namespace):
            await store.aput(namespace, item.key, item.value)
    for item in await other.asearch(("model_registry",)):
        await store.aput(("model_registry",), item.key, item.value)


async def turn(store: Any, tmp_path: Path, steps: list, user: str = "u1", session: str = "s1") -> dict:
    agent = AgentFactory(
        ScriptedChatModel(steps=steps),
        default_catalog(),
        store=store,
        upload_dir=str(tmp_path),
        plan_mode="off",
        pinned_skills=(),
    ).build()
    return await agent.ainvoke(
        {"messages": [HumanMessage("EBITDA 2026 по сетке цен метанола")]},
        context=RequestContext(user_id=user, session_id=session),
    )


def messages_of(out: dict, name: str) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage) and m.name == name]


def file_text(message: ToolMessage) -> str:
    """read_file output without its ``@@ lines ... @@`` header."""
    header, _, body = message.content.partition("\n")
    assert header.startswith("@@ lines ")
    return body


class TestInAgent:
    """AR-Q6 «Большая сценарная сетка», «Полный результат по ссылке», «Полные числа хода в состоянии»."""

    async def test_grid_is_offloaded_and_read_back(self, tmp_path: Path, domain: list) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        first = await turn(store, tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("ок")])
        (message,) = messages_of(first, "run_scenarios")
        assert len(message.content) <= LIMIT
        shown = json.loads(message.content)
        assert "data" not in shown
        ref = shown["ref"]
        assert ref == f"/results/{first['turn_id']}/c1.json"
        assert shown["numbers"][:2] == [
            {"value": 1450.0, "label": "EBITDA при Метанол = 300"},
            {"value": 1750.0, "label": "EBITDA при Метанол = 500"},
        ]
        (step,) = first["step_results"]
        assert step["ref"] == ref
        assert len(step["numbers"]) == 201 * 2  # outputs by scenario + grid values (S1.7 D5)

        steps = [
            tool_call("read_file", "r1", file_path=ref, limit=5000),
            tool_call("read_file", "r2", file_path=ref, offset=10, limit=5),
            AIMessage("ок"),
        ]
        second = await turn(store, tmp_path, steps)
        whole, page = messages_of(second, "read_file")
        full = json.loads(file_text(whole))
        assert len(full["data"]["rows"]) == 201
        assert len(full["numbers"]) == 201 * 2
        assert len(file_text(page).splitlines()) == 5

    async def test_short_result_has_no_ref(self, tmp_path: Path, domain: list) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        out = await turn(store, tmp_path, [tool_call("resolve_terms", "r1", terms=TERMS), AIMessage("ок")])
        (message,) = messages_of(out, "resolve_terms")
        assert "ref" not in json.loads(message.content)
        assert await store.asearch(("results", "u1")) == []
        assert out["step_results"][0]["tool"] == "resolve_terms"

    async def test_no_domain_message_over_limit(self, tmp_path: Path, domain: list) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        steps = [
            tool_call("resolve_terms", "r1", terms=TERMS),
            tool_call("run_scenarios", "c1", **SCENARIOS),
            tool_call("find_input_for_target", "f1", output_id="OUT0", year=2026, target=1630.0, input_ids=["IN1"]),
            AIMessage("ок"),
        ]
        out = await turn(store, tmp_path, steps)
        domain_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
        assert [m.name for m in domain_messages] == ["resolve_terms", "run_scenarios", "find_input_for_target"]
        assert all(len(m.content) <= LIMIT for m in domain_messages)
        assert [s["call_id"] for s in out["step_results"]] == ["r1", "c1", "f1"]

    async def test_cache_hit_returns_same_ref(self, tmp_path: Path, domain: list) -> None:
        """AR-Q6 «Повтор из кэша»."""
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        first = await turn(store, tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("ок")])
        second = await turn(store, tmp_path, [tool_call("run_scenarios", "c2", **SCENARIOS), AIMessage("ок")])
        assert len(domain) == 1
        (shown,) = messages_of(second, "run_scenarios")
        assert shown.tool_call_id == "c2"
        assert json.loads(shown.content)["ref"] == first["step_results"][0]["ref"]
        (step,) = second["step_results"]
        assert step["call_id"] == "c2"
        assert step["ref"] == first["step_results"][0]["ref"]
        assert len(step["numbers"]) == 201 * 2  # outputs by scenario + grid values (S1.7 D5)

    async def test_runtime_store_is_the_service_store(self, tmp_path: Path) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        factory = AgentFactory(ScriptedChatModel(steps=[]), default_catalog(), store=store, pinned_skills=())
        assert factory.service_store() is store


class TestIsolation:
    """AR-Q7 «Свой результат читается», «Чужой результат не читается», «Путь не выбирает пространство»."""

    async def test_other_user_with_same_session(self, tmp_path: Path, domain: list) -> None:
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        await add_user(store, "u2", tmp_path)
        first = await turn(store, tmp_path, [tool_call("run_scenarios", "c1", **SCENARIOS), AIMessage("ок")])
        ref = first["step_results"][0]["ref"]
        turn_id = first["turn_id"]
        attempts = [ref, f"/results/u1/{turn_id}/c1.json", f"/results/../results/{turn_id}/c1.json"]
        steps = [*(tool_call("read_file", f"r{i}", file_path=p) for i, p in enumerate(attempts)), AIMessage("ок")]
        other = await turn(store, tmp_path, steps, user="u2", session="s1")
        replies = messages_of(other, "read_file")
        assert len(replies) == len(attempts)
        for reply in replies:
            assert reply.status == "error"
            assert "not found" in reply.content or "traversal" in reply.content.lower()
            assert "rows" not in reply.content
            assert "1450" not in reply.content
