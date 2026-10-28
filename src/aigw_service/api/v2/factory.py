"""AgentFactory (ADR-1): the one place that assembles model, tools, prompt and middleware into ``create_agent``."""

from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.middleware.calc_cache import CalcCache
from aigw_service.api.v2.middleware.cancellation import Cancellation
from aigw_service.api.v2.middleware.context_meter import ContextMeter
from aigw_service.api.v2.middleware.files import file_backend, files_middleware, skills_middleware
from aigw_service.api.v2.middleware.grounding import AnswerGroundingGuard
from aigw_service.api.v2.middleware.language import LanguageGuard
from aigw_service.api.v2.middleware.limits import TurnLimits
from aigw_service.api.v2.middleware.plan import ForcePlanFirst, PlanCompletionGuard
from aigw_service.api.v2.middleware.preview_check import PreviewModelCheck, default_preview_checker
from aigw_service.api.v2.middleware.resilience import engine_retry, model_retry, tool_errors
from aigw_service.api.v2.middleware.skill_gate import SkillToolGate
from aigw_service.api.v2.middleware.stop_event import StopEventGuard
from aigw_service.api.v2.middleware.summarization import ContextBudget, context_tokens, summarization_middleware
from aigw_service.api.v2.middleware.telemetry import Telemetry
from aigw_service.api.v2.middleware.todos import PlanTodoList
from aigw_service.api.v2.middleware.turn_context import TurnContext
from aigw_service.api.v2.middleware.turn_summary import TurnSummary
from aigw_service.api.v2.models import build_model
from aigw_service.api.v2.prompts.system import SYSTEM_PROMPT
from aigw_service.api.v2.skills_check import SKILL_FILE, SKILLS_DIR, SkillsError, validate_skills
from aigw_service.api.v2.state import CopilotAgentState
from aigw_service.api.v2.tools.catalog import ToolCatalog, default_catalog
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX

ANSWER_SKILL = "economic-answer"
"""Sets the answer structure of FR-12; pinned by default through AGENT_PINNED_SKILLS (FR-19)."""

