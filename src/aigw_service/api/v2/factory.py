"""AgentFactory (ADR-1): the one place that assembles model, tools, prompt and middleware into ``create_agent``."""

from collections.abc import Sequence
from functools import cache
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.language_models import BaseChatModel

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.models import build_model
from aigw_service.api.v2.prompts.middleware import TODO_SYSTEM_PROMPT, TODO_TOOL_DESCRIPTION
from aigw_service.api.v2.prompts.system import SYSTEM_PROMPT
from aigw_service.api.v2.state import CopilotAgentState
from aigw_service.api.v2.tools.catalog import ToolCatalog, default_catalog


class AgentFactory:
    """Middleware order is fixed here, not by callers: ``before_*`` top-down, ``after_*`` bottom-up (spec §8)."""

    def __init__(
        self,
        model: BaseChatModel,
        catalog: ToolCatalog,
        middleware: Sequence[AgentMiddleware] = (),
        checkpointer: Any = None,
    ) -> None:
        self.model = model
        self.catalog = catalog
        self.extra_middleware = list(middleware)
        self.checkpointer = checkpointer

    def middleware(self) -> list[AgentMiddleware]:
        return [
            TodoListMiddleware(system_prompt=TODO_SYSTEM_PROMPT, tool_description=TODO_TOOL_DESCRIPTION),
            *self.extra_middleware,
        ]

    def build(self) -> Any:
        return create_agent(
            self.model,
            self.catalog.base_tools(),
            system_prompt=SYSTEM_PROMPT,
            middleware=self.middleware(),
            state_schema=CopilotAgentState,
            context_schema=RequestContext,
            checkpointer=self.checkpointer,
        )


@cache
def get_agent_v2() -> Any:
    """One compiled agent per service process; per-request data travels in ``RequestContext``."""
    return AgentFactory(build_model(), default_catalog()).build()
