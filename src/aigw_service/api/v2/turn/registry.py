"""Model registry (FR-1): index and card of each uploaded workbook in the agent store, keyed by file hash.

Built once per distinct workbook — at upload when AGENT_ENGINE=v2, otherwise on the first turn — so a turn
reads the index without opening the workbook.
"""

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from aigw_service.api.v2.tools.context import ModelIndex
from aigw_service.api.v2.tools.index_builder import build_model_index
from aigw_service.api.v2.turn.card import build_card

NAMESPACE = ("model_registry",)


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dump(index: ModelIndex) -> dict[str, Any]:
    return {
        "inputs": dict(index.inputs),
        "outputs": dict(index.outputs),
        "input_values": {k: {str(y): v for y, v in vals.items()} for k, vals in index.input_values.items()},
        "output_years": {k: list(v) for k, v in index.output_years.items()},
    }


def _load(raw: dict[str, Any]) -> ModelIndex:
    return ModelIndex(
        raw["inputs"],
        raw["outputs"],
        {k: {int(y): v for y, v in vals.items()} for k, vals in raw["input_values"].items()},
        {k: tuple(v) for k, v in raw["output_years"].items()},
    )


class ModelRegistry:
    """Store-backed; one record per distinct workbook, shared by users (isolation is the per-user file path)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    async def get(self, digest: str) -> tuple[ModelIndex, dict[str, Any]] | None:
        item = await self.store.aget(NAMESPACE, digest)
        if item is None:
            return None
        return _load(item.value["index"]), item.value["card"]

    async def register(self, path: str | Path, file_name: str | None = None) -> str:
        """Hash and (only if new) build the index off the event loop; returns the file hash."""
        digest = await asyncio.to_thread(file_hash, path)
        if await self.store.aget(NAMESPACE, digest) is None:
            index = await asyncio.to_thread(build_model_index, str(path))
            card = build_card(file_name or Path(path).name, index)
            await self.store.aput(NAMESPACE, digest, {"index": _dump(index), "card": card})
        return digest
