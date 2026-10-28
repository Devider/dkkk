"""Agent v2 behind the router (ADR-12): one turn in, the answer text out."""

import time
from typing import Any

from langchain_core.messages import HumanMessage

from aigw_service.api.v2.context import RequestContext
from aigw_service.api.v2.factory import get_agent_v2
from aigw_service.api.v2.stream.turn import TurnStream
from aigw_service.api.v2.turn.lock import TURN_LOCKS, TurnLocks
from aigw_service.config import APP_CONFIG


class AgentV2:
    """What ``router.get_agent`` returns when ``AGENT_ENGINE=v2``."""

    def __init__(self, agent: Any = None, locks: TurnLocks = TURN_LOCKS) -> None:
        self._agent = agent
        self._locks = locks

    @property
    def agent(self) -> Any:
        if self._agent is None:
            self._agent = get_agent_v2()
        return self._agent

    @staticmethod
    def _prepare(context: RequestContext) -> None:
        """Shared by ``answer`` and ``stream`` (S6.2 design D9)."""
        if context.deadline is None:  # AR-Q10, S2.8 design D1: the whole turn, TurnContext included
            context.deadline = time.monotonic() + APP_CONFIG.agent.turn_timeout

    async def answer(self, message: str, context: RequestContext, config: dict[str, Any]) -> str:
        with self._locks.hold(context.thread_id):  # TurnBusyError → HTTP 409 (FR-15)
            self._prepare(context)
            result = await self.agent.ainvoke({"messages": [HumanMessage(message)]}, config=config, context=context)
        last = result["messages"][-1]
        return last.content if isinstance(last.content, str) else str(last.content)

    def stream(self, message: str, context: RequestContext, config: dict[str, Any]) -> TurnStream:
        """The same turn delivered as events (ADR-14); the lock is taken here, before the first event.

        ``TurnBusyError`` is raised by this call, so the endpoint answers 409 before opening the stream (FR-21).
        The turn task releases the lock when the turn ends (AR-Q17).
        """
        release = self._locks.acquire(context.thread_id)
        try:
            self._prepare(context)
            return TurnStream(self.agent, {"messages": [HumanMessage(message)]}, context, config, release)
        except BaseException:
            release()
            raise
