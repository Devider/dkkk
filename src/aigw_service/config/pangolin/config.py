from typing import Optional
from urllib.parse import quote_plus

from pydantic import Field, model_validator

from ..base_config import BaseAppSettings
from ..utils import filepath_from_env_validator


class PangolinSettings(BaseAppSettings):
    """
    Настройки Pangolin.
    """

    enabled: bool = Field(validation_alias="DB_ENABLED", default=False)
    host: Optional[str] = Field(validation_alias="DB_HOST", default=None)
    port: Optional[str] = Field(validation_alias="DB_PORT", default=None)
    user: Optional[str] = Field(validation_alias="DB_USER", default=None)
    password: Optional[str] = Field(validation_alias="DB_PASS", default=None)
    database: Optional[str] = Field(validation_alias="DB_DATABASE", default=None)
    ca_bundle_filepath: Optional[str] = Field(validation_alias="DB_CA_BUNDLE_FILEPATH", default="")
    tls_cert_filepath: Optional[str] = Field(validation_alias="DB_TLS_CERT_FILEPATH", default="")
    key_filepath: Optional[str] = Field(validation_alias="DB_KEY_FILEPATH", default="")

    @model_validator(mode="after")
    def validate_required_when_enabled(self):
        if self.enabled:
            missing = []
            for field_name in ("host", "port", "user", "password", "database"):
                if getattr(self, field_name) is None:
                    missing.append(field_name)
            if missing:
                raise ValueError(f"DB_ENABLED=True, но не заданы: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def validate_file_path(self):
        if self.local and self.enabled:
            for cert_file in (
                self.tls_cert_filepath,
                self.ca_bundle_filepath,
                self.key_filepath,
            ):
                if cert_file:
                    filepath_from_env_validator(cert_file)
        return self

    @property
    def db_uri(self) -> str:
        """Строка подключения к Pangolin"""
        _uri = (
            f"postgresql://{quote_plus(self.user)}:{quote_plus(self.password)}@{self.host}:{self.port}/{self.database}"
        )
        if self.local:
            _uri += (
                "?sslmode=require"
                # Если возникает ошибка при использовании сертов, то необходимон раздать права на серты chmod 0600
                f"&sslrootcert={self.ca_bundle_filepath}"
                f"&sslcert={self.tls_cert_filepath}"
                f"&sslkey={self.key_filepath}"
            )
        return _uri
