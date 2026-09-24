"""
Мультиагентный граф: extract_excel_data -> orchestrator <-> tool_executor -> synthesizer.

Узел `orchestrator` — единственная точка принятия решения: с помощью нативного tool-calling LLM
сам решает, нужен ли для ответа расчёт, и если да — какой инструмент (или оба) вызвать.
Узел `tool_executor` выполняет вызванные инструменты (через `OrchestratorTools`, который оборачивает
существующие IFT/EMA сабагенты и функции расчёта) и возвращает управление оркестратору — цикл
повторяется, пока LLM не перестанет запрашивать инструменты. `synthesizer` формирует финальный
ответ на основе истории диалога и результатов расчётов этого хода (если они были).

Граф скомпилирован с чекпоинтером (`APP_CTX.agent_memory.checkpointer`), поэтому история
сообщений сохраняется между запросами в рамках одного `thread_id`.
"""

import json
import os
import tempfile
import time
from collections.abc import Sequence
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph, add_messages

from aigw_service.api.v1.prompts.ema_analizer import EMA_ANALIZER_PROMPT
from aigw_service.api.v1.prompts.ift_analizer import IFT_ANALIZER_PROMPT
from aigw_service.api.v1.prompts.lookup_more import LOOKUP_MORE_PROMPT
from aigw_service.api.v1.prompts.orchestrator import ORCHESTRATOR_PROMPT
from aigw_service.api.v1.prompts.synthesizer import SYNTHESIZER_PROMPT
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
from aigw_service.api.v1.subagents.orchestrator_tools import OrchestratorTools, ToolCallResult
from aigw_service.api.v1.subagents.utils import _build_catalog_with_ids, build_diagnostic_data, get_tokens
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()


class AgentInput(TypedDict):
    messages: Sequence[BaseMessage]
    thread_id: str | None
    user_id: str | None


