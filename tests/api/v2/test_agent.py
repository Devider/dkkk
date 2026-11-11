"""Offline provider construction and graph assembly contracts."""

from unittest.mock import Mock

import pytest
from langchain_ollama import ChatOllama

from aigw_service.api.v2 import agent
from aigw_service.config.v2 import V2Settings


def test_ollama_configuration_and_http_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use real adapter construction to inspect both underlying HTTP clients."""
    monkeypatch.setattr(agent.APP_CONFIG, "v2", V2Settings(V2_MODEL_TO_USE="OLLAMA"))
    settings = agent.APP_CONFIG.ollama.model_copy(
        update={
            "base_url": "http://localhost:11434",
            "model_name": "qwen3.5:9b-mlx",
            "temperature": 0.2,
            "timeout": 137,
        }
    )
    monkeypatch.setattr(agent.APP_CONFIG, "ollama", settings)
    giga = Mock(side_effect=AssertionError("GigaChat must not be constructed"))
    monkeypatch.setattr(agent, "GigaChat", giga)
    model = agent.create_v2_model()
    assert isinstance(model, ChatOllama)
    assert model.model == settings.model_name
    assert model.temperature == settings.temperature
    assert model.base_url == settings.base_url
    assert model.reasoning is False
    assert model._client._client.timeout.read == 137
    assert model._async_client._client.timeout.read == 137
    giga.assert_not_called()


def test_gigachat_preserves_existing_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default generation uses the same GigaChat configuration as before."""
    monkeypatch.setattr(agent.APP_CONFIG, "v2", V2Settings(V2_MODEL_TO_USE="GIGACHAT"))
    factory = Mock()
    monkeypatch.setattr(agent, "GigaChat", factory)
    assert agent.create_v2_model() is factory.return_value
    factory.assert_called_once_with(**agent.APP_CTX.get_gigachat_base_params(), model=agent.APP_CONFIG.gigachat.model)


@pytest.mark.parametrize("provider", ["GIGACHAT", "OLLAMA"])
def test_graph_and_resolver_share_selected_model(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither resolver construction nor graph assembly switches providers."""
    monkeypatch.setattr(agent.APP_CONFIG, "v2", V2Settings(V2_MODEL_TO_USE=provider))
    model = Mock()
    monkeypatch.setattr(agent, "create_v2_model", lambda: model)
    resolver = Mock(return_value={"name": "workbook-name-resolver"})
    graph = Mock()
    monkeypatch.setattr(agent, "build_name_resolver", resolver)
    monkeypatch.setattr(agent, "create_deep_agent", graph)
    agent.create_v2_agent()
    resolver.assert_called_once_with(model)
    assert graph.call_args.kwargs["model"] is model
    assert graph.call_args.kwargs["subagents"] == [resolver.return_value]
    assert graph.call_args.kwargs["checkpointer"] is agent.APP_CTX.agent_memory.checkpointer
    assert graph.call_args.kwargs["store"] is agent.APP_CTX.agent_memory.store
