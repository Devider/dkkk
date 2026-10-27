# Tasks

## 1. Настройки (AR-Q5)

- [x] 1.1 В `config/agent/config.py` добавить `max_model_calls` (`AGENT_MAX_MODEL_CALLS`, 40), `max_tool_calls` (`AGENT_MAX_TOOL_CALLS`, 30), `max_calcs` (`AGENT_MAX_CALCS`, 10) с `ge=1`; `ge=0` для `llm_retry_attempts` и `llm_retry_wait` (D7). Проверка: тест в `tests/unit/v2/test_limits.py::TestSettings` — значения по умолчанию, переопределение через env, `0` отклоняется `ValidationError`.
- [x] 1.2 README, таблица `AGENT_*`: три новые строки; у `AGENT_LLM_RETRY_ATTEMPTS` / `AGENT_LLM_RETRY_WAIT` — «v2: повторы модели агента и 429 смыслового слоя» (v1 их не читает). Проверка: каждое поле `AgentSettings` есть в таблице (существующий тест документации параметров, если он есть, иначе — просмотр).

## 2. Лимиты хода (FR-9, FR-8)

- [x] 2.1 Тексты `LIMIT_PROMPT`, `LIMIT_FALLBACK`, `LIMIT_REASON` в `prompts/middleware.py` на русском (D1, D2). Проверка: тест «русские промпты» в `test_runtime.py` / `test_limits.py` — в текстах нет латиницы, кроме имён инструментов.
- [x] 2.2 `api/v2/middleware/limits.py`: `TurnLimits` — счёт в `guard_counters` (`model_calls`, `tool_calls`, `calc_calls`, `limit_hit`), `before_model` для лимита модели, `after_model` блокирует вызовы сверх лимитов конвертом `limit_exceeded`, `wrap_model_call`/`awrap_model_call` при `limit_hit` убирает инструменты, добавляет `LIMIT_PROMPT` в запрос и вырезает `tool_calls` из ответа (D1). Проверка: `tests/unit/v2/test_limits.py` на `ScriptedChatModel` и `InlineGate` — сценарии «Вызов сверх лимита расчётов» (движок запущен `max_calcs` раз), «Лимит вызовов инструментов», «Лимит вызовов модели» (`len(model.calls) <= max_model_calls`), «Последний вызов без инструментов» (`model.calls[-1]["tools"] == []`, вызов из финального ответа не выполнен), «Счётчики не копятся между ходами» (два хода с одним `MemorySaver`).
- [x] 2.3 `middleware/plan.py`: `PlanCompletionGuard` при `limit_hit` сразу принимает ответ с «Не выполнено» и `skipped`/`LIMIT_REASON`; `ForcePlanFirst` при `limit_hit` пропускает запрос без сужения и без повтора с `tool_choice=write_todos` (D2). Проверка: в `test_limits.py::TestPartialAnswer` сценарий «Исчерпан лимит хода» (нет напоминания, шаги `skipped` с причиной) и в режиме `always` финальный вызов — один вызов модели.
- [x] 2.4 Сценарий «Зацикливание останавливается частичным ответом»: скриптовая модель — план, `resolve_terms`, `run_scenarios`, затем бесконечные повторы вызова; ответ финального вызова — функция от промпта, возвращающая текст с числом из результата расчёта. Проверка: тест в `test_limits.py` — финальный ответ содержит число из `numbers` расчёта и раздел «Не выполнено», в ответе нет английского текста о лимитах.

## 3. Ошибки и повторы (FR-7, AR-Q11)

