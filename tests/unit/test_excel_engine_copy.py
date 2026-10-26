"""S0.7: src/excel_engine is a verbatim copy of the v1 calculations and engine; only imports may differ."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
V1 = SRC / "aigw_service" / "api" / "v1"
ENGINE = SRC / "excel_engine"

# Copied definitions and where they come from: (copy module, original module)
COPIED_FROM_TOOLS = ("matching.py", "calculations.py")
COPIED_MODULES = ("lo_backend.py", "excel_handler.py", "russian_stemmer.py")
FORBIDDEN_IMPORTS = ("aigw_service.api.v1", "langchain")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _definitions(path: Path) -> dict[str, str]:
    """Top-level definitions (functions, classes, assignments) as AST dumps, imports excluded."""
    return {name: ast.dump(node) for node in _tree(path).body if (name := _name(node))}


def _body_without_imports(path: Path) -> list[str]:
    """Module statements except imports and the module docstring."""
    body = _tree(path).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return [ast.dump(n) for n in body if not isinstance(n, (ast.Import, ast.ImportFrom))]


def _imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    return modules


class TestVerbatimCopy:
    """Bodies in excel_engine equal the v1 originals, so v1 and v2 compute the same thing."""

    @pytest.mark.parametrize("copy_module", COPIED_FROM_TOOLS)
    def test_definitions_equal_tools_py(self, copy_module: str) -> None:
        original = _definitions(V1 / "tools.py")
        copied = _definitions(ENGINE / copy_module)
        assert copied, f"{copy_module} has no definitions"
        diverged = [name for name, dump in copied.items() if original.get(name) != dump]
        assert not diverged, f"{copy_module}: differs from tools.py (or not in it): {diverged}"

    @pytest.mark.parametrize("module", COPIED_MODULES)
    def test_engine_module_equals_original(self, module: str) -> None:
        assert _body_without_imports(ENGINE / module) == _body_without_imports(V1 / module), (
            f"excel_engine/{module} diverged from api/v1/{module}"
        )

    def test_public_api_is_the_two_calculations(self) -> None:
        names = set(_definitions(ENGINE / "calculations.py"))
        assert {"analyze_excel_model", "analyze_model_inputs_for_target"} <= names


class TestPackageBoundaries:
    """excel_engine depends on neither v1 code nor LangChain (spec §7 rule 5, AR-Q3)."""

    @pytest.mark.parametrize("module", sorted(p.name for p in ENGINE.glob("*.py")))
    def test_no_v1_or_langchain_imports(self, module: str) -> None:
        bad = sorted(m for m in _imported_modules(ENGINE / module) if m.startswith(FORBIDDEN_IMPORTS))
        assert not bad, f"excel_engine/{module} imports {bad}"
