"""
Мультиагентный граф: classifier -> (subagent_1 | subagent_2) -> reviewer.

Структура:
    START -> classifier -> (условно) subagent_1 -> reviewer -> END
                            \\-> (условно) subagent_2 -> reviewer -> END

Узел `classifier` маршрутизирует запрос ровно в ОДИН из сабагентов.
Оба сабагента передают управление финальному узлу `reviewer`.
Все исполнители узлов - заглушки (PLACEHOLDER) для последующей реализации.
"""

import os
import tempfile
from collections.abc import Sequence
from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import END, START, StateGraph, add_messages

from aigw_service.api.v1.prompts.ema_analizer import EMA_ANALIZER_PROMPT
from aigw_service.api.v1.prompts.ift_analizer import IFT_ANALIZER_PROMPT
from aigw_service.api.v1.prompts.reviewer import REVIEWER_PROMPT
from aigw_service.api.v1.schemas.llm_outputs import (
    InputItem,
    LookupResult,
    QueryAnalysisEMA,
    QueryAnalysisIFT,
)
from aigw_service.api.v1.states.agents import (
    ExcelModelAnalizerInput,
    ExcelModelAnalizerOutput,
    ExcelModelAnalizerState,
    InputsForTargetAnalizerInput,
    InputsForTargetAnalizerOutput,
    InputsForTargetAnalizerState,
)
from aigw_service.api.v1.subagents.analyzer import AnalyzerSubAgent
from aigw_service.api.v1.subagents.classifier import classify_user_query
from aigw_service.api.v1.subagents.utils import _build_catalog_with_ids, build_diagnostic_data
from aigw_service.api.v1.tools import (
    ExcelAnalysisToolResult,
    ModelInputAnalysisToolResult,
)
from aigw_service.api.v1.tools import (
    analyze_excel_model as calculate_excel_model,
)
from aigw_service.api.v1.tools import (
    analyze_model_inputs_for_target as calculate_model_inputs_for_target,
)
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()


class AgentInput(TypedDict):
    messages: Sequence[BaseMessage]
    thread_id: Optional[str]
    user_id: Optional[str]
    file_path: Optional[str]
    available_inputs: Optional[dict[str, str]]
    available_outputs: Optional[dict[str, str]]


