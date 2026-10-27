"""Per-request context passed to ``agent.ainvoke(..., context=...)`` (spec §9 «Runtime context»).

Never stored by the checkpointer and never shown to the model. The router fills the request part;
``TurnContext`` (S2.2) fills the workbook path and the model index into the same object, which the tools read
from ``runtime.context`` (``ToolContext``).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aigw_service.api.v2.tools.context import ModelIndex


@dataclass
class RequestContext:  # pylint: disable=too-many-instance-attributes  # fields of spec §9
    """One turn of one user's session."""

    user_id: str
    session_id: str
    trace_id: str = ""
    client_id: str = ""
    deadline: float | None = None
    """Monotonic time the turn must finish by; ``AgentV2.answer`` sets it from AGENT_TURN_TIMEOUT (S2.8)."""
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the client disconnects: the turn stops after the current call (AR-Q12)."""
    file_path: str = ""
    model_index: ModelIndex | None = None
    card: dict[str, Any] | None = None
    """Model card for the prompt (TurnContext); tools do not use it."""

    @property
    def thread_id(self) -> str:
        """Checkpointer thread and engine-gate lock key: users never share a thread (AR-Q7)."""
        return f"{self.user_id}:{self.session_id}"
