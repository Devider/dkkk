"""Calculation arguments checked against the model index before LibreOffice starts (spec §10 «Контракт» п. 2).

Every problem comes back as a coded ``ToolError``; missing year, target or range is never filled in (FR-10).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError

UNITS_RATIO = 20.0
UNITS_HINT = (
    "Пересчитай диапазон в тех же единицах, что и базовое значение (доля 0,05 = 5 %), и повтори. "
    "Если диапазон уже в единицах базы, проверь, что он задан относительно неё."
)


@dataclass(frozen=True)
class GridInput:
    """One scenario input as the model sent it; ``None`` means the user did not give it."""

    id: str
    min: float | None
    max: float | None
    step: float | None


def _fmt(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def require(missing: list[str]) -> None:
    """FR-10: name everything that is missing at once, never substitute a default."""
    if missing:
        raise ToolError(ErrorCode.MISSING_INPUT, "Не указаны: " + "; ".join(missing))


def output_has_values(index: ModelIndex, output_id: str) -> None:
    if index.output_years and not index.output_years.get(output_id):
        raise ToolError(
            ErrorCode.NO_VALUES,
            f"У показателя «{index.output_name(output_id)}» ({output_id}) нет значений в модели",
        )


def year_in_horizon(index: ModelIndex, output_id: str, year: int) -> None:
    years = index.output_years.get(output_id)
    if years and year not in years:
        raise ToolError(
            ErrorCode.INVALID_ARGS,
            f"У «{index.output_name(output_id)}» нет значения за {year}; доступны {years[0]}–{years[-1]}",
            "Выбери год из доступных или уточни год у пользователя.",
        )


def points(item: GridInput) -> int:
    return math.floor((item.max - item.min) / item.step + 1e-9) + 1


def ranges_valid(index: ModelIndex, grid: Sequence[GridInput], max_grid: int) -> None:
    bad = [
        f"«{index.input_name(i.id)}»: {_fmt(i.min)}–{_fmt(i.max)}, шаг {_fmt(i.step)}"
        for i in grid
        if i.min > i.max or i.step <= 0
    ]
    if bad:
        raise ToolError(
            ErrorCode.INVALID_ARGS,
            "Некорректный диапазон (нужно начало ≤ конец и шаг > 0): " + "; ".join(bad),
        )
    size = math.prod(points(i) for i in grid)
    if size > max_grid:
        raise ToolError(
            ErrorCode.INVALID_ARGS,
            f"Сетка из {size} сценариев больше лимита {max_grid}",
            "Сократи число входов, сузь диапазоны или увеличь шаг так, чтобы сценариев было не больше лимита.",
        )


def base_value(index: ModelIndex, input_id: str, year: int) -> float | None:
    """Input value in the year, else in the nearest year that has one."""
    values = index.input_values.get(input_id) or {}
    if not values:
        return None
    if year in values:
        return values[year]
    return values[min(values, key=lambda y: abs(y - year))]


def units_match(index: ModelIndex, grid: Sequence[GridInput], year: int) -> None:
    """Share vs percent mix-up against the base value (design D5): a 20× gap in the other direction."""
    for item in grid:
        base = base_value(index, item.id, year)
        if base is None:
            continue
        span = max(abs(item.min), abs(item.max))
        share_as_percent = abs(base) <= 1 and span >= 2 and span >= UNITS_RATIO * max(abs(base), 0.01)
        percent_as_share = abs(base) >= 2 and abs(base) >= UNITS_RATIO * max(span, 0.01)
        if share_as_percent or percent_as_share:
            raise ToolError(
                ErrorCode.BAD_UNITS,
                f"Базовое значение «{index.input_name(item.id)}» в {year}: {_fmt(base)}; "
                f"диапазон {_fmt(item.min)}–{_fmt(item.max)}",
                UNITS_HINT,
            )


def check_scenarios(
    index: ModelIndex, grid: Sequence[GridInput], outputs: Sequence[str], year: int | None, max_grid: int
) -> None:
    """run_scenarios: design D3 order; returns only when the engine may run."""
    missing = [] if year is not None else ["год расчёта"]
    if not grid:
        missing.append("входы и их диапазоны")
    if not outputs:
        missing.append("выходы")
    missing += [
        f"диапазон (начало, конец, шаг) для «{index.inputs.get(i.id, i.id)}»"
        for i in grid
        if None in (i.min, i.max, i.step)
    ]
    require(missing)
    for item in grid:
        index.input_name(item.id)  # unknown_id
    for output_id in outputs:
        index.output_name(output_id)
    for output_id in outputs:
        output_has_values(index, output_id)
    for output_id in outputs:
        year_in_horizon(index, output_id, year)
    ranges_valid(index, grid, max_grid)
    units_match(index, grid, year)


def check_target(
    index: ModelIndex,
    output_id: str,
    year: int | None,
    target: float | None,
    input_ids: Sequence[str],
    tolerance: float,
) -> None:
    """find_input_for_target: design D3 order; returns only when the engine may run."""
    missing = [] if year is not None else ["год цели"]
    if target is None:
        missing.append("целевое значение")
    if not input_ids:
        missing.append("входы, значения которых подбираются")
    require(missing)
    index.output_name(output_id)
    for input_id in input_ids:
        index.input_name(input_id)
    output_has_values(index, output_id)
    year_in_horizon(index, output_id, year)
    if tolerance <= 0:
        raise ToolError(ErrorCode.INVALID_ARGS, f"Допуск должен быть больше нуля, получено {_fmt(tolerance)}")
