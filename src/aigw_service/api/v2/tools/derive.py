"""``derive``: derived numbers are computed by a tool, not by the LLM (FR-13, ADR-9; design S1.7 D1, D3).

Pure arithmetic, no recalculation engine. Every operand must match a number the turn may rely on
(``grounding.py``); the calculation uses the matched source value, not the model's rounding.
"""

import math
from collections.abc import Mapping
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, Field

from aigw_service.api.v2.tools.adapter import ToolSpec
from aigw_service.api.v2.tools.context import ToolContext
from aigw_service.api.v2.tools.envelope import ErrorCode, Number, ToolError, ToolSuccess
from aigw_service.api.v2.tools.grounding import Source, collect_sources, match

Operation = Literal["difference", "pct_change", "headroom", "crossing", "elasticity"]

BREACHED_NOTE = "Порог нарушен: запас отрицательный."
NOT_CROSSED_NOTE = "Порог не пересекается в диапазоне; приведена ближайшая к порогу точка."
UNGROUNDED_HINT = (
    "Получи число инструментом (get_values или расчётом) и передай его так, как оно указано в результате "
    "(доли не переводи в проценты)."
)


class Operand(BaseModel):
    """A number taken from a tool result or the user's message."""

    value: float = Field(description="Число так, как оно указано в результате инструмента или в сообщении")
    label: str = Field(default="", description="Что это за число, например «Долг/EBITDA, 2026»")


class Point(BaseModel):
    """One point of a series: input value ``x`` and output value ``y``."""

    x: float = Field(description="Значение входа")
    y: float = Field(description="Значение выхода при этом входе")


class DeriveArgs(BaseModel):
    """Arguments of derive the model chooses."""

    operation: Operation = Field(
        description=(
            "difference — b − a; pct_change — % изменения от a к b; headroom — запас a до порога threshold; "
            "crossing — x, где ряд points пересекает threshold; elasticity — эластичность по двум точкам points"
        )
    )
    a: Operand | None = Field(default=None, description="Первое число (difference, pct_change, headroom)")
    b: Operand | None = Field(default=None, description="Второе число (difference, pct_change)")
    threshold: float | None = Field(default=None, description="Порог (headroom, crossing)")
    direction: Literal["max", "min"] | None = Field(
        default=None, description="headroom: max — значение не должно превышать порог, min — не опускаться ниже"
    )
    points: list[Point] = Field(default_factory=list, description="Точки ряда (crossing — от 2, elasticity — ровно 2)")


REQUIRED: dict[str, tuple[str, ...]] = {
    "difference": ("a", "b"),
    "pct_change": ("a", "b"),
    "headroom": ("a", "threshold", "direction"),
    "crossing": ("points", "threshold"),
    "elasticity": ("points",),
}

# ---------------------------------------------------------------- arithmetic (property-tested, design D7)


def difference(a: float, b: float) -> float:
    return b - a


def pct_change(a: float, b: float) -> float:
    if a == 0:
        raise ToolError(ErrorCode.INVALID_ARGS, "Базовое значение равно 0: процент изменения не определён")
    return (b - a) / abs(a) * 100


def headroom(a: float, threshold: float, direction: str) -> tuple[float, float | None]:
    """Absolute headroom (negative = breached) and headroom in % of ``|threshold|`` (``None`` for a zero threshold)."""
    room = threshold - a if direction == "max" else a - threshold
    return room, (room / abs(threshold) * 100 if threshold != 0 else None)


def crossing(points: list[tuple[float, float]], threshold: float) -> float | None:
    """``x`` of the first crossing of ``threshold`` along ``x`` by linear interpolation; ``None`` if never crossed."""
    ordered = sorted(points)
    for (x1, y1), (x2, y2) in pairwise(ordered):
        d1, d2 = y1 - threshold, y2 - threshold
        if d1 == 0:
            return x1
        if d2 == 0:
            return x2
        if (d1 < 0) != (d2 < 0):
            x = x1 + (threshold - y1) * (x2 - x1) / (y2 - y1)
            return min(max(x, x1), x2)  # float rounding can land 1 ulp outside the segment
    return None


