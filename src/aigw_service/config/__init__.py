from typing import ClassVar

from langgraph.store.memory import InMemoryStore

from .aef_tracing import AEFTracingPrototypeSettings, AEFTracingSettings
from .agent import AgentSettings
from .app import AppSettings
from .base_config import PROJECT_PATH, BaseAppSettings
from .gigachat import GigaChatSettings, OllamaSettings
from .idp import IDPSettings
from .langfuse import LangfuseSettings
from .logger import LogSettings
from .pangolin import PangolinSettings


class Secrets:
    agent: AgentSettings = AgentSettings()
    app: AppSettings = AppSettings()
    log: LogSettings = LogSettings()
    gigachat: GigaChatSettings = GigaChatSettings()
    ollama: OllamaSettings = OllamaSettings()
    # platform_v_search: PlatformVSearchSettings = PlatformVSearchSettings()
    pangolin: PangolinSettings = PangolinSettings()
    # Опциональные настройки — инициализируются лениво, чтобы не требовать env vars при импорте
    _idp: ClassVar[IDPSettings | None] = None
    _langfuse: ClassVar[LangfuseSettings | None] = None
    _aef_tracing: ClassVar[AEFTracingSettings | None] = None

    @property
    def idp(self) -> IDPSettings:
        if self._idp is None:
            self._idp = IDPSettings()
        return self._idp

    @property
    def langfuse(self) -> LangfuseSettings:
        if self._langfuse is None:
            self._langfuse = LangfuseSettings()
        return self._langfuse

    @property
    def aef_tracing(self) -> AEFTracingSettings:
        if self._aef_tracing is None:
            self._aef_tracing = AEFTracingSettings()
        return self._aef_tracing


APP_CONFIG = Secrets()


def get_store() -> InMemoryStore:
    """
    Возвращает хранилище для агента в зависимости от настройки STORE_TO_USE.

    Returns:
        InMemoryStore: Хранилище для агента.
        Если STORE_TO_USE=PANGOLIN, возвращает пустой InMemoryStore
        (реальное подключение к Pangolin происходит в context.py).
    """
    if APP_CONFIG.app.store_to_use == "PANGOLIN":
        # Для PANGOLIN хранилище будет настроено в context.py
        # Возвращаем пустой InMemoryStore как заглушку
        return InMemoryStore()
    else:
        # По умолчанию используем InMemoryStore
        return InMemoryStore()


# Создаем хранилище по умолчанию
DEFAULT_STORE = get_store()

# __all__ sorted by ruff — PlatformVSearchSettings is temporarily commented
__all__ = [
    "APP_CONFIG",
    "DEFAULT_STORE",
    "PROJECT_PATH",
    "AEFTracingPrototypeSettings",
    "AEFTracingSettings",
    "AgentSettings",
    "AppSettings",
    "BaseAppSettings",
    "GigaChatSettings",
    "IDPSettings",
    "LangfuseSettings",
    "LogSettings",
    "OllamaSettings",
    "PangolinSettings",
    "Secrets",
    "get_store",
]
