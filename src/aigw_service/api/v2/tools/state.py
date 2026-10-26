"""Agent state fields the tools write (spec §9 «Состояние агента»); CopilotAgentState (S2.1) extends this."""

from typing import Annotated, Any, NotRequired

from langchain.agents import AgentState


def merge_terms(current: dict[str, Any] | None, update: dict[str, Any] | None) -> dict[str, Any]:
    """New resolutions extend (and override per term) what the session already resolved."""
    return {**(current or {}), **(update or {})}


class ToolsState(AgentState):
    """``resolved_terms``: term → ``{id, name, confidence}`` from ``resolve_terms`` (FR-6)."""

    resolved_terms: NotRequired[Annotated[dict[str, Any], merge_terms]]
