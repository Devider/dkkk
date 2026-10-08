"""Tool wrappers exposed to the Orchestrator's native tool-calling.

Wraps the existing IFT/EMA subagent flows (`AnalyzerSubAgent` + the calculation functions in
`tools.py`) as LangChain tools, without modifying either. The bound tools take a single
`reformulated_query` argument that the Orchestrator LLM fills in itself; the subagents never see
the full chat history, only that one self-contained request.
"""

import asyncio
import concurrent.futures
import json
import multiprocessing
from typing import Any, ClassVar, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import get_async_callback_manager_for_config
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from aigw_service.api.v1.schemas.llm_outputs import InputItem
from aigw_service.api.v1.subagents.analyzer import AnalyzerSubAgent
from aigw_service.api.v1.tools import ExcelAnalysisToolResult, ModelInputAnalysisToolResult
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


def _safe_content(content: dict[str, Any]) -> dict[str, Any]:
    """Убрать из content значения, не сериализуемые через канал процессов.

    ``analyze_excel_model`` кладёт в content сырой ``pandas.DataFrame`` (``result_df``),
    который нельзя безопасно вернуть из отдельного процесса и который не нужен
    потребителю — данные уже отданы в ``result``. Оставляем только то, что можно
    ``json.dumps``.
    """
    safe: dict[str, Any] = {}
    for key, value in content.items():
        try:
            json.dumps(value)
        except TypeError:
            continue
        safe[key] = value
    return safe


