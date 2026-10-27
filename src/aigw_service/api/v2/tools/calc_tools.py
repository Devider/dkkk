"""Calculation tools over the excel_engine copy: the model passes index IDs, the domain gets canonical names."""

import math
from typing import Any

from pydantic import BaseModel, Field

from aigw_service.api.v2.tools.adapter import ToolSpec
from aigw_service.api.v2.tools.context import ToolContext
from aigw_service.api.v2.tools.engine_gate import EngineGate, default_gate
from aigw_service.api.v2.tools.envelope import Number, ToolFailure, ToolSuccess
from aigw_service.api.v2.tools.prechecks import GridInput, check_scenarios, check_target
from aigw_service.api.v2.tools.v1_results import UNREACHED_NOTE, classify_v1, is_unreached
from aigw_service.config import APP_CONFIG
from excel_engine import (
    ExcelAnalysisToolResult,
    ModelInputAnalysisToolResult,
    analyze_excel_model,
    analyze_model_inputs_for_target,
)

# Same values v1 passes (OrchestratorTools._run_ift), so v1 and v2 compute the same thing
IFT_MAX_SCENARIOS = 1000
IFT_TOLERANCE = 0.1


class InputRange(BaseModel):
    """Grid for one model input."""

    id: str = Field(description="ID входа из resolve_terms, например IN12")
    min: float | None = Field(default=None, description="Начало диапазона; не придумывай, если пользователь не задал")
    max: float | None = Field(default=None, description="Конец диапазона; для одной точки равен началу")
    step: float | None = Field(default=None, description="Шаг сетки; не придумывай, если пользователь не задал")


class RunScenariosArgs(BaseModel):
    """Arguments of run_scenarios the model chooses."""

    inputs: list[InputRange] = Field(default_factory=list, description="Входы и их диапазоны")
    outputs: list[str] = Field(default_factory=list, description="ID выходов из resolve_terms, например OUT40")
    year: int | None = Field(default=None, description="Год, за который нужны выходы; не придумывай, если не назван")


class FindInputForTargetArgs(BaseModel):
    """Arguments of find_input_for_target the model chooses."""

    output_id: str = Field(description="ID выхода, который должен достичь цели")
    year: int | None = Field(default=None, description="Год цели; не придумывай, если не назван")
    target: float | None = Field(default=None, description="Целевое значение выхода, без единиц; не придумывай")
    input_ids: list[str] = Field(default_factory=list, description="ID входов, значения которых подбираются")
    tolerance: float = Field(default=IFT_TOLERANCE, description="Допуск в процентах")


def _gate() -> EngineGate:
    """The service-wide gate; unit tests replace it with an in-process one."""
    return default_gate()


async def run_scenarios(args: RunScenariosArgs, ctx: ToolContext) -> ToolSuccess | ToolFailure:
    index = ctx.model_index
    grid = [GridInput(i.id, i.min, i.max, i.step) for i in args.inputs]
    check_scenarios(index, grid, args.outputs, args.year, APP_CONFIG.agent.max_grid)
    input_names = [index.input_name(i.id) for i in args.inputs]
    result = await _gate().run(
        ctx.thread_id,
        analyze_excel_model,
        file_name=ctx.file_path,
        input_names=input_names,
        output_names=[index.output_name(o) for o in args.outputs],
        output_years=[args.year] * len(args.outputs),
        ranges=[[i.min, i.max] for i in args.inputs],
        steps=[i.step for i in args.inputs],
        user_id=None,  # the path already identifies the user's file (S0.7 D2)
    )
    return scenarios_envelope(result, input_names)


async def find_input_for_target(args: FindInputForTargetArgs, ctx: ToolContext) -> ToolSuccess | ToolFailure:
    index = ctx.model_index
    check_target(index, args.output_id, args.year, args.target, args.input_ids, args.tolerance)
    output_name = index.output_name(args.output_id)
    result = await _gate().run(
        ctx.thread_id,
        analyze_model_inputs_for_target,
        file_name=ctx.file_path,
        output_name=output_name,
        output_year=args.year,
        target_value=args.target,
        input_names=[index.input_name(i) for i in args.input_ids],
        tolerance=args.tolerance,
        max_scenarios=IFT_MAX_SCENARIOS,
    )
    return target_envelope(result, output_name, args.year)


