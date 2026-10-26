"""AgentFactory (ADR-1): the one place that assembles model, tools, prompt and middleware into ``create_agent``."""

from collections.abc import Sequence
from functools import cache
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.middleware.language import LanguageGuard
from aigw_service.api.v2.middleware.plan import ForcePlanFirst, PlanCompletionGuard
from aigw_service.api.v2.middleware.todos import PlanTodoList
from aigw_service.api.v2.middleware.turn_context import TurnContext
from aigw_service.api.v2.models import build_model
from aigw_service.api.v2.prompts.system import SYSTEM_PROMPT
from aigw_service.api.v2.state import CopilotAgentState
from aigw_service.api.v2.tools.catalog import ToolCatalog, default_catalog
from aigw_service.config import APP_CONFIG


class AgentFactory:
    """Middleware order is fixed here, not by callers: ``before_*`` top-down, ``after_*`` bottom-up (spec §8)."""

    def __init__(
        self,
        model: BaseChatModel,
        catalog: ToolCatalog,
        middleware: Sequence[AgentMiddleware] = (),
        checkpointer: Any = None,
        store: Any = None,
        upload_dir: str | None = None,
        plan_mode: str | None = None,
    ) -> None:
        self.model = model
        self.store = store
        self.upload_dir = upload_dir
        self.catalog = catalog
        self.extra_middleware = list(middleware)
        self.checkpointer = checkpointer
        self.plan_mode = plan_mode or APP_CONFIG.agent.plan_mode

    def middleware(self) -> list[AgentMiddleware]:
        planned = self.plan_mode != "off"  # FR-4: off installs neither ForcePlanFirst nor PlanCompletionGuard
        force = [ForcePlanFirst(self.plan_mode)] if planned else []  # §8 №11
        guard = [PlanCompletionGuard(APP_CONFIG.agent.plan_max_returns)] if planned else []  # §8 №19, FR-8
        return [
            TurnContext(
                store=self.store, upload_dir=self.upload_dir
            ),  # §8 №1: workbook, index and card; no workbook — answer without LLM
            LanguageGuard(),  # §8 №2: ru / en only, no LLM
            PlanTodoList(),  # §8 №10: write_todos with skipped / blocked and a reason (FR-5)
            *force,
            *self.extra_middleware,
            *guard,  # last: after_* hooks run bottom-up, the plan is checked first
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
