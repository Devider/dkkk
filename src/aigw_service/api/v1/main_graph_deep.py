"""
Экспериментальный граф на deepagents: extract_excel_data -> deep_agent.

Заменяет ручной цикл orchestrator <-> tool_executor -> synthesizer из `main_graph.py` на
`create_deep_agent` (пакет `deepagents`, построен поверх `langchain.agents` middleware):
LLM с нативным tool-calling сам решает, нужен ли расчёт, и делегирует его через встроенный
инструмент `task` одному из двух subagent'ов (`analyze_model_inputs_for_target` /
`analyze_excel_model`). Каждый subagent — тонкая обёртка над существующим
`AnalyzerSubAgent` (резолвинг имён) + LibreOffice-расчётом из `tools.py`, вызываемым через
`OrchestratorTools.dispatch` без изменений.

Включается флагом `AGENT_GRAPH_IMPL=DEEPAGENTS` (см. `config/base_config.py`), по умолчанию
используется легаси-граф из `main_graph.py`. Внешний контракт идентичен `AgentGraph`:
`.graph.ainvoke({"messages": [...], "thread_id": ..., "user_id": ...}, config=...)` возвращает
словарь с `messages[-1]` — финальным `AIMessage`, поэтому `router.py` не нужно менять, кроме
выбора класса в `get_agent()`.
"""

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Sequence
from typing import Annotated, TypedDict

from deepagents import CompiledSubAgent, DeepAgentState, create_deep_agent
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph, add_messages

from aigw_service.api.v1.prompts.deep_agent import DEEP_AGENT_SYSTEM_PROMPT
from aigw_service.api.v1.prompts.ema_analizer import EMA_ANALIZER_PROMPT
from aigw_service.api.v1.prompts.ift_analizer import IFT_ANALIZER_PROMPT
from aigw_service.api.v1.prompts.lookup_more import LOOKUP_MORE_PROMPT
from aigw_service.api.v1.schemas.llm_outputs import QueryAnalysisEMA, QueryAnalysisIFT
from aigw_service.api.v1.states.agents import (
    ExcelModelAnalizerInput,
    ExcelModelAnalizerOutput,
    ExcelModelAnalizerState,
    InputsForTargetAnalizerInput,
    InputsForTargetAnalizerOutput,
    InputsForTargetAnalizerState,
)
from aigw_service.api.v1.subagents.analyzer import AnalyzerSubAgent
from aigw_service.api.v1.subagents.orchestrator_tools import TOOL_TASK_DESCRIPTIONS, OrchestratorTools
from aigw_service.api.v1.subagents.utils import _build_catalog_with_ids
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

# Each orchestrator turn is now 2 graph steps inside the deep agent (model call + task
# call); sized generously above the legacy MAX_ORCHESTRATOR_ITERATIONS=4 to give the same
# effective number of tool round-trips headroom.
RECURSION_LIMIT = 16

RECURSION_LIMIT_MESSAGE = (
    "Не удалось сформировать ответ за отведённое число шагов. Пожалуйста, переформулируйте "
    "запрос проще или разбейте его на несколько вопросов."
)

DUPLICATE_CALL_MESSAGE = "Этот вызов с такими же параметрами уже был выполнен в рамках этого ответа."


class AgentInput(TypedDict):
    messages: Sequence[BaseMessage]
    thread_id: str | None
    user_id: str | None


class DeepAgentGraphState(DeepAgentState):
    """`DeepAgentState` + доменные поля, прокидываемые в subagent'ы `task`-инструментом.

    `SubAgentMiddleware` передаёт subagent'у весь родительский state, кроме `messages`/`todos`/
    `structured_response`/`skills_metadata` (см. `_EXCLUDED_STATE_KEYS` в
    `deepagents.middleware.subagents`) — поэтому `available_inputs`/`available_outputs`/
    `filename`/`user_id`/`executed_tool_fingerprints` доходят до subagent-обёрток без
    дополнительной прокладки.
    """

    thread_id: str | None
    user_id: str | None
    filename: str | None
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    executed_tool_fingerprints: list[str]


