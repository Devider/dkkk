"""resolve_terms (FR-6, ADR-4): user terms → index candidates + confidence, over the unchanged semantic layer.

Decisions of spike S0.3: the analyzer's choice comes first; Jaccard top-2 other names follow; confidence is
``high`` only when the analyzer's choice is among the Jaccard top-3 for the term (97.7 % precision vs 69.1 %).
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal

from gigachat.exceptions import ForbiddenError, RateLimitError
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from aigw_service.api.v2.tools.adapter import ToolSpec
from aigw_service.api.v2.tools.context import ModelIndex, ToolContext
from aigw_service.api.v2.tools.envelope import ErrorCode, ToolFailure, ToolSuccess, failure
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX
from aigw_service.exceptions import StopEventError
from excel_engine.matching import jaccard_similarity, normalize_text

logger = APP_CTX.get_logger()

Role = Literal["input", "output"]
TOP_K = 3
MAX_CANDIDATES = 3
LOW_CONFIDENCE_HINT = "Сверь кандидатов с запросом по смыслу или переформулируй термин и вызови resolve_terms снова."


class Term(BaseModel):
    """A term from the user's request."""

    text: str = Field(description="Термин как в запросе, например «цена меди» или «EBITDA»")
    role: Role = Field(description="input — параметр модели (вход), output — показатель модели (выход)")


class ResolveTermsArgs(BaseModel):
    """Arguments of resolve_terms."""

    terms: list[Term] = Field(min_length=1, description="Все термины запроса, которые нужно найти в модели")


@dataclass(frozen=True)
class Pick:
    """One choice of the analyzer: the term as the analyzer echoed it and the index ID it chose."""

    role: Role
    mentioned: str
    item_id: str | None


Analyzer = Callable[[str, ModelIndex], Awaitable[list[Pick]]]


# ---------------------------------------------------------------- semantic layer (unchanged v1 analyzer)


@cache
def _ema_analyzer() -> Any:
    """The v1 scenario analyzer, built from the semantic-layer modules (not from main_graph, spec §7 rule 5)."""
    # pylint: disable=import-outside-toplevel
    from aigw_service.api.v1.prompts.ema_analizer import EMA_ANALIZER_PROMPT
    from aigw_service.api.v1.prompts.lookup_more import LOOKUP_MORE_PROMPT
    from aigw_service.api.v1.schemas.llm_outputs import QueryAnalysisEMA
    from aigw_service.api.v1.states.agents import (
        ExcelModelAnalizerInput,
        ExcelModelAnalizerOutput,
        ExcelModelAnalizerState,
    )
    from aigw_service.api.v1.subagents.analyzer import AnalyzerSubAgent

    return AnalyzerSubAgent(
        state_schema=ExcelModelAnalizerState,
        input_schema=ExcelModelAnalizerInput,
        output_schema=ExcelModelAnalizerOutput,
        llm_output_schema=QueryAnalysisEMA,
        name="resolve_terms",
        analyze_prompt_template=EMA_ANALIZER_PROMPT,
        lookup_prompt_template=LOOKUP_MORE_PROMPT,
    )


def _id_by_name(names: Mapping[str, str], name: str | None) -> str | None:
    wanted = (name or "").strip()
    return next((item_id for item_id, item_name in names.items() if item_name.strip() == wanted), None)


async def analyze_with_v1(message: str, index: ModelIndex) -> list[Pick]:
    """Run the analyzer; IDs come from the canonical names (v1 keeps a stale ID after ``lookup_more``)."""
    response = await _ema_analyzer().ainvoke([HumanMessage(message)], dict(index.inputs), dict(index.outputs))
    analysis = response.get("q_analysis")
    picks = [
        Pick("input", item.mentioned_input_name, _id_by_name(index.inputs, item.equivalent_input_name))
        for item in response.get("resolved_inputs") or []
    ]
    picks += [
        Pick("output", item.mentioned_output_name, _id_by_name(index.outputs, item.equivalent_output_name))
        for item in (analysis.mentioned_outputs if analysis else [])
    ]
    return picks


# ---------------------------------------------------------------- resilience (design D3)


def _is_fatal(exc: BaseException) -> bool:
    """GigaChat StopEvent must reach the API as 503; it is never retried or swallowed (AR-Q11)."""
    return isinstance(exc, (StopEventError, ForbiddenError))


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _retry_rate_limit(call: Callable[[], Awaitable[list[Pick]]]) -> list[Pick]:
    attempts, wait = APP_CONFIG.agent.llm_retry_attempts, APP_CONFIG.agent.llm_retry_wait
    for attempt in range(attempts + 1):
        try:
            return await call()
        except RateLimitError:
            if attempt == attempts:
                raise
            await _sleep(wait * 2**attempt)
    raise AssertionError("unreachable")


def _message(terms: list[Term]) -> str:
    inputs = "; ".join(t.text for t in terms if t.role == "input") or "—"
    outputs = "; ".join(t.text for t in terms if t.role == "output") or "—"
    return f"Сопоставь термины с моделью. Входные параметры: {inputs}. Выходные показатели: {outputs}."


