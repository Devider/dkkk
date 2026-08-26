import json
from http import HTTPStatus
from typing import ClassVar


class HTTPCodeError(Exception):
    KIND: ClassVar[str] = "HTTP"

    def __init__(self, http_code: int, http_url: str, content: bytes = b""):
        self.details = self.format_details(http_code, http_url, content)
        super().__init__(self.details, content)
        self.error_code = http_code
        self.http_url = http_url
        self.content = content

    def format_details(self, http_code: int, http_url: str, content: bytes) -> str:
        return f"{self.KIND} error: {http_code} during {http_url} call"


class ClientError(HTTPCodeError):
    """Ошибка взаимодействия со стороны клиента (HTTP 400-499)."""

    KIND: ClassVar[str] = "Client"


class ServerError(HTTPCodeError):
    """Ошибка взаимодействия со стороны сервера (HTTP 500-599)."""

    KIND: ClassVar[str] = "Server"


class RetryError(Exception):
    """Ошибки, предполагающие повторное обращение к API."""


class BaseGigachatException(HTTPCodeError):
    """Базовое исключение, связанное с работой GigaChat."""

    def to_json(self) -> str:
        return json.dumps({"error_description": self.details})


class GigaChatTooManyRequests(BaseGigachatException, ClientError, RetryError):
    """Исключение, связанное с ограничениями на количество запросов к GigaChat."""

    def __init__(self, http_url: str, content: bytes):
        super().__init__(HTTPStatus.TOO_MANY_REQUESTS, http_url, content)

    def format_details(self, http_code: int, http_url: str, content: bytes) -> str:
        return (
            "The number of requests to GigaChat within the client CN has been exceeded. Increase the number of threads"
        )


class GigaChatModelNotAvailable(BaseGigachatException, ClientError):
    """Исключение, связанное с отсутствием модели в GigaChat."""

    def __init__(self, model_name: str, http_url: str, content: bytes):
        self.model_name = model_name
        super().__init__(HTTPStatus.NOT_FOUND, http_url, content)

    def format_details(self, http_code: int, http_url: str, content: bytes) -> str:
        return f"The requested model {self.model_name} is not available or does not exist"


class GigaChatException(BaseGigachatException, ServerError):
    """Исключение, связанное с ошибками в работе GigaChat."""

    def format_details(self, http_code: int, http_url: str, content: bytes) -> str:
        return f"Unexpected error in GigaChat. Code: {http_code}, Details: {content}"


class GigaChatStopEventError(BaseGigachatException, ClientError):
    """
    Исключение, возникающее при получении 403 ошибки от GigaChat.
    Указывает на то, что платформа отклонила запрос из-за нагрузки и ограниченности доступа.
    """

    def __init__(self, http_url: str, content: bytes):
        super().__init__(HTTPStatus.FORBIDDEN, http_url, content)

    def format_details(self, http_code: int, http_url: str, content: bytes) -> str:
        return (
            "GigaChat StopEvent (403): Request rejected by platform. "
            "The service might be under high load or the agent has low criticality."
        )
