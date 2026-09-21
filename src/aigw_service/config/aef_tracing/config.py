from typing import Any

from pydantic import Field, ValidationInfo, field_validator, model_validator

from ..base_config import BaseAppSettings


class AEFTracingSettings(BaseAppSettings):
    """
    Настройки AEF Tracing.
    """

    enabled: bool = Field(validation_alias="AEF_TRACING_ENABLED", default=True)
    tracing_sender_type: str = Field(validation_alias="AEF_TRACING_SENDER_TYPE", default="console")
    kafka_topic: str = Field(validation_alias="AEF_TRACING_KAFKA_TOPIC")
    kafka_hosts: str | list[str] = Field(validation_alias="AEF_TRACING_KAFKA_HOSTS")
    cluster_id: str = Field(validation_alias="CLUSTER_NAME")
    agent_id: str = Field(validation_alias="AGENT_ID")
    distributive: str = Field(validation_alias="DISTRIB")
    namespace: str = Field(validation_alias="NAMESPACE")

    @field_validator("kafka_hosts", mode="before")
    @classmethod
    def parse_kafka_hosts(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return value.replace(" ", "").split(",")
        return value

    @model_validator(mode="before")
    @classmethod
    def check_enabled(cls, values):
        if values.get("enabled") is False:
            # Оставляем только enabled, остальные поля игнорируем
            return {"enabled": False}
        return values


class AEFTracingPrototypeSettings(AEFTracingSettings):
    """
    Настройки AEF Tracing для тестирования агента-прототипа.
    """

    @field_validator("cluster_id", "agent_id", "distributive", "namespace", mode="before")
    @classmethod
    def add_prototype_prefix(cls, value: Any, info: ValidationInfo) -> str:
        if not isinstance(value, str):
            raise TypeError(f"Field '{info.field_name}' must be string, got {type(value).__name__}")
        return f"prototype-{value}"
