Story S6.1 · ADR-13, ADR-14, ADR-15 · AR-Q16 · риски §16 «AI GW буферизует SSE», «GigaChat в режиме streaming ведёт себя иначе с функциями», «`log_requests` копит тело ответа»

## Why

Эпик E6 (потоковый `/invoke-agent/stream`) опирается на допущения, которые на установленных версиях не проверены. Источник событий в ADR-14 — `agent.astream(stream_mode=["updates", "custom", "messages"])`, но неизвестно, какие узлы middleware нашего стека (20 позиций §8) появляются в обновлениях, видны ли там отклонённые ранние ответы (`AnswerGroundingGuard`, `PlanCompletionGuard` с `jump_to`) и работает ли `runtime.stream_writer` в `before_model` и `wrap_tool_call`. Ответ по токенам (ADR-15) требует, чтобы GigaChat в режиме streaming сохранял `function_call`, `functions_state_id` и `usage_metadata`, и чтобы токены основного вызова отделялись от вложенных вызовов (`resolve_terms`, сводка истории, preview-модель). По коду видно ещё два препятствия: `api/middleware.py:log_requests` собирает всё тело ответа (`[chunk async for chunk in response.body_iterator]`), а Starlette 1.6 отменяет генератор `StreamingResponse` при разрыве. До S6.2 нужно знать, как это ведёт себя на деле.

## What Changes

- Одноразовый стенд `scripts/spikes/s6_1_stream/` (не часть приложения и не `pytest`):
  - ход через `AgentFactory` со всем стеком middleware на `ScriptedChatModel` (сценарий U2, фейки движка и реестра из `tests/unit/v2`), запись всех частей `astream(stream_mode=["updates", "custom", "messages"])` в журнал;
  - прототип `ProgressReporter` на `runtime.stream_writer` в нескольких местах стека;
  - проверки GigaChat в режиме streaming с функциями (api_key; режим сертификатов — в контуре);
  - прототип FastAPI-приложения с `log_requests` и SSE-endpoint на живом uvicorn: буферизация, первое событие, разрыв соединения, ход в отдельной задаче.
- Отчёт и решения — в `design.md`: выбранный режим потока, таблица «узел / событие LangGraph → событие прогресса», решения по ADR-14 и ADR-15, место `ProgressReporter`, способ отбора токенов, правка `log_requests`.
- Правки документа спецификации по итогам: ADR-14, ADR-15, §8 №5а, §13 «Поток», §16; снять пометки «уточняет spike S6.1».

**Вне рамок:** код в `src/` (в том числе правка `log_requests` — S6.3); промышленные `StreamTranslator`, `AnswerStreamGate`, `ProgressReporter`, `AgentV2.stream()` (S6.2, S6.4, S6.6); схема событий (S6.2); endpoint и OpenAPI (S6.3); наблюдаемость и параметры `AGENT_STREAM_*` (S6.5). Проверка SSE через шлюз AI GW до фронта и режим сертификатов GigaChat — в контуре, отдельными задачами.

## Capabilities

### New Capabilities

—

### Modified Capabilities

—

Spike не меняет поведение системы (`skip_specs: true`); требования `agent-stream` добавят S6.2, S6.3, S6.4 (S6.6 снята по итогам spike: финальный ответ целиком).

## Impact

- Новый каталог `scripts/spikes/s6_1_stream/` (стенд).
- `docs/Copilot cashflow analyst v2 project spec.md` — уточнения по итогам.
- Вызовы живого GigaChat (api_key), ≈ 20–30 вызовов.
- Код приложения, зависимости и `poetry.lock` не меняются; `sse-starlette` не добавляется (кадры SSE пишутся вручную, решение — в design D5).
