"""Answer checks shared by tests and eval (FR-12 structure, FR-14 hygiene); deterministic, no LLM.

``structure_issues`` applies to an analytic answer (one with at least one section heading, ``is_analytic``);
``hygiene_issues`` applies to every answer. Each returns human-readable violations, empty when the answer passes.
"""

import re

from aigw_service.api.v2.tools.envelope import ErrorCode

SECTIONS = ("Вывод", "Ключевые цифры", "Анализ", "Где в модели", "Оговорки")
MAX_CONCLUSION_SENTENCES = 3
MIN_CYRILLIC_SHARE = 0.6
SERVICE_TOOLS = (
    "resolve_terms",
    "run_scenarios",
    "find_input_for_target",
    "derive",
    "trace_dependencies",
    "set_inputs_and_recalc",
    "write_todos",
    "read_file",
)

_HEADING = re.compile(r"^\s*(?:#{1,3}\s*(?P<h>[^#\n]+?)|\*\*(?P<b>[^*\n]+?)\*\*)\s*:?\s*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<!\d)[.!?…]+(?=\s|$)|(?<=\d)[.!?…]+(?=\s+[А-ЯЁA-Z]|\s*$)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", re.MULTILINE)
_INDEX_ID = re.compile(r"\b(?:IN|OUT)\d+\b")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)|File \"[^\"]+\", line \d+")
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def _sections(text: str) -> list[tuple[str, int, int]]:
    """Known section headings in order of appearance: ``(name, heading_start, body_start)``."""
    found = []
    for match in _HEADING.finditer(text):
        title = (match.group("h") or match.group("b") or "").strip().rstrip(":").strip()
        name = next((s for s in SECTIONS if s.lower() == title.lower()), None)
        if name:
            found.append((name, match.start(), match.end()))
    return found


def is_analytic(text: str) -> bool:
    return bool(_sections(text))


def _sentences(text: str) -> int:
    plain = re.sub(r"\s+", " ", text).strip()
    if not plain:
        return 0
    parts = [p for p in _SENTENCE_END.split(plain) if p and p.strip(" .!?…")]
    return len(parts)


def structure_issues(text: str) -> list[str]:
    """FR-12: «Вывод» first, sections in order and once each, 1–3 sentence conclusion, a table in the numbers."""
    found = _sections(text)
    names = [name for name, _, _ in found]
    issues: list[str] = []
    if "Вывод" not in names:
        issues.append("нет раздела «Вывод»")
    elif names[0] != "Вывод":
        issues.append("раздел «Вывод» не первый")
    duplicates = sorted({n for n in names if names.count(n) > 1}, key=SECTIONS.index)
    issues.extend(f"раздел «{n}» повторяется" for n in duplicates)
    order = [SECTIONS.index(n) for n in dict.fromkeys(names)]
    if order != sorted(order):
        issues.append("разделы не в порядке: " + ", ".join(dict.fromkeys(names)))
    bounds = [*(start for _, start, _ in found[1:]), len(text)][: len(found)]
    for (name, _, body_start), end in zip(found, bounds, strict=True):
        body = text[body_start:end].strip()
        if not body:
            issues.append(f"раздел «{name}» пустой")
        elif name == "Вывод" and _sentences(body) > MAX_CONCLUSION_SENTENCES:
            issues.append(f"«Вывод» длиннее {MAX_CONCLUSION_SENTENCES} предложений")
        elif name == "Ключевые цифры" and not (_TABLE_ROW.search(body) and _TABLE_RULE.search(body)):
            issues.append("в «Ключевых цифрах» нет таблицы")
    return issues


def hygiene_issues(text: str) -> list[str]:
    """FR-14: Russian; no error codes, tracebacks, index IDs or tool names."""
    issues: list[str] = []
    letters = [c for c in text if c.isalpha()]
    if letters and sum(bool(_CYRILLIC.match(c)) for c in letters) / len(letters) < MIN_CYRILLIC_SHARE:
        issues.append("ответ не на русском")
    issues.extend(f"код ошибки {c.value}" for c in ErrorCode if re.search(rf"\b{c.value}\b", text))
    if _TRACEBACK.search(text):
        issues.append("traceback")
    ids = sorted(set(_INDEX_ID.findall(text)))
    if ids:
        issues.append("ID индекса: " + ", ".join(ids))
    issues.extend(f"имя инструмента {t}" for t in SERVICE_TOOLS if re.search(rf"\b{t}\b", text))
    return issues
