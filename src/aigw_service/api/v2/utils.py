"""
Здесь можно разместить функции, не связанные с бизнес-логикой, например, нормализация ответов, обогащение данных и т.д.
"""

import uuid

from fastapi import Header


# =====================================================================================================================
# ЗАГОЛОВКИ ЗАПРОСА В AIGATEWAY, СООТВЕТСТВУЮЩИЕ ТРЕБОВАНИЯМ GENAI READY API И AEF
# =====================================================================================================================
# pylint: disable=C0103
def common_headers(
    # Заголовки AEF
    header_x_trace_id: str = Header(
        ...,
        alias="x-trace-id",
        description=(
            "Уникальный идентификатор экземпляра процесса (основной операции). Сквозной ID всей цепочки вызовов."
        ),
        max_length=36,
        pattern=r"^([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})?$",
        example=uuid.uuid4(),
    ),
    header_x_client_id: str = Header(
        default="",
        alias="x-client-id",
        description="КЭ системы отправляющей запрос.",
        max_length=10,
        pattern=r"^[A-Z]{2}\d{8}$",
        example="CI00163870",
    ),
    header_x_session_id: str = Header(
        default="",
        alias="x-session-id",
        description="ID пользовательской сессии. Можно использовать для сессий внутри AIGW (например, для БД).",
        max_length=36,
        pattern=r"^([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})?$",
        example=uuid.uuid4(),
    ),
    # Заголовки AIGW
    header_x_request_time: str = Header(
        ...,
        alias="x-request-time",
        description="Время отправки запроса в формате RFC-3339 (ISO 8601).",
        max_length=32,
        pattern=(
            r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"  # Дата: YYYY-MM-DD
            r"[Tt]"  # Разделитель "T" или "t"
            r"([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9])"  # Время: HH:MM:SS
            r"(?:\.\d+)?"  # Опциональные миллисекунды
            r"(?:([Zz])|([+-](?:[01][0-9]|2[0-3]):[0-5][0-9]))$"  # Таймзона: Z или ±HH:MM
        ),
        example="2025-04-08T11:31:45.748539+03:00",
    ),
    header_x_user_id: str = Header(
        default="",
        alias="x-user-id",
        description="ID пользователя.",
        max_length=8,
        example="12345678",
    ),
) -> dict:
    """Возвращает заголовки запроса, необходимые для работы сервиса на платформе AIGateWay"""
    return {
        "x-trace-id": header_x_trace_id,
        "x-client-id": header_x_client_id,
        "x-session-id": header_x_session_id,
        "x-request-time": header_x_request_time,
        "x-user-id": header_x_user_id,
    }
