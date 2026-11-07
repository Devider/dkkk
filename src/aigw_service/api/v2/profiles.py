"""Harness profiles for the API v2 Deep Agent."""

from deepagents import HarnessProfile, register_harness_profile

# langchain-gigachat reports ``ls_provider="giga"`` to LangChain.
V2_GIGACHAT_PROFILE_NAME = "giga"
V2_EXCLUDED_BUILTIN_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "task",
        "write_todos",
    }
)


def register_v2_harness_profile() -> None:
    """Register V2's GigaChat profile without Deep Agents' standard tools."""
    register_harness_profile(
        V2_GIGACHAT_PROFILE_NAME,
        HarnessProfile(excluded_tools=V2_EXCLUDED_BUILTIN_TOOLS),
    )
