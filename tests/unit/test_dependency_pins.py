"""AR-Q4: installed agent-framework versions match pyproject pins and v2 API entry points exist."""

import importlib
import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([^\s;,]+)\s*$")

# Framework packages AR-Q4 requires to be pinned with "=="
FRAMEWORK_PACKAGES = (
    "langchain",
    "langchain-core",
    "langgraph",
    "deepagents",
    "langchain-gigachat",
    "gigachat",
    "langfuse",
)

# API entry points agent v2 relies on (spec §6, §8): (module, attribute)
V2_API_ENTRY_POINTS = (
    ("langchain.agents", "create_agent"),
    ("langchain.agents.middleware", "AgentMiddleware"),
    ("langchain.agents.middleware", "TodoListMiddleware"),
    ("langchain.agents.middleware", "ModelCallLimitMiddleware"),
    ("langchain.agents.middleware", "ToolCallLimitMiddleware"),
    ("langchain.agents.middleware", "ModelRetryMiddleware"),
    ("langchain.agents.middleware", "ToolRetryMiddleware"),
    ("langchain.agents.middleware", "ToolErrorMiddleware"),
    ("deepagents.middleware", "SkillsMiddleware"),
    ("deepagents.middleware", "FilesystemMiddleware"),
    ("deepagents.middleware", "SummarizationMiddleware"),
    ("langchain_gigachat", "GigaChat"),
    ("langgraph.checkpoint.memory", "InMemorySaver"),
    ("langgraph.store.memory", "InMemoryStore"),
)


def _exact_pins() -> dict[str, str]:
    """Return {normalized package name: version} for every "==" dependency in pyproject."""
    with PYPROJECT.open("rb") as f:
        dependencies = tomllib.load(f)["project"]["dependencies"]
    pins = {}
    for spec in dependencies:
        match = PIN_RE.match(spec)
        if match:
            pins[_normalize(match.group(1))] = match.group(2)
    return pins


def _normalize(name: str) -> str:
    """PEP 503 package name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


class TestDependencyPins:
    """Framework packages are pinned exactly and the installed environment matches."""

    @pytest.mark.parametrize("package", FRAMEWORK_PACKAGES)
    def test_framework_package_is_pinned(self, package: str) -> None:
        assert _normalize(package) in _exact_pins(), f"{package} must be pinned with '==' in pyproject.toml"

    @pytest.mark.parametrize(("package", "version"), sorted(_exact_pins().items()))
    def test_installed_version_matches_pin(self, package: str, version: str) -> None:
        installed = importlib.metadata.version(package)
        assert installed == version, f"{package}: installed {installed}, pinned {version} (run `poetry install`)"


class TestV2ApiEntryPoints:
    """Framework API used by agent v2 is importable at the pinned versions."""

    @pytest.mark.parametrize(("module", "attribute"), V2_API_ENTRY_POINTS)
    def test_entry_point_exists(self, module: str, attribute: str) -> None:
        assert hasattr(importlib.import_module(module), attribute), f"{module}.{attribute} is missing"
