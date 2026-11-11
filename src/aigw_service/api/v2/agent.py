"""Deep Agent construction for API v2."""

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_gigachat import GigaChat
from langchain_ollama import ChatOllama
from langgraph.graph.state import CompiledStateGraph

from aigw_service.api.v1.tools import TOOLS
from aigw_service.api.v2.name_resolution import build_name_resolver
from aigw_service.api.v2.profiles import register_v2_harness_profile
from aigw_service.api.v2.prompts import CORE_SYSTEM_PROMPT
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX


def create_v2_model() -> BaseChatModel:
    """Construct the configured V2 generative client without provider fallback."""
    if APP_CONFIG.v2.model_to_use == "OLLAMA":
        settings = APP_CONFIG.ollama
        return ChatOllama(
            base_url=settings.base_url,
            model=settings.model_name,
            temperature=settings.temperature,
            reasoning=settings.reasoning,
            client_kwargs={"timeout": settings.timeout},
        )
    return GigaChat(**APP_CTX.get_gigachat_base_params(), model=APP_CONFIG.gigachat.model)


def create_v2_agent() -> CompiledStateGraph:
    """Assemble the V2 graph with one selected model for generation and resolution."""
    register_v2_harness_profile()
    llm = create_v2_model()
    return create_deep_agent(
        model=llm,
        tools=TOOLS,
        subagents=[build_name_resolver(llm)],
        system_prompt=CORE_SYSTEM_PROMPT,
        checkpointer=APP_CTX.agent_memory.checkpointer,
        store=APP_CTX.agent_memory.store,
    )
