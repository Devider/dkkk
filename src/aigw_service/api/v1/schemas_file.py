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


# =====================================================================================================================
# СХЕМЫ ДЛЯ ШАБЛОННОГО РОУТА, СООТВЕТСТВУЮЩИЕ ТРЕБОВАНИЯМ GENAI READY API
# =====================================================================================================================
class ExampleGenAIReadyAPIRequest(BaseModel):
    """Пример бизнес-данных запроса"""

    request_field_1: str = Field(
        description="Описание поля request_field_1",
        max_length=30,
        pattern=r"^[0-9a-zA-Z]+$",
        example="exampleOfTheRequestField1",
    )
    request_field_2: int = Field(
        default=1,
        description="Описание поля request_field_2",
        ge=1,
        le=15,
        example=8,
    )


class ExampleGenAIReadyAPIResponse(BaseModel):
    """Пример ответа"""

    response_field_1: str = Field(
        description="Описание поля response_field_1",
        max_length=255,
        example="exampleOfTheResponseField1",
    )
    response_field_2: int = Field(
        description="Описание поля response_field_2",
        ge=1,
        le=15,
        example=3,
    )
    response_field_3: bool = Field(
        default=True,
        description="Описание поля response_field_3",
        example=True,
    )


# =====================================================================================================================
# СХЕМЫ ДЛЯ РОУТА /memorizer_agent
# =====================================================================================================================
class MemorizerAgentRequest(BaseModel):
    """Тело запроса для роута /memorizer_agent"""

    message: str = Field(
        description="Запрос пользователя",
        min_length=4,
        max_length=2000,
        example="Добавь следующую информацию в базу: LLM (Large language model) - это большая языковая модель.",
    )


class MemorizerAgentResponse(BaseModel):
    """Ответ для роута /memorizer_agent"""

    content: str = Field(
        description="Ответ от Memorizer Agent",
        max_length=2000,
        example="LLM (Large language model) - это большая языковая модель.",
    )


# =====================================================================================================================
# СХЕМЫ ДЛЯ РОУТА /greeter_agent
# =====================================================================================================================
class GreeterAgentRequest(BaseModel):
    """Тело запроса для роута /greeter_agent"""

    message: str = Field(
        description="Запрос пользователя",
        min_length=4,
        max_length=2000,
        example="Привет! Меня зовут Иванов Иван Иванович.",
    )


class GreeterAgentResponse(BaseModel):
    """Ответ для роута /greeter_agent"""

    content: str = Field(
        description="Ответ от Greeter Agent",
        max_length=2000,
        example="Привет, Иванов Иван Иванович! Рад тебя видеть!",
    )


# =====================================================================================================================
# СХЕМЫ ДЛЯ РОУТА /graph_agent
# =====================================================================================================================
class GraphAgentRequest(BaseModel):
    """Тело запроса для роута /graph_agent"""

    message: str = Field(
        description="Запрос пользователя",
        min_length=4,
        max_length=2000,
        example="Привет! Запомни информацию: в конце года поднимется ключевая ставка ЦБ.",
    )


class GraphAgentResponse(BaseModel):
    """Ответ для роута /graph_agent"""

    content: str = Field(
        description="Ответ от Greeter Agent",
        max_length=2000,
        example=(
            "Привет! Хорошо запомню твою информацию про ключевую ставку ЦБ к концу года. Если захочешь обсудить это"
            " подробнее или что-то еще — обращайся!"
        ),
    )


class CopilotAgentResponse(BaseModel):
    """Ответ для роута /copilot_agent"""

    content: str = Field(
        description="Ответ от Copilot Agent",
        max_length=10000,
        example=(
            "Привет! Я проанализировал Methanex_Finmodel.xlsx со следующими параметрами: <Параметры> и получил следующие результаты: <результаты>"
        ),
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
