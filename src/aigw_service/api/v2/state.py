"""Agent state of v2 (spec §9 «Состояние агента»), saved by the checkpointer.

``todos`` comes from ``TodoListMiddleware``'s own state schema, ``resolved_terms`` and ``step_results`` from ``ToolsState``;
the remaining fields are declared here and written by the stories that own them.
"""

from typing import Any, NotRequired

from aigw_service.api.v2.tools.state import ToolsState


class CopilotAgentState(ToolsState):
    """Session state of agent v2."""

    turn_id: NotRequired[str]
    """Current turn (TurnContext, S2.2)."""
    turn_message_id: NotRequired[str | None]
    """Id of the user message this turn started with: the turn's messages follow it (TurnContext, S2.6)."""
    model_ref: NotRequired[dict[str, Any]]
    """``{file_name, file_hash}`` of the workbook the session works with (S2.2)."""
    guard_counters: NotRequired[dict[str, int]]
    """Per-turn counters of the guard middleware (S2.4, S4.1)."""
    turn_summaries: NotRequired[list[dict[str, Any]]]
    """Last turn summaries for follow-up turns (S2.6)."""
