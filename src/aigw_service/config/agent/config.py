from typing import Literal

from pydantic import Field

from ..base_config import BaseAppSettings


class AgentSettings(BaseAppSettings):
    """Настройки AI-агента: выбор движка и модели v2, устойчивость (retry, StopEvent), лимиты, пул движка."""

    # Agent engine switch (FR-20): v1 — LangGraph graph of main_graph.py, v2 — create_agent (api/v2)
    engine: Literal["v1", "v2"] = Field(
        default="v1",
        validation_alias="AGENT_ENGINE",
    )
    # Model of agent v2 (AR-Q1): GigaChat (api_key / certificates by LOCAL and GIGACHAT_*) or a scripted model
    model: Literal["gigachat", "scripted"] = Field(
        default="gigachat",
        validation_alias="AGENT_MODEL",
    )
    script_path: str = Field(
        default="",
        validation_alias="AGENT_SCRIPT_PATH",
    )

    # Retry — количество попыток вызова LLM при транзитных ошибках
    llm_retry_attempts: int = Field(
        default=3,
        validation_alias="AGENT_LLM_RETRY_ATTEMPTS",
    )
    llm_retry_wait: float = Field(
        default=2.0,
        validation_alias="AGENT_LLM_RETRY_WAIT",
    )

    # Loop prevention — лимиты на итерации, сообщения, токены
    max_iterations: int = Field(
        default=10,
        validation_alias="AGENT_MAX_ITERATIONS",
    )
    max_messages: int = Field(
        default=30,
        validation_alias="AGENT_MAX_MESSAGES",
    )
    max_prompt_tokens: int = Field(
        default=30000,
        validation_alias="AGENT_MAX_PROMPT_TOKENS",
    )
    # Scenario grid size checked before the engine runs (spec §10 «Контракт» п. 2)
    max_grid: int = Field(
        default=400,
        validation_alias="AGENT_MAX_GRID",
    )
    # Engine pool (ADR-6): concurrent LibreOffice recalculations in the service and per-call timeout, seconds
    calc_workers: int = Field(
        default=2,
        validation_alias="AGENT_CALC_WORKERS",
    )
    calc_timeout: float = Field(
        default=120.0,
        validation_alias="AGENT_CALC_TIMEOUT",
    )
    duplicate_tool_call_threshold: int = Field(
        default=3,
        validation_alias="AGENT_DUP_TOOL_THRESHOLD",
    )

    # StopEvent (ТН08) — обработка реализована в infra-слое (context.py).
    # При 403 + "temporarily unavailable" → StopEventError → HTTP 503.
    stop_on_critical: bool = Field(
        default=True,
        validation_alias="AGENT_STOP_ON_CRITICAL",
    )
    stop_on_error: bool = Field(
        default=False,
        validation_alias="AGENT_STOP_ON_ERROR",
    )
