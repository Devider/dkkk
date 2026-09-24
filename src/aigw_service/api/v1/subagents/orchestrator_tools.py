"""Tool wrappers exposed to the Orchestrator's native tool-calling.

Wraps the existing IFT/EMA subagent flows (`AnalyzerSubAgent` + the calculation functions in
`tools.py`) as LangChain tools, without modifying either. The bound tools take a single
`reformulated_query` argument that the Orchestrator LLM fills in itself; the subagents never see
the full chat history, only that one self-contained request.
"""

import json
from typing import Any, ClassVar, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from aigw_service.api.v1.schemas.llm_outputs import InputItem
from aigw_service.api.v1.subagents.analyzer import AnalyzerSubAgent
from aigw_service.api.v1.tools import ExcelAnalysisToolResult, ModelInputAnalysisToolResult
from aigw_service.api.v1.tools import analyze_excel_model as calculate_excel_model
from aigw_service.api.v1.tools import analyze_model_inputs_for_target as calculate_model_inputs_for_target
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

TOOL_TASK_DESCRIPTIONS: dict[str, str] = {
    "analyze_model_inputs_for_target": (
        "Подобрать комбинацию входных параметров для целевого выхода для заданного года"
    ),
    "analyze_excel_model": (
        "Просчитать несколько сценариев для выбранных целевых показателей по набору "
        "входных параметров в заданных промежутках"
    ),
}


class ToolCallResult(TypedDict):
    """One tool call's outcome, consumed by the Synthesizer."""

    tool_name: str
    task_description: str
    status: str
    result_text: str
    content: dict[str, Any]


class ToolQueryArgs(BaseModel):
    reformulated_query: str = Field(
        description=(
            "Самодостаточная формулировка запроса: включи целевые значения, названия показателей "
            "и параметров, диапазоны, год — всё, что нужно для расчёта, даже если это упоминалось "
            "в более ранних сообщениях. Инструмент не увидит остальную историю переписки, только "
            "эту строку."
        )
    )


@tool(args_schema=ToolQueryArgs)
def analyze_model_inputs_for_target(reformulated_query: str) -> str:
    """Подбор входных параметров, при которых выходной показатель достигает целевого значения.

    Используй, когда пользователь называет ЦЕЛЕВОЕ значение выходного показателя и хочет узнать,
    каким должен быть один или несколько входных параметров, чтобы его достичь.
    """
    raise NotImplementedError("Диспетчеризуется через OrchestratorTools.dispatch, не вызывается напрямую")


@tool(args_schema=ToolQueryArgs)
def analyze_excel_model(reformulated_query: str) -> str:
    """Сценарный анализ «что-если»: пересчёт выходных показателей при изменении входных параметров в диапазоне.

    Используй, когда пользователь хочет увидеть, как меняются один или несколько выходных
    показателей при переборе значений входных параметров в заданном диапазоне (с шагом).
    """
    raise NotImplementedError("Диспетчеризуется через OrchestratorTools.dispatch, не вызывается напрямую")


