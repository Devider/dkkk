"""Domain function → LangChain tool (ADR-5).

The model sees only the arguments of ``ToolSpec.args_schema``. The user, the workbook path and the model
index come from the turn context in ``runtime.context`` and never appear in a tool schema.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from aigw_service.api.v2.tools.context import ToolContext

BASE = "base"
"""Availability of a tool the model always sees; a skill tool carries the skill name instead."""


@dataclass(frozen=True)
class ToolSpec:
    """Everything the adapter needs to expose one domain function as a tool."""

    name: str
    description: str
    args_schema: type[BaseModel]
    call: Callable[[BaseModel, ToolContext], Any]
    """Maps model arguments + turn context to the domain call; runs in a worker thread."""
    availability: str = BASE
    uses_engine: bool = False


class ToolContextError(RuntimeError):
    """The agent was assembled without a turn context the tools need: a wiring bug, not a model error."""


def turn_context(runtime: ToolRuntime) -> ToolContext:
    context = runtime.context
    if not isinstance(context, ToolContext):
        raise ToolContextError(
            f"runtime.context must provide user_id, file_path and model_index, got {type(context).__name__}"
        )
    return context


def serialize(result: Any) -> str:
    """Domain result → tool message text (the S1.2 envelope will replace this)."""
    if isinstance(result, BaseModel):
        result = result.model_dump()
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def build_tool(spec: ToolSpec) -> StructuredTool:
    """``StructuredTool`` whose schema is ``spec.args_schema``; ``runtime`` is injected by LangChain."""

    async def run(runtime: ToolRuntime, **kwargs: Any) -> str:
        context = turn_context(runtime)
        args = spec.args_schema.model_validate(kwargs)
        result = await asyncio.to_thread(spec.call, args, context)
        return serialize(result)

    return StructuredTool.from_function(
        coroutine=run,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
    )
