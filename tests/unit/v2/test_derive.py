"""S1.7: ``derive`` (ADR-9, FR-13) — property-based arithmetic, number sources, units and the envelope."""

import asyncio
import json
import math
import threading
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from tests.unit.v2.fakes import FakeContext, uploaded_store

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import AgentFactory
from aigw_service.api.v2.scripted import ScriptedChatModel, tool_call
from aigw_service.api.v2.tools.catalog import default_catalog
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.derive import (
    BREACHED_NOTE,
    NOT_CROSSED_NOTE,
    DeriveArgs,
    crossing,
    derive,
    difference,
    elasticity,
    fmt,
    headroom,
    pct_change,
)
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError
from aigw_service.api.v2.tools.grounding import Source, agent_note, collect_sources, match, numbers_in_text

finite = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)
nonzero = finite.filter(lambda v: abs(v) > 1e-6)


def state(*numbers: tuple[float, str, str | None], user: str = "", summaries: list | None = None) -> dict[str, Any]:
    step = {"numbers": [{"value": v, "label": label, "unit": unit} for v, label, unit in numbers]}
    messages = [HumanMessage(user)] if user else []
    return {"step_results": [step], "messages": messages, "turn_summaries": summaries or []}


def run(state_: dict[str, Any], **args: Any) -> Any:
    return derive(DeriveArgs.model_validate(args), FakeContext(), state_)


def code(state_: dict[str, Any], **args: Any) -> str:
    with pytest.raises(ToolError) as exc_info:
        run(state_, **args)
    return exc_info.value.code


# ---------------------------------------------------------------- arithmetic properties


class TestArithmetic:
    @given(finite, finite)
    def test_difference_is_antisymmetric(self, a: float, b: float) -> None:
        assert difference(a, b) == -difference(b, a)

    @given(nonzero)
    def test_no_change_is_zero_percent(self, a: float) -> None:
        assert pct_change(a, a) == 0

    @given(nonzero, finite)
    def test_pct_change_restores_b(self, a: float, b: float) -> None:
        assert a + pct_change(a, b) / 100 * abs(a) == pytest.approx(b, rel=1e-9, abs=1e-6)

    @given(finite, nonzero, st.sampled_from(["max", "min"]))
    def test_headroom_sign_flips_at_threshold(self, a: float, t: float, direction: str) -> None:
        room, pct = headroom(a, t, direction)
        within = a <= t if direction == "max" else a >= t
        assert (room >= 0) == within
        assert pct == pytest.approx(room / abs(t) * 100)

    def test_zero_threshold_has_no_percent(self) -> None:
        assert headroom(1.0, 0.0, "max") == (-1.0, None)

    @given(nonzero, finite, st.lists(finite, min_size=2, max_size=20, unique=True), finite)
    def test_crossing_of_a_line_is_its_root(self, k: float, c: float, xs: list[float], t: float) -> None:
        points = [(x, k * x + c) for x in xs]
        ys = [y for _, y in points]
        assume(min(ys) < t < max(ys))
        found = crossing(points, t)
        assert found is not None
        scale = max(abs(y) for y in ys) + abs(t) + abs(c)
        assert k * found + c == pytest.approx(t, abs=1e-9 * scale + 1e-9)

    @given(st.lists(st.tuples(finite, finite), min_size=2, max_size=20, unique_by=lambda p: p[0]), finite)
    def test_crossing_lies_in_its_segment(self, points: list[tuple[float, float]], t: float) -> None:
        found = crossing(points, t)
        ordered = sorted(points)
        if found is None:
            assert all(y > t for _, y in points) or all(y < t for _, y in points)
            return
        assert ordered[0][0] <= found <= ordered[-1][0]

    @given(nonzero, nonzero, nonzero)
    def test_elasticity_of_proportional_line_is_one(self, k: float, x1: float, x2: float) -> None:
        assume(abs(x1 - x2) > 1e-6 * max(abs(x1), abs(x2)))
        assert elasticity((x1, k * x1), (x2, k * x2)) == pytest.approx(1.0, rel=1e-6)

    @pytest.mark.parametrize(
        "call",
        [
            lambda: pct_change(0.0, 1.0),
            lambda: elasticity((1.0, 1.0), (1.0, 2.0)),
            lambda: elasticity((0.0, 1.0), (1.0, 2.0)),
            lambda: elasticity((1.0, 0.0), (2.0, 2.0)),
        ],
    )
    def test_zero_denominator_is_invalid_args(self, call: Any) -> None:
        with pytest.raises(ToolError) as exc_info:
            call()
        assert exc_info.value.code == ErrorCode.INVALID_ARGS

    @pytest.mark.parametrize(
        ("value", "text"), [(0.7, "0.7"), (3931.847, "3932"), (-12.345678, "-12.35"), (125000.4, "125000")]
    )
    def test_fmt(self, value: float, text: str) -> None:
        assert fmt(value) == text