class AgentState(TypedDict):
    """Состояние агента."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    thread_id: str | None
    user_id: str | None
    filename: str | None
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    tool_call_count: int
    executed_tool_fingerprints: list[str]
    tool_results: list[ToolCallResult]


class AgentGraph:
    MAX_ORCHESTRATOR_ITERATIONS = 4

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
        self.llm_with_tools = self.llm.bind_tools(self.orchestrator_tools.TOOLS)
        self.graph = self._build_graph()

    def _build_graph(self):
        """Создаёт и компилирует граф."""
        workflow = StateGraph(state_schema=AgentState, input_schema=AgentInput)

        workflow.add_node("extract_excel_data", self.get_available_inputs_outputs)
        workflow.add_node("orchestrator", self.orchestrator)
        workflow.add_node("tool_executor", self.tool_executor)
        workflow.add_node("synthesizer", self.synthesizer)

        workflow.add_edge(START, "extract_excel_data")
        workflow.add_edge("extract_excel_data", "orchestrator")
        workflow.add_conditional_edges(
            "orchestrator",
            self.route_after_orchestrator,
            {"tool_executor": "tool_executor", "synthesizer": "synthesizer"},
        )
        workflow.add_edge("tool_executor", "orchestrator")
        workflow.add_edge("synthesizer", END)

        return workflow.compile(checkpointer=APP_CTX.agent_memory.checkpointer)

    async def get_available_inputs_outputs(self, state: AgentState) -> AgentState:
        """Загружает имя файла из memory store и строит каталог входов/выходов.

        Выполняется в начале каждого хода — также сбрасывает счётчики цикла оркестратора,
        которые иначе остались бы от предыдущего хода этой же сессии (чекпоинтер хранит их
        между вызовами `ainvoke`, а не только `messages`).
        """
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
        inputs_catalog, outputs_catalog = _build_catalog_with_ids(file_path)
        return {
            "available_inputs": inputs_catalog,
            "available_outputs": outputs_catalog,
            "filename": file_name,
            "tool_call_count": 0,
            "executed_tool_fingerprints": [],
            "tool_results": [],
        }

    def orchestrator(self, state: AgentState) -> AgentState:
        """LLM с нативным tool-calling решает, вызывать ли инструмент(ы) или ответить сразу."""
        messages = state.get("messages", [])
        prompt = [SystemMessage(content=ORCHESTRATOR_PROMPT), *messages]

        start = time.time()
        response = self.llm_with_tools.invoke(prompt)
        elapsed = time.time() - start

        tokens = get_tokens(response)
        tool_call_names = [call["name"] for call in (response.tool_calls or [])]
        data = build_diagnostic_data("orchestrator", elapsed, tokens, extra={"tool_calls": tool_call_names})
        logger.info(json.dumps(data, ensure_ascii=False))

        return {"messages": [response]}

    def route_after_orchestrator(self, state: AgentState) -> str:
        last_message = state["messages"][-1]
        tool_calls = getattr(last_message, "tool_calls", None)
        if not tool_calls:
            return "synthesizer"
        if state.get("tool_call_count", 0) >= self.MAX_ORCHESTRATOR_ITERATIONS:
            logger.warning(
                "Orchestrator iteration cap ({}) reached, forcing synthesizer",
                self.MAX_ORCHESTRATOR_ITERATIONS,
            )
            return "synthesizer"
        return "tool_executor"

    def tool_executor(self, state: AgentState) -> AgentState:
        """Выполняет все tool_calls последнего сообщения оркестратора и возвращает ToolMessage'и."""
        last_message = state["messages"][-1]
        tool_calls = last_message.tool_calls or []

        fingerprints = set(state.get("executed_tool_fingerprints", []))
        tool_results = list(state.get("tool_results", []))
        tool_messages = [self._execute_single_call(call, state, fingerprints, tool_results) for call in tool_calls]

        return {
            "messages": tool_messages,
            "tool_results": tool_results,
            "executed_tool_fingerprints": list(fingerprints),
            "tool_call_count": state.get("tool_call_count", 0) + 1,
        }

    def _execute_single_call(
        self,
        call: dict,
        state: AgentState,
        fingerprints: set[str],
        tool_results: list[ToolCallResult],
    ) -> ToolMessage:
        """Выполняет один tool_call, либо пропускает его, если это точный дубликат в рамках хода."""
        fingerprint = self._fingerprint_call(call)
        if fingerprint in fingerprints:
            logger.info("Skipping duplicate tool call: {}", fingerprint)
            return ToolMessage(
                content="Этот вызов с такими же параметрами уже был выполнен в рамках этого ответа.",
                tool_call_id=call["id"],
                name=call["name"],
            )

        fingerprints.add(fingerprint)
        args = call.get("args", {})
        logger.info("TOOL ARGS: {} | {}", call["name"], args)

        result = self.orchestrator_tools.dispatch(
            tool_name=call["name"],
            reformulated_query=args.get("reformulated_query", ""),
            available_inputs=state.get("available_inputs") or {},
            available_outputs=state.get("available_outputs") or {},
            filename=state.get("filename"),
            user_id=state.get("user_id"),
        )
        tool_results.append(result)
        return ToolMessage(content=result["result_text"], tool_call_id=call["id"], name=call["name"])

    @staticmethod
    def _fingerprint_call(call: dict) -> str:
        return f"{call['name']}:{json.dumps(call.get('args', {}), sort_keys=True, ensure_ascii=False)}"

    def synthesizer(self, state: AgentState) -> AgentState:
        """Формирует финальный ответ на основе истории диалога и результатов расчётов этого хода."""
        messages = state.get("messages", [])
        tool_results = state.get("tool_results", [])
        calc_results_text = self._format_tool_results(tool_results)

        system_message_content = SYNTHESIZER_PROMPT.format(calc_results=calc_results_text)
        # GigaChat returns a degenerate, unparseable completion when the prompt's last message
        # is an AIMessage (e.g. the orchestrator's own final reply after deciding no more tools
        # are needed) — a trailing human turn keeps the request well-formed. Not persisted to
        # state: only the actual answer below is added to `messages`.
        finalize_instruction = HumanMessage(content="Сформулируй финальный ответ на основе истории выше.")
        prompt = [SystemMessage(content=system_message_content), *messages, finalize_instruction]

        start = time.time()
        response = self.llm.invoke(prompt)
        elapsed = time.time() - start

        tokens = get_tokens(response)
        data = build_diagnostic_data("synthesizer", elapsed, tokens)
        logger.info(json.dumps(data, ensure_ascii=False))

        return {"messages": response}

    @staticmethod
    def _format_tool_results(tool_results: list[ToolCallResult]) -> str:
        if not tool_results:
            return "Расчёты не выполнялись в рамках этого ответа - отвечай на основе истории диалога."

        blocks = [
            f"ЗАДАЧА: {r['task_description']}\nСТАТУС: {r['status']}\nРЕЗУЛЬТАТ: {r['result_text']}"
            for r in tool_results
        ]
        return "\n\n---\n\n".join(blocks)
