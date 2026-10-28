"""S6.2 (FR-22, FR-24, ADR-14) and S6.4 (FR-23): stream event schema, labels, summaries, hygiene, StreamTranslator."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from aigw_service.api.v2.middleware.limits import LIMIT_HIT, TURN_TIMEOUT
from aigw_service.api.v2.stream.events import (
    EVENT_TYPES,
    AnswerEvent,
    EventFactory,
    Phase,
    PhaseEvent,
    PlanEvent,
    ToolFinished,
    TurnSummaryEvent,
)
from aigw_service.api.v2.stream.hygiene import violations
from aigw_service.api.v2.stream.labels import (
    DEFAULT_LABEL,
    FAILURE_VIEW,
    PHASE_TEXT,
    RETRY_TEXT,
    SUMMARY_MAX,
    failure_view,
    retry_text,
    scrub,
    step_summary,
    tool_label,
    turn_summary,
)
from aigw_service.api.v2.stream.translator import StreamTranslator
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ErrorCode, failure, to_json

INDEX = ModelIndex(
    inputs={"IN1": "Цена метанола", "IN2": "Курс USD/RUB"},
    outputs={"OUT0": "EBITDA", "OUT5": "DSCR"},
)
COMMON = {"seq", "ts", "trace_id", "v"}
CATALOG_FIELDS = {
    "turn.started": {"session_id"},
    "phase": {"phase", "text"},
    "plan": {"steps"},
    "tool.started": {"call", "step", "label"},
    "tool.finished": {"call", "label", "outcome", "elapsed_s", "summary", "text"},
    "summary": {"completed", "skipped", "blocked", "text"},
    "answer": {"text", "outcome"},
    "error": {"kind", "text"},
}
SAMPLE: dict[str, dict[str, Any]] = {
    "turn.started": {"session_id": "s1"},
    "phase": {"phase": "checking", "text": "Проверяю ответ"},
    "plan": {"steps": [{"n": 1, "title": "Шаг", "status": "pending"}]},
    "tool.started": {"call": 1, "step": None, "label": "Беру базовые значения"},
    "tool.finished": {"call": 1, "label": "Беру базовые значения", "outcome": "ok", "elapsed_s": 0.1},
    "summary": {"completed": 1, "skipped": 0, "blocked": 0, "text": "Выполнено 1 из 1 шагов"},
    "answer": {"text": "Ответ", "outcome": "full"},
    "error": {"kind": "failed", "text": "Ошибка"},
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def updates(node: str, **update: Any) -> dict[str, Any]:
    return {"type": "updates", "ns": (), "data": {node: update}}


def calls(*items: tuple[str, str, dict]) -> dict[str, Any]:
    message = AIMessage("", tool_calls=[{"name": n, "id": i, "args": a} for n, i, a in items])
    return updates("model", messages=[message])


def result(call_id: str, name: str, content: str = '{"status": "ok", "result": "готово"}') -> dict[str, Any]:
    return updates("tools", messages=[ToolMessage(content, tool_call_id=call_id, name=name)])


def kinds(drafts: list[dict]) -> list[str]:
    return [d["event"] for d in drafts]


def make(drafts: list[dict]) -> list[Any]:
    factory = EventFactory("t-1")
    return [factory.make(d) for d in drafts]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tr(clock: Clock) -> StreamTranslator:
    return StreamTranslator(lambda: INDEX, clock)


class TestEvents:
    def test_catalog_fields(self) -> None:
        factory = EventFactory("t-1")
        assert set(EVENT_TYPES) == set(CATALOG_FIELDS)
        for kind, fields in SAMPLE.items():
            event = factory.make({"event": kind, **fields})
            assert set(event.data()) == COMMON | CATALOG_FIELDS[kind]
            assert event.event == kind
            assert event.v == 1
            assert event.trace_id == "t-1"

    def test_extra_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EventFactory("t").make({"event": "answer", "text": "x", "outcome": "full", "code": "x"})

    def test_seq_and_ts(self) -> None:
        start = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        ticks = iter(start + timedelta(seconds=i) for i in range(3))
        factory = EventFactory("t", clock=lambda: next(ticks))
        events = [factory.make({"event": "turn.started", "session_id": "s"}) for _ in range(3)]
        assert [e.seq for e in events] == [1, 2, 3]
        assert datetime.fromisoformat(events[1].ts) == start + timedelta(seconds=1)
        assert events[0].ts.endswith("+00:00")

    def test_frozen(self) -> None:
        event = EventFactory("t").make({"event": "answer", "text": "x", "outcome": "full"})
        with pytest.raises(ValidationError):
            event.text = "y"  # type: ignore[misc]


class TestLabels:
    def test_resolve_terms(self) -> None:
        args = {"terms": [{"text": "цена меди", "role": "input"}, {"text": "EBITDA", "role": "output"}]}
        assert tool_label("resolve_terms", args, INDEX) == "Сопоставляю показатели: цена меди, EBITDA"

    def test_resolve_terms_many_and_empty(self) -> None:
        many = {"terms": [{"text": f"т{i}"} for i in range(7)]}
        assert tool_label("resolve_terms", many, INDEX).endswith("т4, …")
        assert tool_label("resolve_terms", {"terms": [{"text": "IN3"}]}, INDEX) == "Сопоставляю показатели"

    def test_run_scenarios(self) -> None:
        args = {"inputs": [{"id": "IN1", "min": 300.0, "max": 500, "step": 100}], "outputs": ["OUT0"], "year": 2026}
        label = tool_label("run_scenarios", args, INDEX)
        assert label == "Считаю сценарии: EBITDA при Цена метанола от 300 до 500"
        assert "IN1" not in label
        assert "OUT0" not in label

    def test_run_scenarios_point_and_no_range(self) -> None:
        point = {"inputs": [{"id": "IN1", "min": 0.15, "max": 0.15}], "outputs": ["OUT0", "OUT5"]}
        assert tool_label("run_scenarios", point, INDEX) == "Считаю сценарии: EBITDA, DSCR при Цена метанола = 0.15"
        assert tool_label("run_scenarios", {"inputs": [{"id": "IN2"}]}, INDEX).endswith("показатели при Курс USD/RUB")

    def test_unknown_ids_are_never_shown(self) -> None:
        args = {"inputs": [{"id": "IN9", "min": 1, "max": 2}], "outputs": ["OUT9"]}
        assert tool_label("run_scenarios", args, INDEX) == "Считаю сценарии"
        assert tool_label("run_scenarios", args, None) == "Считаю сценарии"
        target = tool_label("find_input_for_target", {"output_id": "OUT9", "input_ids": ["IN9"]}, INDEX)
        assert "9" not in target

    def test_find_input_for_target(self) -> None:
        args = {"output_id": "OUT5", "input_ids": ["IN1"], "target": 1.2, "year": 2026}
        assert tool_label("find_input_for_target", args, INDEX) == "Подбираю Цена метанола под DSCR = 1.2 в 2026"
        assert tool_label("find_input_for_target", {}, INDEX) == "Подбираю значения входов"

    @pytest.mark.parametrize(
        ("operation", "text"),
        [
            ("difference", "разницу"),
            ("pct_change", "изменение в процентах"),
            ("headroom", "запас до порога"),
            ("crossing", "точку пересечения порога"),
            ("elasticity", "эластичность"),
        ],
    )
    def test_derive(self, operation: str, text: str) -> None:
        assert tool_label("derive", {"operation": operation}, INDEX) == f"Считаю {text}"

    @pytest.mark.parametrize(
        ("path", "label"),
        [
            ("/skills/scenario-analysis/SKILL.md", "Изучаю методику"),
            ("/results/u1/c3.json", "Перечитываю прошлый расчёт"),
            ("/other.txt", "Читаю материалы"),
        ],
    )
    def test_read_file(self, path: str, label: str) -> None:
        assert tool_label("read_file", {"file_path": path}, INDEX) == label

    def test_other_tools(self) -> None:
        assert tool_label("get_values", {"ids": ["OUT0"]}, INDEX) == "Беру базовые значения"
        assert tool_label("describe_model", {}, INDEX) == "Изучаю структуру модели"
        assert tool_label("trace_dependencies", {"output_id": "OUT5"}, INDEX) == "Смотрю, от чего зависит DSCR"
        assert tool_label("trace_dependencies", {}, INDEX) == "Смотрю зависимости показателя"
        assert tool_label("something_new", {}, INDEX) == DEFAULT_LABEL
        assert tool_label("run_scenarios", {"outputs": 5}, INDEX) == DEFAULT_LABEL


class TestFailureView:
    def test_covers_every_code(self) -> None:
        assert set(FAILURE_VIEW) == set(ErrorCode)

    @pytest.mark.parametrize(
        ("code", "outcome"),
        [
            ("ambiguous_term", "retry"),
            ("unknown_id", "retry"),
            ("engine_unavailable", "failed"),
            ("missing_input", "failed"),
            ("internal_error", "failed"),
        ],
    )
    def test_outcome(self, code: str, outcome: str) -> None:
        assert failure_view(code)[0] == outcome

    def test_unknown_code(self) -> None:
        assert failure_view("no_such_code") == FAILURE_VIEW[ErrorCode.INTERNAL_ERROR]
        assert retry_text("engine_unavailable") == RETRY_TEXT[ErrorCode.ENGINE_UNAVAILABLE]
        assert retry_text("no_such_code") == retry_text("bad_units")

    def test_texts_are_clean(self) -> None:
        factory = EventFactory("t")
        texts = [t for _, t in FAILURE_VIEW.values()] + list(RETRY_TEXT.values()) + list(PHASE_TEXT.values())
        for text in texts:
            event = factory.make({"event": "error", "kind": "failed", "text": text})
            assert violations(event) == [], text


class TestHygiene:
    @pytest.mark.parametrize(
        ("dirty", "clean"),
        [
            ("Вызвать resolve_terms для IN12", "Вызвать для"),
            ("Посчитать DSCR (OUT40)", "Посчитать DSCR"),
            ("Перечитать /results/u1/c3.json", "Перечитать"),
            ("Код ambiguous_term: выбрать", "Код: выбрать"),
            ("Ход cancelled пользователем", "Ход cancelled пользователем"),
            ("write_todos", ""),
        ],
    )
    def test_scrub(self, dirty: str, clean: str) -> None:
        assert scrub(dirty) == clean

    def test_finds_every_violation(self) -> None:
        factory = EventFactory("t")
        dirty = factory.make(
            {
                "event": "tool.finished",
                "call": 1,
                "label": "run_scenarios IN1",
                "outcome": "failed",
                "elapsed_s": 0.0,
                "text": 'ToolError: engine_unavailable /results/x Traceback {"hint": "x"}',
            }
        )
        found = " ".join(violations(dirty))
        for what in ("index ID", "run_scenarios", "engine_unavailable", "results path", "traceback", "exception"):
            assert what in found
        assert "envelope key" in found

    def test_answer_text_is_exempt(self) -> None:
        answer = EventFactory("t").make({"event": "answer", "text": "OUT0 через run_scenarios", "outcome": "full"})
        assert isinstance(answer, AnswerEvent)
        assert violations(answer) == []


class TestTranslatorUpdates:
    def test_tool_pair(self, tr: StreamTranslator, clock: Clock) -> None:
        started = tr.feed(calls(("resolve_terms", "c2", {"terms": [{"text": "EBITDA", "role": "output"}]})))
        clock.now = 1.26
        finished = tr.feed(result("c2", "resolve_terms"))
        assert started == [
            {"event": "tool.started", "call": 1, "step": None, "label": "Сопоставляю показатели: EBITDA"}
        ]
        assert finished == [
            {
                "event": "tool.finished",
                "call": 1,
                "label": "Сопоставляю показатели: EBITDA",
                "outcome": "ok",
                "elapsed_s": 1.3,
                "summary": "готово",
                "text": None,
            }
        ]

    def test_write_todos_gives_only_plan(self, tr: StreamTranslator) -> None:
        assert tr.feed(calls(("write_todos", "c1", {"todos": []}))) == []
        todos = [{"content": "Сопоставить", "status": "in_progress"}]
        out = tr.feed(
            updates("tools", messages=[ToolMessage("ok", tool_call_id="c1", name="write_todos")], todos=todos)
        )
        assert kinds(out) == ["plan"]
        step = tr.feed(calls(("get_values", "c2", {"ids": ["OUT0"]})))
        assert step[0]["step"] == 1
        assert step[0]["call"] == 1

    def test_error_envelope(self, tr: StreamTranslator) -> None:
        tr.feed(calls(("resolve_terms", "c2", {"terms": []})))
        (finished,) = tr.feed(result("c2", "resolve_terms", to_json(failure(ErrorCode.AMBIGUOUS_TERM))))
        assert finished["outcome"] == "retry"
        assert finished["text"] == FAILURE_VIEW[ErrorCode.AMBIGUOUS_TERM][1]

    def test_unparsed_envelope_is_ok(self, tr: StreamTranslator) -> None:
        tr.feed(calls(("read_file", "c3", {"file_path": "/skills/x/SKILL.md"})))
        (finished,) = tr.feed(result("c3", "read_file", "# Skill\nтекст"))
        assert finished["outcome"] == "ok"

    def test_several_calls_in_one_message(self, tr: StreamTranslator) -> None:
        out = tr.feed(calls(("get_values", "a", {}), ("describe_model", "b", {})))
        assert [d["call"] for d in out] == [1, 2]
        assert tr.feed(result("b", "describe_model"))[0]["call"] == 2
        assert tr.feed(result("zzz", "get_values")) == []

    def test_service_nodes_give_nothing(self, tr: StreamTranslator) -> None:
        skill = HumanMessage('<skill name="economic-answer">…</skill>')
        assert tr.feed(updates("SkillsMiddleware.before_model", messages=[skill], pinned_skills=["x"])) == []
        assert tr.feed(updates("TurnLimits.before_model", guard_counters={"model_calls": 1})) == []
        assert tr.feed({"type": "updates", "ns": (), "data": {"LanguageGuard.before_agent": None}}) == []
        assert tr.feed({"type": "messages", "data": ()}) == []


class TestTranslatorPlan:
    def test_only_changes(self, tr: StreamTranslator) -> None:
        todos = [{"content": "Посчитать", "status": "pending"}]
        assert kinds(tr.feed(updates("tools", todos=todos))) == ["plan"]
        assert tr.feed(updates("tools", todos=list(todos))) == []
        assert tr.feed(updates("TurnContext.before_agent", todos=[])) == [{"event": "plan", "steps": []}]

    def test_empty_plan_at_start_gives_nothing(self, tr: StreamTranslator) -> None:
        assert tr.feed(updates("TurnContext.before_agent", todos=[])) == []

    def test_titles_and_reasons(self, tr: StreamTranslator) -> None:
        todos = [
            {"content": "Вызвать resolve_terms для IN12", "status": "completed"},
            {"content": "OUT3", "status": "blocked", "reason": "Нет года (missing_input)"},
        ]
        (plan,) = tr.feed(updates("tools", todos=todos))
        event = make([plan])[0]
        assert isinstance(event, PlanEvent)
        assert [s.title for s in event.steps] == ["Вызвать для", "Шаг 2"]
        assert event.steps[1].reason == "Нет года"
        assert violations(event) == []


class TestTranslatorAnswer:
    def test_full(self, tr: StreamTranslator) -> None:
        out = tr.feed(updates("model", messages=[AIMessage("Ответ", id="a1")]))
        assert out == [{"event": "phase", "phase": "checking", "text": PHASE_TEXT["checking"]}]
        assert tr.feed(updates("TurnSummary.after_agent", turn_summaries=[])) == []
        assert tr.finish() == [{"event": "answer", "text": "Ответ", "outcome": "full"}]  # no plan — no summary

    def test_rejected_candidate_never_leaves(self, tr: StreamTranslator) -> None:
        drafts = tr.feed(updates("model", messages=[AIMessage("Ранний ответ 9999", id="a1")]))
        drafts += tr.feed(
            updates("PlanCompletionGuard.after_model", messages=[HumanMessage("Напоминание")], jump_to="model")
        )
        drafts += tr.feed(calls(("get_values", "c5", {})))
        drafts += tr.feed(updates("model", messages=[AIMessage("Итог", id="a2")]))
        drafts += tr.finish()
        assert "Ранний ответ" not in json.dumps(drafts, ensure_ascii=False)
        assert "Напоминание" not in json.dumps(drafts, ensure_ascii=False)
        assert drafts[-1]["text"] == "Итог"
        assert kinds(drafts).count("phase") == 1  # checking only once in a row

    def test_rejected_last_candidate_is_an_error(self, tr: StreamTranslator) -> None:
        tr.feed(updates("model", messages=[AIMessage("x", id="a1")]))
        tr.feed(updates("AnswerGroundingGuard.after_model", jump_to="model"))
        with pytest.raises(RuntimeError):
            tr.finish()

    def test_rewritten_with_same_id(self, tr: StreamTranslator) -> None:
        tr.feed(updates("model", messages=[AIMessage("EBITDA 9999", id="a1")]))
        tr.feed(updates("AnswerGroundingGuard.after_model", messages=[AIMessage("EBITDA н/д", id="a1")]))
        assert tr.finish()[-1]["text"] == "EBITDA н/д"

    def test_other_ai_message_is_not_a_candidate(self, tr: StreamTranslator) -> None:
        tr.feed(updates("model", messages=[AIMessage("Ответ", id="a1")]))
        tr.feed(updates("SomeMiddleware.after_model", messages=[AIMessage("служебное", id="zz")]))
        assert tr.finish()[-1]["text"] == "Ответ"

    @pytest.mark.parametrize("node", ["TurnContext.before_agent", "LanguageGuard.before_agent"])
    def test_refusal(self, tr: StreamTranslator, node: str) -> None:
        assert tr.feed(updates(node, messages=[AIMessage("Отказ")], jump_to="end")) == []
        assert tr.finish() == [{"event": "answer", "text": "Отказ", "outcome": "refusal"}]

    def test_cancelled(self, tr: StreamTranslator) -> None:
        tr.feed(updates("Cancellation.before_model", messages=[AIMessage("Ход прерван")], jump_to="end"))
        assert tr.finish()[-1]["outcome"] == "partial"

    @pytest.mark.parametrize("flag", [LIMIT_HIT, TURN_TIMEOUT])
    def test_partial(self, tr: StreamTranslator, flag: str) -> None:
        tr.feed(updates("TurnLimits.before_model", guard_counters={flag: 1}))
        tr.feed(updates("tools", todos=[{"content": "x", "status": "blocked", "reason": "r"}]))
        tr.feed(updates("model", messages=[AIMessage("Частично", id="a1")]))
        assert tr.finish()[-1]["outcome"] == "partial"

    def test_clarify(self, tr: StreamTranslator) -> None:
        tr.feed(updates("tools", todos=[{"content": "Год", "status": "blocked", "reason": "Нет года"}]))
        tr.feed(updates("model", messages=[AIMessage("За какой год?", id="a1")]))
        assert tr.finish()[-1]["outcome"] == "clarify"

    def test_list_content(self, tr: StreamTranslator) -> None:
        tr.feed(updates("model", messages=[AIMessage([{"type": "text", "text": "x"}], id="a1")]))
        assert tr.finish()[-1]["text"] == str([{"type": "text", "text": "x"}])


class TestTranslatorCustom:
    def test_engine_retry(self, tr: StreamTranslator, clock: Clock) -> None:
        drafts = tr.feed(calls(("run_scenarios", "c3", {"outputs": ["OUT0"]})))
        clock.now = 2.0
        drafts += tr.feed(
            {"type": "custom", "data": {"progress": "retry", "tool_call_id": "c3", "code": "engine_unavailable"}}
        )
        clock.now = 3.0
        drafts += tr.feed(result("c3", "run_scenarios"))
        assert [(d["event"], d.get("outcome")) for d in drafts] == [
            ("tool.started", None),
            ("tool.finished", "retry"),
            ("tool.started", None),
            ("tool.finished", "ok"),
        ]
        assert {d["call"] for d in drafts} == {1}
        assert drafts[1]["elapsed_s"] == 2.0
        assert drafts[3]["elapsed_s"] == 1.0
        assert drafts[1]["text"] == RETRY_TEXT[ErrorCode.ENGINE_UNAVAILABLE]
        assert isinstance(make(drafts)[1], ToolFinished)

    def test_phase(self, tr: StreamTranslator) -> None:
        part = {"type": "custom", "data": {"progress": "phase", "phase": "planning"}}
        assert tr.feed(part) == [{"event": "phase", "phase": "planning", "text": PHASE_TEXT["planning"]}]
        assert tr.feed(part) == []
        assert tr.feed({"type": "custom", "data": {"progress": "phase", "phase": "nonsense"}}) == []

    def test_foreign_custom_parts(self, tr: StreamTranslator) -> None:
        assert tr.feed({"type": "custom", "data": {"anything": 1}}) == []
        assert tr.feed({"type": "custom", "data": "text"}) == []
        assert tr.feed({"type": "custom", "data": {"progress": "retry", "tool_call_id": "nope"}}) == []
        assert tr.feed({"type": "custom", "data": {"progress": "other"}}) == []


NUMBER = re.compile(r"\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?")


def numbers_found_in(summary: str, result: str) -> bool:
    """FR-23: every number of a summary is a whole number of the envelope's ``result``."""
    whole = set(NUMBER.findall(result))
    return all(n in whole for n in NUMBER.findall(summary))


