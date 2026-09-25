"""Dummy factories and validation for structured LLM outputs.

If LLM returns None for required fields, the validation fallback returns
a sensible dummy so the pipeline can continue instead of crashing with
``AttributeError`` / ``ValidationError`` downstream.
"""

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from aigw_service.api.v1.schemas.llm_outputs import (
    QueryAnalysisEMA,
    QueryAnalysisIFT,
)

logger = None  # set at import-time from APP_CTX


def _get_logger():
    global logger
    if logger is None:
        from aigw_service.context import APP_CTX

        logger = APP_CTX.get_logger()
    return logger


T = TypeVar("T", bound=BaseModel)


# ===================================================================
# Dummy factories
# ===================================================================


def dummy_query_ift() -> "QueryAnalysisIFT":
    # target_value/output_year stay None (not a fake 0) so a parse failure routes through
    # OrchestratorTools' missing-parameter check and asks the user to clarify, instead of
    # silently running the calc engine on a fabricated year/target.
    return QueryAnalysisIFT(
        analysis="Dummy: LLM failed to parse required fields after 3 retries",
        mentioned_output_name="",
        mentioned_inputs=[],
        output_name="",
        target_value=None,
        output_year=None,
    )


def dummy_query_ema() -> "QueryAnalysisEMA":
    return QueryAnalysisEMA(
        analysis="Dummy: LLM failed to parse required fields after 3 retries",
        mentioned_outputs=[],
        mentioned_inputs=[],
        year=None,
    )


# ===================================================================
# Validation
# ===================================================================


def validate_structured_output(parsed: object, schema: type[T]) -> T:
    """Validate structured output against a Pydantic schema.

    Tries ``schema.model_validate(parsed)`` so that even same-type
    instances are re-validated.  Raises ``ValidationError`` on failure.
    """
    return schema.model_validate(parsed)  # type: ignore[return-value]


# ===================================================================
# Retry wrapper
# ===================================================================


def retry_structured_llm(
    llm,
    schema: type[T],
    prompt: list,
    max_retries: int = 3,
) -> tuple[T, bool, Any]:
    """Invoke LLM with structured output and retry on validation failure.

    Parameters
    ----------
    llm : LangChain LLM
        The LLM instance to call.
    schema : type[BaseModel]
        Pydantic schema for structured output.
    prompt : list
        Message list to send to the LLM.
    max_retries : int
        Maximum number of LLM calls (default 3).

    Returns
    -------
    tuple[BaseModel, bool, Any]
        ``(validated_output, success, raw_response)`` — *success* is
        ``True`` when the response passed validation.  *raw_response*
        is the raw LangChain response (for token extraction) or ``None``
        when dummy was used.
    """
    from time import time as _time

    structured_llm = llm.with_structured_output(schema, include_raw=True)

    for attempt in range(1, max_retries + 1):
        start = _time()
        raw_response = structured_llm.invoke(prompt)
        elapsed = _time() - start
        _get_logger().info(raw_response)
        _get_logger().info(
            "LLM structured call (attempt {}, {}, {:.2f}s)",
            attempt,
            max_retries,
            elapsed,
        )

        parsed = raw_response["parsed"]

        try:
            validated = validate_structured_output(parsed, schema)
            return validated, True, raw_response
        except ValidationError:
            _get_logger().warning(
                "Validation attempt {}, {} failed",
                attempt,
                max_retries,
            )

    # All retries exhausted — fall back to dummy
    _get_logger().warning(
        "Max retries ({}) reached for schema {}, using dummy",
        max_retries,
        schema.__name__,
    )
    dummy_fn = {
        QueryAnalysisIFT: dummy_query_ift,
        QueryAnalysisEMA: dummy_query_ema,
    }.get(schema, schema)  # type: ignore[dict-item]

    return dummy_fn(), False, None  # type: ignore[return-value]
