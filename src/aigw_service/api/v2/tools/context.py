"""Turn context the tool adapter reads from the LangGraph runtime (spec §9 «Runtime context»)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from aigw_service.api.v2.tools.envelope import ErrorCode, ToolError


@dataclass(frozen=True)
class ModelIndex:
    """Inputs and outputs of the uploaded workbook: index ID (``IN12``, ``OUT40``) → canonical name."""

    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    input_values: Mapping[str, Mapping[int, float]] = field(default_factory=dict)
    """Input ID → {year: value}: the base the units check compares a range with (empty → check skipped)."""
    output_years: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    """Output ID → years that have a value; empty for a section header (empty mapping → checks skipped)."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))
        values = {k: MappingProxyType(dict(v)) for k, v in self.input_values.items()}
        object.__setattr__(self, "input_values", MappingProxyType(values))
        object.__setattr__(self, "output_years", MappingProxyType({k: tuple(v) for k, v in self.output_years.items()}))

    def input_name(self, input_id: str) -> str:
        """Canonical input name; ``ToolError(unknown_id)`` for an ID that is not in the index."""
        return _lookup(self.inputs, input_id)

    def output_name(self, output_id: str) -> str:
        """Canonical output name; ``ToolError(unknown_id)`` for an ID that is not in the index."""
        return _lookup(self.outputs, output_id)


def _lookup(names: Mapping[str, str], item_id: str) -> str:
    try:
        return names[item_id]
    except KeyError:
        raise ToolError(ErrorCode.UNKNOWN_ID, f"ID {item_id} нет в индексе модели") from None


@runtime_checkable
class ToolContext(Protocol):
    """What a tool needs from the turn; never part of a tool schema, so the model cannot see or forge it."""

    user_id: str
    thread_id: str
    """``"{user_id}:{session_id}"``: the engine gate runs one calculation at a time per thread."""
    file_path: str
    model_index: ModelIndex
