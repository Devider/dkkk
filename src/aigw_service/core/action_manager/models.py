import uuid
from enum import Enum

from pydantic import BaseModel, Field

from aigw_service.core.action_manager.base_action import BaseAction


class ActionHistoryPoint(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Action point ID")
    x_trace_id: str | None = Field(None, description="Trace ID")
    x_client_id: str | None = Field(None, description="Client ID")
    x_session_id: str | None = Field(None, description="Session ID")
    x_request_time: str | None = Field(None, description="Request time")
    x_user_id: str | None = Field(None, description="User ID")
    action: BaseAction = Field(None, description="Action")
    type: str = Field(description="Type of action")


class ActionsHistory(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Action ID")
    chain: list[ActionHistoryPoint] = Field([], description="List of Action points")


class ActionsChain(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Action ID")
    chain: list[BaseAction] = Field([], description="List of Action")


class ActionType(Enum):
    EXECUTE = "execute"
    UNDO = "undo"