class TestStepSummary:
    def test_short_result_as_is(self) -> None:
        assert step_summary({"status": "ok", "result": "EBITDA 2026 при цене 300: 1 200 млн руб."}) == (
            "EBITDA 2026 при цене 300: 1 200 млн руб."
        )

    @pytest.mark.parametrize(
        "envelope",
        [
            None,
            {"status": "error", "error": "x", "code": "ambiguous_term", "hint": "h"},
            {"status": "ok"},
            {"status": "ok", "result": 5},
            {"status": "ok", "result": "IN12 /results/c1.json"},
        ],
    )
    def test_none(self, envelope: Any) -> None:
        assert step_summary(envelope) is None

    def test_scrubbed(self) -> None:
        text = step_summary(
            {"status": "ok", "result": "Сценарии для IN12 и OUT3 посчитаны, полный результат /results/c4.json"}
        )
        assert text == "Сценарии для и посчитаны, полный результат"
        assert (
            violations(
                make(
                    [
                        {
                            "event": "tool.finished",
                            "call": 1,
                            "label": "x",
                            "outcome": "ok",
                            "elapsed_s": 0.1,
                            "summary": text,
                        }
                    ]
                )[0]
            )
            == []
        )

    def test_sentence_boundary(self) -> None:
        result = "Первое предложение с числом 1 200 и запасом текста " * 4 + ". " + "хвост " * 60
        text = step_summary({"status": "ok", "result": result})
        assert text is not None
        assert len(text) <= SUMMARY_MAX
        assert text.endswith("…")
        assert "хвост" not in text

    @pytest.mark.parametrize("shift", range(12))
    def test_number_never_cut(self, shift: int) -> None:
        result = "а" * (290 + shift) + " значение 1 234 567,89 млн руб. и ещё 42 по OUT5"
        text = step_summary({"status": "ok", "result": result})
        assert text is not None
        assert len(text) <= SUMMARY_MAX
        assert text.endswith("…")
        assert numbers_found_in(text, result)

    def test_word_boundary(self) -> None:
        result = " ".join(f"слово{i}" for i in range(100))
        text = step_summary({"status": "ok", "result": result})
        assert text is not None
        assert len(text) <= SUMMARY_MAX
        words = text.removesuffix("…").split()
        assert words == result.split()[: len(words)]

    def test_one_long_token(self) -> None:
        text = step_summary({"status": "ok", "result": "x" * 290 + "1234567890" * 3})
        assert text is not None
        assert len(text) <= SUMMARY_MAX
        assert not re.search(r"\d", text)


