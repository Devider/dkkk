"""Per-request context passed to ``agent.ainvoke(..., context=...)`` (spec §9 «Runtime context»).

Never stored by the checkpointer and never shown to the model. The router fills the request part;
``TurnContext`` (S2.2) fills the workbook path and the model index into the same object, which the tools read
from ``runtime.context`` (``ToolContext``).
"""

from dataclasses import dataclass

from aigw_service.api.v2.tools.context import ModelIndex


@dataclass
class RequestContext:
    """One turn of one user's session."""

    user_id: str
    session_id: str
    trace_id: str = ""
    client_id: str = ""
    deadline: float | None = None
    """Monotonic time the turn must finish by (S2.8)."""
    file_path: str = ""
    model_index: ModelIndex | None = None

    @property
    def thread_id(self) -> str:
        """Checkpointer thread and engine-gate lock key: users never share a thread (AR-Q7)."""
        return f"{self.user_id}:{self.session_id}"
