"""AR-Q3 and spec §7 rule 5: LangChain stays in api/v2; v2 does not import the v1 agent (AST only, nothing imported)."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src"
V1 = SRC / "aigw_service" / "api" / "v1"
V2 = SRC / "aigw_service" / "api" / "v2"
FRAMEWORK = ("langchain", "langgraph")

# Framework imports the domain had before v2 (2026-10-26); they may shrink, never grow
DOMAIN_BASELINE = {
    "tools.py": {"langchain.tools"},
    "subagents/analyzer.py": {"langchain_core.messages", "langgraph.graph"},
    "subagents/utils.py": set(),
    "subagents/dummies.py": set(),
    "russian_stemmer.py": set(),
    "schemas/llm_outputs.py": set(),
}
V1_AGENT_MODULES = (
    "aigw_service.api.v1.main_graph",
    "aigw_service.api.v1.subagents.orchestrator_tools",
    "aigw_service.api.v1.services",
)


def imported_modules(path: Path, *, with_names: bool = False) -> set[str]:
    """Modules a file imports; ``with_names`` adds ``pkg.name`` for ``from pkg import name`` (may be a submodule)."""
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            if with_names:
                modules.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    return modules


def framework_imports(path: Path) -> set[str]:
    return {m for m in imported_modules(path) if m.startswith(FRAMEWORK)}


@pytest.mark.parametrize("module", sorted(p.name for p in (SRC / "excel_engine").glob("*.py")))
def test_engine_has_no_framework_imports(module: str) -> None:
    assert not framework_imports(SRC / "excel_engine" / module)


@pytest.mark.parametrize(("module", "baseline"), sorted(DOMAIN_BASELINE.items()))
def test_domain_framework_imports_do_not_grow(module: str, baseline: set[str]) -> None:
    added = framework_imports(V1 / module) - baseline
    assert not added, f"api/v1/{module}: new framework imports {sorted(added)} — keep LangChain in api/v2 (AR-Q3)"


@pytest.mark.parametrize("module", sorted(str(p.relative_to(V2)) for p in V2.rglob("*.py")))
def test_v2_does_not_import_v1_agent(module: str) -> None:
    bad = sorted(m for m in imported_modules(V2 / module, with_names=True) if m.startswith(V1_AGENT_MODULES))
    assert not bad, f"api/v2/{module} imports the v1 agent: {bad}"