def elasticity(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 or x1 == 0 or y1 == 0:
        raise ToolError(
            ErrorCode.INVALID_ARGS,
            "Эластичность не определена: значения входа совпадают или базовое значение входа или выхода равно 0",
        )
    return ((y2 - y1) / y1) / ((x2 - x1) / x1)


# ---------------------------------------------------------------- grounding and units


def fmt(value: float) -> str:
    """Four significant digits for the summary; ``numbers`` keep the full value."""
    if value == 0 or not math.isfinite(value):
        return str(value)
    decimals = max(0, 3 - math.floor(math.log10(abs(value))))
    text = f"{round(value, decimals):.{decimals}f}"  # no digit grouping: the answer quotes it as is
    return text.rstrip("0").rstrip(".") if "." in text else text


class _Grounded:
    """Matches every operand against the turn's sources; unmatched ones are reported together."""

    def __init__(self, state: Mapping[str, Any]) -> None:
        self.sources = collect_sources(state)
        self.missing: list[str] = []
        self.used: list[dict[str, Any]] = []

    def take(self, role: str, value: float, label: str = "") -> Source:
        source = match(value, self.sources)
        if source is None:
            self.missing.append(f"{value:g} ({role})")
            return Source(value, label or role)
        self.used.append(
            {"role": role, "passed": value, "used": source.value, "source": source.label, "unit": source.unit}
        )
        return Source(source.value, source.label or label or role, source.unit)

    def check(self) -> None:
        if self.missing:
            raise ToolError(
                ErrorCode.INVALID_ARGS,
                "Числа не найдены в результатах инструментов и сообщениях пользователя: " + ", ".join(self.missing),
                UNGROUNDED_HINT,
            )


def common_unit(*sources: Source) -> str | None:
    """The one known unit of the operands; ``bad_units`` when they differ (no conversion, design D3)."""
    units = {s.unit for s in sources if s.unit}
    if len(units) > 1:
        raise ToolError(
            ErrorCode.BAD_UNITS,
            "У чисел разные единицы: " + ", ".join(sorted(units)),
            "Возьми числа в одних единицах (доли и проценты не смешивай) и повтори вызов.",
        )
    return next(iter(units), None)


def _required(args: DeriveArgs) -> None:
    lacking = [f for f in REQUIRED[args.operation] if getattr(args, f) in (None, [])]
    if lacking:
        raise ToolError(
            ErrorCode.MISSING_INPUT,
            f"Для операции {args.operation} не хватает: {', '.join(lacking)}",
            f"Передай {', '.join(lacking)} и повтори вызов.",
        )


# ---------------------------------------------------------------- operations → envelope


def _pair(args: DeriveArgs, grounded: _Grounded) -> tuple[Source, Source]:
    a = grounded.take("a", args.a.value, args.a.label)  # type: ignore[union-attr]  # checked by _required
    b = grounded.take("b", args.b.value, args.b.label)  # type: ignore[union-attr]
    grounded.check()
    return a, b


def _difference(args: DeriveArgs, grounded: _Grounded) -> tuple[str, list[Number], list[str]]:
    a, b = _pair(args, grounded)
    unit = common_unit(a, b)
    value = difference(a.value, b.value)
    label = f"Разница: {b.label} − {a.label}"
    return f"{label} = {fmt(value)}{' ' + unit if unit else ''}.", [Number(value=value, label=label, unit=unit)], []


def _pct_change(args: DeriveArgs, grounded: _Grounded) -> tuple[str, list[Number], list[str]]:
    a, b = _pair(args, grounded)
    common_unit(a, b)
    value = pct_change(a.value, b.value)
    label = f"Изменение, %: от {a.label} к {b.label}"
    return f"{label} = {fmt(value)} %.", [Number(value=value, label=label, unit="%")], []


def _headroom(args: DeriveArgs, grounded: _Grounded) -> tuple[str, list[Number], list[str]]:
    a = grounded.take("a", args.a.value, args.a.label)  # type: ignore[union-attr]
    t = grounded.take("порог", args.threshold)  # type: ignore[arg-type]
    grounded.check()
    unit = common_unit(a, t)
    room, pct = headroom(a.value, t.value, args.direction or "max")
    label = f"Запас до порога {fmt(t.value)}: {a.label}"
    numbers = [Number(value=room, label=label, unit=unit)]
    summary = f"{label} = {fmt(room)}{' ' + unit if unit else ''}"
    if pct is not None:
        numbers.append(Number(value=pct, label=f"{label}, % от порога", unit="%"))
        summary += f" ({fmt(pct)} % от порога)"
    return summary + ".", numbers, [BREACHED_NOTE] if room < 0 else []


def _points(args: DeriveArgs, grounded: _Grounded) -> tuple[list[Source], list[Source]]:
    xs = [grounded.take(f"x{i + 1}", p.x) for i, p in enumerate(args.points)]
    ys = [grounded.take(f"y{i + 1}", p.y) for i, p in enumerate(args.points)]
    return xs, ys


def _crossing(args: DeriveArgs, grounded: _Grounded) -> tuple[str, list[Number], list[str]]:
    if len(args.points) < 2:
        raise ToolError(ErrorCode.INVALID_ARGS, "Для точки пересечения нужно не меньше 2 точек")
    xs, ys = _points(args, grounded)
    t = grounded.take("порог", args.threshold)  # type: ignore[arg-type]
    grounded.check()
    x_unit = common_unit(*xs)
    common_unit(*ys, t)
    pairs = [(x.value, y.value) for x, y in zip(xs, ys, strict=True)]
    found = crossing(pairs, t.value)
    if found is None:
        x, y = min(pairs, key=lambda p: abs(p[1] - t.value))
        label = f"Ближайшая к порогу {fmt(t.value)} точка"
        numbers = [Number(value=x, label=f"{label}: вход", unit=x_unit), Number(value=y, label=f"{label}: выход")]
        return (
            f"Порог {fmt(t.value)} не пересекается; ближе всего при {fmt(x)}: {fmt(y)}.",
            numbers,
            [NOT_CROSSED_NOTE],
        )
    label = f"Точка пересечения порога {fmt(t.value)}"
    return (
        f"{label}: {fmt(found)}{' ' + x_unit if x_unit else ''}.",
        [Number(value=found, label=label, unit=x_unit)],
        [],
    )


def _elasticity(args: DeriveArgs, grounded: _Grounded) -> tuple[str, list[Number], list[str]]:
    if len(args.points) != 2:
        raise ToolError(ErrorCode.INVALID_ARGS, "Для эластичности нужно ровно 2 точки")
    xs, ys = _points(args, grounded)
    grounded.check()
    value = elasticity((xs[0].value, ys[0].value), (xs[1].value, ys[1].value))
    return f"Эластичность = {fmt(value)}.", [Number(value=value, label="Эластичность выхода по входу")], []


OPERATIONS = {
    "difference": _difference,
    "pct_change": _pct_change,
    "headroom": _headroom,
    "crossing": _crossing,
    "elasticity": _elasticity,
}


def derive(args: DeriveArgs, ctx: ToolContext, state: Mapping[str, Any]) -> ToolSuccess:  # pylint: disable=unused-argument
    _required(args)
    grounded = _Grounded(state)
    summary, numbers, notes = OPERATIONS[args.operation](args, grounded)
    return ToolSuccess(
        result=summary,
        data={"operation": args.operation, "operands": grounded.used},
        numbers=numbers,
        notes=notes,
    )


DERIVE = ToolSpec(
    name="derive",
    description=(
        "Считает производные числа: разницу, % изменения, запас до порога, точку пересечения порога, эластичность. "
        "Вызывай вместо подсчёта в уме; числа передавай так, как они указаны в результатах или сообщении пользователя."
    ),
    args_schema=DeriveArgs,
    call=derive,
    needs_state=True,
)
