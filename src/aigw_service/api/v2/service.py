"""Agent v2 behind the router (ADR-12): one turn in, the answer text out."""

from typing import Any

from langchain_core.messages import HumanMessage

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import get_agent_v2


class AgentV2:
    """What ``router.get_agent`` returns when ``AGENT_ENGINE=v2``."""

    def __init__(self, agent: Any = None) -> None:
        self._agent = agent

    @property
    def agent(self) -> Any:
        if self._agent is None:
            self._agent = get_agent_v2()
        return self._agent

    async def answer(self, message: str, context: RequestContext, config: dict[str, Any]) -> str:
        result = await self.agent.ainvoke({"messages": [HumanMessage(message)]}, config=config, context=context)
        last = result["messages"][-1]
        return last.content if isinstance(last.content, str) else str(last.content)
