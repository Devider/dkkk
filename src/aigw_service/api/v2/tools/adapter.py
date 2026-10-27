"""Domain function → LangChain tool (ADR-5).

The model sees only the arguments of ``ToolSpec.args_schema``. The user, the workbook path and the model
index come from the turn context in ``runtime.context`` and never appear in a tool schema. Every result is
an envelope (``envelope.py``); unexpected exceptions propagate to ``ToolErrorMiddleware`` (S2.5). A transient
failure (``engine_unavailable``) is raised as ``ToolError`` so ``ToolRetryMiddleware`` can repeat the call.

A result longer than ``AGENT_RESULT_MAX_CHARS`` is offloaded to ``/results/`` and the model gets a compact envelope
(``offload.py``, ADR-7). Every call also records a ``step_results`` entry with all the numbers of the result (S1.6).
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel

from aigw_service.api.v2.tools.context import ToolContext
from aigw_service.api.v2.tools.envelope import TRANSIENT_CODES, ToolError, ToolFailure, ToolSuccess, to_json
from aigw_service.api.v2.tools.offload import offload
from aigw_service.config import APP_CONFIG

BASE = "base"
"""Availability of a tool the model always sees; a skill tool carries the skill name instead."""


@dataclass(frozen=True)
class ToolSpec:  # pylint: disable=too-many-instance-attributes  # catalog fields of spec §10
    """Everything the adapter needs to expose one domain function as a tool."""

    name: str
    description: str
    args_schema: type[BaseModel]
    call: Callable[..., ToolSuccess | ToolFailure] | None = None
    """Sync mapping of model arguments + turn context (+ state, see ``needs_state``) to the domain call and its
    envelope; runs in a worker thread."""
    acall: Callable[[BaseModel, ToolContext], Awaitable[ToolSuccess | ToolFailure]] | None = None
    """Async alternative to ``call`` (e.g. a semantic-layer LLM call); runs on the event loop."""
    availability: str = BASE
    uses_engine: bool = False
    needs_state: bool = False
    """``call(args, ctx, state)`` gets a read-only snapshot of the agent state (``derive`` reads the turn's numbers)."""

    def __post_init__(self) -> None:
        if (self.call is None) == (self.acall is None):
            raise ValueError(f"tool {self.name!r}: set exactly one of call / acall")
        if self.needs_state and self.call is None:
            raise ValueError(f"tool {self.name!r}: needs_state is supported for a sync call only")


class ToolContextError(RuntimeError):
    """The agent was assembled without a turn context the tools need: a wiring bug, not a model error."""


def turn_context(runtime: ToolRuntime) -> ToolContext:
    context = runtime.context
    if not isinstance(context, ToolContext):
        raise ToolContextError(
            f"runtime.context must provide user_id, thread_id, file_path and model_index, got {type(context).__name__}"
        )
    if context.model_index is None:
        raise ToolContextError("runtime.context has no model_index: TurnContext must load the workbook first")
    return context


def state_snapshot(runtime: ToolRuntime) -> Mapping[str, Any]:
    """Agent state as the tool sees it: top level read-only, the tool cannot change what the graph holds."""
    return MappingProxyType(dict(runtime.state or {}))


def step_entry(
    tool: str, runtime: ToolRuntime, envelope: ToolSuccess | ToolFailure, shown: ToolSuccess | ToolFailure
) -> dict[str, Any]:
    """``step_results`` record (spec §9): ``numbers`` come from the full envelope, ``ref`` from what the model saw."""
    if isinstance(envelope, ToolFailure):
        return {
            "call_id": runtime.tool_call_id,
            "tool": tool,
            "status": envelope.status,
            "code": str(envelope.code),
            "summary": envelope.error,
            "ref": None,
            "numbers": [],
        }
    return {
        "call_id": runtime.tool_call_id,
        "tool": tool,
        "status": envelope.status,
        "code": None,
        "summary": envelope.result,
        "ref": shown.ref if isinstance(shown, ToolSuccess) else None,
        "numbers": [n.model_dump(mode="json", exclude_none=True) for n in envelope.numbers],
    }


def build_tool(spec: ToolSpec) -> StructuredTool:
    """``StructuredTool`` whose schema is ``spec.args_schema``; ``runtime`` is injected by LangChain."""

    async def run(runtime: ToolRuntime, **kwargs: Any) -> Command:
        context = turn_context(runtime)
        args = spec.args_schema.model_validate(kwargs)
        try:
            if spec.acall is not None:
                envelope = await spec.acall(args, context)
            elif spec.needs_state:
                envelope = await asyncio.to_thread(spec.call, args, context, state_snapshot(runtime))
            else:
                envelope = await asyncio.to_thread(spec.call, args, context)
        except ToolError as exc:
            envelope = exc.failure()
        if isinstance(envelope, ToolFailure) and envelope.code in TRANSIENT_CODES:
            raise ToolError(envelope.code, envelope.error, envelope.hint)
        shown: ToolSuccess | ToolFailure = envelope
        if isinstance(envelope, ToolSuccess):
            shown = await offload(
                envelope,
                store=runtime.store,
                user_id=context.user_id,
                turn_id=(runtime.state or {}).get("turn_id"),
                call_id=runtime.tool_call_id,
                limit=APP_CONFIG.agent.result_max_chars,
            )
        message = ToolMessage(to_json(shown), tool_call_id=runtime.tool_call_id, name=spec.name)
        update: dict[str, Any] = {
            "messages": [message],
            "step_results": [step_entry(spec.name, runtime, envelope, shown)],
        }
        if isinstance(envelope, ToolSuccess) and envelope.state_update:
            update.update(envelope.state_update)
        return Command(update=update)

    if not spec.args_schema.model_fields:
        # langchain-core drops every argument, the injected runtime too, for a schema without fields;
        # a schema inferred from ``(runtime)`` keeps the runtime and still shows the model no arguments.
        async def run_without_args(runtime: ToolRuntime[Any, Any]) -> Command:
            return await run(runtime)

        return StructuredTool.from_function(coroutine=run_without_args, name=spec.name, description=spec.description)
    return StructuredTool.from_function(
        coroutine=run,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
    )