class TestTurnSummaryText:
    @staticmethod
    def steps(*statuses: str) -> list[dict[str, Any]]:
        return [{"n": i + 1, "title": "t", "status": s} for i, s in enumerate(statuses)]

    def test_no_plan(self) -> None:
        assert turn_summary([]) is None

    def test_mixed(self) -> None:
        draft = turn_summary(self.steps("completed", "completed", "completed", "skipped", "blocked"))
        assert draft == {
            "event": "summary",
            "completed": 3,
            "skipped": 1,
            "blocked": 1,
            "text": "Выполнено 3 из 5 шагов, 1 пропущен, 1 ждёт ответа пользователя",
        }
        event = make([draft])[0]
        assert isinstance(event, TurnSummaryEvent)
        assert violations(event) == []

    @pytest.mark.parametrize(
        ("n", "text"),
        [
            (1, "Выполнено 0 из 1 шага, 1 пропущен, 1 ждёт ответа пользователя"),
            (2, "Выполнено 0 из 2 шагов, 2 пропущено, 2 ждут ответа пользователя"),
            (5, "Выполнено 0 из 5 шагов, 5 пропущено, 5 ждут ответа пользователя"),
            (11, "Выполнено 0 из 11 шагов, 11 пропущено, 11 ждут ответа пользователя"),
            (21, "Выполнено 0 из 21 шага, 21 пропущен, 21 ждёт ответа пользователя"),
        ],
    )
    def test_plural(self, n: int, text: str) -> None:
        draft = turn_summary(self.steps(*["skipped"] * n, *["blocked"] * n))
        assert draft is not None
        total = text.replace(f"из {n} ", f"из {2 * n} ")  # the plan has 2n steps
        assert draft["text"].split(",")[1:] == total.split(",")[1:]

    def test_open_steps_counted_in_total_only(self) -> None:
        draft = turn_summary(self.steps("completed", "pending", "in_progress"))
        assert draft is not None
        assert draft["text"] == "Выполнено 1 из 3 шагов"

    def test_reasons_never_leave(self) -> None:
        steps = [{"n": 1, "title": "t", "status": "blocked", "reason": "Нет года 2031"}]
        draft = turn_summary(steps)
        assert draft is not None
        assert "2031" not in draft["text"]


