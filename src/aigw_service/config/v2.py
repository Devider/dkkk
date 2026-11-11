"""Independent generative provider configuration for API v2."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class V2Settings(BaseSettings):
    """Select V2 generation without altering V1 or embedding settings."""

    model_to_use: Literal["GIGACHAT", "OLLAMA"] = Field(default="GIGACHAT", validation_alias="V2_MODEL_TO_USE")
