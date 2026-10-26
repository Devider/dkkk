"""Chat model of agent v2 by configuration (AR-Q1, ADR-10): GigaChat or a scripted model."""

from langchain_core.language_models import BaseChatModel
from langchain_gigachat import GigaChat

from aigw_service.api.v2.scripted import ScriptedChatModel
from aigw_service.config import APP_CONFIG


def build_model() -> BaseChatModel:
    """``AGENT_MODEL=gigachat``: api_key or certificates as chosen by ``LOCAL`` and ``GIGACHAT_*`` (as in v1).

    A plain ``GigaChat`` — no ``llm_executor.Agent`` subclass: the A/B preview check moves to middleware (S4.4).
    """
    settings = APP_CONFIG.agent
    if settings.model == "scripted":
        if not settings.script_path:
            raise ValueError("AGENT_MODEL=scripted needs AGENT_SCRIPT_PATH with a JSON script")
        return ScriptedChatModel.from_file(settings.script_path)
    return GigaChat(**APP_CONFIG.gigachat.base_params, model=APP_CONFIG.gigachat.model)
