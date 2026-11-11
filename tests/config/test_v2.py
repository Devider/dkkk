"""V2 provider selection stays independent of the global model selector."""

import pytest
from pydantic import ValidationError

from aigw_service.config.v2 import V2Settings


def test_default_is_independent_of_global_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preexisting global variable must not opt V2 into Ollama."""
    monkeypatch.delenv("V2_MODEL_TO_USE", raising=False)
    monkeypatch.setenv("MODEL_TO_USE", "OLLAMA")
    assert V2Settings().model_to_use == "GIGACHAT"


@pytest.mark.parametrize("provider", ["GIGACHAT", "OLLAMA"])
def test_explicit_provider(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both supported exact provider values can be selected by deployment."""
    monkeypatch.setenv("V2_MODEL_TO_USE", provider)
    assert V2Settings().model_to_use == provider


@pytest.mark.parametrize("provider", ["", "ollama", "OPENAI"])
def test_unknown_provider_is_rejected(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid configuration must not silently choose a fallback provider."""
    monkeypatch.setenv("V2_MODEL_TO_USE", provider)
    with pytest.raises(ValidationError, match="V2_MODEL_TO_USE"):
        V2Settings()
