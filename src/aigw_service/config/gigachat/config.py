from typing import Optional

from pydantic import Field, model_validator

from ..base_config import BaseAppSettings
from ..utils import filepath_from_env_validator


class GigaChatSettings(BaseAppSettings):
    """
    Настройки GigaChat.
    """

    host: str = Field(validation_alias="GIGACHAT_HOST")
    port: str = Field(validation_alias="GIGACHAT_PORT")
    endpoint: str = Field(validation_alias="GIGACHAT_ENDPOINT", default="/v1")
    tls_cert_filepath: Optional[str] = Field(validation_alias="GIGACHAT_TLS_CERT_FILEPATH", default="")
    key_filepath: Optional[str] = Field(validation_alias="GIGACHAT_KEY_FILEPATH", default="")
    ca_bundle_filepath: Optional[str] = Field(validation_alias="GIGACHAT_CA_BUNDLE_FILEPATH", default="")
    credentials: Optional[str] = Field(None, validation_alias="GIGACHAT_CREDENTIALS")
    temperature: float = 0.000001
    max_tokens: int = 8192

    @model_validator(mode="after")
    def validate_file_path(self):
        if self.local:
            for cert_file in (
                self.tls_cert_filepath,
                self.ca_bundle_filepath,
                self.key_filepath,
            ):
                if cert_file:
                    filepath_from_env_validator(cert_file)
        return self

    @property
    def base_url(self) -> str:
        """Базовый API URL."""
        return f"{self.protocol}://{self.host}:{self.port}{self.endpoint}"

    @property
    def certs(self) -> dict:
        """Сертификаты для подключения."""
        _certs = {}
        if self.local:
            _certs = {
                # "ca_bundle_file": self.ca_bundle_filepath,
                "cert_file": self.tls_cert_filepath,
                "key_file": self.key_filepath,
            }
        return _certs

    @property
    def base_params(self) -> dict:
        """Базовые параметры модели."""
        return {
            "base_url": self.base_url,
            "verify_ssl_certs": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self.certs,
        }


class OllamaSettings(BaseAppSettings):
    """
    Настройки Ollama.
    """

    base_url: str = Field(validation_alias="OLLAMA_BASE_URL", default="http://localhost:11434")
    model_name: str = Field(validation_alias="OLLAMA_MODEL", default="llama3")
    temperature: float = Field(validation_alias="OLLAMA_TEMPERATURE", default=0.000001)
    timeout: int = Field(validation_alias="OLLAMA_TIMEOUT", default=60)