class TestTranslatorSummaries:
    def test_ok_carries_result(self, tr: StreamTranslator) -> None:
        tr.feed(calls(("run_scenarios", "c4", {"outputs": ["OUT0"]})))
        content = to_json_ok("EBITDA 2026: от 1 200 до 1 501 млн руб.")
        (finished,) = tr.feed(result("c4", "run_scenarios", content))
        assert finished["summary"] == "EBITDA 2026: от 1 200 до 1 501 млн руб."

    def test_offloaded_result(self, tr: StreamTranslator) -> None:
        tr.feed(calls(("run_scenarios", "c4", {})))
        content = json.dumps(
            {
                "status": "ok",
                "result": "Сетка из 3 точек",
                "numbers": [{"value": 3, "label": "n"}],
                "ref": "/results/c4.json",
            },
            ensure_ascii=False,
        )
        (finished,) = tr.feed(result("c4", "run_scenarios", content))
        assert finished["summary"] == "Сетка из 3 точек"

    def test_no_summary(self, tr: StreamTranslator) -> None:
        tr.feed(calls(("resolve_terms", "c2", {}), ("read_file", "c3", {"file_path": "/skills/x/SKILL.md"})))
        (failed,) = tr.feed(result("c2", "resolve_terms", to_json(failure(ErrorCode.AMBIGUOUS_TERM))))
        (skill,) = tr.feed(result("c3", "read_file", "# Skill\nтекст 42"))
        assert failed["summary"] is None
        assert skill["summary"] is None

    def test_custom_retry_has_no_summary(self, tr: StreamTranslator) -> None:
        tr.feed(calls(("run_scenarios", "c3", {})))
        retry, _ = tr.feed(
            {"type": "custom", "data": {"progress": "retry", "tool_call_id": "c3", "code": "engine_unavailable"}}
        )
        assert retry["summary"] is None

    def test_summary_before_answer(self, tr: StreamTranslator) -> None:
        todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "skipped", "reason": "нет"}]
        tr.feed(updates("tools", todos=todos))
        tr.feed(updates("model", messages=[AIMessage("Ответ", id="a1")]))
        out = tr.finish()
        assert kinds(out) == ["summary", "answer"]
        assert out[0]["completed"] == 1
        assert out[0]["skipped"] == 1
        assert out[0]["blocked"] == 0


