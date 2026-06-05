import os
from logging import DEBUG, INFO

from pydantic import Field, field_validator

from ..base_config import BaseAppSettings
from ..utils import dirpath_from_env_validator


class LogSettings(BaseAppSettings):
    """
    Настройки логирования.
    """

    private_log_file_path: str = Field(validation_alias="LOG_PATH", default=os.getcwd())
    private_log_file_name: str = Field(validation_alias="LOG_FILE_NAME", default="app.log")
    log_rotation: str = Field(validation_alias="LOG_ROTATION", default="10 MB")
    private_metric_file_path: str = Field(validation_alias="METRIC_PATH", default=os.getcwd())
    private_metric_file_name: str = Field(validation_alias="METRIC_FILE_NAME", default="app-metric.log")
    private_audit_file_path: str = Field(validation_alias="AUDIT_LOG_PATH", default=os.getcwd())
    private_audit_file_name: str = Field(validation_alias="AUDIT_LOG_FILE_NAME", default="events.log")
    audit_host_ip: str = Field(validation_alias="HOST_IP", default="127.0.0.1")
    audit_host_uid: str = Field(validation_alias="HOST_UID", default="63bd6cbe-170b-49bf-a65c-3ce967398ccd")

    @field_validator(
        "private_log_file_path",
        "private_metric_file_path",
        "private_audit_file_path",
    )
    @classmethod
    def validate_path(cls, value):
        dirpath_from_env_validator(value)
        return value

    @staticmethod
    def get_file_abs_path(path_name: str, file_name: str) -> str:
        """Получает абсолютный путь к файлу.

        Args:
            path_name (str): путь к каталогу
            file_name (str): имя файла

        Returns:
            str: абсолютный путь к файлу
        """
        return os.path.join(path_name.strip(), file_name.lstrip("/").strip())

    @property
    def log_file_abs_path(self) -> str:
        return self.get_file_abs_path(self.private_log_file_path, self.private_log_file_name)

    @property
    def metric_file_abs_path(self) -> str:
        return self.get_file_abs_path(self.private_metric_file_path, self.private_metric_file_name)

    @property
    def audit_file_abs_path(self) -> str:
        return self.get_file_abs_path(self.private_audit_file_path, self.private_audit_file_name)

    @property
    def log_lvl(self) -> int:
        return DEBUG if self.debug else INFO
