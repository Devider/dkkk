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


PREVIEW_SUFFIX = "-preview"
"""Name of the preview model: the agent's model plus this suffix, as in v1 ``llm_executor.Agent`` (ADR-10)."""


def build_preview_model() -> BaseChatModel:
    """S4.4 design D4: ``<GIGACHAT_MODEL>-preview`` with the same connection; it exists only inside the contour."""
    return GigaChat(**APP_CONFIG.gigachat.base_params, model=f"{APP_CONFIG.gigachat.model}{PREVIEW_SUFFIX}")


def build_judge_model() -> BaseChatModel:
    """S4.4 design D4: the judge is the agent's GigaChat model, a separate instance outside the agent's stack."""
    return GigaChat(**APP_CONFIG.gigachat.base_params, model=APP_CONFIG.gigachat.model)
