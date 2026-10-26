"""Skills and files of agent v2 (FR-18, AR-Q14, ADR-2, spec §8 №8–9): deepagents middleware on a read-only backend.

``/skills/`` is the skills folder of the image (``virtual_mode`` rejects ``..``), ``/results/`` is the store in the
namespace of the request's user (never taken from the path); any other path finds nothing. The only file tool is
``read_file`` — no writing, listing, search or ``execute`` (AR-Q14).
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend
from deepagents.middleware import FilesystemMiddleware, SkillsMiddleware
from langchain_core.tools import BaseTool

from aigw_service.api.v2.prompts.middleware import READ_FILE_DESCRIPTION, SKILLS_SYSTEM_PROMPT

SKILLS_ROUTE = "/skills/"
RESULTS_ROUTE = "/results/"
EVICT_TOKENS = 20000
"""Built-in offload of long tool results: only a safety net, the adapter offloads first (ADR-7, S1.6)."""


def results_namespace(runtime: Any) -> tuple[str, ...]:
    """Per user from the runtime context, so nobody reads another user's results (AR-Q7)."""
    return ("results", runtime.context.user_id)


def file_backend(skills_dir: Path, store: Any) -> CompositeBackend:
    return CompositeBackend(
        default=StateBackend(),  # nothing writes agent-state files: no write tools, so other paths are not found
        routes={
            SKILLS_ROUTE: FilesystemBackend(root_dir=skills_dir, virtual_mode=True),
            RESULTS_ROUTE: StoreBackend(namespace=results_namespace, store=store),
        },
        artifacts_root=RESULTS_ROUTE,
    )


def skills_middleware(backend: CompositeBackend, skill_tools: Sequence[BaseTool]) -> SkillsMiddleware:
    """Catalogue of skills in the prompt; ``skill_tools`` appear after the skill naming them is read (§8 №8)."""
    return SkillsMiddleware(
        backend=backend, sources=[SKILLS_ROUTE], system_prompt=SKILLS_SYSTEM_PROMPT, tools=list(skill_tools)
    )


def files_middleware(backend: CompositeBackend) -> FilesystemMiddleware:
    """``read_file`` only (§8 №9)."""
    return FilesystemMiddleware(
        backend=backend,
        tools=["read_file"],
        custom_tool_descriptions={"read_file": READ_FILE_DESCRIPTION},
        tool_token_limit_before_evict=EVICT_TOKENS,
    )
