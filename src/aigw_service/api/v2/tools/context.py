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

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))

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
    file_path: str
    model_index: ModelIndex
