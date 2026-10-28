"""User-facing texts of the stream (spec §13 «Подписи и сводки», S6.2 design D6–D8, S6.4 design D4–D5).

Labels are built from call arguments and canonical index names only — never from IDs. Error codes become an
outcome and a Russian text for the user (not ``ERROR_TEXT``: that one addresses the model). Text written by
the model or the user (step titles, reasons, terms) goes through ``scrub`` before it reaches an event (FR-24).
"""

import re
from collections.abc import Iterable, Mapping
from functools import cache
from typing import Any

from aigw_service.api.v2.stream.events import Phase, ToolOutcome
from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.envelope import ErrorCode

PLAN_TOOL = "write_todos"
READ_TOOL = "read_file"
MAX_TERMS = 5

PHASE_TEXT: dict[Phase, str] = {
    "planning": "Составляю план",
    "executing": "Выполняю план",
    "checking": "Проверяю ответ",
    "answering": "Готовлю ответ",
}

FAILURE_VIEW: dict[ErrorCode, tuple[ToolOutcome, str]] = {
    ErrorCode.INVALID_ARGS: ("retry", "Уточняю параметры расчёта"),
    ErrorCode.UNKNOWN_ID: ("retry", "Уточняю показатели"),
    ErrorCode.AMBIGUOUS_TERM: ("retry", "Показатель назван неоднозначно — выбираю подходящий"),
    ErrorCode.TERM_NOT_FOUND: ("retry", "Показатель не найден — ищу под другим названием"),
    ErrorCode.NO_VALUES: ("retry", "У показателя нет значений — беру другой"),
    ErrorCode.BAD_UNITS: ("retry", "Сверяю единицы диапазона"),
    ErrorCode.ENGINE_TIMEOUT: ("retry", "Расчёт слишком долгий — сужаю сетку"),
    ErrorCode.PLAN_REQUIRED: ("retry", "Сначала составляю план"),
    ErrorCode.SKILL_REQUIRED: ("retry", "Сначала изучаю методику"),
    # in a ToolMessage the system retry is already used up (ToolRetryMiddleware sits inside ToolErrorMiddleware)
    ErrorCode.ENGINE_UNAVAILABLE: ("failed", "Движок пересчёта недоступен — пропускаю шаг"),
    ErrorCode.MISSING_INPUT: ("failed", "Не хватает данных — задам уточняющий вопрос"),
    ErrorCode.LIMIT_EXCEEDED: ("failed", "Лимит хода исчерпан — готовлю ответ по полученным результатам"),
    ErrorCode.CANCELLED: ("failed", "Ход остановлен — готовлю ответ по полученным результатам"),
    ErrorCode.INTERNAL_ERROR: ("failed", "Шаг не удался — пропускаю его"),
}
UNKNOWN_FAILURE: tuple[ToolOutcome, str] = FAILURE_VIEW[ErrorCode.INTERNAL_ERROR]

RETRY_TEXT: dict[ErrorCode, str] = {ErrorCode.ENGINE_UNAVAILABLE: "Движок пересчёта не ответил — повторяю"}
"""Texts for a retry the system makes (a ``custom`` part of ``ProgressReporter``, S6.4)."""
DEFAULT_RETRY_TEXT = "Повторяю попытку"

DERIVE_TEXT = {
    "difference": "разницу",
    "pct_change": "изменение в процентах",
    "headroom": "запас до порога",
    "crossing": "точку пересечения порога",
    "elasticity": "эластичность",
}

ID_PATTERN = re.compile(r"\b(?:IN|OUT)\d+\b")
PATH_PATTERN = re.compile(r"/(?:results|skills)/\S*")


def failure_view(code: Any) -> tuple[ToolOutcome, str]:
    """Outcome and user text of an error envelope; an unknown code reads as an unexpected failure."""
    try:
        return FAILURE_VIEW[ErrorCode(code)]
    except ValueError:
        return UNKNOWN_FAILURE


def retry_text(code: Any) -> str:
    try:
        return RETRY_TEXT.get(ErrorCode(code), DEFAULT_RETRY_TEXT)
    except ValueError:
        return DEFAULT_RETRY_TEXT


# ---------------------------------------------------------------- scrub (FR-24)


