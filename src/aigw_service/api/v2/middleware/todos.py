"""Plan schema of v2 (FR-5, spike S0.3): five step statuses and a reason for ``skipped`` / ``blocked``.

The built-in ``write_todos`` knows only ``pending`` / ``in_progress`` / ``completed`` and keeps no reason.
``PlanTodoList`` is ``TodoListMiddleware`` with its own state and argument schema; the tool keeps the name
``write_todos`` that prompts, ``ForcePlanFirst`` and ``TurnContext`` rely on.
"""

from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain.agents.middleware import AgentState, TodoListMiddleware
from langchain.agents.middleware.types import OmitFromInput
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field, model_validator

from aigw_service.api.v2.prompts.middleware import TODO_SYSTEM_PROMPT, TODO_TOOL_DESCRIPTION

Status = Literal["pending", "in_progress", "completed", "skipped", "blocked"]
OPEN: frozenset[str] = frozenset({"pending", "in_progress"})
NEEDS_REASON: frozenset[str] = frozenset({"skipped", "blocked"})


class PlanStep(BaseModel):
    """One plan step as the model writes it."""

    content: str = Field(min_length=1, description="Что делает шаг")
    status: Status = Field(description="pending, in_progress, completed, skipped или blocked")
    reason: str | None = Field(
        default=None, description="Почему шаг пропущен (skipped) или чего он ждёт от пользователя (blocked)"
    )

    @model_validator(mode="after")
    def _reason_for_closed_without_result(self) -> "PlanStep":
        if self.status in NEEDS_REASON and not (self.reason or "").strip():
            raise ValueError(f"шаг со статусом {self.status} должен иметь причину reason")
        return self


class WritePlanInput(BaseModel):
    """Arguments of ``write_todos``: the whole plan every time."""

    todos: list[PlanStep]


class PlanStepState(TypedDict):
    """A plan step in the agent state."""

    content: str
    status: Status
    reason: NotRequired[str]


class PlanState(AgentState):
    """Same ``todos`` field as the built-in ``PlanningState``, with the extended step."""

    todos: Annotated[NotRequired[list[PlanStepState]], OmitFromInput]


def _steps(todos: list[PlanStep | dict[str, Any]]) -> list[dict[str, Any]]:
    return [(t if isinstance(t, PlanStep) else PlanStep(**t)).model_dump(exclude_none=True) for t in todos]


def plan_text(steps: list[dict[str, Any]]) -> str:
    """``N. [status] content — reason`` per step."""
    lines = []
    for i, step in enumerate(steps, 1):
        reason = f" — {step['reason']}" if step.get("reason") else ""
        lines.append(f"{i}. [{step['status']}] {step['content']}{reason}")
    return "\n".join(lines)


def _write_plan(runtime: ToolRuntime, todos: list[PlanStep]) -> Command:
    steps = _steps(todos)
    message = ToolMessage(f"План обновлён:\n{plan_text(steps)}", tool_call_id=runtime.tool_call_id)
    return Command(update={"todos": steps, "messages": [message]})


async def _awrite_plan(runtime: ToolRuntime, todos: list[PlanStep]) -> Command:
    return _write_plan(runtime, todos)


class PlanTodoList(TodoListMiddleware):
    """``TodoListMiddleware`` with the v2 plan schema and Russian prompts (spec §8 №10)."""

    state_schema = PlanState  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__(system_prompt=TODO_SYSTEM_PROMPT, tool_description=TODO_TOOL_DESCRIPTION)
        self.tools = [
            StructuredTool.from_function(
                name="write_todos",
                description=TODO_TOOL_DESCRIPTION,
                func=_write_plan,
                coroutine=_awrite_plan,
                args_schema=WritePlanInput,
                infer_schema=False,
            )
        ]
