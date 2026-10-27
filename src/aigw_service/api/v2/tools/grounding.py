"""Where a number may come from (FR-13): tool results of this turn, past turn summaries and the user's messages.

``derive`` (S1.7) accepts an operand only if it matches a source (``match``, the sign counts). ``AnswerGroundingGuard``
(S4.1) checks the numbers of the answer with ``answer_numbers`` and ``confirm``: the sign is ignored, a percent also
matches a share, and the tolerance follows the digits written in the answer (design S4.1 D2, D3).
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from langchain_core.messages import HumanMessage

RELATIVE_TOLERANCE = 0.005
"""A rounded operand may differ from its source by at most 0.5 % of the source, so ``3`` never matches ``2.6``."""

# Optional sign, digits with optional space-separated thousands, optional decimal part; then an optional percent sign.
# The look-behind keeps a match from starting inside another number.
_NUMBER = re.compile(r"(?<![\d.,])([-−]?)(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,](\d+))?(?:\s?(%))?")


@dataclass(frozen=True)
class Source:
    """A number the agent may rely on, with where it came from."""

    value: float
    label: str
    unit: str | None = None


def numbers_in_text(text: str, label: str) -> list[Source]:
    """Numbers written in a message: ``3,5``, ``1 200``, ``−2.5``, ``15 %`` (unit ``%``), ``3.5x``."""
    found = []
    for sign, whole, fraction, percent in _NUMBER.findall(text):
        digits = re.sub(r"\D", "", whole) + (f".{fraction}" if fraction else "")
        value = float(digits) * (-1 if sign else 1)
        found.append(Source(value, label, "%" if percent else None))
    return found


def _numbers(items: Any, label_prefix: str = "") -> list[Source]:
    sources = []
    for n in items or []:
        try:
            value = float(n["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            sources.append(Source(value, f"{label_prefix}{n.get('label', '')}", n.get("unit")))
    return sources


AGENT_NOTE = "agent_guard"
"""``additional_kwargs["lc_source"]`` of a user-role message the agent itself adds (a guard reminder)."""


def agent_note(text: str) -> HumanMessage:
    """A reminder in the user's role (spike S0.2 C7) that is not the user's input: never a source of numbers."""
    return HumanMessage(text, additional_kwargs={"lc_source": AGENT_NOTE})


def is_agent_note(message: HumanMessage) -> bool:
    """Guard reminders and pinned skill texts (deepagents ``lc_source="pinned_skill"``) are not the user's words."""
    return bool(message.additional_kwargs.get("lc_source"))


def collect_sources(state: Mapping[str, Any]) -> list[Source]:
    """Every number the turn may quote: ``step_results`` (full lists), ``turn_summaries`` and the user's messages."""
    sources: list[Source] = []
    for step in state.get("step_results") or []:
        sources += _numbers(step.get("numbers"))
    for summary in state.get("turn_summaries") or []:
        for result in summary.get("results") or []:
            sources += _numbers(result.get("numbers"), "прошлый ход: ")
    for message in state.get("messages") or []:
        if isinstance(message, HumanMessage) and not is_agent_note(message):
            sources += numbers_in_text(message.text, "сообщение пользователя")
    return sources


def tolerance(value: float, source: float) -> float:
    """Half a unit of the last digit the operand was written with, capped by 0.5 % of the source."""
    if value.is_integer():
        decimals = 0
    else:
        exponent = Decimal(repr(value)).as_tuple().exponent
        decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return min(0.5 * 10.0**-decimals, RELATIVE_TOLERANCE * abs(source))


def match(value: float, sources: list[Source]) -> Source | None:
    """The source nearest to ``value`` among those it rounds from; ``None`` if there is none."""
    fits = [s for s in sources if s.value == value or abs(value - s.value) <= tolerance(value, s.value)]
    return min(fits, key=lambda s: abs(value - s.value), default=None)


# ---------------------------------------------------------------- numbers of the answer (S4.1)

YEARS = (1900, 2100)
SMALL_INTEGER = 10
"""Integers 0–10 without a fraction or ``%`` are counts («за 3 года») and are not checked (design S4.1 D2)."""

_DATE = re.compile(r"(?<![\d.])\d{1,2}\.\d{1,2}\.\d{2,4}(?![\d.])")
_LIST_MARKER = re.compile(r"[.)]\s")
_MULTIPLIERS = "xXхХ"


@dataclass(frozen=True)
class Mention:
    """A number as written in the answer: its place, value, digits after the decimal mark and a ``%`` sign."""

    start: int
    end: int
    written: str
    value: float
    decimals: int
    percent: bool


def _is_identifier(text: str, start: int, end: int) -> bool:
    """``P143``, ``Q4``, ``CO2``, ``FY2025``, ``S1``: a letter right before, or a Latin letter right after."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before.isalpha() or before == "_":
        return True
    return after.isascii() and (after.isalpha() or after == "_") and after not in _MULTIPLIERS


def _is_list_marker(text: str, start: int, end: int) -> bool:
    """``1.`` / ``2)`` opening a line."""
    line_start = text.rfind("\n", 0, start) + 1
    return not text[line_start:start].strip() and bool(_LIST_MARKER.match(text, end))


def _is_exempt(text: str, m: re.Match[str]) -> bool:
    """Years, small integers, identifiers and list numbers (design S4.1 D2); ``m`` is a match of ``_NUMBER``."""
    sign, whole, fraction, percent = m.groups()
    digits_end = m.end(3) if fraction else m.end(2)  # without the ``%`` and the space before it
    if not fraction and not percent and whole.isdigit():
        value = int(whole)
        if not sign and (value <= SMALL_INTEGER or YEARS[0] <= value <= YEARS[1]):
            return True
    return _is_identifier(text, m.start(), digits_end) or _is_list_marker(text, m.start(), digits_end)


def answer_numbers(text: str) -> list[Mention]:
    """Numbers of an answer to check: years, list numbers, identifiers, dates and small integers left out."""
    dates = [d.span() for d in _DATE.finditer(text)]
    found = []
    for m in _NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in dates) or _is_exempt(text, m):
            continue
        sign, whole, fraction, percent = m.groups()
        value = float(re.sub(r"\D", "", whole) + (f".{fraction}" if fraction else "")) * (-1 if sign else 1)
        found.append(Mention(m.start(), m.end(), m.group(0), value, len(fraction or ""), bool(percent)))
    return found


def confirm(mention: Mention, sources: list[Source]) -> Source | None:
    """The nearest source within half a unit of the last written digit (at most 0.5 % of it), sign ignored.

    A number written with ``%`` also matches a share: ``12,5 %`` ↔ ``0.125``.
    """
    unit = 0.5 * 10.0**-mention.decimals
    value = abs(mention.value)
    scales = (1.0, 100.0) if mention.percent else (1.0,)
    fits = [
        (gap, source)
        for source in sources
        for target in (abs(source.value) * scale for scale in scales)
        if (gap := abs(value - target)) == 0 or gap <= min(unit, RELATIVE_TOLERANCE * target)
    ]
    return min(fits, key=lambda fit: fit[0])[1] if fits else None