def _run_lo_calc(func_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Точка входа в отдельном процессе: выполняет LO-расчёт с собственным GIL.

    Вызывается из ``ProcessPoolExecutor`` из ``OrchestratorTools``. Логируем/возвращаем
    только сериализуемое (без ``DataFrame``). Кэш расчётов (``_ANALYSIS_CACHE`` в
    ``tools.py``) живёт внутри worker-процесса и переиспользуется между вызовами
    пула, поэтому повторные запросы не теряют преимуществ кэша.
    """
    from aigw_service.api.v1 import tools as _tools

    func = getattr(_tools, func_name)
    result = func(**kwargs)
    return {
        "status": result.status,
        "result": result.result,
        "content": _safe_content(result.content),
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
        # LO-расчёты выполняем в отдельном процессе (свой GIL), чтобы интенсивный
        # Python-код (сценарии, scipy, openpyxl) не блокировал event loop и /health.
        # spawn вместо fork: fork из worker-потока LangGraph опасен (дедлоки).
        self._lo_pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        )

    async def dispatch(
        self,
        tool_name: str,
        reformulated_query: str,
        available_inputs: dict[str, str],
        available_outputs: dict[str, str],
        filename: str | None,
        user_id: str | None,
        config: RunnableConfig | None = None,
    ) -> ToolCallResult:
        if tool_name == "analyze_model_inputs_for_target":
            return await self._run_ift(
                reformulated_query, available_inputs, available_outputs, filename, user_id, config
            )
        if tool_name == "analyze_excel_model":
            return await self._run_ema(
                reformulated_query, available_inputs, available_outputs, filename, user_id, config
            )
        raise ValueError(f"Unknown orchestrator tool: {tool_name}")

    async def _dispatch_lo_calc(
        self, tool_name: str, kwargs: dict[str, Any], config: RunnableConfig | None
    ) -> dict[str, Any]:
        """Выполняет LO-расчёт в пуле, оборачивая его явным LangChain tool-run'ом.

        Используем ``on_tool_start``/``on_tool_end``/``on_tool_error`` через callback-менеджер,
        унаследованный от ``config`` узла графа, а не сырой ``Langfuse.start_as_current_observation``:
        LangGraph выполняет тело каждого узла в свежесозданном ``asyncio.Task``, чей скопированный
        context НЕ содержит OTEL-спан, который Langfuse's ``CallbackHandler.on_chain_start``
        прикрепляет к ambient-контексту - поэтому raw OTEL-подход давал отдельный root trace без
        родителя (проверено эмпирически). ``parent_run_id``-механизм LangChain, в отличие от этого,
        корректно прокидывается через ``config["callbacks"]"`` и всегда даёт правильное вложение.
        Безопасно no-op'ает, если в ``config`` нет колбэков (трейсинг отключён) - ``on_tool_start``
        без хендлеров ничего не делает.
        """
        manager = get_async_callback_manager_for_config(config or {})
        run_manager = await manager.on_tool_start({"name": tool_name}, str(kwargs), name=tool_name, inputs=kwargs)
        try:
            calc = await asyncio.to_thread(self._lo_pool.submit(_run_lo_calc, tool_name, kwargs).result)
        except Exception as e:
            await run_manager.on_tool_error(e)
            raise
        await run_manager.on_tool_end(calc)
        return calc

    async def _run_ift(
        self,
        reformulated_query: str,
        available_inputs: dict[str, str],
        available_outputs: dict[str, str],
        filename: str | None,
        user_id: str | None,
        config: RunnableConfig | None = None,
    ) -> ToolCallResult:
        messages = [HumanMessage(content=reformulated_query)]
        response = await self.ift_agent.ainvoke(messages, available_inputs, available_outputs, user_id)
        q = response.get("q_analysis")
        resolved = response.get("resolved_inputs")
        if q is None or resolved is None:
            return self._error_result("analyze_model_inputs_for_target", "Не удалось распознать параметры запроса")

        missing_parts = []
        if q.target_value is None:
            missing_parts.append("целевое значение показателя")
        if not q.output_year:
            missing_parts.append("год расчёта")
        if missing_parts:
            return self._missing_params_result(
                "analyze_model_inputs_for_target",
                missing_parts,
                example="какой должна быть цена меди, чтобы EBITDA в 2025 году составила 1000 млн",
            )

        input_names = [inp.equivalent_input_name for inp in resolved]
        logger.info(
            "IFT dispatch: input_names={}, output={}, year={}, query={}",
            input_names,
            q.output_name,
            q.output_year,
            reformulated_query,
        )
        kwargs = {
            "file_name": filename,
            "output_name": q.output_name,
            "output_year": q.output_year,
            "target_value": q.target_value,
            "input_names": input_names,
            "tolerance": 0.1,
            "max_scenarios": 1000,
            "user_id": user_id,
        }
        # LO-расчёт выполняется в отдельном процессе (spawn, свой GIL), поэтому не
        # блокирует event loop даже при интенсивном Python-коде. .result() блокирует
        # только текущую (async) корутину, а не loop.
        calc = await self._dispatch_lo_calc("analyze_model_inputs_for_target", kwargs, config)
        calc_result = ModelInputAnalysisToolResult(**calc)
        return self._to_tool_result("analyze_model_inputs_for_target", calc_result)

    async def _run_ema(
        self,
        reformulated_query: str,
        available_inputs: dict[str, str],
        available_outputs: dict[str, str],
        filename: str | None,
        user_id: str | None,
        config: RunnableConfig | None = None,
    ) -> ToolCallResult:
        messages = [HumanMessage(content=reformulated_query)]
        response = await self.ema_agent.ainvoke(messages, available_inputs, available_outputs, user_id)
        q = response.get("q_analysis")
        resolved = response.get("resolved_inputs")
        if q is None or resolved is None:
            return self._error_result("analyze_excel_model", "Не удалось распознать параметры запроса")

        missing_ranges, ranges, steps = self._split_ranges(resolved)
        missing_parts = []
        if not q.year:
            missing_parts.append("год расчёта")
        if missing_ranges:
            missing_parts.append(f"числовые диапазоны (min, max, шаг) для параметров: {', '.join(missing_ranges)}")
        if missing_parts:
            return self._missing_params_result(
                "analyze_excel_model",
                missing_parts,
                example="посчитай FCFF и DSCR за 2025 при изменении цены метанола от 450 до 500 с шагом 5",
            )

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
        kwargs = {
            "file_name": filename,
            "input_names": input_names,
            "output_names": output_names,
            "output_years": years,
            "ranges": ranges,
            "steps": steps,
            "user_id": user_id,
        }
        calc = await self._dispatch_lo_calc("analyze_excel_model", kwargs, config)
        calc_result = ExcelAnalysisToolResult(**calc)
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
            content=_safe_content(calc_result.content),
        )

    @staticmethod
    def _error_result(tool_name: str, message: str) -> ToolCallResult:
        return ToolCallResult(
            tool_name=tool_name,
            task_description=TOOL_TASK_DESCRIPTIONS[tool_name],
            status="ERROR",
            result_text=message,
            content={},
        )

    @staticmethod
    def _missing_params_result(tool_name: str, missing_parts: list[str], example: str) -> ToolCallResult:
        """WARNING-результат для расчёта, которому не хватает обязательных данных.

        Никогда не подставляем значения по умолчанию (0, текущий год и т.п.) вместо
        реально не указанных пользователем данных — лучше спросить, чем считать на
        придуманных цифрах.
        """
        message = f"Не указаны данные, необходимые для расчёта: {'; '.join(missing_parts)}.\n\nПример: '{example}'"
        return ToolCallResult(
            tool_name=tool_name,
            task_description=TOOL_TASK_DESCRIPTIONS[tool_name],
            status="WARNING",
            result_text=message,
            content={},
        )