logger = APP_CTX.get_logger()


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
        pinned_skills: Sequence[str] | None = None,
        prompt_budget: ContextBudget | None = None,
        preview_check: PreviewModelCheck | None = None,
    ) -> None:
        self.model = model
        self.store = store
        self.upload_dir = upload_dir
        self.catalog = catalog
        self.extra_middleware = list(middleware)
        self.checkpointer = checkpointer
        self.plan_mode = plan_mode or APP_CONFIG.agent.plan_mode
        self.skills_dir = skills_dir or SKILLS_DIR
        # §11 «Активация» п. 2: None — the list from AGENT_PINNED_SKILLS
        self.pinned_skills = tuple(APP_CONFIG.agent.pinned_skills if pinned_skills is None else pinned_skills)
        self.prompt_budget = prompt_budget or ContextBudget.from_settings()
        # ADR-10 (S4.4 design D4): None — by AGENT_PREVIEW_CHECK and AGENT_MODEL; tests pass their own check
        self.preview_check = preview_check if preview_check is not None else self._preview_from_settings()

    @staticmethod
    def _preview_from_settings() -> PreviewModelCheck | None:
        settings = APP_CONFIG.agent
        if not settings.preview_check:
            return None
        if settings.model != "gigachat":  # the preview model is GigaChat's; a scripted run has none
            logger.warning(
                "AGENT_PREVIEW_CHECK=true без AGENT_MODEL=gigachat: A/B-проверка preview-модели не включена"
            )
            return None
        return PreviewModelCheck(default_preview_checker(), settings.preview_check_percent)

    def service_store(self) -> Any:
        """One store for ``/results/`` (read and write, S1.6), the registry and the calculation cache."""
        return self.store if self.store is not None else APP_CTX.agent_memory.store

    def middleware(self) -> list[AgentMiddleware]:
        planned = self.plan_mode != "off"  # FR-4: off installs neither ForcePlanFirst nor PlanCompletionGuard
        force = [ForcePlanFirst(self.plan_mode)] if planned else []  # §8 №11
        guard = [PlanCompletionGuard(APP_CONFIG.agent.plan_max_returns)] if planned else []  # §8 №19, FR-8
        settings = APP_CONFIG.agent
        engine_tools = self.catalog.engine_tool_names()
        preview = [self.preview_check] if self.preview_check is not None else []
        backend = file_backend(self.skills_dir, self.service_store())
        return [
            # §8 №1: workbook, index and card; no workbook — answer without LLM; pins skills (FR-12)
            TurnContext(store=self.store, upload_dir=self.upload_dir, pinned_skills=self.pinned_skills),
            LanguageGuard(),  # §8 №2: ru / en only, no LLM
            Cancellation(),  # §8 №3: turn deadline and client disconnect stop the turn after the current call
            StopEventGuard(),  # §8 №4: GigaChat StopEvent → StopEventError → HTTP 503, never retried
            Telemetry(),  # §8 №5 (S4.3 design D1): LLM CALL / TOOL CALL lines, one per logical call
            # §8 №7 (S2.7, design D1): compresses the request's history; inside StopEventGuard, outside limits
            summarization_middleware(self.model, backend, self.prompt_budget),
            skills_middleware(backend, self.catalog.skill_tools()),  # §8 №8: catalogue of skills, skill tools
            # §11 «Активация» п. 3 (S3.4 D1): inside SkillsMiddleware, which has attached every tool it showed;
            # a skill tool still detached gets skill_required with the skill to read — before ForcePlanFirst
            SkillToolGate(self.catalog.tool_skills()),
            files_middleware(backend),  # §8 №9: read_file only (AR-Q14)
            PlanTodoList(),  # §8 №10: write_todos with skipped / blocked and a reason (FR-5)
            *force,
            # §8 №12–13 (design D1): per-turn limits; once hit — one model call without tools
            TurnLimits(settings.max_model_calls, settings.max_tool_calls, settings.max_calcs, engine_tools),
            model_retry(settings.llm_retry_attempts, settings.llm_retry_wait),  # §8 №14: inside StopEventGuard
            CalcCache(engine_tools, store=self.store),  # §8 №15: a hit runs neither the engine nor a retry
            tool_errors(),  # §8 №16: exception → envelope; StopEvent passes through
            engine_retry(engine_tools),  # §8 №17: one retry on engine_unavailable
            *self.extra_middleware,
            AnswerGroundingGuard(settings.grounding_max_returns),  # §8 №18, FR-13: in every plan mode
            *guard,  # after_* hooks run bottom-up, the plan is checked first
            TurnSummary(),  # §8 №20: dialog memory in the prompt, the turn's digest after it (FR-16)
            # §8 №6 (S4.4 design D1): sees the request the model really gets; inside the retries — one check a call
            *preview,
            # S2.7 design D5: innermost model wrapper, logs the layers of the request the model really gets
            ContextMeter(self.prompt_budget.max_prompt_tokens),
        ]

    def _check_pins(self, names: set[str]) -> None:
        """FR-19: every pinned skill exists and all of them fit the verbatim tail, or compression would loop."""
        missing = [n for n in self.pinned_skills if n not in names]
        if missing:  # deepagents would skip an unknown pinned name silently
            raise SkillsError([f"{n}: закреплённый skill не найден" for n in missing])
        sizes = {n: self._skill_tokens(n) for n in self.pinned_skills}
        tail = self.prompt_budget.keep_tokens
        if sum(sizes.values()) > tail:  # design D5: each compression would cut one pin and re-pin it next call
            listed = ", ".join(f"{n} ≈ {t}" for n, t in sizes.items())
            problem = (
                f"закреплённые skills ({listed}; всего ≈ {sum(sizes.values())} токенов) не помещаются "
                f"в дословный хвост истории ≈ {tail} токенов (AGENT_SUMMARY_KEEP × AGENT_MAX_PROMPT_TOKENS)"
            )
            raise SkillsError([problem])
        if ANSWER_SKILL not in self.pinned_skills:  # design D3: allowed (eval), but visible at start
            logger.warning(
                "AGENT_PINNED_SKILLS без {}: структура ответа FR-12 не гарантируется (закреплены: {})",
                ANSWER_SKILL,
                ", ".join(self.pinned_skills) or "—",
            )

    def _skill_tokens(self, name: str) -> int:
        text = (self.skills_dir / name / SKILL_FILE).read_text(encoding="utf-8")
        return context_tokens([HumanMessage(text)])

    def build(self) -> Any:
        """A broken skill stops the build (AR-Q15): deepagents would skip it silently."""
        known = {t.name for t in [*self.catalog.base_tools(), *self.catalog.skill_tools()]}
        self._check_pins({s.name for s in validate_skills(self.skills_dir, known)})
        return create_agent(
            self.model,
            self.catalog.base_tools(),
            system_prompt=SYSTEM_PROMPT,
            middleware=self.middleware(),
            state_schema=CopilotAgentState,
            context_schema=RequestContext,
            checkpointer=self.checkpointer,
            store=self.service_store(),  # ToolRuntime.store: the adapter offloads long results here (ADR-7)
        )


@cache
def get_agent_v2() -> Any:
    """One compiled agent per service process; per-request data travels in ``RequestContext``.

    Session state lives in the service checkpointer by ``thread_id`` (ADR-8); today a process-memory ``MemorySaver``.
    """
    return AgentFactory(build_model(), default_catalog(), checkpointer=APP_CTX.agent_memory.checkpointer).build()
