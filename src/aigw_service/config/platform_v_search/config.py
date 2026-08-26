from typing import Optional

from pydantic import Field, model_validator

from ..base_config import BaseAppSettings
from ..utils import filepath_from_env_validator


class PlatformVSearchSettings(BaseAppSettings):
    """
    Настройки Platform V Search.
    """

    enabled: bool = Field(validation_alias="PVS_ENABLED", default=False)
    host: str = Field(validation_alias="PVS_HOST", default="")
    port: str = Field(validation_alias="PVS_PORT", default="")
    login: str = Field(validation_alias="PVS_LOGIN", default="")
    password: str = Field(validation_alias="PVS_PASSWORD", default="")
    index_name: Optional[str] = Field(validation_alias="PVS_INDEX_NAME", default=None)
    ca_bundle_filepath: Optional[str] = Field(validation_alias="PVS_CA_BUNDLE_FILEPATH", default="")
    tls_cert_filepath: Optional[str] = Field(validation_alias="PVS_TLS_CERT_FILEPATH", default="")
    key_filepath: Optional[str] = Field(validation_alias="PVS_KEY_FILEPATH", default="")

    @model_validator(mode="after")
    def validate_file_path(self):
        if self.local and self.enabled:
            for cert_file in (
                self.tls_cert_filepath,
                self.ca_bundle_filepath,
                self.key_filepath,
            ):
                filepath_from_env_validator(cert_file)
        return self

    @model_validator(mode="before")
    @classmethod
    def check_enabled(cls, values):
        if values.get("enabled") is False:
            # Оставляем только enabled, остальные поля игнорируем
            return {"enabled": False}
        return values

    @property
    def hosts(self) -> list[str]:
        """Список url-адресов, состоящих из хоста и порта."""
        _hosts = []
        _ports = self.port.split(",")
        for host_number, host in enumerate(self.host.split(",")):
            _host = f"{host}:{_ports[host_number]}"
            _hosts.append(_host)
        return _hosts

    @property
    def connection_params(self) -> dict:
        """Параметры соединения с Platform V Search."""
        _connection_params = {"http_auth": (self.login, self.password)}
        if self.local:
            _connection_params.update(
                {
                    "use_ssl": True,
                    "verify_certs": True,
                    "ssl_assert_hostname": False,
                    "ssl_show_warn": False,
                    "client_cert": self.tls_cert_filepath,
                    "client_key": self.key_filepath,
                    "ca_certs": self.ca_bundle_filepath,
                }
            )
        return _connection_params
