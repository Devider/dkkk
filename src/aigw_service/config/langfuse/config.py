from typing import Optional

from pydantic import Field, model_validator

from ..base_config import BaseAppSettings
from ..utils import filepath_from_env_validator


class LangfuseSettings(BaseAppSettings):
    """
    Настройки Langfuse.
    """

    tracing_enabled: bool = Field(validation_alias="LANGFUSE_TRACING_ENABLED", default=True)
    host: str = Field(validation_alias="LANGFUSE_HOST")
    port: str = Field(validation_alias="LANGFUSE_PORT", default="")
    endpoint: str = Field(validation_alias="LANGFUSE_ENDPOINT", default="/langfuse")
    public_key: str = Field(validation_alias="LANGFUSE_PUBLIC_KEY")
    secret_key: str = Field(validation_alias="LANGFUSE_SECRET_KEY")
    tls_cert_filepath: Optional[str] = Field(validation_alias="LANGFUSE_TLS_CERT_FILEPATH", default=None)
    key_filepath: Optional[str] = Field(validation_alias="LANGFUSE_KEY_FILEPATH", default=None)
    ca_bundle_filepath: Optional[str] = Field(validation_alias="LANGFUSE_CA_BUNDLE_FILEPATH", default=None)

    @model_validator(mode="after")
    def validate_file_path(self):
        if self.local:
            for cert_file in [
                self.ca_bundle_filepath,
                self.tls_cert_filepath,
                self.key_filepath,
            ]:
                filepath_from_env_validator(cert_file)
        return self

    @staticmethod
    def _build_port(port: str | None) -> str:
        """Создает строку с портом.
        Args:
            port (str | None): порт
        Returns:
            str: строка с портом
        """
        return f":{port}" if port else ""

    @property
    def base_url(self) -> str:
        """Базовый API URL."""
        return f"{self.protocol}://{self.host}{self._build_port(self.port)}{self.endpoint}"

    @property
    def certs(self) -> dict:
        """Сертификаты для подключения."""
        _certs = {}
        if self.local:
            _certs["ca_bundle"] = self.ca_bundle_filepath
            if self.tls_cert_filepath:
                _certs["certs"] = (self.tls_cert_filepath, self.key_filepath)
        return _certs

    @property
    def base_params(self) -> dict:
        """Базовые параметры для подключения."""
        return {
            "base_url": self.base_url,
            "public_key": self.public_key,
            "secret_key": self.secret_key,
            **self.certs,
        }
