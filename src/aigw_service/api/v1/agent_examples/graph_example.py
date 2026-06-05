"""
EXAMPLE MODULE: graph_agent_example.py
----------------------------------
!!! ВНИМАНИЕ !!!
Это демонстрационный пример создания агента на основе графа.
Используй данный пример для разработки своего агента.
"""

import uuid
import warnings
from functools import cache

from langchain_core.runnables import RunnableConfig
from langchain_gigachat import GigaChat
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from aigw_service.context import APP_CTX

warnings.warn(
    "Это демонстрационный пример создания агента на основе графа. Не используй его в production без модификации!",
    category=UserWarning,
)

# Инициализируем модель GigaChat
llm = GigaChat(
    **APP_CTX.get_gigachat_base_params(),
    model="GigaChat-2-Max",
)


# Описываем узлы графа
async def call_model(
    state: MessagesState,
    config: RunnableConfig,
    *,
    store: BaseStore,
):
    user_id = config["configurable"]["user_id"]
    namespace = ("memories", user_id)
    memories = await store.asearch(namespace, query=str(state["messages"][-1].content))
    info = "\n".join([d.value["data"] for d in memories])
    system_prompt = (
        "Ты отличный собеседник, который может поговорить на любые темы с пользователем. Информация о пользователе:"
        f" {info}"
    )

    # Сохраняем в память, если пользователь попросит в своем сообщении
    last_message = state["messages"][-1]
    if "запомни" in last_message.content.lower():
        await store.aput(namespace, str(uuid.uuid4()), {"data": last_message.content})

    response = await llm.ainvoke([{"role": "system", "content": system_prompt}] + state["messages"])
    return {"messages": response}


# Инициализируем граф
workflow = StateGraph(MessagesState)

# Регистрируем узлы графа
workflow.add_node(call_model)

# Определяем связи между узлами (рёбра)
workflow.add_edge(START, "call_model")
workflow.add_edge("call_model", END)


@cache
def get_graph_agent() -> CompiledStateGraph:
    """Возвращает скомпилированный граф с памятью (checkpointer и store)."""
    return workflow.compile(checkpointer=APP_CTX.agent_memory.checkpointer, store=APP_CTX.agent_memory.store)
