"""LanguageGuard (FR-2, ADR-11, spec §8 №2): Russian or English only, decided without an LLM.

A deterministic check on letters, diacritics and stopwords; refuses only when another language is recognised
with confidence, so short or ambiguous requests go through.
"""

import re
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

LANGUAGE_REFUSAL = (
    "Я отвечаю на вопросы на русском или английском языке. Пожалуйста, задайте вопрос по-русски или по-английски."
)

Language = Literal["ru", "en", "other", "unknown"]

_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_NON_RUSSIAN_CYRILLIC = re.compile(r"[іїєґўәғқңөұүһ]", re.IGNORECASE)
_LATIN = re.compile(r"[a-zà-ÿœæ]", re.IGNORECASE)
_FOREIGN_DIACRITICS = re.compile(r"[äöüßàâçéèêëîïôûùÿñœæãõáíóú]", re.IGNORECASE)
_WORD = re.compile(r"[a-zà-ÿœæ]+", re.IGNORECASE)

_EN = {
    "the",
    "and",
    "of",
    "to",
    "is",
    "what",
    "how",
    "at",
    "for",
    "in",
    "if",
    "with",
    "on",
    "show",
    "calculate",
    "which",
}
_OTHER = {
    "der", "die", "das", "und", "ist", "nicht", "mit", "wie", "bei", "für", "ein", "eine", "berechne", "zeige",  # de
    "le", "la", "les", "et", "est", "des", "du", "une", "avec", "pour", "quelle", "calculez", "montre",  # fr
    "el", "los", "las", "y", "es", "del", "con", "para", "una", "cuál", "calcula", "muestra",  # es
    "il", "di", "che", "per", "quale", "calcola", "mostra",  # it
    "o", "os", "um", "uma", "não", "qual", "mostre",  # pt
}  # fmt: skip


def detect_language(text: str) -> Language:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return "unknown"
    return _by_script(text, letters) or _latin_language(text)


def _by_script(text: str, letters: list[str]) -> Language | None:
    """Decide by alphabet when it is conclusive: non-Latin/Cyrillic scripts, non-Russian Cyrillic, Russian."""
    cyrillic = sum(bool(_CYRILLIC.match(c)) for c in letters)
    latin = sum(bool(_LATIN.match(c)) for c in letters)
    if (len(letters) - cyrillic - latin) / len(letters) > 0.3:
        return "other"  # CJK, Arabic, Greek, ...
    if _NON_RUSSIAN_CYRILLIC.search(text):
        return "other"  # Ukrainian, Belarusian, Kazakh, ...
    return "ru" if cyrillic / len(letters) >= 0.3 else None


def _latin_language(text: str) -> Language:
    """English vs other Latin-script languages by stopwords and diacritics."""
    words = [w.lower() for w in _WORD.findall(text)]
    english = sum(w in _EN for w in words)
    foreign = sum(w in _OTHER for w in words)
    if english < 2 and (foreign >= 2 or _FOREIGN_DIACRITICS.search(text)):
        return "other"
    return "en" if english >= 1 or len(words) >= 3 else "unknown"


class LanguageGuard(AgentMiddleware):
    """Ends the turn with a fixed refusal, before any model or tool call, for a non-ru/en request."""

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        last = next((m for m in reversed(state.get("messages") or []) if isinstance(m, HumanMessage)), None)
        if last is not None and detect_language(str(last.content)) == "other":
            return {"messages": [AIMessage(LANGUAGE_REFUSAL)], "jump_to": "end"}
        return None
