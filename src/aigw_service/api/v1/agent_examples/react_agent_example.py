"""
EXAMPLE MODULE: react_agent_example.py
----------------------------------
!!! ВНИМАНИЕ !!!
Это демонстрационный пример создания агента с помощью create_react_agent.
Используй данный пример для разработки своего агента.
"""

import warnings
from functools import cache

from langchain_gigachat import GigaChat
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from aigw_service.context import APP_CTX

warnings.warn(
    "Это демонстрационный пример создания агента с помощью create_react_agent. Не используй его в production без"
    " модификации!",
    category=UserWarning,
)

# Инициализируем модель GigaChat
llm = GigaChat(
    **APP_CTX.get_gigachat_base_params(),
    model="GigaChat-2-Pro",
)

# Формируем системный промпт
system_prompt = """Ты консьерж, который приветствует всех, кто с тобой поздоровается. \
Если человек тебе ранее представлялся, то вспомни как его зовут и поприветствуй. \
Если человек только представился, то поприветствуй его, используя его имя. \
Если человек тебе не знаком, то попроси его представиться.
"""


# Описываем инструменты агента
def greet_user(name: str) -> str:
    """
    Сформируй персонализированное приветствие для пользователя используя его имя.

    Args:
        name: Tип str. Имя пользователя, которого нужно поприветствовать

    Returns:
        str: Персонализированное приветствие
    """
    return f"Привет, {name}! Рад тебя видеть!"


@cache
def get_react_agent() -> CompiledStateGraph:
    """Возвращает реактивного агента с памятью (checkpointer)."""
    return create_react_agent(
        model=llm,
        tools=[greet_user],
        prompt=system_prompt,
        checkpointer=APP_CTX.agent_memory.checkpointer,
    )