def to_json_ok(text: str) -> str:
    return json.dumps({"status": "ok", "result": text}, ensure_ascii=False)


class TestPhaseCatalog:
    def test_no_preparing(self) -> None:
        assert set(get_args(Phase)) == {"planning", "executing", "checking", "answering"}
        assert set(PHASE_TEXT) == set(get_args(Phase))
        with pytest.raises(ValidationError):
            make([{"event": "phase", "phase": "preparing", "text": "x"}])
        assert isinstance(make([{"event": "phase", "phase": "planning", "text": "x"}])[0], PhaseEvent)

    def test_preparing_part_gives_nothing(self, tr: StreamTranslator) -> None:
        assert tr.feed({"type": "custom", "data": {"progress": "phase", "phase": "preparing"}}) == []

    def test_openapi_enum(self) -> None:
        import yaml  # pylint: disable=import-outside-toplevel

        spec = yaml.safe_load((Path(__file__).parents[3] / "openapi_copilot.yaml").read_text(encoding="utf-8"))
        phase = spec["components"]["schemas"]["StreamEventPhase"]
        enum = _find_enum(phase, "phase")
        assert enum == list(get_args(Phase))


def _find_enum(node: Any, field: str) -> list[str] | None:
    """The ``enum`` of property ``field`` anywhere under an OpenAPI schema (``allOf`` / ``properties``)."""
    if isinstance(node, dict):
        props = node.get("properties") or {}
        if field in props and "enum" in props[field]:
            return props[field]["enum"]
        for value in node.values():
            if (found := _find_enum(value, field)) is not None:
                return found
    if isinstance(node, list):
        for value in node:
            if (found := _find_enum(value, field)) is not None:
                return found
    return None
