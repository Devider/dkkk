from pydantic import Field

from ..base_config import BaseAppSettings


class AppSettings(BaseAppSettings):
    """
    Настройки приложения.
    """

    app_host: str = Field(validation_alias="APP_HOST", default="0.0.0.0")
    app_port: int = Field(validation_alias="APP_PORT", default=8080)
    kube_net_name: str = Field(validation_alias="PROJECT_NAME", default="AIGATEWAY")
    timezone: str = Field(validation_alias="TIMEZONE", default="Europe/Moscow")
    openapi_version: str = Field(validation_alias="OPENAPI_VERSION", default="3.0.2")
    preview_model_check: bool = Field(validation_alias="ENABLE_PREVIEW_MODEL_CHECK", default=False)
    preview_model_check_percent: float = Field(validation_alias="PREVIEW_MODEL_CHECK_PERCENT", default=0.05)
    model_call_max_depth: int = Field(validation_alias="MODEL_CALL_MAX_DEPTH", default=10)

    @property
    def metadata(self) -> dict:
        """Метаданные собранного дистрибутива шаблонного проекта"""

        from importlib.metadata import distribution

        dist = distribution("aigw-rest-service")

        return {
            "name": str(dist.metadata["Name"]),
            "description": str(dist.metadata["Summary"]),
            "type": "REST API",
            "version": str(dist.version),
        }
