"""Where a number may come from (FR-13): tool results of this turn, past turn summaries and the user's messages.

``derive`` (S1.7) accepts an operand only if it matches a source; ``AnswerGroundingGuard`` (S4.1) reuses
``collect_sources`` and ``match`` for the numbers of the answer (design S1.7 D1).
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


def collect_sources(state: Mapping[str, Any]) -> list[Source]:
    """Every number the turn may quote: ``step_results`` (full lists), ``turn_summaries`` and user messages."""
    sources: list[Source] = []
    for step in state.get("step_results") or []:
        sources += _numbers(step.get("numbers"))
    for summary in state.get("turn_summaries") or []:
        for result in summary.get("results") or []:
            sources += _numbers(result.get("numbers"), "прошлый ход: ")
    for message in state.get("messages") or []:
        if isinstance(message, HumanMessage):
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
