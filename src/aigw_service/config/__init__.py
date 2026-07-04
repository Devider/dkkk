# from .platform_v_search import PlatformVSearchSettings
from langgraph.store.memory import InMemoryStore

from .agent import AgentSettings
from .app import AppSettings
from .base_config import PROJECT_PATH, BaseAppSettings
from .gigachat import GigaChatSettings, OllamaSettings
from .idp import IDPSettings

# from .langfuse import LangfuseSettings
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
    # idp: IDPSettings = IDPSettings()
    # langfuse: LangfuseSettings = LangfuseSettings()


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

__all__ = [
    "AgentSettings",
    "APP_CONFIG",
    "PROJECT_PATH",
    "AppSettings",
    "BaseAppSettings",
    "GigaChatSettings",
    # "IDPSettings",
    # "LangfuseSettings",
    "LogSettings",
    "OllamaSettings",
    # "PlatformVSearchSettings",
    "PangolinSettings",
    "Secrets",
    "get_store",
    "DEFAULT_STORE",
]
