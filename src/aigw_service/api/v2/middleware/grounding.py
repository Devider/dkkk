"""AnswerGroundingGuard (FR-13, ADR-9, spec §8 правило 7, №18): every number of the final answer comes from a source.

Sources are the turn's tool results, past turn summaries and the user's messages (``grounding.collect_sources``).
An answer with unconfirmed numbers goes back to the model once with their list as a user message (spike S0.2 C7);
after that, or when the turn's limit or time is exhausted, each unconfirmed number becomes «н/д» and a caveat is
added to «Оговорки». Placed right above PlanCompletionGuard: ``after_*`` hooks run bottom-up, so it only sees an
answer the plan check accepted (a plan return jumps to the model before this hook runs).
"""

from typing import Any, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage

from aigw_service.api.v2.answer_check import is_analytic, sections, unconfirmed_numbers
from aigw_service.api.v2.middleware.limits import limit_hit
from aigw_service.api.v2.middleware.plan import final_answer
from aigw_service.api.v2.prompts.middleware import GROUNDING_CAVEAT, GROUNDING_PLACEHOLDER, GROUNDING_REMINDER
from aigw_service.api.v2.tools.grounding import Mention, agent_note, collect_sources
from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()

RETURNS = "grounding_returns"
REMOVED = "grounding_removed"
CAVEATS = "Оговорки"


class GroundingState(AgentState):
    """Counters live in the state, so they hold across model calls of the turn (TurnContext resets them)."""

    guard_counters: NotRequired[dict[str, int]]


def _with_caveat(text: str) -> str:
    """The caveat goes to the end of «Оговорки», into a new «Оговорки» section, or is the last paragraph."""
    if not is_analytic(text):
        return f"{text.rstrip()}\n\n{GROUNDING_CAVEAT}"
    found = sections(text)
    caveats = next((i for i, (name, _, _) in enumerate(found) if name == CAVEATS), None)
    if caveats is None:
        return f"{text.rstrip()}\n\n## {CAVEATS}\n\n- {GROUNDING_CAVEAT}"
    end = found[caveats + 1][1] if caveats + 1 < len(found) else len(text)
    head, tail = text[:end].rstrip(), text[end:]
    return f"{head}\n- {GROUNDING_CAVEAT}" + (f"\n\n{tail.lstrip()}" if tail.strip() else "")


def remove_numbers(text: str, mentions: list[Mention]) -> str:
    """Each mention (with its ``%``) becomes «н/д», then the caveat is added; the numbers themselves are not named."""
    for m in sorted(mentions, key=lambda m: m.start, reverse=True):
        text = text[: m.start] + GROUNDING_PLACEHOLDER + text[m.end :]
    return _with_caveat(text)


class AnswerGroundingGuard(AgentMiddleware):
    """§8 №18; ``max_returns`` — how many times an answer with unconfirmed numbers goes back to the model."""

    state_schema = GroundingState

    def __init__(self, max_returns: int) -> None:
        super().__init__()
        self.max_returns = max_returns

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        answer = final_answer(state)
        if answer is None:
            return None
        unconfirmed = unconfirmed_numbers(answer.text, collect_sources(state))
        if not unconfirmed:
            return None
        counters = state.get("guard_counters") or {}
        returns = counters.get(RETURNS, 0)
        if returns < self.max_returns and not limit_hit(state):  # FR-9: the partial answer is final
            logger.info("AnswerGroundingGuard: {} unconfirmed numbers, action=return", len(unconfirmed))
            numbers = ", ".join(dict.fromkeys(m.written.strip() for m in unconfirmed))
            return {
                "messages": [agent_note(GROUNDING_REMINDER.format(numbers=numbers))],
                "guard_counters": {**counters, RETURNS: returns + 1},
                "jump_to": "model",
            }
        logger.info("AnswerGroundingGuard: {} unconfirmed numbers, action=removed", len(unconfirmed))
        final = AIMessage(remove_numbers(answer.text, unconfirmed), id=answer.id)
        return {"messages": [final], "guard_counters": {**counters, REMOVED: len(unconfirmed)}}

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
