"""
Здесь можно организовать бизнес-логику, специфичную для конкретного сервиса.
"""

from collections.abc import Sequence
from typing import Annotated, Any, Optional, TypedDict

from dotenv import load_dotenv

load_dotenv()

try:
    from langchain_gigachat import GigaChat
except ImportError:  # pragma: no cover - optional dependency for tests
    GigaChat = None
import json
import pprint
from datetime import datetime

# from langchain.schema import HumanMessage, SystemMessage, AIMessage
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph, add_messages

from aigw_service.context import APP_CTX

from .tools import TOOLS


class AgentState(TypedDict):
    """Состояние агента."""

    # messages: List[Dict[str, Any]]
    messages: Annotated[Sequence[BaseMessage], add_messages]
    thread_id: Optional[str]
    user_id: Optional[str]
    generated_graphs: list[dict[str, Any]]  # Store generated graphs
    log_messages: list[dict[str, Any]]  # Store log messages
    summary: str
    scratchpad: list[str]
    last_token_usage: dict[str, int]


class Agent:
    def __init__(
        self,
        api_key: Optional[str] = None,
        thread_id: Optional[str] = None,
        logger: Optional[Any] = None,
        # store: BaseStore
    ):
        """Инициализация агента."""
        try:
            self.logger = logger
            self.memory = None
            # Initialize state first
            self.state = self._reset_state()
            # Now we can start logging

            self.checkpointer = APP_CTX.agent_memory.checkpointer
            self.store = APP_CTX.agent_memory.store

            # self.logger.info("Initializing agent...")
            # Initialize LLM based on model type
            self.llm = APP_CTX.create_llm(
                model_name="GigaChat-2-Pro-preview",
                temperature=0.000001,
                timeout=60,
            )
            # Initialize tools

            if TOOLS is not None:
                # self.logger.info(f"Available TOOLS: {TOOLS}")
                self.tools = TOOLS
            else:
                self.logger.warning("TOOLS is None")
                self.tools = []

            # Store system message with explicit format requirements
            self.system_message = SystemMessage(
                content="""
                Ты - ИИ-ассистент финансовый аналитик, обладающий инструментами для поиска информации в интернете и базе знаний о компаниях.
                Отвечай на русском языке.

                Твоя задача: 
                - Использовать только точные данные при проведение расчетов и формировании ответов.
                - Предоставлять точные и понятные ответы на вопросы пользователя.
                - Стоимость твоих ошибок - очень высокая, поэтому ты должен быть очень внимательным и аккуратным.

                СТРОГО следуй следующим правилам:

                1. Примеры информации, требующей ОБЯЗАТЕЛЬНОЙ проверки через инструменты перед использованием:
                   - Дата, время, день недели
                   - Погода, температура
                   - Курсы валют, цены товаров и услуг
                   - Новости, события
                   - ИНН компании

                3. Если запрос содержит несколько вопросов:
                   - Отвечай на каждый вопрос отдельно
                   - Для каждого вопроса используй соответствующие инструменты
                   - Проверяй результаты каждого инструмента
                   - Если необходимо использовать дополнительные инструменты - используй их

                4. Если не можешь получить информацию через инструмент:
                   - Если не хватает параметров для вызова функции - попробуй другие параметры
                   - Не пытайся угадать или использовать данные не из инструментов

                5. Всегда проверяй достоверность информации:
                   - Если информация противоречива - укажи это
                   - Если не уверен в точности - укажи это

                6. При работе с путями к файлам:
                   - НЕ форматируй пути как ссылки для скачивания
                   - Просто указывай путь к файлу как есть
                   - Не добавляй никаких дополнительных форматирований или HTML-тегов

                 7. ВЫБОР ИНСТРУМЕНТА ДЛЯ РАБОТЫ С EXCEL МОДЕЛЬЮ. Строгие правила:

                   a) **ЗАПРЕЩАЕТСЯ** разбивать запрос с несколькими входными
                      или выходными параметрами на отдельные вызовы
                      `analyze_excel_model`. Инструмент принимает ВСЕ входы
                      и ВСЕ выходы в одном вызове. Если пользователь указал
                      два входа и три выхода — сделай ОДИН вызов с
                      input_names из двух элементов и output_names из трёх.
                      Любое разбиение на несколько вызовов — ошибка.

                      **НЕПРАВИЛЬНО** (3 отдельных вызова вместо одного):
                      → analyze_excel_model(
                            input_names=["цена метанола"],
                            output_names=["debt/ebitda"], ...)
                      → analyze_excel_model(
                            input_names=["цена метанола"],
                            output_names=["net debt/ebitda"], ...)
                      → analyze_excel_model(
                            input_names=["инфляция USD CPI"],
                            output_names=["icr corr"], ...)

                      **ПРАВИЛЬНО** (один вызов):
                      → analyze_excel_model(
                            input_names=["цена метанола",
                                         "инфляция USD CPI"],
                            output_names=["debt/ebitda",
                                          "net debt/ebitda",
                                          "icr corr"],
                            output_years=[2025, 2025, 2025],
                            ranges=[[450, 500], [0.1, 0.2]],
                            steps=[5, 0.1]
                        )

                   b) Если пользователь просит «что-если», «проанализировать»,
                      «подобрать», «варьировать X и смотреть Y» — используй
                      ТОЛЬКО `analyze_excel_model`. Этот инструмент сам найдёт
                      ячейки на Inputs и Outputs, пересчитает модель и вернёт
                      таблицу со всеми сценариями.

                      ВАЖНО: количество элементов в output_years должно
                      строго соответствовать количеству элементов в
                      output_names — по одному году на каждый выходной
                      параметр.

                      Пример (один выход):
                      Запрос: «Проанализируй models.xlsx со значением метанола
                      2025 (450,500) и шагом 5. Покажи debt/ebitda 2025»
                      → analyze_excel_model(
                            input_names=["цена метанола"],
                            output_names=["debt/ebitda"],
                            output_years=[2025],
                            ranges=[[450, 500]],
                            steps=[5]
                        )

                   c) `get_output_info` используй ТОЛЬКО для чтения значений
                      выходных показателей на листе Outputs без изменения
                      входных данных.
                      Пример: «Покажи debt/ebitda за 2025-2027»

                   d) НЕ используй `get_output_info` для поиска входных
                      параметров. Он ищет ТОЛЬКО по листу Outputs. Входные
                      параметры (Inputs) через него не найти.

                   e) Если сомневаешься — вызывай `analyze_excel_model`.

                СТРОГИЕ Правила формирования ответа, обязательно неукоснительно следуй им:
                    - Вы НЕ должны отвечать на вопросы, требующие актуальной или фактической информации, без использования инструментов.
                    - Если есть конкретный инструмент для получения информации - обязательно используй его.
                    - Не отвечай, что не нашел информацию, пока не попробуешь все инструменты. 
                    - Не останавливайся на промежуточных шагах.
                    - При формировании финального ответа добавь объяснение, какую информацию ты использовал и какими методами ты ее получил.
                    - Если из контекста непонятно, какая именно информация нужна пользователю - выдай несколько вариантов ответов с пояснением.
                    - Твой ответ должен быть информативным и понятным.
                    - Если инструмент вернул таблицу со сценариями — не разбивай её на несколько таблиц и не сокращай строки. Выводи данные в точности так, как их вернул инструмент: все строки и все столбцы в одной таблице.
                """
            )
            # Bind tools to the LLM with provider-specific handling
            self._bind_tools_to_llm()
            # Create the workflow graph
            self._create_graph()
            self.logger.info("Agent initialized successfully")
        except Exception as e:
            # If we haven't initialized state yet, use logger directly
            if not hasattr(self, "state"):
                logger.info(f"Error initializing Agent: {str(e)}")
            else:
                self.logger.info(f"Error initializing Agent: {str(e)}")
            raise Exception(f"Ошибка инициализации агента: {str(e)}")

    def _bind_tools_to_llm(self) -> None:
        """Bind configured tools to the current LLM instance."""
        if not getattr(self, "tools", None):
            self.logger.warning("No tools available to bind")
            return

        try:
            if hasattr(self.llm, "bind_functions"):
                self.llm = self.llm.bind_functions(self.tools)
                self.logger.info("Successfully bound tools to LLM")
            elif hasattr(self.llm, "bind_tools"):
                self.llm = self.llm.bind_tools(self.tools)
                self.logger.info("Successfully bound tools to LLM via bind_tools")
            else:
                self.logger.error("LLM instance does not support bind_functions or bind_tools")
                raise AttributeError("LLM instance does not support bind_functions or bind_tools")

        except Exception as e:
            self.logger.error(f"Error binding tools: {str(e)}")
            raise

    def _reset_state(self) -> AgentState:
        """Сброс состояния агента."""
        return {
            "messages": [],
            "thread_id": None,
            "user_id": None,
            "generated_graphs": [],  # Initialize empty list for graphs
            "log_messages": [],  # Initialize empty list for log messages
            "summary": "",
            "scratchpad": [],
            "last_token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _estimate_tokens(self, messages: list[Any]) -> int:
        """Simple token estimation based on message lengths."""
        return sum(len(getattr(m, "content", str(m))) for m in messages)

    def summarize_history(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
        """Суммировать историю сообщений."""
        state.setdefault("scratchpad", [])
        self.logger.info("Summarizing conversation history...")
        messages = state["messages"]
        if not messages:
            return state

        summary_prompt = messages + [HumanMessage(content="Кратко суммируй предыдущий диалог в 1-2 предложениях")]
        response = self.llm.invoke(summary_prompt, config=config)
        summary = getattr(response, "content", "")
        state["summary"] = summary
        state["scratchpad"].append(summary)
        # Keep only the last three conversation turns (approx. six messages)
        state["messages"] = state["messages"][-6:]
        return state

    def _create_graph(self) -> None:
        """Создание графа агента."""
        try:
            # self.logger.info("Creating agent workflow graph...", "info")
            # self.logger.info("Creating agent workflow graph...")
            # Create the graph
            workflow = StateGraph(AgentState)

            # Add nodes
            workflow.add_node("init_node", self.init_node)
            workflow.add_node("analyze_step", self.analyze_step)
            workflow.add_node("execute_tool", self.execute_tool)
            # workflow.add_node("summarize_history", self.summarize_history)

            # Decide next action
            def next_action(state: AgentState) -> str:
                if not state["messages"]:
                    return END

                # if len(state["messages"]) > 20 or self._estimate_tokens(state["messages"]) > 8000:
                #     self.logger.info(f"length of messages is {len(state['messages'])}")
                #     self.logger.info(f"tokens of messages is {self._estimate_tokens(state['messages'])}")
                #     return "summarize_history"

                last_message = state["messages"][-1]
                if hasattr(last_message, "additional_kwargs") and "tool_calls" in last_message.additional_kwargs:
                    tool_calls = last_message.additional_kwargs["tool_calls"]
                    if tool_calls:
                        return "execute_tool"
                elif last_message.tool_calls:
                    tool_calls = last_message.tool_calls
                    if tool_calls:
                        self.logger.info("tools calls found.")
                        return "execute_tool"
                else:
                    self.logger.info("no tool calls found.")
                return END

            workflow.add_conditional_edges(
                "analyze_step",
                next_action,
                {
                    "execute_tool": "execute_tool",
                    # "summarize_history": "summarize_history",
                    END: END,
                },
            )

            # Return to analyze_step after tool execution or summarization
            workflow.add_edge("execute_tool", "analyze_step")
            # workflow.add_edge("summarize_history", "analyze_step")
            workflow.add_edge("init_node", "analyze_step")
            # Set entry point
            workflow.set_entry_point("init_node")

            self.graph = workflow.compile(checkpointer=self.checkpointer, store=self.store)

            self.logger.info("Agent workflow graph created successfully")
        except Exception as e:
            self.logger.info(f"Error creating agent workflow graph: {str(e)}")
            raise

    def init_node(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
        """Initialize the agent state."""
        # self.logger.info("inside init node")
        # self.logger.info(f"inside init node config is the following: {config}")

        thread_id = None
        if config and "configurable" in config:
            thread_id = config["configurable"].get("thread_id")
        user_id = None
        if config and "configurable" in config:
            user_id = config["configurable"].get("user_id")

        state["thread_id"] = thread_id
        state["user_id"] = user_id

        self.logger.info(f"state:\n{pprint.pformat(self._summarize_state(state), width=100)}")
        return state

    @staticmethod
    def _summarize_state(state: AgentState) -> dict:
        msgs = state.get("messages", [])
        return {
            "messages": [
                {
                    "type": type(m).__name__,
                    "content": str(m.content)[:200] if m.content else "",
                    "n_tool_calls": len(getattr(m, "tool_calls", [])),
                }
                for m in msgs
            ]
        }

    def analyze_step(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
        """Analyze the current state and determine next action."""
        self.logger.info(f"inside analyze step:\n{pprint.pformat(self._summarize_state(state), width=100)}")

        thread_id = None
        if config and "configurable" in config:
            thread_id = config["configurable"].get("thread_id")

        user_id = None
        if config and "configurable" in config:
            user_id = config["configurable"].get("user_id")

        state["thread_id"] = thread_id
        state["user_id"] = user_id
        try:
            state["thread_id"] = thread_id
            state["user_id"] = user_id
            messages = state["messages"]
            if not messages:  # Safety check
                return state

            # Avoid repeated analysis if last message is from assistant
            if isinstance(messages[-1], AIMessage):
                return state

            # self.logger.info("=== Analyzing Step ===")
            # self.logger.info("Анализирую запрос и определяю необходимые действия...")

            # Create messages list with system message first
            llm_messages = [self.system_message]

            # Add scratchpad summaries then recent conversation
            # llm_messages.extend(SystemMessage(content=s) for s in state["scratchpad"])
            llm_messages.extend(messages)

            # Single LLM invocation with proper message format
            # self.logger.info("Invoking LLM...")

            try:
                # Add debug logging for messages
                # self._log(f"Messages being sent to LLM: {[msg.content for msg in llm_messages]}", "debug")

                response = self.llm.invoke(llm_messages, config=config)
                token_usage = response.response_metadata.get("token_usage", {})
                # self.logger.info(f"""
                #     Prompt tokens: {token_usage.get('prompt_tokens')}
                #     Completion tokens: {token_usage.get('completion_tokens')}
                #     Total tokens: {token_usage.get('total_tokens')}
                # """)

                state["last_token_usage"] = {
                    "prompt_tokens": token_usage.get("prompt_tokens", 0),
                    "completion_tokens": token_usage.get("completion_tokens", 0),
                    "total_tokens": token_usage.get("total_tokens", 0),
                }

                # Add debug logging for response
                self.logger.debug(f"Raw LLM Response: {response}")

                if not hasattr(response, "content"):
                    self.logger.error("LLM response missing 'content' attribute")
                    state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
                    return state

                self.logger.info(f"Step Analysis: {response.content}")

                # Add the response to messages
                state["messages"].append(response)

                # Check for tool calls
                if hasattr(response, "additional_kwargs") and "tool_calls" in response.additional_kwargs:
                    tool_calls = response.additional_kwargs["tool_calls"]
                    if tool_calls and isinstance(tool_calls, list) and len(tool_calls) > 0:
                        # Safely get tool name
                        tool_call = tool_calls[0]["function"]
                        if isinstance(tool_call, dict) and "name" in tool_call:
                            tool_name = tool_call["name"]
                        else:
                            tool_name = "Unknown Tool"

                        # Add thinking step for tool selection
                        self.logger.debug(f"Выбран инструмент {tool_name}.")
                        return state

                # If no tool calls, check if we need to continue
                if "?" in state["messages"][-2].content:  # Check if last user message had questions
                    # If we haven't used tools and there were questions, try again
                    if not any(
                        hasattr(msg, "additional_kwargs") and "tool_calls" in msg.additional_kwargs
                        for msg in state["messages"]
                    ):
                        return state

                # If no tool calls and no unanswered questions, we're done
                return state

            except Exception as e:
                self.logger.info(f"Error during LLM invocation: {str(e)}")
                self.logger.info(f"Error type: {type(e)}")
                self.logger.info(f"Error details: {e.__dict__ if hasattr(e, '__dict__') else 'No details available'}")
                state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
                return state

        except Exception as e:
            self.logger.info(f"Error in analyze_step: {str(e)}")
            self.logger.info(f"Error type: {type(e)}")
            self.logger.info(f"Error details: {e.__dict__ if hasattr(e, '__dict__') else 'No details available'}")
            state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
            return state

    def execute_tool(self, state: AgentState) -> AgentState:
        """Execute a tool and return the result."""
        # Get the last message which should contain tool calls

        self.logger.info(f"inside execute tool agent state is the following: {state}")
        if not state["messages"]:
            self.logger.info("No messages found", "error")
            state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
            return state

        last_message = state["messages"][-1]

        # Check if the last message has tool calls
        if (
            not (hasattr(last_message, "additional_kwargs") and "tool_calls" in last_message.additional_kwargs)
            and not last_message.tool_calls
        ):
            self.logger.info("No tool calls found in last message")
            state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
            return state
        tool_calls = last_message.tool_calls
        if not tool_calls:
            self.logger.info("No tool calls found in last message", "error")
            state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
            return state
        # Execute each tool call
        for tool_call in tool_calls:
            try:
                # Get tool name and arguments from the tool call
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]

                if not tool_name:
                    self.logger.info("No tool name found in tool call")
                    continue

                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except Exception:
                        self.logger.info(f"Invalid JSON args: {tool_args}")
                        tool_args = {}

                # Find the tool
                tool_found = False

                for tool in self.tools:
                    # self.logger.info(f'tool: {tool}')
                    # self.logger.info(f'TOOL_NAME: {tool_name}')
                    # self.logger.info(f'TOOL_NAME.name: {tool.name}')
                    if tool.name == tool_name:
                        # self.logger.info(f"Executing tool: {tool_name} with args: {tool_args}")
                        # self.logger.info(f"tool_args in tool {tool_name}: {tool_args}")
                        # self.logger.info(f"Выполняю инструмент {tool_name}", "tool", tool=tool_name, tool_input=tool_args)

                        # tool_args_with_thread = tool_args.copy.deepcopy() if tool_args else {}
                        import copy

                        tool_args_with_thread = copy.deepcopy(tool_args) if tool_args else {}

                        # tool_args_with_thread['thread_id'] = state.get("thread_id")
                        # tool_args_with_thread['user_id'] = state.get("user_id")
                        if state.get("thread_id"):
                            tool_args_with_thread["thread_id"] = state.get("thread_id")
                        if state.get("user_id"):
                            tool_args_with_thread["user_id"] = state.get("user_id")

                        self.logger.info(f"TOOL ARGS: {tool_args_with_thread}")

                        try:
                            # Execute the tool with the provided arguments
                            # tool_result = tool.invoke(tool_args, thread_id = state.get("thread_id"))
                            tool_result = tool.invoke(tool_args_with_thread)

                            # Add tool result to log
                            self.logger.info(f"Результат выполнения {tool_name}, tool_output={tool_result.result}")

                            # Check if the tool generated a graph
                            if hasattr(tool_result, "image_path"):
                                # Store the graph in state
                                if tool_result.image_path:
                                    state["generated_graphs"].append(
                                        {
                                            "path": tool_result.image_path,
                                            "description": tool_result.content,
                                            "timestamp": datetime.now().isoformat(),
                                        }
                                    )
                                    content = tool_result.content + f"\n\nГрафик: {tool_result.image_path}"
                            else:
                                # Use .result for tools where .content is a dict (not LLM-readable)
                                if isinstance(tool_result.content, dict):
                                    content = tool_result.result
                                else:
                                    content = tool_result.content

                            # Add tool result to messages with the corresponding tool_call_id
                            state["messages"].append(
                                ToolMessage(
                                    content=content,
                                    name=tool_name,
                                    tool_call_id=tool_call.get("id"),
                                )
                            )

                            tool_found = True
                            break
                        except Exception as e:
                            self.logger.info(f"Error executing tool {tool_name}: {str(e)}")
                            state["messages"].append(
                                ToolMessage(
                                    content=f"Ошибка при выполнении {tool_name}: {str(e)}",
                                    name=tool_name,
                                    tool_call_id=tool_call.get("id"),
                                )
                            )
                            tool_found = True
                            break

                if not tool_found:
                    self.logger.info(f"Tool {tool_name} not found")
                    state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
                    return state

            except Exception as e:
                self.logger.info(f"Error processing tool call: {str(e)}")
                state["messages"].append(AIMessage(content="Извините, произошла ошибка при обработке запроса."))
                return state

        return state

    # async def process_message(self, message: str, config: Optional[RunnableConfig] = None) -> str:
    async def process_message(self, message: dict[str, Any], config: Optional[RunnableConfig] = None) -> str:
        """Обработка сообщения пользователя."""

        # self.logger.info(f"Message:\n {message}")

        thread_id = config.get("configurable", {}).get("thread_id") if config else None
        user_id = config.get("configurable", {}).get("user_id") if config else None

        user_message = message["messages"][0]["content"]

        try:
            # self.logger.info(f"\n=== New Message ===\nUser: {user_message}")

            user_message = message["messages"][0]["content"]
            # self.state["messages"].append(HumanMessage(content=user_message))
            # self.logger.info(f"State:\n {self.state}")

            input_data = {"messages": [HumanMessage(content=user_message)]}

            # self.logger.info(f'Input_Data: {input_data},\n thread_id: {config['configurable']['thread_id']}, \n user_id {config['configurable']['user_id']}')
            result = await self.graph.ainvoke(
                input_data,
                config={
                    "configurable": {
                        "thread_id": config["configurable"]["thread_id"],
                        "user_id": config["configurable"]["user_id"],
                    }
                },
            )

            # result = await self.graph.ainvoke(input_data, config=config)

            # Get the response from the last message
            response = None
            if result["messages"]:
                last_message = result["messages"][-1]
                if isinstance(last_message, dict) and "content" in last_message:
                    response = last_message.content  # Из каких элементов формируется content?
                elif hasattr(last_message, "content"):
                    response = last_message.content

            if not response:
                response = "Извините, не удалось сформировать ответ."

            # Update state with the result
            # self.state = result

            # return response
            return result

        except Exception as e:
            self.logger.info(f"\nError: {str(e)}")
            return "Извините, произошла ошибка при обработке запроса."

        # def save_graph(self):
        #     from IPython.display import Image, display
        #     save_path = "graph.png"
        #     with open(save_path, "wb") as f:
        #         f.write(self.graph.get_graph(xray=True).draw_mermaid_png())
        #     logger.info(f"Graph saved to {save_path}")
