# Tasks

## 1. Настройка и контекст

- [x] 1.1 `config/agent/config.py`: `turn_timeout` (`AGENT_TURN_TIMEOUT`, 300.0, `gt=0`) (D6); README — строка с назначением и верхней границей хода ≈ `AGENT_TURN_TIMEOUT` + `AGENT_CALC_TIMEOUT` + один вызов LLM. Проверка: `tests/unit/v2/test_cancellation.py::TestSettings` — значение по умолчанию, переопределение, `0` и `-1` отклоняются; `test_runtime.py::test_every_agent_variable_is_in_readme` зелёный.
- [x] 1.2 `RequestContext.cancel: asyncio.Event` (D2); `AgentV2.answer` ставит `deadline = monotonic() + turn_timeout`, если он не задан (D1). Проверка: тест в `test_cancellation.py` — после `answer` у контекста есть дедлайн в пределах `turn_timeout`; заданный заранее дедлайн не перезаписывается.

## 2. `Cancellation` и последний вызов (AR-Q10, AR-Q12, FR-8)

- [x] 2.1 Тексты `TIMEOUT_PROMPT`, `TIMEOUT_FALLBACK`, `TIMEOUT_REASON`, `CANCELLED_ANSWER` в `prompts/middleware.py` (D3, D4). Проверка: тест «русские тексты» в `test_cancellation.py` — кириллица есть, латиницы нет.
- [x] 2.2 `limits.py`: флаг `turn_timeout` и хелпер `turn_timeout(state)`; `TurnLimits` выбирает `TIMEOUT_PROMPT` / `TIMEOUT_FALLBACK` при флаге; `plan.py`: `PlanCompletionGuard` берёт `TIMEOUT_REASON` (D4). Проверка: тесты S2.5 в `test_limits.py` зелёные без изменений; новый тест — при `turn_timeout` последний запрос содержит `TIMEOUT_PROMPT`, шаги `skipped` с `TIMEOUT_REASON`.
- [x] 2.3 `api/v2/middleware/cancellation.py`: `Cancellation` — `abefore_model` (отмена → `CANCELLED_ANSWER` и `jump_to="end"`; дедлайн → `limit_hit` + `turn_timeout`), `awrap_tool_call` (дедлайн или отмена → конверт `cancelled`, обработчик не вызывается) (D3); `factory.py` — на месте №3, между `LanguageGuard` и `StopEventGuard`. Проверка: `test_cancellation.py` на `ScriptedChatModel` и `InlineGate`, дедлайн двигается подменой `time.monotonic` в модуле — сценарии «Таймаут даёт частичный ответ» (ответ с числом расчёта, последний вызов без инструментов, `cancelled` у вызова после дедлайна), «Расчёт после дедлайна не начинается», «Разрыв соединения» (флаг поднят во время расчёта: расчёт доведён, модель больше не вызвана, ответ — `CANCELLED_ANSWER`), «Идущий расчёт не прерывается», FR-8 «Истекло время хода»; порядок стека в `test_plan.py::TestFactory` и `test_api.py::test_place_in_stack` обновлён.
- [x] 2.4 Сценарий «Чекпоинт цел»: ход, отменённый флагом, и ход по таймауту с `InMemorySaver`, затем следующий ход в ту же сессию. Проверка: тест в `test_cancellation.py` — у каждого `tool_call` истории есть `ToolMessage`, второй ход отвечает обычно, `guard_counters` сброшены.

## 3. Разрыв соединения в API (AR-Q12)

- [x] 3.1 `api/v2/turn/disconnect.py`: `watch_disconnect(request, event)` — ожидание `receive()` до `http.disconnect`, затем `event.set()` (D2, уточнено задачей 3.3). Проверка: тест с поддельным `request`, у которого `receive` отдаёт два сообщения тела и затем разрыв, — флаг поднят, задача завершилась; без разрыва задача отменяется после хода и не бросает наружу.
- [x] 3.2 `api/v1/router.py`: `invoke_agent` получает `http_request: Request`; для v2 запускает наблюдатель на время `agent.answer` и отменяет его в `finally`; v1-ветка не меняется. Проверка: `test_api.py` — ход через API с поддельным наблюдателем, поднимающим флаг во время расчёта: ответ HTTP 200 с `CANCELLED_ANSWER`, блокировка снята, следующий запрос в сессию — не 409; «Ответ клиенту при таймауте» — HTTP 200, ZIP с частичным ответом; все тесты `test_api.py` и `tests/integration/v1_api` (если доступны локально) зелёные.
- [x] 3.3 Проверить на живом uvicorn, что разрыв соединения доходит до обработчика через `log_requests` (Risks): стенд — приложение FastAPI с тем же `log_requests` и наблюдателем, `curl --max-time 1`, в обеих ветках `log_requests` (с `x-trace-id` и без). Полный ход v2 локально не поднять: нет LibreOffice для загрузки книги и GigaChat для проверки здоровья на старте; ход при поднятом флаге покрыт задачей 3.2. Проверка: строки стенда приведены в отчёте; если разрыв не виден — пауза и решение с пользователем.

## 4. Без сирот `soffice` (AR-Q12)

- [x] 4.1 `api/__init__.py`: `await default_gate().shutdown()` в `lifespan` после `APP_CTX.on_shutdown()` (D5). Проверка: тест — `lifespan` с подменённым `default_gate` вызывает `shutdown` один раз; при v1 без расчётов пул не создаётся.
- [x] 4.2 Тест в `tests/unit/v2/test_engine_gate.py` с настоящим `EngineGate` и `engine_jobs.hang_with_child`: задача расчёта отменена посреди (`task.cancel()`), затем `shutdown()` — дочерний процесс и воркеры не живы. Проверка: тест зелёный, `alive(child)` ложно, `_worker_pids` пуст.

## 5. Проверки качества

- [x] 5.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` плюс новые файлы); новых замечаний ноль, старые не блокируют и попутно не правятся. Проверка: `ruff check <файлы>` без замечаний.
- [x] 5.2 `pylint src` — оценка > 7. Проверка: вывод pylint.
- [x] 5.3 `pytest tests/unit` зелёный (кроме известных вне S2.8: `TestNoHardcodedRetry` и зависимого от `.env` `test_runtime::test_defaults`); покрытие `cancellation.py` и `disconnect.py` ≥ 80 % (AR-Q15). Проверка: `pytest tests/unit --cov=src/aigw_service/api/v2 --cov-report=term-missing`.
