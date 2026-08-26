from typing import Any, Optional

from pydantic import Field, ValidationInfo, field_validator, model_validator

from ..base_config import BaseAppSettings


class AEFTracingSettings(BaseAppSettings):
    """
    Настройки AEF Tracing.
    """

    enabled: bool = Field(validation_alias="AEF_TRACING_ENABLED", default=False)
    tracing_sender_type: str = Field(validation_alias="AEF_TRACING_SENDER_TYPE", default="console")
    kafka_topic: Optional[str] = Field(validation_alias="AEF_TRACING_KAFKA_TOPIC", default=None)
    kafka_hosts: Optional[str | list[str]] = Field(validation_alias="AEF_TRACING_KAFKA_HOSTS", default=None)
    cluster_id: Optional[str] = Field(validation_alias="CLUSTER_NAME", default=None)
    agent_id: Optional[str] = Field(validation_alias="AGENT_ID", default=None)
    distributive: Optional[str] = Field(validation_alias="DISTRIB", default=None)
    namespace: Optional[str] = Field(validation_alias="NAMESPACE", default=None)

    @field_validator("kafka_hosts", mode="before")
    @classmethod
    def parse_kafka_hosts(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return value.replace(" ", "").split(",")
        return value

    @model_validator(mode="after")
    def validate_required_when_enabled(self):
        if self.enabled:
            missing = []
            for field_name in ("kafka_topic", "kafka_hosts", "cluster_id", "agent_id", "distributive", "namespace"):
                if getattr(self, field_name) is None:
                    missing.append(field_name)
            if missing:
                raise ValueError(f"AEF_TRACING_ENABLED=True, но не заданы: {', '.join(missing)}")
        return self


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
