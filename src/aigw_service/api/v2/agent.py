"""Deep Agent construction for API v2."""

from deepagents import create_deep_agent
from langchain_gigachat import GigaChat

from aigw_service.api.v1.tools import TOOLS
from aigw_service.api.v2.profiles import register_v2_harness_profile
from aigw_service.api.v2.prompts import CORE_SYSTEM_PROMPT
from aigw_service.config import APP_CONFIG
from aigw_service.context import APP_CTX


def create_v2_agent():
    """Create the v2 GigaChat client and Deep Agent."""
    register_v2_harness_profile()
    llm = GigaChat(**APP_CTX.get_gigachat_base_params(), model=APP_CONFIG.gigachat.model)
    return create_deep_agent(
        model=llm,
        tools=TOOLS,
        system_prompt=CORE_SYSTEM_PROMPT,
        checkpointer=APP_CTX.agent_memory.checkpointer,
        store=APP_CTX.agent_memory.store,
    )