# ---------------------------------------------------------------- envelopes (spec §10 «Контракт» п. 3)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def scenarios_envelope(result: ExcelAnalysisToolResult, input_names: list[str]) -> ToolSuccess | ToolFailure:
    """Grid rows in ``data.rows``; every output value in ``numbers`` labelled with its scenario.

    ``numbers`` start with the minimum and maximum of each output, so a cut list in an offloaded result keeps them
    (S1.6 design D3).
    """
    failed = classify_v1(result.status, result.result)
    if failed:
        return failed
    frame = (result.content or {}).get("result_df")
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []
    numbers: list[Number] = []
    extremes: dict[str, tuple[int, int]] = {}  # output → positions in numbers of its minimum and maximum
    for row in rows:
        scenario = ", ".join(f"{name} = {row.get(name)}" for name in input_names)
        for column, value in row.items():
            number = _finite(value)
            if column in input_names or number is None:
                continue
            numbers.append(Number(value=number, label=f"{column} при {scenario}"))
            at = len(numbers) - 1
            low, high = extremes.get(column, (at, at))
            low = at if number < numbers[low].value else low
            high = at if number > numbers[high].value else high
            extremes[column] = (low, high)
    first = list(dict.fromkeys(i for pair in extremes.values() for i in pair))
    ordered = [numbers[i] for i in first] + [n for i, n in enumerate(numbers) if i not in set(first)]
    summary = f"Сценариев: {len(rows)}." + "".join(
        f" {col}: от {numbers[lo].value} до {numbers[hi].value}." for col, (lo, hi) in extremes.items()
    )
    return ToolSuccess(result=summary if rows else result.result, data={"rows": rows}, numbers=ordered)


def target_envelope(result: ModelInputAnalysisToolResult, output_name: str, year: int) -> ToolSuccess | ToolFailure:
    """Found scenarios (or the nearest one for an unreached target) with their input and output values."""
    failed = classify_v1(result.status, result.result)
    if failed:
        return failed
    content = result.content or {}
    notes: list[str] = []
    scenarios = content.get("matching_scenarios") or []
    reachable = not is_unreached(result.status, result.result)
    if not reachable:
        notes.append(UNREACHED_NOTE)
        nearest = min(
            content.get("all_scenarios") or [], key=lambda s: abs(s.get("deviation", math.inf)), default=None
        )
        scenarios = [nearest] if nearest else []
    output_label = f"{content.get('actual_output_name') or output_name} {year}"
    numbers = [
        Number(value=v, label=f"{name}, базовое значение")
        for name, raw in (content.get("current_input_values") or {}).items()
        if (v := _finite(raw)) is not None
    ]
    for scenario in scenarios:
        numbers += [
            Number(value=v, label=name)
            for name, raw in scenario["input_values"].items()
            if (v := _finite(raw)) is not None
        ]
        if (v := _finite(scenario.get("output_value"))) is not None:
            numbers.append(Number(value=v, label=output_label))
    data = {
        "reachable": reachable,
        "output": output_label,
        "target": content.get("target_value"),
        "scenarios": [
            {
                "inputs": s["input_values"],
                "output": s.get("output_value"),
                "deviation_percent": s.get("deviation_percent"),
            }
            for s in scenarios
        ],
        "base_inputs": content.get("current_input_values"),
    }
    return ToolSuccess(result=result.result.split("\n\n", 1)[0], data=data, numbers=numbers, notes=notes)


CALC_TOOLS = [
    ToolSpec(
        name="run_scenarios",
        description=(
            "Пересчитывает выходы модели по сетке значений входов (сценарный анализ). "
            "Вызывай с ID из resolve_terms, когда нужно посмотреть выходы при диапазонах входов."
        ),
        args_schema=RunScenariosArgs,
        acall=run_scenarios,
        uses_engine=True,
    ),
    ToolSpec(
        name="find_input_for_target",
        description=(
            "Подбирает значения входов, при которых выход достигает цели в заданном году. "
            "Вызывай с ID из resolve_terms, когда спрашивают «каким должен быть X, чтобы Y = Z»."
        ),
        args_schema=FindInputForTargetArgs,
        acall=find_input_for_target,
        uses_engine=True,
    ),
]
