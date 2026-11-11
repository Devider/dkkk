"""Harness profiles for the API v2 Deep Agent."""

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile

# langchain-gigachat reports ``ls_provider="giga"`` to LangChain.
V2_GIGACHAT_PROFILE_NAME = "giga"
V2_OLLAMA_PROFILE_NAME = "ollama"
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
        "write_todos",
    }
)


def register_v2_harness_profile() -> None:
    """Apply the same V2 tool policy to both supported generative providers."""
    for provider in (V2_GIGACHAT_PROFILE_NAME, V2_OLLAMA_PROFILE_NAME):
        register_harness_profile(
            provider,
            HarnessProfile(
                excluded_tools=V2_EXCLUDED_BUILTIN_TOOLS,
                general_purpose_subagent=GeneralPurposeSubagentProfile(
                    enabled=True,
                    description="Делает резолвинг упомянутых пользователем входных параметрови и выходных показателей для "
                    " по каталогу загруженной модели до расчётов. Вызывай его всегда, когда пользователь упоминает входные "
                    "параметры или выходные показатели.",
                ),
            ),
        )
