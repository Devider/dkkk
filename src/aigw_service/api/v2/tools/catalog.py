"""Tool registry: which tools exist, which are always visible and which a skill opens (spec §10 «Каталог»)."""

from langchain_core.tools import StructuredTool

from aigw_service.api.v2.tools.adapter import BASE, ToolSpec, build_tool


class ToolCatalog:
    """Specs by name; tools are built once per catalog."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._tools: dict[str, StructuredTool] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        self._tools[spec.name] = build_tool(spec)

    def spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def tool(self, name: str) -> StructuredTool:
        return self._tools[name]

    def base_tools(self) -> list[StructuredTool]:
        """Tools the model always sees."""
        return [self._tools[n] for n, s in self._specs.items() if s.availability == BASE]

    def skill_tools(self) -> list[StructuredTool]:
        """Tools a skill opens after it is read (passed to ``SkillsMiddleware(tools=...)``)."""
        return [self._tools[n] for n, s in self._specs.items() if s.availability != BASE]

    def engine_tool_names(self) -> set[str]:
        """Tools that run a LibreOffice recalculation (``engine_gate``, calc limits)."""
        return {n for n, s in self._specs.items() if s.uses_engine}


def default_catalog() -> ToolCatalog:
    """Catalog with every tool implemented so far."""
    # pylint: disable=import-outside-toplevel
    from aigw_service.api.v2.tools.calc_tools import CALC_TOOLS
    from aigw_service.api.v2.tools.derive import DERIVE
    from aigw_service.api.v2.tools.read_tools import READ_TOOLS
    from aigw_service.api.v2.tools.resolve_terms import RESOLVE_TERMS

    catalog = ToolCatalog()
    for spec in [RESOLVE_TERMS, *READ_TOOLS, *CALC_TOOLS, DERIVE]:
        catalog.register(spec)
    return catalog