- [x] 3.1 Проверить допущение из Risks: исключение из инструмента в `create_agent` доходит до `wrap_tool_call` (`ToolNode` его не перехватывает). Проверка: тест в `tests/unit/v2/test_resilience.py` с инструментом, бросающим `ZeroDivisionError`, и записывающим middleware; при провале — пересмотреть D4 до продолжения.
- [x] 3.2 `envelope.py`: `TRANSIENT_CODES = {ErrorCode.ENGINE_UNAVAILABLE}`; `adapter.py`: конверт с временным кодом (пойманный `ToolError` или возвращённый `ToolFailure`) поднимается как `ToolError` (D4). Проверка: `test_tool_adapter.py` — `engine_unavailable` из `classify_v1` и из `EngineGate` поднимается исключением, остальные коды по-прежнему возвращаются конвертом.
- [x] 3.3 `stop_event.py`: выделить чистый предикат `is_stop_event(exc)` без логирования, `stop_event()` использует его. Проверка: `test_api.py` (StopEvent → 503) и `tests/unit/test_stop_event.py` зелёные (кроме двух устаревших `TestNoHardcodedRetry`, падающих и до S2.5 — см. `fix-context-undefined-imports`).
- [x] 3.4 `api/v2/middleware/resilience.py`: `is_transient_llm_error`, `is_transient_tool_error`, `tool_error_content`, фабрики `model_retry()`, `tool_errors()`, `engine_retry(engine_tools)` (D3, D4). Проверка: `test_resilience.py` — предикат на `ServerError`, `RateLimitError`, `httpx.ConnectError`/`ReadTimeout` → да, на `ForbiddenError`, `StopEventError`, `BadRequestError`, `AuthenticationError` → нет; `tool_error_content` на StopEvent → `None`, на `ZeroDivisionError` → конверт `internal_error` без текста исключения.
- [x] 3.5 Сценарии AR-Q11 на агенте (паузы обнулены через `AGENT_LLM_RETRY_WAIT=0` / подмену задержки): «Временный сбой» (модель падает `ServerError` дважды, затем отвечает), «429», «Повторы исчерпаны» (ход падает, текст исключения не в ответе), «Неповторяемая ошибка» (один вызов), «StopEvent при вызове модели» (один вызов, `StopEventError`), «StopEvent внутри инструмента» (модель не получает конверт), «Сбой движка один раз/дважды» (`InlineGate.error` на один/все запуски; запусков 2), «Ошибка модели не повторяется» (`engine_timeout` — один запуск), FR-7 «Исключение в ходе агента» (ход продолжается, `internal_error`). Проверка: `test_resilience.py` зелёный; паузы растут вдвое — проверено подменой `asyncio.sleep`/`time.sleep` в модуле повтора langchain.

## 4. `CalcCache` (FR-17)

- [x] 4.1 `api/v2/middleware/calc_cache.py`: `CalcCache(engine_tools, store)` — namespace `("calc_cache", thread_id)`, ключ из хэша файла, инструмента и нормализованных аргументов, сохраняются только успешные `ToolMessage`, попадание — новый `ToolMessage` с текущим `tool_call_id` и строкой журнала `CalcCache hit` (D5). Проверка: `tests/unit/v2/test_calc_cache.py` — «Второй идентичный вызов» (`len(inline_gate.sessions) == 1`, контент одинаковый), «Порядок ключей и значения по умолчанию», «Другие аргументы», «Другая книга или другая сессия», «Ошибка не кэшируется», попадание в следующем ходе той же сессии.

## 5. Сборка стека

- [x] 5.1 `factory.py`: порядок D6 — `TurnLimits`, `model_retry()`, `CalcCache`, `tool_errors()`, `engine_retry()` после `ForcePlanFirst` и до `extra`/`PlanCompletionGuard`; лимиты и повторы — из `APP_CONFIG.agent`, инструменты движка — `catalog.engine_tool_names()`, store — тот же, что у `TurnContext`. Проверка: обновлённые `test_plan.py::TestFactory::test_order_and_mode_from_settings` и проверки порядка в `test_api.py`, `test_turn_context.py`, `test_memory.py`; `StopEventGuard` по-прежнему снаружи `ModelRetryMiddleware` и `ToolErrorMiddleware`.
- [x] 5.2 Полный ход на скриптовой модели через `AgentV2.answer` и роутер (`httpx.AsyncClient(app=app_main)`, `AGENT_ENGINE=v2`): зацикливание даёт HTTP 200 с частичным ответом. Проверка: тест в `test_api.py`.

## 6. Проверки качества

- [x] 6.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` плюс новые файлы); новых замечаний ноль, старые не блокируют и попутно не правятся. Проверка: `ruff check <файлы>` без новых замечаний.
- [x] 6.2 `pylint src` — оценка > 7. Проверка: вывод pylint.
- [x] 6.3 `pytest tests/unit` зелёный, включая `tests/unit/test_stop_event.py`; покрытие новых модулей `limits.py`, `resilience.py`, `calc_cache.py` ≥ 80 % (AR-Q15). Проверка: `pytest tests/unit --cov=src/aigw_service/api/v2/middleware --cov-report=term-missing`.
