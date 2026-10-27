"""Agent state fields the tools write (spec §9 «Состояние агента»); CopilotAgentState (S2.1) extends this."""

from typing import Annotated, Any, NotRequired

from langchain.agents import AgentState

RESET = "__reset__"
"""``{RESET: True}`` as an update clears the session's resolutions (a new workbook, spec §9 правило 2)."""


def merge_terms(current: dict[str, Any] | None, update: dict[str, Any] | None) -> dict[str, Any]:
    """New resolutions extend (and override per term) what the session already resolved."""
    update = update or {}
    if update.get(RESET):
        return {k: v for k, v in update.items() if k != RESET}
    return {**(current or {}), **update}


def merge_steps(current: list[dict[str, Any]] | None, update: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Results of parallel tool calls append; ``[{RESET: True}, ...]`` starts the turn's list anew (TurnContext)."""
    update = list(update or [])
    if update and update[0].get(RESET):
        return update[1:]
    return [*(current or []), *update]


STEPS_RESET: list[dict[str, Any]] = [{RESET: True}]
"""Update that clears ``step_results`` at the start of a turn."""


class ToolsState(AgentState):
    """Agent-state fields the tool adapter writes.

    ``resolved_terms``: term → ``{id, name, confidence}`` from ``resolve_terms`` (FR-6).
    ``step_results``: ``{call_id, tool, status, code, summary, ref, numbers}`` per call of this turn, with every number
    of the result even when the model saw only part of them (ADR-7, S1.6).
    """

    resolved_terms: NotRequired[Annotated[dict[str, Any], merge_terms]]
    step_results: NotRequired[Annotated[list[dict[str, Any]], merge_steps]]
