import uuid
import warnings
from typing import Optional

from pydantic import Field, field_validator, model_validator

from ..base_config import BaseAppSettings
from ..utils import filepath_from_env_validator


class IDPSettings(BaseAppSettings):
    host: str = Field(validation_alias="IDP_HOST")
    port: str = Field(validation_alias="IDP_PORT")

    source_uuid: Optional[str] = Field(validation_alias="IDP_SOURCE_UUID", default=None)
    index_id: Optional[str] = Field(validation_alias="IDP_INDEX_ID", default=None)
    scenario_id: str = Field(validation_alias="IDP_SCENARIO_ID", default="")

    ca_bundle_filepath: Optional[str] = Field(validation_alias="IDP_CA_BUNDLE_FILEPATH", default=None)
    tls_cert_filepath: Optional[str] = Field(validation_alias="IDP_TLS_CERT_FILEPATH", default=None)
    key_filepath: Optional[str] = Field(validation_alias="IDP_KEY_FILEPATH", default=None)

    retry_stop: int = Field(validation_alias="IDP_RETRY_STOP", default=3)
    retry_wait: int = Field(validation_alias="IDP_RETRY_WAIT", default=2)
    request_timeout: int = Field(validation_alias="IDP_REQUEST_TIMEOUT", default=60)

    @field_validator("source_uuid")
    @classmethod
    def validate_source_uuid(cls, value):
        if value is None:
            value = ""
            warnings.warn(
                "\n\033[91mПеременная окружения IDP_SOURCE_UUID не установлена! "
                "Это обязательный параметр для работы с IDP GigaSearch.\033[0m",
                UserWarning,
            )
        return value

    @field_validator("index_id")
    @classmethod
    def validate_index_id(cls, value):
        if value is None:
            value = str(uuid.uuid4())
            print(
                f"\033[93mНе установлена переменная окружения IDP_INDEX_ID. Сгенерированное значение: {value}.\033[0m",
            )
        return value

    @model_validator(mode="after")
    def validate_file_path(self):
        if self.local:
            for cert_file in (
                self.tls_cert_filepath,
                self.ca_bundle_filepath,
                self.key_filepath,
            ):
                filepath_from_env_validator(cert_file)
        return self

    @property
    def base_url(self) -> str:
        """Базовый IDP GigaSearch API URL."""
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def certs(self) -> Optional[dict]:
        """Сертификаты для подключения."""
        _certs = {}
        if self.local:
            _certs.update(
                {
                    "ca_bundle": self.ca_bundle_filepath,
                    "client_cert": self.tls_cert_filepath,
                    "client_key": self.key_filepath,
                }
            )
        return _certs
