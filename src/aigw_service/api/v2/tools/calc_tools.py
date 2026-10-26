"""Calculation tools over the excel_engine copy: the model passes index IDs, the domain gets canonical names."""

from pydantic import BaseModel, Field

from aigw_service.api.v2.tools.adapter import ToolSpec
from aigw_service.api.v2.tools.context import ToolContext
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
    min: float = Field(description="Начало диапазона")
    max: float = Field(description="Конец диапазона")
    step: float = Field(description="Шаг сетки")


class RunScenariosArgs(BaseModel):
    """Arguments of run_scenarios the model chooses."""

    inputs: list[InputRange] = Field(description="Входы и их диапазоны")
    outputs: list[str] = Field(description="ID выходов из resolve_terms, например OUT40")
    year: int = Field(description="Год, за который нужны выходы")


class FindInputForTargetArgs(BaseModel):
    """Arguments of find_input_for_target the model chooses."""

    output_id: str = Field(description="ID выхода, который должен достичь цели")
    year: int = Field(description="Год цели")
    target: float = Field(description="Целевое значение выхода, без единиц")
    input_ids: list[str] = Field(description="ID входов, значения которых подбираются")
    tolerance: float = Field(default=IFT_TOLERANCE, description="Допуск в процентах")


def run_scenarios(args: RunScenariosArgs, ctx: ToolContext) -> ExcelAnalysisToolResult:
    index = ctx.model_index
    return analyze_excel_model(
        file_name=ctx.file_path,
        input_names=[index.input_name(i.id) for i in args.inputs],
        output_names=[index.output_name(o) for o in args.outputs],
        output_years=[args.year] * len(args.outputs),
        ranges=[[i.min, i.max] for i in args.inputs],
        steps=[i.step for i in args.inputs],
        user_id=None,  # the path already identifies the user's file (S0.7 D2)
    )


def find_input_for_target(args: FindInputForTargetArgs, ctx: ToolContext) -> ModelInputAnalysisToolResult:
    index = ctx.model_index
    return analyze_model_inputs_for_target(
        file_name=ctx.file_path,
        output_name=index.output_name(args.output_id),
        output_year=args.year,
        target_value=args.target,
        input_names=[index.input_name(i) for i in args.input_ids],
        tolerance=args.tolerance,
        max_scenarios=IFT_MAX_SCENARIOS,
    )


CALC_TOOLS = [
    ToolSpec(
        name="run_scenarios",
        description=(
            "Пересчитывает выходы модели по сетке значений входов (сценарный анализ). "
            "Вызывай с ID из resolve_terms, когда нужно посмотреть выходы при диапазонах входов."
        ),
        args_schema=RunScenariosArgs,
        call=run_scenarios,
        uses_engine=True,
    ),
    ToolSpec(
        name="find_input_for_target",
        description=(
            "Подбирает значения входов, при которых выход достигает цели в заданном году. "
            "Вызывай с ID из resolve_terms, когда спрашивают «каким должен быть X, чтобы Y = Z»."
        ),
        args_schema=FindInputForTargetArgs,
        call=find_input_for_target,
        uses_engine=True,
    ),
]
