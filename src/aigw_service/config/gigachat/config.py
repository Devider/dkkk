from enum import Enum
from typing import ClassVar, Optional

from pydantic import Field, model_validator

from ..base_config import BaseAppSettings
from ..utils import filepath_from_env_validator


class GigaChatAuthMode(str, Enum):
    """Способ аутентификации в GigaChat."""

    TOKEN = "token"
    CERTIFICATE = "certificate"


class GigaChatSettings(BaseAppSettings):
    """
    Настройки GigaChat.
    """

    host: str = Field(validation_alias="GIGACHAT_HOST", default="localhost")
    port: str = Field(validation_alias="GIGACHAT_PORT", default="8080")
    endpoint: str = Field(validation_alias="GIGACHAT_ENDPOINT", default="/v1")
    model: str = Field(validation_alias="GIGACHAT_MODEL_NAME")
    tls_cert_filepath: Optional[str] = Field(validation_alias="GIGACHAT_TLS_CERT_FILEPATH", default=None)
    key_filepath: Optional[str] = Field(validation_alias="GIGACHAT_KEY_FILEPATH", default=None)
    ca_bundle_filepath: Optional[str] = Field(validation_alias="GIGACHAT_CA_BUNDLE_FILEPATH", default=None)
    credentials: Optional[str] = Field(None, validation_alias="GIGACHAT_CREDENTIALS")
    scope: str = Field(default="GIGACHAT_API_PERS", validation_alias="GIGACHAT_SCOPE")
    verify_ssl_certs: bool = Field(default=False, validation_alias="GIGACHAT_VERIFY_SSL_CERTS")
    max_retries: int = Field(default=5, validation_alias="GIGACHAT_MAX_RETRIES")
    retry_backoff_factor: float = Field(default=0.5, validation_alias="GIGACHAT_RETRY_BACKOFF_FACTOR")
    temperature: ClassVar[float] = 0.000001
    max_tokens: ClassVar[int] = 8192

    @property
    def auth_mode(self) -> GigaChatAuthMode:
        """Определяет способ аутентификации по заполненным переменным окружения."""
        has_token = bool(self.credentials)
        has_cert = bool(self.tls_cert_filepath or self.key_filepath)
        if has_token and has_cert:
            raise ValueError(
                "GigaChat auth is ambiguous: both GIGACHAT_CREDENTIALS and "
                "GIGACHAT_TLS_CERT_FILEPATH/GIGACHAT_KEY_FILEPATH are set. "
                "Unset one of the two groups."
            )
        if has_token:
            return GigaChatAuthMode.TOKEN
        if has_cert:
            return GigaChatAuthMode.CERTIFICATE
        raise ValueError(
            "GigaChat auth is not configured: set either GIGACHAT_CREDENTIALS (token mode) "
            "or GIGACHAT_TLS_CERT_FILEPATH + GIGACHAT_KEY_FILEPATH (certificate mode)."
        )

    @model_validator(mode="after")
    def validate_auth_mode(self) -> "GigaChatSettings":
        mode = self.auth_mode  # raises ValueError for "both set" / "neither set"
        if mode is GigaChatAuthMode.CERTIFICATE:
            if not (self.tls_cert_filepath and self.key_filepath):
                raise ValueError(
                    "GigaChat certificate auth requires both GIGACHAT_TLS_CERT_FILEPATH "
                    "and GIGACHAT_KEY_FILEPATH to be set."
                )
            for path in (self.tls_cert_filepath, self.key_filepath, self.ca_bundle_filepath):
                if path:
                    filepath_from_env_validator(path)
        return self

    @property
    def base_url(self) -> str:
        """Базовый API URL."""
        port_segment = f":{self.port}" if self.port else ""
        return f"{self.protocol}://{self.host}{port_segment}/{self.endpoint}"

    @property
    def certs(self) -> dict:
        """Сертификаты для подключения."""
        if self.auth_mode is not GigaChatAuthMode.CERTIFICATE:
            return {}
        _certs = {
            "cert_file": self.tls_cert_filepath,
            "key_file": self.key_filepath,
        }
        if self.ca_bundle_filepath:
            _certs["ca_bundle_file"] = self.ca_bundle_filepath
        return _certs

    @property
    def base_params(self) -> dict:
        """Базовые параметры модели."""
        params = {
            "base_url": self.base_url,
            "verify_ssl_certs": self.verify_ssl_certs,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_retries": self.max_retries,
            "retry_backoff_factor": self.retry_backoff_factor,
        }
        if self.auth_mode is GigaChatAuthMode.TOKEN:
            params["credentials"] = self.credentials
            params["scope"] = self.scope
        else:
            params.update(self.certs)
        return params


class OllamaSettings(BaseAppSettings):
    """
    Настройки Ollama.
    """

    base_url: str = Field(validation_alias="OLLAMA_BASE_URL", default="http://localhost:11434")
    model_name: str = Field(validation_alias="OLLAMA_MODEL", default="llama3")
    temperature: float = Field(validation_alias="OLLAMA_TEMPERATURE", default=0.000001)
    timeout: int = Field(validation_alias="OLLAMA_TIMEOUT", default=60)
