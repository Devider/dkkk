# Proposal

## Why

При продолжении сессии после сбоев в выполнении инструментов (например, HTTP-ошибки 424 от IFT/EMA) или достижении лимита итераций оркестратора (`MAX_ORCHESTRATOR_ITERATIONS`) GigaChat возвращает ошибку 422: "every assistant function call must have a result in history". Это происходит потому, что в истории сообщений checkpointer остаются `tool_call_id` без соответствующих `ToolMessage`, что делает сессию необратимо сломанной — пользователь не может продолжить диалог без полного перезапуска.

## What Changes

- **tool_executor**: обернуть вызов `OrchestratorTools.dispatch()` в `try/except` — при любой внутренней ошибке возвращать `ToolMessage(content="Ошибка выполнения: <reason>", tool_call_id=..., name=...)` вместо пропадания сообщения
- **Новый узел `force_execute_remaining`**: добавлен в граф как альтернативный путь после `orchestrator` при достижении лимита итераций. Узел принимает все unexecuted tool_calls последнего AIMessage и закрывает их `ToolMessage`-заглушками через `batch_execute_remaining`
- **Обновлённая маршрутизация**: `route_after_orchestrator` при достижении лимита итераций возвращает `"force_execute_remaining"` вместо `"synthesizer"`

## Capabilities

### New Capabilities

- `robust-tool-execution`: гарантированное создание `ToolMessage` для каждого `tool_call_id` в истории, даже при внутренних ошибках инструментов или досрочном завершении цикла оркестратора

### Modified Capabilities

<!-- No existing capabilities are modified -- this is a new capability. -->

## Impact

| Область | Влияние |
|---------|---------|
| `main_graph.py` — `AgentGraph.tool_executor()`, `_execute_single_call()` | добавить try/except вокруг dispatch |
| `main_graph.py` — `AgentGraph.route_after_orchestrator()` | вернуть `force_execute_remaining` при достижении лимита |
| `main_graph.py` — `AgentGraph._build_graph()` | добавить узел `force_execute_remaining` и edge к `synthesizer` |
| LangGraph граф | добавление одного нового узла (минимальное изменение структуры) |
| Checkpointer / история сообщений | поведение меняется — каждый `tool_call_id` теперь гарантированно имеет результат |
| Тесты | требуется добавление unit-тестов для эмуляции ошибок dispatch и сценария iteration cap |
| `/invoke-agent` API | не изменяется |
