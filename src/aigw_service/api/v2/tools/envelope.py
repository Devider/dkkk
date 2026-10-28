"""One result format for every tool (spec §10 «Контракт» п. 3, 4) and the error-code catalogue (FR-7)."""

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from aigw_service.context import APP_CTX

logger = APP_CTX.get_logger()


class ErrorCode(StrEnum):
    """Spec §10 «Справочник кодов ошибок»: the code tells the model whether to retry, re-plan or ask."""

    INVALID_ARGS = "invalid_args"
    UNKNOWN_ID = "unknown_id"
    AMBIGUOUS_TERM = "ambiguous_term"
    TERM_NOT_FOUND = "term_not_found"
    NO_VALUES = "no_values"
    BAD_UNITS = "bad_units"
    MISSING_INPUT = "missing_input"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    ENGINE_TIMEOUT = "engine_timeout"
    PLAN_REQUIRED = "plan_required"
    SKILL_REQUIRED = "skill_required"
    LIMIT_EXCEEDED = "limit_exceeded"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


TRANSIENT_CODES = frozenset({ErrorCode.ENGINE_UNAVAILABLE})
"""Failures the system fixes by repeating the call (``ToolRetryMiddleware``, AR-Q11); the model never sees a retry."""

ERROR_TEXT: dict[ErrorCode, str] = {
    ErrorCode.INVALID_ARGS: "Аргументы вызова не прошли проверку",
    ErrorCode.UNKNOWN_ID: "ID нет в индексе модели",
    ErrorCode.AMBIGUOUS_TERM: "У термина несколько кандидатов с близкой оценкой",
    ErrorCode.TERM_NOT_FOUND: "Термин не найден среди показателей модели",
    ErrorCode.NO_VALUES: "У показателя нет значений в модели",
    ErrorCode.BAD_UNITS: "Единицы диапазона не совпадают с единицами показателя",
    ErrorCode.MISSING_INPUT: "Не хватает данных для расчёта",
    ErrorCode.ENGINE_UNAVAILABLE: "Движок пересчёта недоступен",
    ErrorCode.ENGINE_TIMEOUT: "Расчёт не уложился в отведённое время",
    ErrorCode.PLAN_REQUIRED: "Расчёт без плана не выполняется",
    ErrorCode.SKILL_REQUIRED: "Инструмент открывается только после чтения своего skill",
    ErrorCode.LIMIT_EXCEEDED: "Исчерпан лимит вызовов",
    ErrorCode.CANCELLED: "Ход прерван",
    ErrorCode.INTERNAL_ERROR: "Непредвиденная ошибка инструмента",
}

DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.INVALID_ARGS: "Исправь аргументы по описанию инструмента и повтори вызов.",
    ErrorCode.UNKNOWN_ID: "Получи ID через resolve_terms и повтори вызов с ним.",
    ErrorCode.AMBIGUOUS_TERM: (
        "Выбери кандидата по смыслу запроса; если нельзя — спроси пользователя, перечислив варианты."
    ),
    ErrorCode.TERM_NOT_FOUND: (
        "Переформулируй термин (синоним, русское название) и вызови resolve_terms снова; "
        "после двух неудач спроси пользователя."
    ),
    ErrorCode.NO_VALUES: "Это заголовок раздела или строка без значений. Возьми следующего кандидата из resolve_terms.",
    ErrorCode.BAD_UNITS: "Сверь единицы с базовым значением (проценты или доли), пересчитай диапазон и повтори.",
    ErrorCode.MISSING_INPUT: "Переведи шаг в blocked и задай пользователю один вопрос ровно о недостающем.",
    ErrorCode.ENGINE_UNAVAILABLE: "Пропусти этот шаг с причиной и доведи остальные шаги плана.",
    ErrorCode.ENGINE_TIMEOUT: "Сузь сетку: меньше входов или крупнее шаг — и повтори.",
    ErrorCode.PLAN_REQUIRED: "Сначала составь план через write_todos.",
    ErrorCode.SKILL_REQUIRED: "Прочитай SKILL.md этого skill через read_file и повтори вызов.",
    ErrorCode.LIMIT_EXCEEDED: "Больше не вызывай инструменты: подготовь частичный ответ по полученным результатам.",
    ErrorCode.CANCELLED: "Подготовь частичный ответ по полученным результатам.",
    ErrorCode.INTERNAL_ERROR: "Пропусти этот шаг с причиной и доведи остальные шаги плана.",
}


class Number(BaseModel):
    """A number the answer may quote; ``AnswerGroundingGuard`` (S4.1) checks answers against these."""

    value: float
    label: str
    unit: str | None = None


class ToolSuccess(BaseModel):
    """Successful tool result."""

    status: Literal["ok"] = "ok"
    result: str = Field(description="Сводка на русском с единицами")
    data: Any = None
    numbers: list[Number] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    hint: str | None = None
    ref: str | None = None
    state_update: dict[str, Any] | None = Field(default=None, exclude=True)
    """Agent-state fields to update with this result; never shown to the model."""


class ToolFailure(BaseModel):
    """Failed tool result: the model reads ``code`` and ``hint`` to correct the plan (FR-7)."""

    status: Literal["error"] = "error"
    error: str
    code: ErrorCode
    hint: str


class ToolError(Exception):
    """A known, coded failure raised by the adapter or a tool wrapper."""

    def __init__(self, code: ErrorCode | str, message: str | None = None, hint: str | None = None) -> None:
        self.code = ErrorCode(code)  # ValueError for a code outside the catalogue: a development error
        self.message = message or ERROR_TEXT[self.code]
        self.hint = hint or DEFAULT_HINTS[self.code]
        super().__init__(f"{self.code}: {self.message}")

    def failure(self) -> ToolFailure:
        return ToolFailure(error=self.message, code=self.code, hint=self.hint)


def failure(code: ErrorCode | str, message: str | None = None, hint: str | None = None) -> ToolFailure:
    return ToolError(code, message, hint).failure()


def failure_from_exception(exc: BaseException) -> ToolFailure:
    """Exception → envelope for ``ToolErrorMiddleware`` (S2.5): the exception text never reaches the model."""
    if isinstance(exc, ToolError):
        return exc.failure()
    logger.opt(exception=exc).error("Tool failed with unexpected {}: {}", type(exc).__name__, str(exc))
    return failure(ErrorCode.INTERNAL_ERROR)


def to_json(envelope: ToolSuccess | ToolFailure) -> str:
    """Compact JSON: no ``null`` fields and no empty lists, to save context (AR-Q6)."""
    payload = {k: v for k, v in envelope.model_dump(mode="json", exclude_none=True).items() if v != []}
    return json.dumps(payload, ensure_ascii=False, default=str)
