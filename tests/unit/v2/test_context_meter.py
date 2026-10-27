"""S2.7 (AR-Q6, design D5): ContextMeter — layers of the request the model really gets, one log line per call."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.store.memory import InMemoryStore
from tests.unit.v2.fakes import uploaded_store
from tests.unit.v2.test_memory import STEPS, plan
from tests.unit.v2.test_prechecks import INDEX
from tests.unit.v2.test_summarization import long_session

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.middleware import context_meter
from aigw_service.api.v2.middleware.context_meter import LAYERS, ContextMeter, call_number, measure_layers, text_tokens
from aigw_service.api.v2.middleware.prompt import append_block
from aigw_service.api.v2.middleware.summarization import SUMMARY_SOURCE, ContextBudget
from aigw_service.api.v2.middleware.turn_summary import MEMORY_HEADER
from aigw_service.api.v2.prompts.middleware import SKILLS_SYSTEM_PROMPT, TODO_SYSTEM_PROMPT
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.turn.card import CARD_HEADER


@tool
def run_scenarios(year: int) -> str:
    """Сценарный расчёт."""
    return str(year)


def full_request() -> ModelRequest:
    system = SystemMessage("Ты — копилот кредитного аналитика.")
    for block in (
        f"{CARD_HEADER}\nФайл: model.xlsx.",
        SKILLS_SYSTEM_PROMPT,
        TODO_SYSTEM_PROMPT,
        f"{MEMORY_HEADER}\n\nПрошлые ходы: 1.",
        "## Прочее\nПравила read_file.",
    ):
        system = append_block(system, block)
    messages = [
        HumanMessage("Сводка прошлых ходов.", additional_kwargs={"lc_source": SUMMARY_SOURCE}),
        HumanMessage("EBITDA 2026?", id="q1"),
        HumanMessage("<skill>текст</skill>", additional_kwargs={"lc_source": "pinned_skill"}),
        tool_call("write_todos", "p1", todos=STEPS),
        ToolMessage("План обновлён", tool_call_id="p1", name="write_todos"),
        tool_call("read_file", "s1", file_path="/skills/target-seeking/SKILL.md"),
        ToolMessage("# Подбор входа", tool_call_id="s1", name="read_file"),
        tool_call("read_file", "r1", file_path="/results/t1/c1.json"),
        ToolMessage('{"data": [1, 2, 3]}', tool_call_id="r1", name="read_file"),
        tool_call("run_scenarios", "c1", year=2026),
        ToolMessage('{"status": "ok", "result": "3 сценария"}', tool_call_id="c1", name="run_scenarios"),
        AIMessage("EBITDA 2026: 1350.5."),
    ]
    state = {"messages": messages[1:], "turn_message_id": "q1", "turn_id": "t1"}
    runtime = SimpleNamespace(context=RequestContext(user_id="u1", session_id="s1", trace_id="tr-1"))
    return ModelRequest(
        model=ScriptedChatModel(steps=[]),
        messages=messages,
        system_message=system,
        tools=[run_scenarios],
        state=state,
        runtime=runtime,
    )


class TestMeasureLayers:
    """AR-Q6 «Слои по запросу модели»."""

    def test_every_layer_and_sum(self) -> None:
        sizes = measure_layers(full_request())
        assert set(sizes) == {*LAYERS, "total"}
        assert all(sizes[layer] > 0 for layer in LAYERS), sizes
        assert sizes["total"] == sum(sizes[layer] for layer in LAYERS)

    def test_attribution(self) -> None:
        request = full_request()
        base = measure_layers(request)

        def grown(message: Any) -> dict[str, int]:
            sizes = measure_layers(request.override(messages=[*request.messages, message]))
            return {k: sizes[k] - base[k] for k in LAYERS if sizes[k] != base[k]}

        text = "ы" * 30
        assert grown(HumanMessage(text, additional_kwargs={"lc_source": SUMMARY_SOURCE})) == {"history_summary": 10}
        assert grown(HumanMessage(text, additional_kwargs={"lc_source": "pinned_skill"})) == {"skill_text": 10}
        assert grown(HumanMessage(text)) == {"dialog": 10}
        assert grown(ToolMessage(text, tool_call_id="s1", name="read_file")) == {"skill_text": 10}  # skill read
        assert grown(ToolMessage(text, tool_call_id="r1", name="read_file")) == {"calls_results": 10}  # result read
        assert grown(ToolMessage(text, tool_call_id="p1", name="write_todos")) == {"plan": 10}

    def test_system_blocks(self) -> None:
        request = full_request()
        sizes = measure_layers(request)
        assert sizes["card"] == text_tokens(f"\n\n{CARD_HEADER}\nФайл: model.xlsx.")
        assert sizes["skills_catalog"] == text_tokens("\n\n" + SKILLS_SYSTEM_PROMPT)
        assert sizes["instructions"] == text_tokens("Ты — копилот кредитного аналитика.") + text_tokens(
            "\n\n## Прочее\nПравила read_file."
        )  # an unknown block counts as instructions

    def test_plain_string_system_and_no_tools(self) -> None:
        request = full_request().override(system_message=SystemMessage("Инструкции"), tools=[], messages=[])
        sizes = measure_layers(request)
        assert sizes["instructions"] == sizes["total"] == text_tokens("Инструкции")

    def test_call_number(self) -> None:
        request = full_request()
        assert call_number(request.state) == 1 + 5  # five model answers after the question
        assert call_number({"messages": [], "turn_message_id": None}) == 1


class TestContextMeter:
    def test_logs_one_line_and_warns_over_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        infos: list = []
        warnings: list = []
        monkeypatch.setattr(context_meter.logger, "info", lambda *a: infos.append(a))
        monkeypatch.setattr(context_meter.logger, "warning", lambda *a: warnings.append(a))
        request = full_request()
        total = measure_layers(request)["total"]

        assert ContextMeter(max_prompt_tokens=total).wrap_model_call(request, lambda r: "ответ") == "ответ"
        assert not warnings
        record = json.loads(infos[0][1])
        assert infos[0][0] == "CONTEXT LAYERS {}"
        assert (record["trace_id"], record["turn_id"], record["call"]) == ("tr-1", "t1", 6)
        assert record["layers"]["total"] == total

        assert ContextMeter(max_prompt_tokens=total - 1).wrap_model_call(request, lambda r: "ответ") == "ответ"
        assert warnings[0][1:] == (total, total - 1)  # «Превышение бюджета»: the call still runs


class TestOnAgent:
    """AR-Q6 «Строка на каждый вызов», «Слой сводки после сжатия», «Ход без LLM»."""

    async def test_line_per_call(self, tmp_path: Path, layer_logs: list) -> None:
        model = ScriptedChatModel(
            steps=[
                tool_call("write_todos", "p1", todos=STEPS),
                tool_call("write_todos", "p2", todos=plan("completed")),
                AIMessage("Готово."),
            ]
        )
        store = await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path)).build()
        out = await agent.ainvoke(
            {"messages": [HumanMessage("Вопрос")]},
            context=RequestContext(user_id="u1", session_id="s1", trace_id="tr-9"),
        )
        assert [r["call"] for r in layer_logs] == [1, 2, 3]
        assert {r["turn_id"] for r in layer_logs} == {out["turn_id"]}
        assert {r["trace_id"] for r in layer_logs} == {"tr-9"}
        first = layer_logs[0]["layers"]
        assert first["card"] > 0
        assert first["skill_text"] > 0
        assert first["tools"] > 0
        assert layer_logs[1]["layers"]["plan"] > first["plan"]

    async def test_summary_layer_after_compression(self, tmp_path: Path, domain: list, layer_logs: list) -> None:
        model, _, _ = await long_session(tmp_path, 8)
        with_summary = [r["layers"] for r in layer_logs if r["layers"]["history_summary"] > 0]
        assert with_summary
        assert max(r["total"] for r in with_summary) < ContextBudget(12000, 0.7, 0.3).max_prompt_tokens
        assert len(layer_logs) == len(model.calls)  # the summary call itself is not an agent call

    @pytest.mark.parametrize("case", ["no_workbook", "language"])
    async def test_no_llm_no_lines(self, tmp_path: Path, layer_logs: list, case: str) -> None:
        model = ScriptedChatModel(steps=[])
        store = InMemoryStore() if case == "no_workbook" else await uploaded_store("u1", INDEX, str(tmp_path))
        agent = AgentFactory(model, default_catalog(), store=store, upload_dir=str(tmp_path)).build()
        text = "Вопрос" if case == "no_workbook" else "Wie hoch ist das EBITDA im Jahr 2026 bei diesem Preis?"
        await agent.ainvoke({"messages": [HumanMessage(text)]}, context=RequestContext(user_id="u1", session_id="s1"))
        assert model.calls == []
        assert layer_logs == []