class SubAgentWrapperState(TypedDict):
    """State-схема обёрточных графов IFT/EMA, регистрируемых как `CompiledSubAgent`."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    filename: str | None
    user_id: str | None
    executed_tool_fingerprints: list[str]


class DeepAgentGraph:
    RECURSION_LIMIT = RECURSION_LIMIT

    def __init__(self):
        self.llm = APP_CTX.llm
        self.ift_agent = AnalyzerSubAgent(
            state_schema=InputsForTargetAnalizerState,
            input_schema=InputsForTargetAnalizerInput,
            output_schema=InputsForTargetAnalizerOutput,
            llm_output_schema=QueryAnalysisIFT,
            name="ift_subagent",
            analyze_prompt_template=IFT_ANALIZER_PROMPT,
            lookup_prompt_template=LOOKUP_MORE_PROMPT,
        )
        self.ema_agent = AnalyzerSubAgent(
            state_schema=ExcelModelAnalizerState,
            input_schema=ExcelModelAnalizerInput,
            output_schema=ExcelModelAnalizerOutput,
            llm_output_schema=QueryAnalysisEMA,
            name="ema_subagent",
            analyze_prompt_template=EMA_ANALIZER_PROMPT,
            lookup_prompt_template=LOOKUP_MORE_PROMPT,
        )
        self.orchestrator_tools = OrchestratorTools(self.ift_agent, self.ema_agent)

        self.deep_agent = create_deep_agent(
            model=self.llm,
            system_prompt=DEEP_AGENT_SYSTEM_PROMPT,
            subagents=[
                self._build_compiled_subagent("analyze_model_inputs_for_target"),
                self._build_compiled_subagent("analyze_excel_model"),
            ],
            state_schema=DeepAgentGraphState,
        )
        self.graph = self._build_graph()

    def _build_graph(self):
        """Внешний граф: тот же `extract_excel_data`, затем единственный вызов deep agent."""
        workflow = StateGraph(state_schema=DeepAgentGraphState, input_schema=AgentInput)

        workflow.add_node("extract_excel_data", self.get_available_inputs_outputs)
        workflow.add_node("deep_agent", self.run_deep_agent)

        workflow.add_edge(START, "extract_excel_data")
        workflow.add_edge("extract_excel_data", "deep_agent")
        workflow.add_edge("deep_agent", END)

        return workflow.compile(checkpointer=APP_CTX.agent_memory.checkpointer)

    async def get_available_inputs_outputs(self, state: DeepAgentGraphState) -> DeepAgentGraphState:
        """Идентично `AgentGraph.get_available_inputs_outputs` — см. `main_graph.py`."""
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
        temp_dir = tempfile.gettempdir()
        file_path = os.path.abspath(os.path.join(temp_dir, file_name))
        logger.info("Reading excel from {}", file_path)
        inputs_catalog, outputs_catalog = await asyncio.to_thread(_build_catalog_with_ids, file_path)
        return {
            "available_inputs": inputs_catalog,
            "available_outputs": outputs_catalog,
            "filename": file_name,
            "executed_tool_fingerprints": [],
        }

    async def run_deep_agent(self, state: DeepAgentGraphState) -> DeepAgentGraphState:
        """Запускает deep agent на всю историю диалога этого хода (цикл model<->task внутри)."""
        sub_input = {
            "messages": state.get("messages", []),
            "available_inputs": state.get("available_inputs") or {},
            "available_outputs": state.get("available_outputs") or {},
            "filename": state.get("filename"),
            "user_id": state.get("user_id"),
            "executed_tool_fingerprints": state.get("executed_tool_fingerprints", []),
        }

        start = time.time()
        try:
            result = await self.deep_agent.ainvoke(sub_input, config={"recursion_limit": self.RECURSION_LIMIT})
        except GraphRecursionError:
            elapsed = time.time() - start
            logger.warning("Deep agent recursion limit ({}) reached after {:.3f}s", self.RECURSION_LIMIT, elapsed)
            return {"messages": [AIMessage(content=RECURSION_LIMIT_MESSAGE)]}
        elapsed = time.time() - start

        data = {"node": "deep_agent", "elapsed_s": round(elapsed, 3)}
        logger.info(json.dumps(data, ensure_ascii=False))

        return {
            "messages": result.get("messages", []),
            "executed_tool_fingerprints": result.get("executed_tool_fingerprints", []),
        }

    def _build_compiled_subagent(self, tool_name: str) -> CompiledSubAgent:
        return CompiledSubAgent(
            name=tool_name,
            description=TOOL_TASK_DESCRIPTIONS[tool_name],
            runnable=self._build_subagent_wrapper_graph(tool_name),
        )

    def _build_subagent_wrapper_graph(self, tool_name: str):
        """Однонодовый граф: делегирует резолвинг имён + расчёт в `OrchestratorTools.dispatch`
        (которое само маршрутизирует по `tool_name` на `self.ift_agent`/`self.ema_agent`).
        """

        async def _node(state: SubAgentWrapperState) -> SubAgentWrapperState:
            messages = state.get("messages", [])
            query = messages[-1].content if messages else ""
            fingerprint = f"{tool_name}:{query}"
            executed = state.get("executed_tool_fingerprints") or []

            if fingerprint in executed:
                logger.info("Skipping duplicate subagent call: {}", fingerprint)
                return {"messages": [AIMessage(content=DUPLICATE_CALL_MESSAGE)]}

            logger.info("TOOL ARGS: {} | {}", tool_name, {"reformulated_query": query})
            result = await self.orchestrator_tools.dispatch(
                tool_name=tool_name,
                reformulated_query=query,
                available_inputs=state.get("available_inputs") or {},
                available_outputs=state.get("available_outputs") or {},
                filename=state.get("filename"),
                user_id=state.get("user_id"),
            )
            return {
                "messages": [AIMessage(content=result["result_text"])],
                "executed_tool_fingerprints": [*executed, fingerprint],
            }

        workflow = StateGraph(state_schema=SubAgentWrapperState)
        workflow.add_node(tool_name, _node)
        workflow.add_edge(START, tool_name)
        workflow.add_edge(tool_name, END)
        return workflow.compile()