# ---------------------------------------------------------------- sources (design D1)


class TestSources:
    @pytest.mark.parametrize(
        ("text", "values"),
        [
            ("ковенант 3,5x", [3.5]),
            ("выручка 1 200 млн", [1200.0]),
            ("рост на 15%", [15.0]),
            ("убыток −2.5", [-2.5]),
            ("в 2025-2027 годах", [2025.0, 2027.0]),
        ],
    )
    def test_numbers_in_text(self, text: str, values: list[float]) -> None:
        assert [s.value for s in numbers_in_text(text, "msg")] == values

    def test_match_keeps_the_sign(self) -> None:
        """S4.1 regression: the answer check ignores the sign, ``derive`` operands do not."""
        assert match(30.0, [Source(-30.0, "a")]) is None
        assert match(-30.0, [Source(-30.0, "a")]) == Source(-30.0, "a")
        assert match(2.81, [Source(2.8134, "a")]) == Source(2.8134, "a")

    def test_agent_notes_are_not_sources(self) -> None:
        """S4.1: guard reminders and pinned skill texts are in the user's role but are not the user's words."""
        skill = HumanMessage("Вывод — 1–3 предложения, порог 4,5", additional_kwargs={"lc_source": "pinned_skill"})
        messages = [HumanMessage("Порог 3,5x"), skill, agent_note("Чисел 57,4 нет в результатах")]
        assert [s.value for s in collect_sources({"messages": messages})] == [3.5]

    def test_percent_after_number_is_a_unit(self) -> None:
        assert numbers_in_text("15 %", "msg")[0].unit == "%"

    def test_all_three_kinds(self) -> None:
        summaries = [{"results": [{"numbers": [{"label": "EBITDA 2025", "value": 1100.0}]}]}]
        sources = collect_sources(state((2.8134, "Долг/EBITDA 2026", None), user="порог 3,5", summaries=summaries))
        assert {s.value for s in sources} == {2.8134, 3.5, 1100.0}

    @pytest.mark.parametrize(
        ("value", "source", "found"),
        [
            (2.81, 2.8134, True),
            (2.8134, 2.8134, True),
            (3.0, 2.6, False),
            (300.0, 300.4, True),
            (0.0, 0.0, True),
            (0.001, 0.0, False),
            (2.9, 2.8134, False),
        ],
    )
    def test_match_tolerance(self, value: float, source: float, found: bool) -> None:
        assert (match(value, [Source(source, "s")]) is not None) is found

    def test_nearest_source_wins(self) -> None:
        assert match(2.81, [Source(2.8134, "far"), Source(2.8101, "near")]).label == "near"


# ---------------------------------------------------------------- tool envelope


