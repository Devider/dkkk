"""
Инфраструктурные исключения.
"""

from datetime import UTC, datetime


class StopEventError(Exception):
    """ТН08: GigaChat вернул HTTP 403 с сообщением о временной недоступности.

    Данное исключение генерируется в инфраструктурном слое (context.py) при
    обнаружении стоп-сигнала от GigaChat. Оно должно беспрепятственно
    распространяться вверх по стеку до HTTP-границы (routers.py), где
    преобразуется в понятный ответ пользователю.

    Недопустимо перехватывать это исключение через ``except Exception`` в
    бизнес-логике — только явный ``except StopEventError`` для пропуска (re-raise).
    """

    def __init__(
        self,
        user_message: str,
        url: str,
        status_code: int = 403,
        reason: str = "",
    ):
        self.user_message = user_message
        self.url = url
        self.status_code = status_code
        self.reason = reason
        self.timestamp = datetime.now(UTC).isoformat()
        super().__init__(user_message)