async def _picks(terms: list[Term], index: ModelIndex, analyze: Analyzer) -> list[Pick]:
    """All terms in one call; if the analyzer crashes, one call per term so one bad term does not sink the rest."""
    try:
        return await _retry_rate_limit(lambda: analyze(_message(terms), index))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if _is_fatal(exc):
            raise
        logger.opt(exception=True).warning(
            "resolve_terms: analyzer failed on {} terms, retrying one by one", len(terms)
        )
    picks: list[Pick] = []
    for term in terms:
        try:
            picks += await _retry_rate_limit(lambda term=term: analyze(_message([term]), index))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if _is_fatal(exc):
                raise
            logger.opt(exception=True).warning("resolve_terms: analyzer failed on term {!r}", term.text)
    return picks


# ---------------------------------------------------------------- candidates and confidence (design D2)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def assign(terms: list[Term], picks: list[Pick]) -> list[str | None]:
    """Analyzer choice per term: by echoed name first, then in order among the unused picks of the same role."""
    chosen: list[str | None] = [None] * len(terms)
    unused = list(picks)
    for i, term in enumerate(terms):
        match = next((p for p in unused if p.role == term.role and _norm(p.mentioned) == _norm(term.text)), None)
        if match:
            chosen[i] = match.item_id
            unused.remove(match)
    for i, term in enumerate(terms):
        if chosen[i] is None:
            match = next((p for p in unused if p.role == term.role), None)
            if match:
                chosen[i] = match.item_id
                unused.remove(match)
    return chosen


def jaccard_top(text: str, names: Mapping[str, str], k: int = TOP_K) -> list[tuple[str, str, float]]:
    """(ID, name, score) of the k index names lexically closest to the term."""
    query = normalize_text(text)
    scored = [(item_id, name, jaccard_similarity(query, normalize_text(name))) for item_id, name in names.items()]
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored[:k]


def term_result(term: Term, analyzer_id: str | None, index: ModelIndex) -> dict[str, Any]:
    names = index.inputs if term.role == "input" else index.outputs
    if analyzer_id not in names:
        analyzer_id = None  # wrong role or unknown ID: not a usable choice
    top = jaccard_top(term.text, names)
    query = normalize_text(term.text)
    candidates = []
    if analyzer_id:
        candidates.append(
            (analyzer_id, names[analyzer_id], jaccard_similarity(query, normalize_text(names[analyzer_id])))
        )
    candidates += [c for c in top if c[0] != analyzer_id and c[2] > 0][: MAX_CANDIDATES - len(candidates)]
    if not candidates:
        return {"text": term.text, "role": term.role, "status": ErrorCode.TERM_NOT_FOUND.value}
    # agreement with a zero-similarity name is a tie, not evidence
    confidence = "high" if analyzer_id and analyzer_id in {c[0] for c in top if c[2] > 0} else "low"
    return {
        "text": term.text,
        "role": term.role,
        "status": "ok",
        "confidence": confidence,
        "candidates": [{"id": i, "name": n, "score": round(s, 2)} for i, n, s in candidates],
    }


def envelope(results: list[dict[str, Any]]) -> ToolSuccess | ToolFailure:
    found = [r for r in results if r["status"] == "ok"]
    if not found:
        return failure(ErrorCode.TERM_NOT_FOUND, "Ни один термин не найден среди показателей модели")
    low = [r["text"] for r in found if r["confidence"] == "low"]
    missing = [r["text"] for r in results if r["status"] != "ok"]
    summary = f"Сопоставлено {len(found)} из {len(results)} терминов."
    hints = []
    if low:
        summary += " С низкой уверенностью: " + "; ".join(low) + "."
        hints.append(LOW_CONFIDENCE_HINT)
    if missing:
        summary += " Не найдено: " + "; ".join(missing) + "."
        hints.append("Для ненайденных: " + failure(ErrorCode.TERM_NOT_FOUND).hint)
    resolved = {
        r["text"]: {"id": r["candidates"][0]["id"], "name": r["candidates"][0]["name"], "confidence": r["confidence"]}
        for r in found
    }
    return ToolSuccess(
        result=summary,
        data=results,
        hint=" ".join(hints) or None,
        state_update={"resolved_terms": resolved},
    )


async def resolve_terms(
    args: ResolveTermsArgs, ctx: ToolContext, analyze: Analyzer | None = None
) -> ToolSuccess | ToolFailure:
    index = ctx.model_index
    picks = await _picks(args.terms, index, analyze or analyze_with_v1)
    chosen = assign(args.terms, picks)
    return envelope([term_result(t, c, index) for t, c in zip(args.terms, chosen, strict=True)])


RESOLVE_TERMS = ToolSpec(
    name="resolve_terms",
    description=(
        "Находит показатели модели для терминов запроса и возвращает их ID с уверенностью. "
        "Вызывай до любого расчёта сразу по всем терминам плана; расчётные инструменты принимают только эти ID."
    ),
    args_schema=ResolveTermsArgs,
    acall=resolve_terms,
)