@cache
def tool_names() -> frozenset[str]:
    """Every tool name the model can call: the catalogue plus the deepagents / langchain built-ins."""
    from aigw_service.api.v2.tools.catalog import default_catalog  # pylint: disable=import-outside-toplevel

    catalog = default_catalog()
    names = {t.name for t in [*catalog.base_tools(), *catalog.skill_tools()]}
    return frozenset(names | {PLAN_TOOL, READ_TOOL})


def error_codes() -> frozenset[str]:
    """Codes that cannot be ordinary words (``cancelled`` can, so the model's text keeps it)."""
    return frozenset(c.value for c in ErrorCode if "_" in c.value)


@cache
def _word_pattern() -> re.Pattern[str]:
    words = sorted(tool_names() | error_codes(), key=len, reverse=True)
    return re.compile(r"(?<![\w/])(?:" + "|".join(map(re.escape, words)) + r")(?!\w)")


def scrub(text: str) -> str:
    """Drops index IDs, tool names, error codes and ``/results/`` / ``/skills/`` paths from free text."""
    text = PATH_PATTERN.sub("", str(text))
    text = ID_PATTERN.sub("", text)
    text = _word_pattern().sub("", text)
    text = re.sub(r"\(\s*\)|\[\s*\]|«\s*»|`\s*`", "", text)
    text = re.sub(r"\s+([,.;:!?)])", r"\1", text)
    text = re.sub(r"([(«])\s+", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return re.sub(r"^[,.;:—-]+\s*|\s*[,;:—-]+$", "", text).strip()


# ---------------------------------------------------------------- summaries (FR-23)

SUMMARY_MAX = 300
_SENTENCE_FROM = SUMMARY_MAX // 2
"""A sentence boundary is preferred only when it keeps at least half of the room."""
ELLIPSIS = "…"
_TRAILING = re.compile(r"[\s,;:—-]+$")
_NUMBER_TAIL = re.compile(r"[\d\s.,]+$")


def _splits_number(text: str, i: int) -> bool:
    """A space between two digits groups thousands (``1 234 567``): cutting there would cut a number."""
    return 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()


def _shorten(text: str, limit: int) -> str:
    """At most ``limit`` characters, cut at a sentence or word boundary, never inside a number."""
    if len(text) <= limit:
        return text
    room = limit - len(ELLIPSIS)
    head = text[:room]
    sentence = max(head.rfind(". ", _SENTENCE_FROM), head.rfind("; ", _SENTENCE_FROM))
    if sentence != -1:
        return text[:sentence].rstrip() + ELLIPSIS
    cut = next((i for i in range(room, 0, -1) if text[i].isspace() and not _splits_number(text, i)), None)
    kept = text[:cut] if cut is not None else _NUMBER_TAIL.sub("", head)
    return _TRAILING.sub("", kept) + ELLIPSIS


def step_summary(envelope: Mapping[str, Any] | None) -> str | None:
    """The step summary of ``tool.finished`` (FR-23): ``result`` of a successful envelope, scrubbed and cut."""
    if not isinstance(envelope, Mapping) or envelope.get("status") != "ok":
        return None
    result = envelope.get("result")
    text = scrub(result) if isinstance(result, str) else ""
    return _shorten(text, SUMMARY_MAX) if text else None


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def turn_summary(steps: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The ``summary`` event before ``answer`` (FR-23); counts only, no step reasons. ``None`` without a plan."""
    if not steps:
        return None
    count = {s: sum(1 for step in steps if step.get("status") == s) for s in ("completed", "skipped", "blocked")}
    total = len(steps)
    parts = [f"Выполнено {count['completed']} из {total} {_plural(total, 'шага', 'шагов', 'шагов')}"]
    if count["skipped"]:
        parts.append(f"{count['skipped']} {_plural(count['skipped'], 'пропущен', 'пропущено', 'пропущено')}")
    if count["blocked"]:
        parts.append(f"{count['blocked']} {_plural(count['blocked'], 'ждёт', 'ждут', 'ждут')} ответа пользователя")
    return {"event": "summary", **count, "text": ", ".join(parts)}


# ---------------------------------------------------------------- labels


def _num(value: Any) -> str:
    """``300.0`` → ``300``, ``0.15`` → ``0.15``; anything else as is."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    return f"{value:g}" if abs(value) < 1e15 else str(value)


def _names(ids: Iterable[Any], names: Mapping[str, str]) -> list[str]:
    """Canonical names of the IDs that are in the index; unknown IDs are skipped, never shown."""
    return [names[i] for i in ids if isinstance(i, str) and i in names]


def _inputs(index: ModelIndex | None) -> Mapping[str, str]:
    return index.inputs if index is not None else {}


def _outputs(index: ModelIndex | None) -> Mapping[str, str]:
    return index.outputs if index is not None else {}


def _resolve_terms(args: Mapping[str, Any], _index: ModelIndex | None) -> str:
    terms = [scrub(t.get("text", "")) for t in args.get("terms") or [] if isinstance(t, Mapping)]
    terms = [t for t in terms if t]
    if not terms:
        return "Сопоставляю показатели"
    listed = ", ".join(terms[:MAX_TERMS]) + (", …" if len(terms) > MAX_TERMS else "")
    return f"Сопоставляю показатели: {listed}"


def _range(item: Mapping[str, Any], name: str) -> str:
    low, high = item.get("min"), item.get("max")
    if low is None and high is None:
        return name
    if low is None or high is None or low == high:
        return f"{name} = {_num(high if low is None else low)}"
    return f"{name} от {_num(low)} до {_num(high)}"


def _run_scenarios(args: Mapping[str, Any], index: ModelIndex | None) -> str:
    outputs = _names(args.get("outputs") or [], _outputs(index))
    inputs = [
        _range(item, _inputs(index)[item["id"]])
        for item in args.get("inputs") or []
        if isinstance(item, Mapping) and item.get("id") in _inputs(index)
    ]
    if not outputs and not inputs:
        return "Считаю сценарии"
    label = "Считаю сценарии: " + (", ".join(outputs) or "показатели")
    return f"{label} при {', '.join(inputs)}" if inputs else label


def _find_input_for_target(args: Mapping[str, Any], index: ModelIndex | None) -> str:
    inputs = ", ".join(_names(args.get("input_ids") or [], _inputs(index)))
    output = _outputs(index).get(args.get("output_id") or "")
    label = f"Подбираю {inputs}" if inputs else "Подбираю значения входов"
    if output:
        label += f" под {output}"
        if args.get("target") is not None:
            label += f" = {_num(args['target'])}"
    if args.get("year") is not None:
        label += f" в {args['year']}"
    return label


def _derive(args: Mapping[str, Any], _index: ModelIndex | None) -> str:
    return f"Считаю {DERIVE_TEXT.get(args.get('operation') or '', 'показатель')}"


def _trace_dependencies(args: Mapping[str, Any], index: ModelIndex | None) -> str:
    output = _outputs(index).get(args.get("output_id") or "")
    return f"Смотрю, от чего зависит {output}" if output else "Смотрю зависимости показателя"


def _read_file(args: Mapping[str, Any], _index: ModelIndex | None) -> str:
    path = str(args.get("file_path") or args.get("path") or "")
    if path.startswith("/skills/"):
        return "Изучаю методику"
    if path.startswith("/results/"):
        return "Перечитываю прошлый расчёт"
    return "Читаю материалы"


_LABELS = {
    "resolve_terms": _resolve_terms,
    "run_scenarios": _run_scenarios,
    "find_input_for_target": _find_input_for_target,
    "get_values": lambda _a, _i: "Беру базовые значения",
    "describe_model": lambda _a, _i: "Изучаю структуру модели",
    "derive": _derive,
    "trace_dependencies": _trace_dependencies,
    READ_TOOL: _read_file,
}
DEFAULT_LABEL = "Выполняю шаг расчёта"


def tool_label(name: str, args: Mapping[str, Any] | None, index: ModelIndex | None) -> str:
    """Russian label of a tool call (spec §13 table «инструмент → шаблон»)."""
    build = _LABELS.get(name)
    if build is None:
        return DEFAULT_LABEL
    try:
        return build(args if isinstance(args, Mapping) else {}, index)
    except (AttributeError, KeyError, TypeError):  # malformed arguments the adapter will reject anyway
        return DEFAULT_LABEL
