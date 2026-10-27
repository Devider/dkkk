Story S4.3 · AR-Q13 (владелец), AR-Q14 (проверка запрета LangSmith — уже сделан в S3.1) · §8 №5 `Telemetry`, §9 правило 6, §13 «Наблюдаемость» · ADR-1

## Why

У агента v2 нет наблюдаемости уровня v1: время, токены и коды ошибок вызовов LLM и инструментов не пишутся, а в Langfuse ход v2 не связывается с пользователем (ключ `lang_fuse_user_id` Langfuse не понимает) и не помечен тегом движка. AR-Q13 требует, чтобы каждый вызов LLM, инструмента и срабатывание middleware было в журнале и в self-hosted Langfuse с `x-trace-id`.

## What Changes

- **Middleware `Telemetry`** (§8 №5, `api/v2/middleware/telemetry.py`) в `wrap_model_call` и `wrap_tool_call`: на каждый вызов модели — строка `LLM CALL` (JSON: `trace_id`, `turn_id`, номер вызова, время, токены, имена вызванных инструментов, код ошибки); на каждый вызов инструмента — строка `TOOL CALL` (имя, время, статус, код ошибки из конверта). Тексты исключений в эти строки не попадают. Стоит в стеке сразу после `StopEventGuard`.
- **Метаданные Langfuse.** `router.py` для обоих движков дописывает в `config["metadata"]` `langfuse_user_id = user_id` и `langfuse_tags = ["agent_engine=<v1|v2>"]` (`langfuse_session_id = thread_id` уже есть). Для v2 вложенные вызовы смыслового слоя (`resolve_terms`) получают те же callbacks и видны в дереве хода под вызовом инструмента.
- Тесты: строки `LLM CALL` / `TOOL CALL`, дерево хода на фейковом callback-handler (корень хода → модель → инструмент → вложенный вызов смыслового слоя), метаданные и теги в config.
- Размеры слоёв (`ContextMeter`, S2.7) и запрет LangSmith (`forbid_langsmith`, S3.1) уже реализованы — story их не меняет, только проверяет вместе с остальной телеметрией.

**Вне рамок:** строки `TOOL ARGS` в формате v1 и работа `scripts/run_tool_queries.py` на v2 — отложено решением разработчика, критерий приёмки S4.3 «`run_tool_queries.py` работает на v2» остаётся открытым; метрики хода через `metric_router.py` (длительность, исход хода — §13 «Метрики хода»); отдельные события Langfuse для срабатываний `wrap_*`-middleware сверх уже существующих строк журнала (они видны в дереве как результаты инструментов, `before_*`/`after_*` — как узлы графа); A/B preview-модели (S4.4); проверка дерева в self-hosted Langfuse — только во внутреннем контуре с GigaChat; eval S5.x.

## Capabilities

### New Capabilities

—

### Modified Capabilities

- `agent-api`: добавляется AR-Q13 (журнал вызовов LLM и инструментов, метаданные и дерево хода в Langfuse) — владелец S4.3.

## Impact

- Новый `api/v2/middleware/telemetry.py`; `factory.py` — `Telemetry` в стеке; `api/v1/router.py` — точечная правка метаданных Langfuse, ветка v1 по поведению не меняется.
- Тесты: `tests/unit/v2/test_telemetry.py`.
- Зависимостей не добавляется.
