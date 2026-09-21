"""Generic analyzer subagent: analyze_query -> lookup_more/resolve_names -> END."""

from collections.abc import Sequence
from typing import Any, Optional

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from aigw_service.api.v1.schemas.llm_outputs import InputItem, LookupResult
from aigw_service.api.v1.subagents.utils import _stringify_catalog, build_diagnostic_data
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()


class AnalyzerSubAgent:
    """Универсальный subagent: LLM-анализ запроса -> поиск -> резольвинг имён.

    Параметры инициализации:
        state_schema          — TypedDict состояния графа
        input_schema          — TypedDict входа
        output_schema         — TypedDict выхода
        name                  - имя агента
        llm_output_schema     — Pydantic-схема для structured_output (QueryAnalysisEMA/IFT)
        analyze_prompt_template  — системный prompt для анализа запроса
        lookup_prompt_template   — системный prompt для доп. поиска имён
    """

    def __init__(
        self,
        state_schema: type,
        input_schema: type,
        output_schema: type,
        llm_output_schema: type,
        name: str,
        analyze_prompt_template: str,
        lookup_prompt_template: str = "",
    ):
        self._state_schema = state_schema
        self._input_schema = input_schema
        self._output_schema = output_schema
        self.LLM_OUTPUT_SCHEMA = llm_output_schema
        self.ANALYZE_PROMPT_TEMPLATE = analyze_prompt_template
        self.LOOKUP_PROMPT_TEMPLATE = lookup_prompt_template
        self.name = name
        self.llm = APP_CTX.llm
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        workflow = StateGraph(
            state_schema=self._state_schema,
            input_schema=self._input_schema,
            output_schema=self._output_schema,
        )

        workflow.add_node("anlyze_query", self.anlyze_query)
        workflow.add_node("lookup_more", self.lookup_more)
        workflow.add_node("resolve_names", self.resolve_names)

        workflow.add_edge(START, "anlyze_query")
        workflow.add_conditional_edges(
            "anlyze_query",
            self.route,
            {"lookup_more": "lookup_more", "sufficient": "resolve_names"},
        )
        workflow.add_edge("lookup_more", "resolve_names")
        workflow.add_edge("resolve_names", END)

        return workflow.compile()

    def invoke(
        self,
        messages: Sequence[BaseMessage] | list[BaseMessage],
        available_inputs: dict[str, str],
        available_outputs: dict[str, str],
        user_id: Optional[str] = None,
    ):
        return self.graph.invoke(
            {
                "messages": messages,
                "available_inputs": available_inputs,
                "available_outputs": available_outputs,
                "user_id": user_id,
            }
        )

    # ================================================================
    # Node-методы (вызываются LangGraph)
    # ================================================================

    def anlyze_query(self, state: dict) -> dict:
        """LLM анализирует запрос -> q_analysis.

        Использует retry-обёртку: при валидации LLM-ответа — до 3
        попыток, затем dummy-заполнитель.
        """
        import json
        from time import time as _time

        from aigw_service.api.v1.subagents.dummies import retry_structured_llm
        from aigw_service.api.v1.subagents.utils import get_tokens

        messages = state.get("messages", [])
        inputs_cat = _stringify_catalog(state.get("available_inputs"))
        outputs_cat = _stringify_catalog(state.get("available_outputs"))

        system_message = self.ANALYZE_PROMPT_TEMPLATE.format(inputs=inputs_cat, outputs=outputs_cat)
        prompt = [SystemMessage(content=system_message), *messages]

        start = _time()
        parsed, success, raw_response = retry_structured_llm(
            self.llm,
            self.LLM_OUTPUT_SCHEMA,
            prompt,
        )
        elapsed = _time() - start

        raw = raw_response["raw"] if (success and raw_response) else None
        tokens = get_tokens(raw) if raw else {}
        data = build_diagnostic_data("anlyze_query", elapsed, tokens)
        logger.info(json.dumps(data, ensure_ascii=False))

        if not success:
            logger.warning("Agent: %s. q_analysis validated as dummy after max retries", self.name)

        return {"q_analysis": parsed}

    def route(self, state: dict) -> str:
        """Проверяет, нужно ли делать доп. поиск имён."""
        q_analysis = state.get("q_analysis")
        if q_analysis is None:
            logger.warning(f"'q_analysis' not found in route of agent '{self.name}'")
            return "sufficient"

        mentioned = getattr(q_analysis, "mentioned_inputs", [])
        for item in mentioned:
            if getattr(item, "lookup_more", False):
                logger.info(f"Agent: {self.name}. Some inputs need to be looked up one more time")
                return "lookup_more"

        logger.info(f"Agent: {self.name}. All required names were detected")
        return "sufficient"

    def lookup_more(self, state: dict) -> dict:
        """Доп. LLM-вызов для неоднозначных имён.

        Использует retry-обёртку для валидации structured output.
        """
        import json
        from time import time as _time

        from aigw_service.api.v1.subagents.dummies import retry_structured_llm
        from aigw_service.api.v1.subagents.utils import get_tokens

        q_analysis = state.get("q_analysis")
        if q_analysis is None:
            logger.warning(f"'q_analysis' not found in lookup_more of agent '{self.name}'")
            return {"lookup_results": LookupResult(mentioned_inputs=[])}

        mentioned = getattr(q_analysis, "mentioned_inputs", [])
        lookup_list = [n for n in mentioned if getattr(n, "lookup_more", False)]

        inputs_cat = _stringify_catalog(state.get("available_inputs"))
        lookup_str = self._stringify_lookup(lookup_list)

        lookup_template = self.LOOKUP_PROMPT_TEMPLATE or "LOOKUP_MORE_PROMPT"
        system_message = lookup_template.format(lookup=lookup_str, inputs=inputs_cat)
        prompt = [
            SystemMessage(content=system_message),
            HumanMessage(content="Посмотри внимательно и подбери точный эквивалент"),
        ]

        start = _time()
        parsed, success, raw_response = retry_structured_llm(
            self.llm,
            LookupResult,
            prompt,
        )
        elapsed = _time() - start

        raw = raw_response["raw"] if (success and raw_response) else None
        tokens = get_tokens(raw) if raw else {}
        data = build_diagnostic_data("lookup_more", elapsed, tokens)
        logger.info(json.dumps(data, ensure_ascii=False))

        if not success:
            logger.warning("Agent: %s. lookup_more validated as dummy after max retries", self.name)

        return {"lookup_results": parsed}

    def resolve_names(self, state: dict) -> dict:
        """Маппинг LLM-предложений -> канонические имена из каталога."""
        lookup_results = state.get("lookup_results")
        analyzer_results = state.get("q_analysis")

        found = getattr(analyzer_results, "mentioned_inputs", []) if analyzer_results is not None else []
        lookups = getattr(lookup_results, "mentioned_inputs", []) if lookup_results is not None else []
        available = state.get("available_inputs") or {}

        resolved = self._resolve_names_internal(found, lookups, available)
        return {"resolved_inputs": resolved}

    # ================================================================
    # Вспомогательные методы
    # ================================================================

    def _stringify_lookup(self, lookup_list: list[InputItem]) -> str:
        return "".join(f"- {item.what_lookup}\n" for item in lookup_list)

    def get_lookup_value(self, for_value: InputItem, lookup_results: list[InputItem]) -> InputItem | None:
        for item in lookup_results:
            if getattr(item, "mentioned_input_name", "") == for_value.what_lookup:
                logger.info("Found lookup value %s for item %s", item, for_value)
                return item
        return None

    def _resolve_names_internal(
        self,
        analyzer_results: list[InputItem],
        lookup_results: list[InputItem],
        available_names: dict[str, str],
    ) -> list[InputItem]:
        resolved: list[InputItem] = []
        for item in analyzer_results:
            next_id = item.equivalent_input_id
            if item.lookup_more:
                lookup_item = self.get_lookup_value(item, lookup_results)
                if lookup_item:
                    next_id = lookup_item.equivalent_input_id
            resolved_name = available_names[next_id]
            resolved.append(
                InputItem(
                    mentioned_input_name=item.mentioned_input_name,
                    explanation=item.explanation,
                    equivalent_input_name=resolved_name,
                    equivalent_input_id=item.equivalent_input_id,
                    range_config=item.range_config,
                    lookup_more=item.lookup_more,
                    what_lookup=item.what_lookup,
                )
            )
        return resolved
