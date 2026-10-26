"""Tracing stays inside the contour (AR-Q14, spec §13): LangSmith must be off for the whole service."""

import os
from collections.abc import Mapping

LANGSMITH_FLAGS = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")
_ON = {"true", "1", "yes", "on"}


def forbid_langsmith(environ: Mapping[str, str] | None = None) -> None:
    """Raise before start-up when any LangSmith tracing switch is on."""
    env = os.environ if environ is None else environ
    enabled = [name for name in LANGSMITH_FLAGS if env.get(name, "").strip().lower() in _ON]
    if enabled:
        raise RuntimeError(
            f"LangSmith запрещён: трейсинг только во внутренний контур (Langfuse). Выключите {', '.join(enabled)}."
        )
