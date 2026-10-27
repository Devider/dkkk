"""Reads without recalculation: ``get_values`` and ``describe_model`` (spec §10 «Каталог»; design S1.7 D4).

Both read the model index of the turn — the cached workbook values the index was built from — by index ID.
Deviation from §10 «Оборачивает»: v1 ``get_output_info`` / ``describe_outputs_sheet`` find the file by ``user_id``
and the indicator by name, the v2 tool gets the path from the turn and an ID from the model.
"""

from collections import Counter

from pydantic import BaseModel, Field

from aigw_service.api.v2.tools.adapter import ToolSpec
from aigw_service.api.v2.tools.context import SHARE_UNIT, ModelIndex, ToolContext
from aigw_service.api.v2.tools.derive import fmt
from aigw_service.api.v2.tools.envelope import ErrorCode, Number, ToolError, ToolSuccess

SUMMARY_CHARS = 600
SUMMARY_TYPES = 5
NO_TYPE = "Без типа"
SHARE_NOTE = "Значения с единицей «доля» указаны в долях: 0.05 = 5 %."


class GetValuesArgs(BaseModel):
    """Arguments of get_values the model chooses."""

    ids: list[str] = Field(default_factory=list, description="ID входов (IN…) и выходов (OUT…) из resolve_terms")
    years: list[int] | None = Field(default=None, description="Годы; без них — все годы, за которые есть значения")


class DescribeModelArgs(BaseModel):
    """describe_model takes no arguments."""


def _cut(text: str) -> str:
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


def _item(index: ModelIndex, item_id: str) -> tuple[str, dict[int, float]]:
    if item_id.startswith("IN"):
        return index.input_name(item_id), dict(index.input_values.get(item_id) or {})
    if item_id.startswith("OUT"):
        return index.output_name(item_id), dict(index.output_values.get(item_id) or {})
    raise ToolError(ErrorCode.UNKNOWN_ID, f"ID {item_id} нет в индексе модели")


def get_values(args: GetValuesArgs, ctx: ToolContext) -> ToolSuccess:
    if not args.ids:
        raise ToolError(ErrorCode.MISSING_INPUT, "Не указаны ID показателей", "Передай ID из resolve_terms.")
    index = ctx.model_index
    items = {i: _item(index, i) for i in dict.fromkeys(args.ids)}
    for item_id, (name, values) in items.items():
        if not values:
            raise ToolError(ErrorCode.NO_VALUES, f"У показателя {item_id} «{name}» нет значений в модели")
    numbers: list[Number] = []
    notes: list[str] = []
    lines: list[str] = []
    data: dict[str, dict] = {}
    for item_id, (name, values) in items.items():  # one indicator after another: a cut list keeps whole ones (S1.6)
        unit = index.unit(item_id)
        years = sorted(set(args.years)) if args.years else sorted(values)
        found = [(y, values[y]) for y in years if y in values]
        numbers += [Number(value=v, label=f"{name}, {y}", unit=unit) for y, v in found]
        if missing := [y for y in years if y not in values]:
            notes.append(f"Нет значения: {name} — " + ", ".join(map(str, missing)))
        shown = ", ".join(f"{y} = {fmt(v)}" for y, v in found) or "нет значений за эти годы"
        lines.append(f"{name}{f' ({unit})' if unit else ''}: {shown}")
        data[item_id] = {"name": name, "unit": unit, "values": {str(y): v for y, v in found}}
    if any(d["unit"] == SHARE_UNIT for d in data.values()):
        notes.append(SHARE_NOTE)
    return ToolSuccess(result=_cut("; ".join(lines) + "."), data=data, numbers=numbers, notes=notes)


def describe_model(args: DescribeModelArgs, ctx: ToolContext) -> ToolSuccess:  # pylint: disable=unused-argument
    index = ctx.model_index
    years = sorted({y for ys in index.output_years.values() for y in ys})
    with_values = [i for i in index.outputs if index.output_years.get(i)]
    headers = len(index.outputs) - len(with_values)
    by_type = Counter(index.output_types.get(i, NO_TYPE) for i in with_values)
    numbers: list[Number] = []
    parts: list[str] = []
    if years:
        numbers += [
            Number(value=years[0], label="Первый год модели"),
            Number(value=years[-1], label="Последний год модели"),
            Number(value=len(years), label="Лет в горизонте модели"),
        ]
        parts.append(f"Горизонт модели: {years[0]}–{years[-1]} ({len(years)} лет)")
    numbers += [
        Number(value=len(index.inputs), label="Входов модели"),
        Number(value=len(with_values), label="Выходов со значениями"),
        Number(value=headers, label="Строк выходов без значений (заголовки разделов)"),
    ]
    parts.append(
        f"входов: {len(index.inputs)}, выходов со значениями: {len(with_values)}, заголовков разделов: {headers}"
    )
    numbers += [Number(value=n, label=f"Выходов типа «{kind}»") for kind, n in by_type.most_common()]
    top = ", ".join(f"«{kind}» — {n}" for kind, n in by_type.most_common(SUMMARY_TYPES))
    rest = len(by_type) - SUMMARY_TYPES
    parts.append(f"по типам: {top}" + (f" и ещё {rest} типов" if rest > 0 else ""))
    data = {
        "horizon": {"first": years[0], "last": years[-1], "years": len(years)} if years else None,
        "counts": {"inputs": len(index.inputs), "outputs": len(with_values), "headers": headers},
        "by_type": dict(by_type.most_common()),
        "outputs": [
            {"id": i, "name": index.outputs[i], "type": index.output_types.get(i), "unit": index.unit(i)}
            for i in with_values
        ],
    }
    return ToolSuccess(result=_cut(". ".join(parts) + "."), data=data, numbers=numbers)


GET_VALUES = ToolSpec(
    name="get_values",
    description=(
        "Показывает значения входов (базовые) и выходов модели по ID за указанные годы без пересчёта. "
        "Вызывай, когда нужно текущее значение показателя или число для derive."
    ),
    args_schema=GetValuesArgs,
    call=get_values,
)

DESCRIBE_MODEL = ToolSpec(
    name="describe_model",
    description=(
        "Описывает загруженную модель: горизонт, число входов и выходов, группы выходов по типу. "
        "Вызывай, когда спрашивают о модели в целом."
    ),
    args_schema=DescribeModelArgs,
    call=describe_model,
)

READ_TOOLS = [GET_VALUES, DESCRIBE_MODEL]
