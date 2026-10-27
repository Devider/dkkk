from typing import Literal

from pydantic import Field, model_validator

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
    # Plan mode of agent v2 (FR-4, spike S0.2): auto — the model decides, a tool call without a plan gets
    # plan_required; always — a plan every turn (eval, debugging); off — ForcePlanFirst is not installed
    plan_mode: Literal["always", "auto", "off"] = Field(
        default="auto",
        validation_alias="AGENT_PLAN_MODE",
    )
    # PlanCompletionGuard (FR-8): how many times an answer with open plan steps goes back to the model
    plan_max_returns: int = Field(
        default=3,
        ge=0,
        validation_alias="AGENT_PLAN_MAX_RETURNS",
    )
    # AnswerGroundingGuard (FR-13): how many times an answer with unconfirmed numbers goes back to the model;
    # then each such number becomes «н/д» with a caveat (0 — at once)
    grounding_max_returns: int = Field(
        default=1,
        ge=0,
        validation_alias="AGENT_GROUNDING_MAX_RETURNS",
    )

    # Retry — количество попыток вызова LLM при транзитных ошибках
    llm_retry_attempts: int = Field(
        default=3,
        ge=0,
        validation_alias="AGENT_LLM_RETRY_ATTEMPTS",
    )
    llm_retry_wait: float = Field(
        default=2.0,
        ge=0,
        validation_alias="AGENT_LLM_RETRY_WAIT",
    )

    # Turn limits of agent v2 (FR-9): counted per turn; once one is hit — one model call without tools
    max_model_calls: int = Field(
        default=40,
        ge=1,
        validation_alias="AGENT_MAX_MODEL_CALLS",
    )
    max_tool_calls: int = Field(
        default=30,
        ge=1,
        validation_alias="AGENT_MAX_TOOL_CALLS",
    )
    max_calcs: int = Field(
        default=10,
        ge=1,
        validation_alias="AGENT_MAX_CALCS",
    )
    # Turn timeout of agent v2 (AR-Q10), seconds: then one model call without tools for a partial answer
    turn_timeout: float = Field(
        default=300.0,
        gt=0,
        validation_alias="AGENT_TURN_TIMEOUT",
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
    # Context budget of agent v2 (AR-Q6), tokens: history is summarized at summary_trigger of it,
    # keeping a verbatim tail of summary_keep of it; the request without history is already ~5.6k tokens, so a budget
    # under 20k would summarize on almost every call
    max_prompt_tokens: int = Field(
        default=30000,
        ge=20000,
        validation_alias="AGENT_MAX_PROMPT_TOKENS",
    )
    summary_trigger: float = Field(
        default=0.7,
        gt=0,
        lt=1,
        validation_alias="AGENT_SUMMARY_TRIGGER",
    )
    summary_keep: float = Field(
        default=0.3,
        gt=0,
        lt=1,
        validation_alias="AGENT_SUMMARY_KEEP",
    )
    # Scenario grid size checked before the engine runs (spec §10 «Контракт» п. 2)
    max_grid: int = Field(
        default=400,
        validation_alias="AGENT_MAX_GRID",
    )
    # Tool result offload (ADR-7, AR-Q6): a longer serialized envelope goes to /results/, the model gets a summary
    result_max_chars: int = Field(
        default=1500,
        ge=500,
        validation_alias="AGENT_RESULT_MAX_CHARS",
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

    @model_validator(mode="after")
    def _tail_below_trigger(self) -> "AgentSettings":
        """The kept tail must be smaller than the threshold, or every call would summarize again (AR-Q6)."""
        if self.summary_keep >= self.summary_trigger:
            raise ValueError("AGENT_SUMMARY_KEEP must be less than AGENT_SUMMARY_TRIGGER")
        return self
