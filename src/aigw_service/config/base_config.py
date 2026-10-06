from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

load_dotenv(override=True)

PROJECT_PATH = Path(__file__).resolve().parents[3]


class BaseAppSettings(BaseSettings):
    """
    Базовый класс для настроек.
    """

    local: bool = Field(validation_alias="LOCAL", default=False)
    debug: bool = Field(validation_alias="DEBUG", default=False)
    store_to_use: str = Field(validation_alias="STORE_TO_USE", default="MEMORY")
    model_to_use: str = Field(validation_alias="MODEL_TO_USE", default="GIGACHAT")
    llm_model_name: str = Field(validation_alias="LLM_MODEL_NAME", default="GigaChat-2-Max")

    @property
    def protocol(self) -> str:
        """Протокол HTTP."""
        return "https" if self.local else "http"
