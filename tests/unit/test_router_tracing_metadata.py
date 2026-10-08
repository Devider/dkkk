"""Unit test for the Langfuse trace user-attribution metadata key in ``router.py``.

``langfuse.langchain.CallbackHandler`` only recognizes the literal metadata key
``"langfuse_user_id"`` - the previous ``"lang_fuse_user_id"`` typo meant ``user_id`` never
reached the Langfuse trace. This calls ``invoke_agent`` directly (bypassing FastAPI's
dependency injection, since the decorated function is still a plain coroutine) with a mocked
``agent.graph.ainvoke`` to capture the ``config`` dict built for the call.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aigw_service.api.v1.router import invoke_agent
from aigw_service.api.v1.schemas_file import CopilotAgentRequest


async def test_invoke_agent_sets_langfuse_user_id_metadata_key():
    request = CopilotAgentRequest(message="test message")
    headers = {"x-user-id": "U1234567", "x-client-id": "CI12345678"}

    captured: dict = {}

    async def fake_ainvoke(_input, config):
        captured["config"] = config
        return {"messages": [SimpleNamespace(content="ok")]}

    agent = MagicMock()
    agent.graph.ainvoke = AsyncMock(side_effect=fake_ainvoke)

    await invoke_agent(request=request, headers=headers, agent=agent, cb_handler=None)

    metadata = captured["config"]["metadata"]
    assert metadata["langfuse_user_id"] == "U1234567"
    assert "lang_fuse_user_id" not in metadata
