"""
LangGraph TypedDict-схемы для состояний агентов.

Содержит только state-схемы графов. Pydantic-модели (llm output schemas)
находятся в: aigw_service.api.v2.schemas.llm_outputs
"""

from collections.abc import Sequence
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

from aigw_service.api.v2.schemas.llm_outputs import InputItem, LookupResult, QueryAnalysisEMA, QueryAnalysisIFT
from aigw_service.api.v2.tools import ExcelAnalysisToolArgs, ModelInputAnalysisToolResult


class InputsForTargetAnalizerInput(TypedDict):
    """Input schema for the inputs-for-target analyzer agent."""

    messages: Sequence[BaseMessage]
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    user_id: Optional[str]


class InputsForTargetAnalizerState(TypedDict):
    """State schema for the inputs-for-target analyzer agent."""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    user_id: Optional[str]
    q_analysis: QueryAnalysisIFT
    lookup_results: LookupResult
    calc_results: ModelInputAnalysisToolResult
    resolved_inputs: list[str]


class InputsForTargetAnalizerOutput(TypedDict):
    """Output schema for the inputs-for-target analyzer agent."""

    messages: Sequence[BaseMessage]
    q_analysis: QueryAnalysisIFT
    lookup_results: LookupResult
    calc_results: ModelInputAnalysisToolResult
    resolved_inputs: list[str]


class ExcelModelAnalizerInput(TypedDict):
    """Input schema for excel model analysis agent"""

    messages: Sequence[BaseMessage]
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    user_id: Optional[str]


class ExcelModelAnalizerState(TypedDict):
    """State schema for excel model analysis agent"""

    messages: Annotated[Sequence[BaseMessage], add_messages]
    available_inputs: dict[str, str]
    available_outputs: dict[str, str]
    user_id: Optional[str]
    q_analysis: QueryAnalysisEMA
    lookup_results: LookupResult
    calc_results: ExcelAnalysisToolArgs
    resolved_inputs: list[InputItem]


class ExcelModelAnalizerOutput(TypedDict):
    """Output schema for excel model analysis agent"""

    messages: Sequence[BaseMessage]
    q_analysis: QueryAnalysisEMA
    lookup_results: LookupResult
    calc_results: ExcelAnalysisToolArgs
    resolved_inputs: list[InputItem]
