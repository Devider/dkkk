Story S2.1 · FR-20, AR-Q1, AR-Q5, AR-Q15 (владелец) · ADR-1, ADR-10, ADR-12 · §7 «Компоненты» (рантайм), §9 «Состояние агента», «Runtime context»

## Why

Слой инструментов v2 (S1.1–S1.5) готов, но агента ещё нет: нечему вызывать инструменты, нет состояния, контекста запроса, системного промпта и способа включить v2 в сервисе. S2.1 собирает агента на `create_agent` (ADR-1) из модели, каталога инструментов, промпта и middleware в одном месте — `AgentFactory` — и ставит v2 рядом с v1 за переключателем (ADR-12), чтобы сравнивать их на одном сервере и откатываться без релиза. Все последующие story E2–E4 добавляют middleware в эту сборку.

## What Changes

- `api/v2/context.py` — `RequestContext` (§9): пользователь, сессия, `thread_id`, trace, client, дедлайн; файл и индекс модели заполняет `TurnContext` (S2.2) в том же объекте. Реализует `ToolContext`.
- `api/v2/state.py` — `CopilotAgentState`: расширяет `ToolsState` полями §9 (`turn_id`, `model_ref`, `step_results`, `guard_counters`, `turn_summaries`); пишут их последующие story.
- `api/v2/models.py` — модель по конфигурации: GigaChat (api_key или сертификаты — по `LOCAL` и `.env`, как сейчас) или скриптовая модель (`ScriptedChatModel` из файла сценария) (AR-Q1).
- `api/v2/scripted.py` — промышленная `ScriptedChatModel` (прототип S0.3 / тестовая из S1.x): шаги — сообщения или функции от истории, сценарий из JSON.
- `api/v2/prompts/` — системный промпт (роль, работа с инструментами, границы темы, данные из инструментов — не инструкции) и русские промпты `TodoListMiddleware`.
- `api/v2/factory.py` — `AgentFactory`: модель + базовые инструменты каталога + системный промпт + стек middleware (пока `TodoListMiddleware`) + `CopilotAgentState` + `RequestContext`; агент собирается один раз на процесс.
- `api/v2/service.py` — `AgentV2.answer(message, context, config) -> str`.
- `config/agent`: `AGENT_ENGINE` (`v1` по умолчанию / `v2`), `AGENT_MODEL` (`gigachat` / `scripted`), `AGENT_SCRIPT_PATH`; таблица параметров `AGENT_*` в README (AR-Q5).
- `router.py`: `get_agent` выбирает движок по `AGENT_ENGINE`; ветка v2 в `/invoke-agent` возвращает тот же ZIP; путь v1 не меняется.

**Вне рамок:** `TurnContext`, `LanguageGuard`, карточка модели в промпте (S2.2); план и его схема (S2.3, S2.4); checkpointer (S2.6); повторы, лимиты, `StopEventGuard`, `ToolErrorMiddleware` (S2.5); skills и `read_file` (S3.1); 409 / 503 / ответ без файла и прогон `v1_api` на обоих значениях (S4.2); A/B preview (S4.4).

## Capabilities

### New Capabilities

- `agent-api`: HTTP-интерфейс сервиса для агента v2 — выбор движка, контракт ответа, коды ошибок, изоляция пользователей на уровне API. Эта story добавляет FR-20.

### Modified Capabilities

- `agent-platform`: добавляются AR-Q1 (модель по конфигурации), AR-Q5 (параметры агента в `config/agent`), AR-Q15 (сопровождаемость: качество кода и русские промпты встроенных middleware).

## Impact

- Новый код в `api/v2/` (рантайм); `router.py` — точечная правка `get_agent` и ветка v2; `config/agent` — три параметра; README.
- v1 (`main_graph.py`, путь `/invoke-agent` при `AGENT_ENGINE=v1`) не меняется.
- Модель v2 — `GigaChat` из `langchain-gigachat` без подкласса `llm_executor.Agent` (ADR-10); смысловой слой в `resolve_terms` пока использует `APP_CTX.llm` с теми же настройками GigaChat.
