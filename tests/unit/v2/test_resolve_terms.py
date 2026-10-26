"""S1.3 (FR-6): resolve_terms over a fake analyzer, adapter async calls and state updates — no network."""

import json
from typing import Any

import pytest
from gigachat.exceptions import RateLimitError
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel
from tests.unit.v2.fakes import FakeContext, ScriptedChatModel, tool_call

from aigw_service.api.v2.tools import resolve_terms as rt
from aigw_service.api.v2.tools.adapter import ToolSpec, build_tool
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ToolSuccess
from aigw_service.api.v2.tools.resolve_terms import Pick, ResolveTermsArgs, Term, assign, resolve_terms
from aigw_service.api.v2.tools.state import ToolsState
from aigw_service.exceptions import StopEventError

INDEX = ModelIndex(
    {
        "IN1": "Метанол",
        "IN2": "Цена урана",
        "IN3": "Медь (LME)",
        "IN4": "Платина (LME)",
        "IN5": "Курс рубля к доллару США в конце года (eop USD/RUB)",
    },
    {"OUT0": "EBITDA", "OUT1": "Чистая прибыль", "OUT2": "Коэффициент покрытия долга в i-ом периоде (DSCR)"},
)
CTX = FakeContext(model_index=INDEX)


def args(*terms: tuple[str, str]) -> ResolveTermsArgs:
    return ResolveTermsArgs(terms=[Term(text=t, role=r) for t, r in terms])


def analyzer(*picks: Pick, calls: list | None = None) -> Any:
    async def fake(message: str, _index: ModelIndex) -> list[Pick]:
        if calls is not None:
            calls.append(message)
        return list(picks)

    return fake


def by_text(envelope: ToolSuccess) -> dict[str, dict]:
    return {r["text"]: r for r in envelope.data}


