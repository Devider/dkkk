# Tasks

## 1. Telemetry

- [x] 1.1 `api/v2/middleware/telemetry.py`: `Telemetry` с `wrap_model_call` / `awrap_model_call` — строка `LLM CALL` (`trace_id`, `turn_id`, `call` через `call_number` из `context_meter.py`, `elapsed_s`, `token_usage` из `usage_metadata`, `tool_calls`, `error`); исключение — код = имя класса, без текста, затем проброс (D1). Типизировано. Проверка: тесты — два вызова модели дают две строки с номерами 1 и 2 и одним `turn_id`; токены и имена инструментов на месте; `StopEventError` даёт строку с кодом и доходит до вызывающего; нет `usage_metadata` — пустой `token_usage`
- [x] 1.2 `Telemetry.wrap_tool_call` / `awrap_tool_call` — строка `TOOL CALL` (имя, `elapsed_s`, `status`, `code` из JSON `ToolMessage` или `Command.update["messages"]`); проходящее исключение — код класса и проброс (D1). Проверка: тесты — успешный вызов `ok`; конверт `unknown_id` → `error` + `unknown_id`; хит `CalcCache` → `ok`; текст исключения не попадает в строку
- [x] 1.3 `factory.py`: `Telemetry()` сразу после `StopEventGuard` (§8 №5). Проверка: тест порядка стека; ход без LLM (нет файла, чужой язык) не даёт строк `LLM CALL` / `TOOL CALL`

## 2. Langfuse

- [x] 2.1 `api/v1/router.py` (точечно): `langfuse_user_id = user_id` и `langfuse_tags = ["agent_engine=<v1|v2>"]` в `config["metadata"]` для обоих движков; прежние ключи остаются (D2). Проверка: тест API — у v2 и v1 в config, переданном агенту, нужные `langfuse_session_id`, `langfuse_user_id`, тег; пути к файлу в метаданных нет; `tests/integration/v1_api` на v1 без правок. (2026-10-27: `v1_api` 2 passed на `AGENT_ENGINE=v1` и `v2`, `LANGFUSE_TRACING_ENABLED=False` в `.env` на время прогона, `.env` восстановлен из копии.)
- [x] 2.2 Тест дерева хода на фейковом `BaseCallbackHandler` (собирает `run_id`, `parent_run_id`, имена, теги, метаданные) для скриптового хода v2: план → `resolve_terms` с вызовом смыслового слоя → расчёт → ответ (D3). Проверка: корневой run с метаданными и тегом; LLM-run смыслового слоя — потомок run `resolve_terms`; есть runs модели, `write_todos`, расчётного инструмента и узла `TurnSummary`. Если вложенный вызов теряет родителя — передать `config` из `runtime` в вызов смыслового слоя в обёртке v2 и повторить тест
- [x] 2.3 Тест «Langfuse выключен»: `cb_handler = None` — ход даёт тот же ответ, строки `LLM CALL` и `TOOL CALL` в журнале есть

## 3. В контуре

- [ ] 3.1 «В контуре»: ход v2 в self-hosted Langfuse. Проверка: одна трасса с `session_id`, `user_id`, тегом `agent_engine=v2`; в дереве — узлы middleware, вызовы модели с токенами, инструменты и вложенный вызов смыслового слоя

## 4. Качество

- [x] 4.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`telemetry.py`, `factory.py`, `router.py`, новые тесты, `test_api.py` и `test_plan.py` — эталон порядка стека). Проверка: новых замечаний ноль, старые не правятся попутно
- [x] 4.2 `pylint src` > 7 и `pytest tests/unit`; покрытие нового кода ≥ 80 % (AR-Q15). Проверка: оценка > 7, тесты зелёные. (2026-10-27: pylint 9.59; 756 passed, 2 failed — устаревшие `TestNoHardcodedRetry`, падают и на HEAD; `telemetry.py` 98 %.)
