Story S4.2 · FR-15 (владелец), FR-20 (сценарий интеграционных тестов), AR-Q7 (проверка на уровне API) · ADR-12 · §12 п. 3 (блокировка хода), §13 «Контракт API» · AR-Q11 (StopEvent → 503)

## Why

Агент v2 уже отвечает через `/invoke-agent` (S2.1–S3.2), но контракт API для него не доведён: второй запрос в занятую сессию перемешает план и историю в чекпоинте (§12 п. 3 требует сразу HTTP 409), а StopEvent GigaChat в v2 уходит наружу как `ForbiddenError` и даёт 424 вместо 503 — обёртка v1 к модели v2 не применяется. FR-15 требует неизменного ZIP-ответа и прохождения `tests/integration/v1_api`, FR-20 — на обоих значениях `AGENT_ENGINE`. Это последняя story минимального сквозного среза.

## What Changes

- **Минимальная блокировка хода** (решение разработчика: из S2.8 в S4.2 переносится только она): `TurnLocks` в `api/v2/turn/lock.py` — неблокирующий захват по `thread_id` в памяти процесса; занятая сессия — `TurnBusyError`; `AgentV2.answer` держит блокировку на весь ход; `router.py` отвечает HTTP 409 с просьбой направить вопрос позже.
- **Минимальный `StopEventGuard`** (из S2.5 переносится только распознавание StopEvent): middleware `api/v2/middleware/stop_event.py` в `wrap_model_call` и `wrap_tool_call` превращает `ForbiddenError` GigaChat с маркером «temporarily unavailable due to technical reasons» в `StopEventError`; `router.py` уже отвечает на неё 503. Стоит в стеке сразу после `LanguageGuard` (§8 №4).
- Тесты API v2 через ASGI: ZIP того же состава, ответ без загруженного файла (200, «загрузите модель», без LLM), 409 на параллельный запрос в ту же сессию и 200 в другую, 503 на StopEvent модели и смыслового слоя, изоляция двух пользователей с одинаковым `x-session-id` (AR-Q7).
- Прогон `tests/integration/v1_api` при `AGENT_ENGINE=v1` и `AGENT_ENGINE=v2`.

**Вне рамок:** дедлайн хода, `Cancellation` и частичный ответ по таймауту (S2.8); повторы `ModelRetryMiddleware` и место `StopEventGuard` относительно них (S2.5); распределённая блокировка для нескольких процессов (§12 п. 7, вне рамок проекта); bugfix StopEvent в v1 (`fix-v1-stop-event-503`); вынос результатов и изоляция `/results/` (S1.6 — владелец AR-Q7; здесь только проверка истории на уровне API).

## Capabilities

### New Capabilities

—

### Modified Capabilities

- `agent-api`: добавляется FR-15 (контракт ответа API, коды 409 / 503, ответ без файла) — владелец S4.2; FR-20 получает сценарий «интеграционные тесты на обоих значениях `AGENT_ENGINE`».

## Impact

- Новые `api/v2/turn/lock.py`, `api/v2/middleware/stop_event.py`; `service.py` — блокировка хода; `factory.py` — `StopEventGuard`; `api/v1/router.py` — ответ 409 на `TurnBusyError` (точечная правка, ветка v1 не меняется).
- Тесты: `tests/unit/v2/test_api.py`; `tests/integration/v1_api` без правок.
- Блокировка живёт в памяти процесса — как и `MemorySaver`: один процесс uvicorn (§12 п. 7).