class OrchestratorTools:
    """Owns the IFT/EMA subagents and executes the Orchestrator's tool calls against them."""

    TOOLS: ClassVar[list] = [analyze_model_inputs_for_target, analyze_excel_model]

    def __init__(self, ift_agent: AnalyzerSubAgent, ema_agent: AnalyzerSubAgent):
        self.ift_agent = ift_agent
        self.ema_agent = ema_agent

    def dispatch(
        self,
        tool_name: str,
        reformulated_query: str,
        available_inputs: dict[str, str],
        available_outputs: dict[str, str],
        filename: str | None,
        user_id: str | None,
    ) -> ToolCallResult:
        if tool_name == "analyze_model_inputs_for_target":
            return self._run_ift(reformulated_query, available_inputs, available_outputs, filename, user_id)
        if tool_name == "analyze_excel_model":
            return self._run_ema(reformulated_query, available_inputs, available_outputs, filename, user_id)
        raise ValueError(f"Unknown orchestrator tool: {tool_name}")

    def _run_ift(
        self,
        reformulated_query: str,
        available_inputs: dict[str, str],
        available_outputs: dict[str, str],
        filename: str | None,
        user_id: str | None,
    ) -> ToolCallResult:
        messages = [HumanMessage(content=reformulated_query)]
        response = self.ift_agent.invoke(messages, available_inputs, available_outputs, user_id)
        q = response.get("q_analysis")
        resolved = response.get("resolved_inputs")
        if q is None or resolved is None:
            return self._error_result("analyze_model_inputs_for_target", "Не удалось распознать параметры запроса")

        input_names = [inp.equivalent_input_name for inp in resolved]
        logger.info(
            "IFT dispatch: input_names={}, output={}, year={}, query={}",
            input_names,
            q.output_name,
            q.output_year,
            reformulated_query,
        )
        calc_result = calculate_model_inputs_for_target(
            file_name=filename,
            output_name=q.output_name,
            output_year=q.output_year,
            target_value=q.target_value,
            input_names=input_names,
            tolerance=0.1,
            max_scenarios=1000,
            user_id=user_id,
        )
        return self._to_tool_result("analyze_model_inputs_for_target", calc_result)

    def _run_ema(
        self,
        reformulated_query: str,
        available_inputs: dict[str, str],
        available_outputs: dict[str, str],
        filename: str | None,
        user_id: str | None,
    ) -> ToolCallResult:
        messages = [HumanMessage(content=reformulated_query)]
        response = self.ema_agent.invoke(messages, available_inputs, available_outputs, user_id)
        q = response.get("q_analysis")
        resolved = response.get("resolved_inputs")
        if q is None or resolved is None:
            return self._error_result("analyze_excel_model", "Не удалось распознать параметры запроса")

        missing_ranges, ranges, steps = self._split_ranges(resolved)
        if missing_ranges:
            calc_result = ExcelAnalysisToolResult(
                status="INFO",
                result=(
                    f"Не указаны числовые диапазоны для параметров: {', '.join(missing_ranges)}. "
                    f"Пожалуйста, уточните диапазоны (min, max, шаг) для этих параметров.\n\n"
                    f"Пример: 'цена метанола от 450 до 500 с шагом 5'"
                ),
                content={},
            )
            return self._to_tool_result("analyze_excel_model", calc_result)

        output_names = [out.equivalent_output_name for out in q.mentioned_outputs]
        years = [q.year] * len(output_names)
        input_names = [inp.equivalent_input_name for inp in resolved]

        logger.info(
            "EMA dispatch: inputs={}, ranges={}, steps={}, outputs={}, year={}, query={}",
            input_names,
            ranges,
            steps,
            output_names,
            q.year,
            reformulated_query,
        )
        calc_result = calculate_excel_model(
            file_name=filename,
            input_names=input_names,
            output_names=output_names,
            output_years=years,
            ranges=ranges,
            steps=steps,
            user_id=user_id,
        )
        return self._to_tool_result("analyze_excel_model", calc_result)

    @staticmethod
    def _split_ranges(resolved: list[InputItem]) -> tuple[list[str], list[list[float]], list[float]]:
        missing_ranges: list[str] = []
        ranges: list[list[float]] = []
        steps: list[float] = []
        for inp in resolved:
            rc = inp.range_config
            if rc and rc.start_value is not None and rc.end_value is not None:
                ranges.append([rc.start_value, rc.end_value])
                steps.append(rc.step if rc.step else 0.5)
            else:
                missing_ranges.append(inp.equivalent_input_name)
        return missing_ranges, ranges, steps

    @staticmethod
    def _to_tool_result(
        tool_name: str, calc_result: ModelInputAnalysisToolResult | ExcelAnalysisToolResult
    ) -> ToolCallResult:
        return ToolCallResult(
            tool_name=tool_name,
            task_description=TOOL_TASK_DESCRIPTIONS[tool_name],
            status=calc_result.status,
            result_text=calc_result.result,
            content=OrchestratorTools._json_safe_content(calc_result.content),
        )

    @staticmethod
    def _json_safe_content(content: dict[str, Any]) -> dict[str, Any]:
        """Drop values tools.py's ``content`` dict may carry that the checkpointer can't persist.

        `analyze_excel_model` embeds a raw ``pandas.DataFrame`` alongside plain file-path strings —
        harmless before a checkpointer existed, but MemorySaver needs every state value
        serializable. The DataFrame's data already lives on disk via the sibling file-path
        entries, so dropping it here loses nothing.
        """
        safe: dict[str, Any] = {}
        for key, value in content.items():
            try:
                json.dumps(value)
            except TypeError:
                continue
            safe[key] = value
        return safe

    @staticmethod
    def _error_result(tool_name: str, message: str) -> ToolCallResult:
        return ToolCallResult(
            tool_name=tool_name,
            task_description=TOOL_TASK_DESCRIPTIONS[tool_name],
            status="ERROR",
            result_text=message,
            content={},
        )
