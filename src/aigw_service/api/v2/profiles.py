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
                tool_description_overrides={
                    "task": (
                        "Делегирует задачу выбранному подагенту и возвращает его результат. "
                        "В subagent_type укажи имя подагента из списка ниже. "
                        "В description передай входные данные согласно его специализации. "
                        "Для workbook-name-resolver передавай только названия двумя списками: "
                        "Inputs: [...] и Outputs: [...], сохраняя формулировки пользователя. "
                        "Пустую категорию обозначай []. Не передавай годы, диапазоны, значения, "
                        "шаги расчётов, полный запрос или инструкции по сопоставлению. "
                        "Все новые названия текущей задачи передавай одним вызовом; "
                        "дождись результата до зависящих от него Excel-вызовов.\n\n"
                        "Доступные подагенты:\n{available_agents}"
                    ),
                },
                excluded_tools=V2_EXCLUDED_BUILTIN_TOOLS,
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            ),
        )
