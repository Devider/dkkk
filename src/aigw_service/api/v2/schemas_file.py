"""
Здесь расположены Pydantic модели для описания ответов, тел запросов, возвращаемых ошибок и т.д.
"""

from pydantic import BaseModel, Field


# =====================================================================================================================
# СХЕМЫ ОТВЕТОВ В СЛУЧАЕ ОШИБОК, СООТВЕТСТВУЮЩИЕ ТРЕБОВАНИЯМ GENAI READY API
# =====================================================================================================================
class FailedDependencyResponse(BaseModel):
    """
    Реализация текущего запроса может зависеть от успешности выполнения другой операции.
    Если она не выполнена и из-за этого нельзя выполнить текущий запрос, то сервер вернёт этот код.
    """

    error_description: str = Field(
        description="Описание возникшей ошибки",
        example="GigaChat aigw_service temporary unavailable.",
    )


class CopilotAgentRequest(BaseModel):
    """Тело запроса для роута /copilot_agent"""

    message: str = Field(
        description="Запрос пользователя",
        min_length=1,
        max_length=2000,
        example="Покажи значения debt/ebitda в 2025-2027 годах в модели, вызови инструмент get_output_info",
    )


class FileLoaderResponse(BaseModel):
    content: str = Field(
        description="Результат выполнения сохранения файла",
        min_length=4,
        max_length=2000,
        example="The file file.excel has successfully been loaded to the server.",
    )
    filename: str = Field(description="Имя сохраненного файла", min_length=4, max_length=2000, example="filename.xlsx")
    save_dir: str = Field(description="Путь сохранения файла", min_length=4, max_length=2000, example="tmp")
