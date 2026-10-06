"""Unit-тест для `api/v1/router.py:get_agent()`.

Покрывает сценарий `agent-graph` спека "Behavior is independent of deployment config":
`/api/v1` всегда отвечает легаси `AgentGraph`, независимо от переменных окружения —
`AGENT_GRAPH_IMPL` удалён (см. `config/base_config.py`), поэтому даже установка такой
переменной не должна влиять на результат.
"""

import os
from unittest.mock import patch

from aigw_service.api.v1.main_graph import AgentGraph
from aigw_service.api.v1.router import get_agent


class TestGetAgentIsDeterministic:
    def test_returns_agent_graph(self):
        get_agent.cache_clear()
        agent = get_agent()
        assert isinstance(agent, AgentGraph)

    def test_unaffected_by_leftover_env_var(self):
        """A deployment with a stale `AGENT_GRAPH_IMPL` setting is simply ignored (per
        design.md - Decisions: the setting is removed, not deprecated-but-kept)."""
        get_agent.cache_clear()
        with patch.dict(os.environ, {"AGENT_GRAPH_IMPL": "DEEPAGENTS"}):
            agent = get_agent()
        assert isinstance(agent, AgentGraph)
