from pydantic import Field

from ..base_config import BaseAppSettings


class AgentSettings(BaseAppSettings):
    """Настройки устойчивости AI-агента (retry, StopEvent, loop prevention)."""

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
