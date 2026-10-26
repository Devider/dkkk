"""AgentFactory (ADR-1): the one place that assembles model, tools, prompt and middleware into ``create_agent``."""

from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.middleware.files import file_backend, files_middleware, skills_middleware
from aigw_service.api.v2.middleware.language import LanguageGuard
from aigw_service.api.v2.middleware.plan import ForcePlanFirst, PlanCompletionGuard
from aigw_service.api.v2.middleware.todos import PlanTodoList
from aigw_service.api.v2.middleware.turn_context import TurnContext
from aigw_service.api.v2.middleware.turn_summary import TurnSummary
from aigw_service.api.v2.models import build_model
from aigw_service.api.v2.prompts.system import SYSTEM_PROMPT
from aigw_service.api.v2.skills_check import SKILLS_DIR, SkillsError, validate_skills
from aigw_service.api.v2.state import CopilotAgentState
from aigw_service.api.v2.tools.catalog import ToolCatalog, default_catalog
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX

DEFAULT_PINNED = ("economic-answer",)
"""Skills in context every turn (§11 «Активация» п. 2); AGENT_PINNED_SKILLS makes it configurable in S3.5."""


class AgentFactory:  # pylint: disable=too-many-instance-attributes  # one field per assembly input
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
        skills_dir: Path | None = None,
        pinned_skills: Sequence[str] = DEFAULT_PINNED,
    ) -> None:
        self.model = model
        self.store = store
        self.upload_dir = upload_dir
        self.catalog = catalog
        self.extra_middleware = list(middleware)
        self.checkpointer = checkpointer
        self.plan_mode = plan_mode or APP_CONFIG.agent.plan_mode
        self.skills_dir = skills_dir or SKILLS_DIR
        self.pinned_skills = tuple(pinned_skills)

    def middleware(self) -> list[AgentMiddleware]:
        planned = self.plan_mode != "off"  # FR-4: off installs neither ForcePlanFirst nor PlanCompletionGuard
        force = [ForcePlanFirst(self.plan_mode)] if planned else []  # §8 №11
        guard = [PlanCompletionGuard(APP_CONFIG.agent.plan_max_returns)] if planned else []  # §8 №19, FR-8
        backend = file_backend(self.skills_dir, self.store if self.store is not None else APP_CTX.agent_memory.store)
        return [
            # §8 №1: workbook, index and card; no workbook — answer without LLM; pins skills (FR-12)
            TurnContext(store=self.store, upload_dir=self.upload_dir, pinned_skills=self.pinned_skills),
            LanguageGuard(),  # §8 №2: ru / en only, no LLM
            skills_middleware(backend, self.catalog.skill_tools()),  # §8 №8: catalogue of skills, skill tools
            files_middleware(backend),  # §8 №9: read_file only (AR-Q14)
            PlanTodoList(),  # §8 №10: write_todos with skipped / blocked and a reason (FR-5)
            *force,
            *self.extra_middleware,
            *guard,  # after_* hooks run bottom-up, the plan is checked first
            TurnSummary(),  # §8 №20: dialog memory in the prompt, the turn's digest after it (FR-16)
        ]

    def build(self) -> Any:
        """A broken skill stops the build (AR-Q15): deepagents would skip it silently."""
        known = {t.name for t in [*self.catalog.base_tools(), *self.catalog.skill_tools()]}
        names = {s.name for s in validate_skills(self.skills_dir, known)}
        missing = [n for n in self.pinned_skills if n not in names]
        if missing:  # deepagents would skip an unknown pinned name silently
            raise SkillsError([f"{n}: закреплённый skill не найден" for n in missing])
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
    """One compiled agent per service process; per-request data travels in ``RequestContext``.

    Session state lives in the service checkpointer by ``thread_id`` (ADR-8); today a process-memory ``MemorySaver``.
    """
    return AgentFactory(build_model(), default_catalog(), checkpointer=APP_CTX.agent_memory.checkpointer).build()
