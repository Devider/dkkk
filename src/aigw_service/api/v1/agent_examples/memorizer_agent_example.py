"""
EXAMPLE MODULE: memorizer_agent_example.py
----------------------------------
!!! ВНИМАНИЕ !!!
Это демонстрационный пример создания агента, работающего с векторной базой данных Platform V Search.
Используй данный пример для разработки своего агента.
"""

import warnings
from functools import cache

from langchain_core.documents import Document
from langchain_gigachat import GigaChat
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from aigw_service.context import APP_CTX

warnings.warn(
    "Это демонстрационный пример создания агента, работающего с векторной базой данных Platform V Search. Не используй"
    " его в production без модификации!",
    category=UserWarning,
)

# Инициализируем модель GigaChat
llm = GigaChat(
    **APP_CTX.get_gigachat_base_params(),
    model="GigaChat-2-Pro",
    temperature=0.000001,
    timeout=60,
)

# Формируем системный промпт
system_prompt = """Ты агент, который помогает работать с векторной базой данных. \
Пользователь может попросить тебя добавить данные в базу или найти необходимую информацию в базе.
"""


# Описываем инструменты агента
async def put_data(data: str) -> str:
    """
    Используй эту функцию для добавления текстовых данных в векторное хранилище.

    Args:
        data: Тип str. Данные в текстовом формате, которые необходимо записать в векторное хранилище.

    Returns:
        str: Id записанного документа.
    """
    documents = [Document(page_content=data)]
    result = await APP_CTX.pvs_vectorstore.aadd_documents(documents)
    return result[0]


async def search_data(query: str, k: int) -> list[str]:
    """
    Используй эту функцию для поиска данных в векторном хранилище согласно запросу пользователя.

    Args:
        query: Тип str. Текстовый запрос пользователя, по которому будет выполнен поиск данных в векторном хранилище.
        k: Тип int. Количество документов, которых необходимо вернуть пользователю.
            Если в запросе не указано количество, то возьми 1.

    Returns:
        list[str]: Список релевантых документов
    """
    documents = await APP_CTX.pvs_vectorstore.asimilarity_search(query, k)
    return [document.page_content for document in documents]


@cache
def get_memorizer_agent() -> CompiledStateGraph:
    """Возвращает агента, работающего с векторной базой данных."""
    return create_react_agent(
        model=llm,
        tools=[put_data, search_data],
        prompt=system_prompt,
    )