class TestAdapterStateAndAsync:
    """Design D4: async tools run on the loop; state_update becomes a Command."""

    async def test_async_tool_updates_state_and_history(self) -> None:
        class Args(BaseModel):
            term: str

        async def acall(a: Args, _ctx: Any) -> ToolSuccess:
            return ToolSuccess(result="ok", state_update={"resolved_terms": {a.term: {"id": "IN1"}}})

        tool = build_tool(ToolSpec("probe", "Проба", Args, acall=acall))
        steps = [tool_call("probe", "c1", term="метанол"), tool_call("probe", "c2", term="медь"), AIMessage("ок")]
        agent = create_agent(
            ScriptedChatModel(steps=steps), [tool], context_schema=FakeContext, state_schema=ToolsState
        )
        out = await agent.ainvoke({"messages": [HumanMessage("ход")]}, context=CTX)
        assert out["resolved_terms"] == {"метанол": {"id": "IN1"}, "медь": {"id": "IN1"}}
        messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
        assert [json.loads(m.content)["status"] for m in messages] == ["ok", "ok"]

    def test_spec_needs_exactly_one_callable(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ToolSpec("x", "x", BaseModel)


class TestAssign:
    def test_by_echoed_name_then_by_order(self) -> None:
        terms = [
            Term(text="EBITDA", role="output"),
            Term(text="цена метанола", role="input"),
            Term(text="медь", role="input"),
        ]
        picks = [
            Pick("input", "copper", "IN3"),
            Pick("output", "ebitda", "OUT0"),
            Pick("input", "Цена метанола", "IN1"),
        ]
        assert assign(terms, picks) == ["OUT0", "IN1", "IN3"]

    def test_role_is_respected(self) -> None:
        assert assign([Term(text="EBITDA", role="output")], [Pick("input", "EBITDA", "IN1")]) == [None]


class TestCandidates:
    """FR-6 «Явное сопоставление терминов»."""

    async def test_high_confidence(self) -> None:
        env = await resolve_terms(
            args(("цена метанола", "input")), CTX, analyzer(Pick("input", "цена метанола", "IN1"))
        )
        term = by_text(env)["цена метанола"]
        assert term["confidence"] == "high"
        assert term["candidates"][0] == {"id": "IN1", "name": "Метанол", "score": 0.5}

    async def test_low_confidence_keeps_analyzer_first(self) -> None:
        env = await resolve_terms(args(("copper (LME)", "input")), CTX, analyzer(Pick("input", "copper (LME)", "IN1")))
        term = by_text(env)["copper (LME)"]
        assert term["confidence"] == "low"
        assert term["candidates"][0]["id"] == "IN1"
        assert {c["id"] for c in term["candidates"][1:]} <= {"IN3", "IN4"}
        assert env.hint

    async def test_not_found_term_does_not_sink_others(self) -> None:
        env = await resolve_terms(
            args(("zzzz qqqq", "input"), ("EBITDA", "output")), CTX, analyzer(Pick("output", "EBITDA", "OUT0"))
        )
        assert by_text(env)["zzzz qqqq"]["status"] == "term_not_found"
        assert by_text(env)["EBITDA"]["status"] == "ok"
        assert "Не найдено: zzzz qqqq" in env.result

    async def test_output_role_gives_outputs_only(self) -> None:
        env = await resolve_terms(args(("EBITDA", "output")), CTX, analyzer())
        assert all(c["id"].startswith("OUT") for c in by_text(env)["EBITDA"]["candidates"])

    async def test_nothing_found_is_a_failure(self) -> None:
        env = await resolve_terms(args(("zzzz qqqq", "input")), CTX, analyzer())
        assert env.status == "error"
        assert env.code == "term_not_found"

    async def test_state_update_has_first_candidates(self) -> None:
        env = await resolve_terms(
            args(("цена метанола", "input")), CTX, analyzer(Pick("input", "цена метанола", "IN1"))
        )
        assert env.state_update == {
            "resolved_terms": {"цена метанола": {"id": "IN1", "name": "Метанол", "confidence": "high"}}
        }
        assert "state_update" not in json.loads(env.model_dump_json())


class TestResilience:
    """FR-6 «Устойчивость сопоставления»."""

    async def test_crash_falls_back_to_one_term_per_call(self) -> None:
        calls: list[str] = []

        async def flaky(message: str, _index: ModelIndex) -> list[Pick]:
            calls.append(message)
            if "медь" in message and "метанол" in message:
                raise KeyError("")  # v1 _resolve_names_internal on an empty ID
            if "медь" in message:
                raise KeyError("")
            return [Pick("input", "цена метанола", "IN1")]

        env = await resolve_terms(args(("цена метанола", "input"), ("медь", "input")), CTX, flaky)
        assert len(calls) == 3
        assert by_text(env)["цена метанола"]["candidates"][0]["id"] == "IN1"
        assert by_text(env)["медь"]["confidence"] == "low"  # Jaccard only, no analyzer choice

    async def test_rate_limit_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pauses: list[float] = []

        async def no_sleep(seconds: float) -> None:
            pauses.append(seconds)

        monkeypatch.setattr(rt, "_sleep", no_sleep)
        attempts = {"n": 0}

        async def limited(_message: str, _index: ModelIndex) -> list[Pick]:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitError("https://gigachat", 429, b"Too Many Requests", None)
            return [Pick("input", "цена метанола", "IN1")]

        env = await resolve_terms(args(("цена метанола", "input")), CTX, limited)
        assert env.status == "ok"
        assert attempts["n"] == 3
        assert pauses[1] == 2 * pauses[0]

    async def test_stop_event_is_not_swallowed(self) -> None:
        async def stopped(_message: str, _index: ModelIndex) -> list[Pick]:
            raise StopEventError(user_message="GigaChat недоступен", url="https://gigachat")

        with pytest.raises(StopEventError):
            await resolve_terms(args(("цена метанола", "input")), CTX, stopped)


class TestCatalogRegistration:
    def test_base_tool_without_engine(self) -> None:
        catalog = default_catalog()
        assert "resolve_terms" in {t.name for t in catalog.base_tools()}
        assert "resolve_terms" not in catalog.engine_tool_names()
        assert set(catalog.tool("resolve_terms").tool_call_schema.model_json_schema()["properties"]) == {"terms"}

    async def test_through_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rt, "analyze_with_v1", analyzer(Pick("output", "EBITDA", "OUT0")))
        call = tool_call("resolve_terms", terms=[{"text": "EBITDA", "role": "output"}])
        agent = create_agent(
            ScriptedChatModel(steps=[call, AIMessage("ок")]),
            default_catalog().base_tools(),
            context_schema=FakeContext,
            state_schema=ToolsState,
        )
        out = await agent.ainvoke({"messages": [HumanMessage("ход")]}, context=CTX)
        assert out["resolved_terms"]["EBITDA"]["id"] == "OUT0"