class AgentState(TypedDict):
    """Состояние агента."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    thread_id: Optional[str]
    user_id: Optional[str]
    file_path: Optional[str]
    classification_result: Literal["analyze_model_inputs_for_target", "analyze_excel_model"]
    filename: str
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    ift_resolved_inputs: list[str]
    ift_analyzer_results: QueryAnalysisIFT
    ift_lookup_results: LookupResult
    ift_calc_results: ModelInputAnalysisToolResult
    ema_analizer_results: QueryAnalysisEMA
    ema_lookup_results: LookupResult
    ema_resolved_inputs: list[InputItem]
    ema_calc_results: ExcelAnalysisToolResult


class AgentGraph:
    def __init__(self):
        self.graph = self._build_graph()
        self.llm = APP_CTX.llm
        self.ift_agent = AnalyzerSubAgent(
            state_schema=InputsForTargetAnalizerState,
            input_schema=InputsForTargetAnalizerInput,
            output_schema=InputsForTargetAnalizerOutput,
            llm_output_schema=QueryAnalysisIFT,
            name="ift_subagent",
            analyze_prompt_template=IFT_ANALIZER_PROMPT,
        )
        self.excel_mdl_agnt = AnalyzerSubAgent(
            state_schema=ExcelModelAnalizerState,
            input_schema=ExcelModelAnalizerInput,
            output_schema=ExcelModelAnalizerOutput,
            llm_output_schema=QueryAnalysisEMA,
            name="ema_subagent",
            analyze_prompt_template=EMA_ANALIZER_PROMPT,
        )

    def _build_graph(self):
        """Создаёт и компилирует граф."""
        workflow = StateGraph(state_schema=AgentState, input_schema=AgentInput)

        workflow.add_node("classifier", self.classifier)
        workflow.add_node("extract_excel_data", self.get_available_inputs_outputs)
        workflow.add_node("analyze_model_inputs_for_target", self.analyze_model_inputs_for_target)
        workflow.add_node("analyze_excel_model", self.analyze_excel_model)
        workflow.add_node("reviewer", self.reviewer)
        workflow.add_edge(START, "classifier")
        workflow.add_edge("classifier", "extract_excel_data")
        workflow.add_conditional_edges(
            "extract_excel_data",
            self.route,
            {
                "analyze_model_inputs_for_target": "analyze_model_inputs_for_target",
                "analyze_excel_model": "analyze_excel_model",
            },
        )
        workflow.add_edge("analyze_excel_model", "reviewer")
        workflow.add_edge("analyze_model_inputs_for_target", "reviewer")
        workflow.add_edge("reviewer", END)

        return workflow.compile()

    def classifier(self, state: AgentState) -> AgentState:
        """Классифицирует запрос и определяет, какой сабагент его обработает"""
        from time import time as _time

        messages = state.get("messages", [])
        try:
            start_cls = _time()
            next_agent = classify_user_query(self.llm, messages=messages)
            if next_agent is None:
                raise ValueError("Classifier returned None response from LLM")
            cls_result = next_agent.next_agent if hasattr(next_agent, "next_agent") else str(next_agent)
            if cls_result not in ("analyze_model_inputs_for_target", "analyze_excel_model"):
                raise ValueError(f"Classifier returned invalid agent: {cls_result}")
            logger.info("Classifier completed: agent={}, elapsed={:.2f}s", cls_result, _time() - start_cls)
            return {
                "classification_result": cls_result,
            }
        except Exception as e:
            logger.opt(exception=True).error("Classifier failed: {}", str(e))
            raise

    def analyze_model_inputs_for_target(self, state: AgentState) -> AgentState:
        messages = state.get("messages", [])
        available_inputs = state.get("available_inputs")
        available_outputs = state.get("available_outputs")
        user_id = state.get("user_id")
        if (available_inputs is None) or (available_outputs is None):
            raise ValueError("Check 'available_inputs' and 'available_outputs'. They are empty")

        # 1. Resolve names (subagent без calculate)
        response = self.ift_agent.invoke(messages, available_inputs, available_outputs, user_id)
        q = response.get("q_analysis")
        resolved = response.get("resolved_inputs")  # list[InputItem] из mixin

        if q is None or resolved is None:
            raise ValueError("'q_analysis' or 'resolved_inputs' is None")

        # 2. MAIN GRAPH: calculate (IFT)
        # IFT нужен list[str], извлекаем имена из InputItem
        input_names = [inp.equivalent_input_name for inp in resolved]

        logger.info(
            "IFT analysis started: input_names={}, output={}, year={}", input_names, q.output_name, q.output_year
        )

        calc_result = calculate_model_inputs_for_target(
            file_name=q.file_name,
            output_name=q.output_name,
            output_year=q.output_year,
            target_value=q.target_value,
            input_names=input_names,
            tolerance=0.1,
            max_scenarios=1000,
            user_id=user_id,
        )

        return {
            "ift_calc_results": calc_result,
            "ift_resolved_inputs": input_names,
            "ift_analyzer_results": q,
            "ift_lookup_results": response.get("lookup_results"),
        }

    def analyze_excel_model(self, state: AgentState) -> AgentState:
        messages = state.get("messages", [])
        available_inputs = state.get("available_inputs")
        available_outputs = state.get("available_outputs")
        user_id = state.get("user_id")
        if (available_inputs is None) or (available_outputs is None):
            raise ValueError("Check 'available_inputs' and 'available_outputs'. They are empty")

        # 1. Resolve names (subagent без calculate)
        response = self.excel_mdl_agnt.invoke(messages, available_inputs, available_outputs, user_id)
        q = response.get("q_analysis")
        resolved = response.get("resolved_inputs")  # list[InputItem]

        if q is None or resolved is None:
            raise ValueError("'q_analysis' or 'resolved_inputs' is None")

        # 2. MAIN GRAPH: calculate (EMA)
        missing_ranges = []
        ranges: list[list[float]] = []
        steps: list[float] = []
        for inp in resolved:
            rc = inp.range_config
            if rc and rc.start_value is not None and rc.end_value is not None:
                ranges.append([rc.start_value, rc.end_value])
                steps.append(rc.step if rc.step else 0.5)
            else:
                missing_ranges.append(inp.equivalent_input_name)

        if missing_ranges:
            return {
                "ema_calc_results": ExcelAnalysisToolResult(
                    status="INFO",
                    result=(
                        f"Не указаны числовые диапазоны для параметров: "
                        f"{', '.join(missing_ranges)}. "
                        f"Пожалуйста, уточните диапазоны (min, max, шаг) для этих параметров.\n\n"
                        f"Пример: 'цена метанола от 450 до 500 с шагом 5'"
                    ),
                    content={},
                ),
                "ema_resolved_inputs": resolved,
                "ema_analizer_results": q,
                "ema_lookup_results": response.get("lookup_results"),
            }

        output_names = [out.equivalent_output_name for out in q.mentioned_outputs]
        years = [q.year] * len(output_names)
        input_names = [inp.equivalent_input_name for inp in resolved]

        logger.info(
            "EMA analysis started: inputs={}, ranges={}, steps={}, outputs={}, year={}",
            input_names,
            ranges,
            steps,
            output_names,
            q.year,
        )

        calc_res = calculate_excel_model(
            file_name=q.file_name,
            input_names=input_names,
            output_names=output_names,
            output_years=years,
            ranges=ranges,
            steps=steps,
            user_id=user_id,
        )

        return {
            "ema_calc_results": calc_res,
            "ema_resolved_inputs": resolved,
            "ema_analizer_results": q,
            "ema_lookup_results": response.get("lookup_results"),
        }

    def reviewer(self, state: AgentState) -> AgentState:
        """
        Отвечает на вопрос пользовалея, на основании полученных расчетов
        """
        import json
        from time import time as _time

        from aigw_service.api.v1.subagents.utils import get_tokens

        messages = state.get("messages", [])
        cls_res = state.get("classification_result")
        if cls_res == "analyze_model_inputs_for_target":
            calc_results = state.get("ift_calc_results")
            task = "Подобрать комбинацию входных параметров для целевого выхода для заданного года"
        elif cls_res == "analyze_excel_model":
            calc_results = state.get("ema_calc_results")
            task = "Просчитать несколько сценариев для выбранных целевых покащателей по набору входных параметров в заданных промежутках"
        system_message_content = REVIEWER_PROMPT.format(task=task, calc_results=calc_results)
        prompt = [SystemMessage(content=system_message_content), *messages]
        start = _time()
        response = self.llm.invoke(prompt)
        elapsed = _time() - start

        tokens = get_tokens(response)
        data = build_diagnostic_data("reviewer", elapsed, tokens)
        logger.info(json.dumps(data, ensure_ascii=False))
        logger.info(
            "Review completed: classification={}, elapsed={:.2f}s, tokens={}",
            cls_res,
            elapsed,
            tokens,
        )
        return {"messages": response}

    def route(self, state: AgentState) -> str:
        classification = state.get("classification_result")
        logger.info(f"classification_result = {classification}")
        if classification is None:
            raise ValueError(
                f"classification_result is None - classifier did not provide a valid result. State: {dict(state)}"
            )
        if classification not in ("analyze_model_inputs_for_target", "analyze_excel_model"):
            raise ValueError(
                f"route() got invalid classification: '{classification}'. "
                f"Expected one of: 'analyze_model_inputs_for_target', 'analyze_excel_model'"
            )
        return classification

    async def get_available_inputs_outputs(self, state: AgentState) -> AgentState:
        # Если file_path передан напрямую (локальный запуск), используем его
        direct_path = state.get("file_path")
        if direct_path:
            logger.info(f"Reading excel from direct file_path: {direct_path}")
            inputs_catalog, outputs_catalog = _build_catalog_with_ids(direct_path)
            return {
                "available_inputs": inputs_catalog,
                "available_outputs": outputs_catalog,
            }

        # Иначе берём имя файла из memory store (куда его положил upload-эндпоинт)
        store = APP_CTX.agent_memory.store
        user_id = state.get("user_id")
        if not user_id:
            raise ValueError("user_id не передан в запросе")
        namespace = ("memories", user_id)
        stored = await store.aget(namespace, user_id)
        if stored and hasattr(stored, "value") and isinstance(stored.value, dict):
            file_name = stored.value.get("filename")
        else:
            raise ValueError(
                f"Файл не найден в store для user_id={user_id}. "
                "Убедитесь, что файл был загружен через /upload перед вызовом агента."
            )
        TEMP_DIR = tempfile.gettempdir()
        file_path = os.path.abspath(os.path.join(TEMP_DIR, file_name))
        logger.info(f"Reading excel from {file_path}")
        inputs_catalog, outputs_catalog = _build_catalog_with_ids(file_path)
        return {"available_inputs": inputs_catalog, "available_outputs": outputs_catalog}
