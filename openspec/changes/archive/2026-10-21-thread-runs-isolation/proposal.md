# Proposal

## Why

UI-клиент имеет короткий timeout, который нельзя изменить на нашей стороне. Когда запрос A ещё выполняется на сервере, пользователь отправляет запрос B на том же `thread_id` (`user_id:session_id`). Текущая реализация вызывает `graph.ainvoke()` напрямую: LangGraph загружает последний checkpoint для этого `thread_id`, и A + B параллельно пишут в один shared state. Это приводит к смешиванию истории сообщений в LLM (corrupted conversation history), непредсказуемому поведению agent и ответу пользователю от "смешанного" состояния.

## What Changes

- **Новый модуль `ThreadRunRegistry`** (`api/v1/thread_runs.py`): process-memory реестр активных запусков, ключевой по `thread_id`. Хранит `HEAD` (checkpoint_id последнего успешно завершённого запуска) и активную `asyncio.Task`.
- **Стратегия `run_latest_wins`**: если для `thread_id` уже есть активный запуск, он отменяется тихо (без HTTP-409 клиенту, только log). Новый запрос форкается от `HEAD` последнего успешно завершённого запуска через `checkpoint_id` в config.
- **Запуск через `astream(stream_mode=["values", "checkpoints"])`** с `asyncio.shield` вместо прямого `ainvoke`: по событиям checkpoints вычисляется итоговый `checkpoint_id`.
- **Обновление `HEAD`**: после успешного завершения HEAD указывает на checkpoint последнего успешного запуска; при `CancelledError` HEAD не обновляется.
- **Ревью `router.py`**: замена `graph.ainvoke()` на `ThreadRunRegistry.run_latest_wins`; обработка superseded-запуска без HTTP-ошибок клиенту; результат по-прежнему возвращается как ZIP (внешний API не меняется).
- **Поддержка `checkpoint_id` в `main_graph.py`**: для корректного fork от HEAD на том же `thread_id`.
- **Unit-тест** `tests/test_thread_runs_isolation.py` с fake-graph: запрос A создаёт checkpoints c1/c2/c3, запрос B отменяет A, HEAD остаётся на предыдущем значении, запрос C стартует от HEAD и не видит checkpoints от A.
- **Проброс отмены через `tool_executor`** (`main_graph.py`): `_execute_single_call` сейчас перехватывает `asyncio.CancelledError` вместе с `Exception` и превращает его в `ToolMessage` «Ошибка выполнения» — из-за этого отмена supersede-запуска глотается, волна вычислений доходит до `synthesizer`, и проигравший клиент всё равно получает ZIP-ответ. Теперь `CancelledError` пробрасывается дальше (как требует `thread-runs-isolation`), а `Exception` по-прежнему конвертируется в `ToolMessage` (требование `robust-tool-execution` сохраняется).

## Capabilities

### New Capabilities
- `thread-runs-isolation`: изоляция параллельных запросов на одном `thread_id` через `ThreadRunRegistry` и стратегию `run_latest_wins` — последний запрос пользователя всегда выигрывает, история LLM остаётся консистентной. Отмена запуска обязана прерывать выполнение целиком, а не превращаться во внутреннюю ошибку инструмента.

### Modified Capabilities
- `robust-tool-execution`: scenario «`Tool dispatch` raises `asyncio.CancelledError`» меняется — отмена пробрасывается, а не превращается в `ToolMessage`. Требование «каждый `tool_call_id` получает `ToolMessage`» продолжает действовать для всех *обычных* исключений и для отмены по причине отказа инструмента; при supersede отменённый ход просто не коммитится в историю.

## Impact

- **Код**: `src/aigw_service/api/v1/thread_runs.py` (новый), `src/aigw_service/api/v1/router.py` (invoke-agent), `src/aigw_service/api/v1/main_graph.py` (проброс `CancelledError`; поддержка checkpoint_id/fork не требует изменений — `checkpoint_id` принимает сам checkpointer).
- **Тесты**: `tests/test_thread_runs_isolation.py` (реестр), `tests/integration/test_checkpoint_fork.py` (реальный `MemorySaver`), `tests/integration/v1_api/test_invoke_agent_latest_wins.py` (HTTP-контракт supersede), `tests/unit/test_tool_executor_error_handling.py` (проброс отмены).
- **API**: внешний HTTP-контракт `/api/v1/invoke-agent` не меняется (по-прежнему ZIP-ответ). Меняется только внутренняя логика конкурентности.
- **Зависимости**: без новых внешних зависимостей — используется штатный LangGraph `MemorySaver` и `asyncio`.
- **Ограничения**: HEAD и активные task живут в process-memory (рассчитано на 1 uvicorn worker). При отмене — только log, без HTTP-ошибок. Сохраняется multi-turn архитектура (тот же `thread_id`, но с `checkpoint_id`).
- **Риски миграции**: при нескольких workers потребуется Persistence-бэкенд (Pangolin/Postgres) с advisory locks — вне scope этого изменения.