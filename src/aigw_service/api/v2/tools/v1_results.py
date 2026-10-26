"""v1 calculation statuses (OK / WARNING / ERROR + free text) → error codes, without touching the domain (FR-7)."""

from aigw_service.api.v2.tools.envelope import ErrorCode, ToolFailure, failure

UNREACHED_NOTE = "Цель не достигнута ни в одном проверенном сценарии"

_MISSING_INPUT = ("не указаны входные параметры", "не удалось определить год")
_TOO_WIDE = ("диапазон слишком широкий",)
_NO_VALUES = ("не удалось найти ячейку", "ошибка при поиске выходного параметра")
_OUTPUT_NOT_FOUND = "выходной параметр"
_FILE_MISSING = "файл "
_ENGINE = ("urp bridge", "disposedexception", "soffice", "pyuno", "com.sun.star", "connection refused")

# Domain text is shown to the model only for codes whose text never carries exception details
_SAFE_DOMAIN_TEXT = {ErrorCode.MISSING_INPUT, ErrorCode.NO_VALUES, ErrorCode.INVALID_ARGS}
_HINTS = {
    ErrorCode.INVALID_ARGS: "Сетка слишком большая: сократи число входов, сузь диапазон или увеличь шаг и повтори.",
}


def is_unreached(status: str, text: str) -> bool:
    """IFT found no scenario hitting the target: a result of the analysis, not an error (FR-11)."""
    return status == "WARNING" and "не найдено сценариев" in text.lower()


def classify_v1(status: str, text: str) -> ToolFailure | None:
    """``None`` when the v1 result is a success (including an unreached target), else the coded failure."""
    if status == "OK" or is_unreached(status, text):
        return None
    code = _code_for(text.lower())
    message = _first_line(text) if code in _SAFE_DOMAIN_TEXT else None
    return failure(code, message, _HINTS.get(code))


def _code_for(text: str) -> ErrorCode:
    if any(m in text for m in _MISSING_INPUT):
        return ErrorCode.MISSING_INPUT
    if any(m in text for m in _TOO_WIDE):
        return ErrorCode.INVALID_ARGS
    if any(m in text for m in _ENGINE):
        return ErrorCode.ENGINE_UNAVAILABLE
    if text.startswith(_FILE_MISSING) and "не найден" in text:
        return ErrorCode.INTERNAL_ERROR  # TurnContext guarantees the file; the model cannot fix this
    if text.startswith(_NO_VALUES) or (text.startswith(_OUTPUT_NOT_FOUND) and "не найден" in text):
        return ErrorCode.NO_VALUES
    return ErrorCode.INTERNAL_ERROR


def _first_line(text: str) -> str:
    """The sentence the domain led with, without a ``": <exception text>"`` tail (FR-14)."""
    line = text.strip().splitlines()[0]
    if line.lower().startswith(_NO_VALUES) and '": ' in line:
        return line.split('": ', 1)[0] + '"'
    if line.lower().startswith(_NO_VALUES[1]) and ": " in line:
        return line.split(": ", 1)[0]
    return line