class TestDerive:
    def test_difference_uses_source_value_and_unit(self) -> None:
        s = state((1100.0, "EBITDA 2025", "USD млн"), (1265.94, "EBITDA 2026", "USD млн"))
        out = run(s, operation="difference", a={"value": 1100}, b={"value": 1265.9})
        assert out.numbers[0].value == pytest.approx(165.94)
        assert out.numbers[0].unit == "USD млн"
        assert out.data["operands"][1]["used"] == 1265.94
        assert "EBITDA 2026" in out.numbers[0].label

    def test_pct_change(self) -> None:
        out = run(
            state((200.0, "a", None), (250.0, "b", None)), operation="pct_change", a={"value": 200}, b={"value": 250}
        )
        assert (out.numbers[0].value, out.numbers[0].unit) == (25.0, "%")

    def test_headroom_from_user_threshold(self) -> None:
        s = state((2.8134, "Долг/EBITDA 2026", None), user="ковенант 3,5x")
        out = run(s, operation="headroom", a={"value": 2.81}, threshold=3.5, direction="max")
        assert [n.value for n in out.numbers] == pytest.approx([0.6866, 0.6866 / 3.5 * 100])
        assert out.notes == []

    def test_headroom_breached(self) -> None:
        s = state((4.1, "Долг/EBITDA 2027", None), user="порог 3,5")
        out = run(s, operation="headroom", a={"value": 4.1}, threshold=3.5, direction="max")
        assert out.numbers[0].value < 0
        assert out.notes == [BREACHED_NOTE]

    def test_crossing_over_grid(self) -> None:
        s = state(
            (300, "Метанол, значение в сетке", None),
            (400, "Метанол, значение в сетке", None),
            (2.0, "DSCR при 300", None),
            (4.0, "DSCR при 400", None),
            user="DSCR не ниже 3",
        )
        out = run(s, operation="crossing", threshold=3, points=[{"x": 300, "y": 2.0}, {"x": 400, "y": 4.0}])
        assert out.numbers[0].value == pytest.approx(350.0)

    def test_crossing_not_found_is_a_result(self) -> None:
        s = state((300, "x", None), (400, "x", None), (2.0, "y", None), (2.5, "y", None), user="порог 3")
        out = run(s, operation="crossing", threshold=3, points=[{"x": 300, "y": 2.0}, {"x": 400, "y": 2.5}])
        assert out.notes == [NOT_CROSSED_NOTE]
        assert [n.value for n in out.numbers] == [400, 2.5]

    def test_elasticity(self) -> None:
        s = state((100, "x", None), (110, "x", None), (50, "y", None), (60, "y", None))
        out = run(s, operation="elasticity", points=[{"x": 100, "y": 50}, {"x": 110, "y": 60}])
        assert out.numbers[0].value == pytest.approx(2.0)
        assert out.numbers[0].unit is None

    def test_invented_operand_is_rejected(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            run(state((2.8134, "a", None)), operation="headroom", a={"value": 2.81}, threshold=3.5, direction="max")
        assert exc_info.value.code == ErrorCode.INVALID_ARGS
        assert "3.5 (порог)" in exc_info.value.message
        assert "get_values" in exc_info.value.hint

    def test_every_invented_operand_is_listed(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            run(state(), operation="difference", a={"value": 1}, b={"value": 2})
        assert "1 (a)" in exc_info.value.message
        assert "2 (b)" in exc_info.value.message

    def test_different_units_are_bad_units(self) -> None:
        s = state((0.05, "Инфляция, 2026", "доля"), user="инфляция 7 %")
        assert code(s, operation="difference", a={"value": 0.05}, b={"value": 7}) == ErrorCode.BAD_UNITS

    @pytest.mark.parametrize(
        ("args", "lacking"),
        [
            ({"operation": "difference", "a": {"value": 1}}, "b"),
            ({"operation": "headroom", "a": {"value": 1}}, "threshold"),
            ({"operation": "crossing", "threshold": 1}, "points"),
        ],
    )
    def test_missing_field_is_missing_input(self, args: dict, lacking: str) -> None:
        with pytest.raises(ToolError) as exc_info:
            run(state(), **args)
        assert exc_info.value.code == ErrorCode.MISSING_INPUT
        assert lacking in exc_info.value.message

    def test_point_counts(self) -> None:
        s = state((1, "x", None), (2, "y", None))
        assert code(s, operation="crossing", threshold=2, points=[{"x": 1, "y": 2}]) == ErrorCode.INVALID_ARGS
        assert code(s, operation="elasticity", points=[{"x": 1, "y": 2}]) == ErrorCode.INVALID_ARGS

    def test_division_by_zero_after_grounding(self) -> None:
        s = state((0.0, "a", None), (5.0, "b", None))
        assert code(s, operation="pct_change", a={"value": 0}, b={"value": 5}) == ErrorCode.INVALID_ARGS

    def test_no_nan_in_numbers(self) -> None:
        s = state((2.0, "a", None), (3.0, "b", None))
        out = run(s, operation="difference", a={"value": 2}, b={"value": 3})
        assert all(math.isfinite(n.value) for n in out.numbers)


def test_ai_message_numbers_are_not_sources() -> None:
    sources = collect_sources({"messages": [AIMessage("запас 0.69")]})
    assert sources == []


# ---------------------------------------------------------------- in the agent (tasks 7.1, 7.2)

AGENT_INDEX = ModelIndex(
    inputs={"IN1": "Метанол"},
    outputs={"OUT0": "Долг/EBITDA"},
    input_values={"IN1": {2026: 386.7}},
    output_years={"OUT0": (2026,)},
    output_values={"OUT0": {2026: 2.8134}},
)


async def agent_turn(tmp_path: Path, steps: list, question: str) -> dict[str, Any]:
    store = await uploaded_store("u1", AGENT_INDEX, str(tmp_path))
    agent = AgentFactory(
        ScriptedChatModel(steps=steps),
        default_catalog(),
        store=store,
        upload_dir=str(tmp_path),
        plan_mode="off",
        pinned_skills=(),
    ).build()
    return await agent.ainvoke(
        {"messages": [HumanMessage(question)]}, context=RequestContext(user_id="u1", session_id="s1")
    )


def envelope_of(out: dict[str, Any], name: str) -> dict[str, Any]:
    return json.loads(next(m.content for m in out["messages"] if isinstance(m, ToolMessage) and m.name == name))


class TestInAgent:
    async def test_headroom_from_values_and_user_threshold(self, tmp_path: Path) -> None:
        steps = [
            tool_call("get_values", "c1", ids=["OUT0"], years=[2026]),
            tool_call(
                "derive",
                "c2",
                operation="headroom",
                a={"value": 2.81, "label": "Долг/EBITDA 2026"},
                threshold=3.5,
                direction="max",
            ),
            AIMessage("Запас до ковенанта — 0.69x."),
        ]
        out = await agent_turn(tmp_path, steps, "Какой запас до ковенанта Долг/EBITDA 3,5x в 2026?")
        derived = next(s for s in out["step_results"] if s["tool"] == "derive")
        assert derived["status"] == "ok"
        assert derived["numbers"][0]["value"] == pytest.approx(3.5 - 2.8134)

    async def test_invented_operand_is_rejected_without_engine(self, tmp_path: Path, inline_gate: Any) -> None:
        steps = [
            tool_call("derive", "c1", operation="difference", a={"value": 1100}, b={"value": 1265.9}),
            AIMessage("Не удалось посчитать."),
        ]
        out = await agent_turn(tmp_path, steps, "На сколько вырастет EBITDA?")
        assert envelope_of(out, "derive")["code"] == ErrorCode.INVALID_ARGS
        assert inline_gate.sessions == []

    @pytest.mark.parametrize(
        "call",
        [
            tool_call("get_values", "c1", ids=["OUT0"]),
            tool_call("describe_model", "c1"),
            tool_call("derive", "c1", operation="difference", a={"value": 2.8134}, b={"value": 2.8134}),
        ],
        ids=["get_values", "describe_model", "derive"],
    )
    async def test_reads_run_off_the_event_loop(self, tmp_path: Path, call: Any) -> None:
        """AR-Q9 «Чтение не блокирует event loop»: the call runs in a worker thread while the loop serves others."""
        loop_thread = threading.get_ident()
        seen: dict[str, Any] = {}
        name = call.tool_calls[0]["name"]
        spec = default_catalog().spec(name)  # the module-level spec every catalog builds its tool from
        original = spec.call
        released = threading.Event()

        def probe(*args: Any) -> Any:
            seen["thread"] = threading.get_ident()
            seen["released"] = released.wait(timeout=5)  # True only if the loop ran ``other`` meanwhile
            return original(*args)

        async def other() -> None:
            await asyncio.sleep(0.01)
            seen["other_ran"] = True
            released.set()

        object.__setattr__(spec, "call", probe)  # frozen dataclass
        try:
            task = asyncio.create_task(other())
            await agent_turn(tmp_path, [call, AIMessage("ок")], "2026: 2.8134")
            await task
        finally:
            object.__setattr__(spec, "call", original)
        assert seen["thread"] != loop_thread
        assert seen["released"]
        assert seen["other_ran"]
