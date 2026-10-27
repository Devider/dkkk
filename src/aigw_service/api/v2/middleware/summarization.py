"""History compression of agent v2 (AR-Q6, ADR-2, spec §8 №7): deepagents ``SummarizationMiddleware``.

deepagents 0.7 compresses the *request*, not the state (design D1): it keeps ``_summarization_event`` with a cutoff
and sends the model ``[summary, *messages[cutoff:]]``; the compressed part goes to ``/results/conversation_history/``
of the user (the ``read_file`` backend). Plan, card, resolved terms and turn summaries live outside ``messages``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from deepagents.backends import CompositeBackend
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, get_buffer_string
from langchain_core.messages.utils import count_tokens_approximately

from aigw_service.api.v2.prompts.middleware import SUMMARY_MESSAGE, SUMMARY_MESSAGE_NO_PATH, SUMMARY_PROMPT
from aigw_service.config import APP_CONFIG

CHARS_PER_TOKEN = 3.0
"""Cyrillic splits finer than English in BPE tokenizers; calibrated on GigaChat in S5.1 (design D3)."""

SUMMARY_SOURCE = "summarization"
"""``additional_kwargs["lc_source"]`` of the summary message (deepagents uses it to chain summaries)."""


def context_tokens(messages: Sequence[Any], tools: Sequence[Any] | None = None) -> int:
    """Approximate request size in tokens: one counter for compression and for ``ContextMeter`` (design D3)."""
    return count_tokens_approximately(messages, chars_per_token=CHARS_PER_TOKEN, tools=list(tools) if tools else None)


@dataclass(frozen=True)
class ContextBudget:
    """``AGENT_MAX_PROMPT_TOKENS`` and the shares of it that trigger compression and stay verbatim."""

    max_prompt_tokens: int
    trigger: float
    keep: float

    @classmethod
    def from_settings(cls) -> "ContextBudget":
        settings = APP_CONFIG.agent
        return cls(settings.max_prompt_tokens, settings.summary_trigger, settings.summary_keep)

    @property
    def trigger_tokens(self) -> int:
        return int(self.max_prompt_tokens * self.trigger)

    @property
    def keep_tokens(self) -> int:
        return int(self.max_prompt_tokens * self.keep)


class CopilotSummarization(SummarizationMiddleware):
    """The summary message in Russian (design D2); the rest is deepagents' own."""

    def _build_new_messages_with_path(self, summary: str, file_path: str | None) -> list[AnyMessage]:
        text = (
            SUMMARY_MESSAGE.format(path=file_path, summary=summary)
            if file_path
            else SUMMARY_MESSAGE_NO_PATH.format(summary=summary)
        )
        return [HumanMessage(content=text, additional_kwargs={"lc_source": SUMMARY_SOURCE})]


def summarizer(model: BaseChatModel) -> Any:
    """One model call per summary, no retry: the built-in ``with_retry`` would repeat a StopEvent (design D6)."""

    async def summarize(messages: list[AnyMessage]) -> str:
        prompt = SUMMARY_PROMPT.format(messages=get_buffer_string(messages, format="xml"))
        response = await model.ainvoke([HumanMessage(prompt)])
        return response.text.strip()

    return summarize


def summarization_middleware(
    model: BaseChatModel, backend: CompositeBackend, budget: ContextBudget
) -> CopilotSummarization:
    """Spec §8 №7: compress at ``trigger`` of the budget, keep ``keep`` of it verbatim (design D1)."""
    return CopilotSummarization(
        model,
        backend=backend,
        trigger=("tokens", budget.trigger_tokens),
        keep=("tokens", budget.keep_tokens),
        token_counter=context_tokens,
        trim_tokens_to_summarize=budget.max_prompt_tokens,
        summarizer=summarizer(model),
    )
