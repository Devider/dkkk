# Intent: Изоляция параллельных запросов через ThreadRunRegistry (run_latest_wins)

## Problem
UI client имеет короткий timeout, который мы не можем изменить. Когда запрос A ещё выполняется на сервере, пользователь отправляет запрос B на том же `thread_id` (`user_id:session_id`). Текущая реализация вызывает `graph.ainvoke()` напрямую — LangGraph загружает последний checkpoint для этого thread_id, и A + B параллельно пишут в один shared state. Это приводит к:
- Смешиванию истории сообщений в LLM (corrupted conversation history)
- Непредсказуемому поведению agent
- Пользователь получает ответ от "смешанного" состояния

## Решение: ThreadRunRegistry

### 1. `api/v1/thread_runs.py` — ThreadRunRegistry
- `dict[thread_id, RunInfo]` в process memory
- `RunInfo`: `HEAD: str | None` (checkpoint_id последнего завершённого), `task: asyncio.Task`
- `run_latest_wins(thread_id, user_id, ainvoke_fn)`:
  1. Если active task → `task.cancel()` + `logger.warning()` (тихо, без 409)
  2. Получаем `base_checkpoint_id = HEAD` (или None если HEAD нет)
  3. Создаём config: если HEAD есть → `{"configurable": {"thread_id": thread_id, "checkpoint_id": HEAD}}`
  4. Запускаем `astream(stream_mode=["values", "checkpoints"])` на task с `asyncio.shield`
  5. При успешном завершении: фильтруем события по empty `checkpoint_ns`, берём последний `checkpoint_id` → обновляем `HEAD`
  6. При CancelledError — HEAD не обновляется

### 2. `router.py` — refactoring invoke-agent
- Заменяем `ainvoke` на `ThreadRunRegistry.run_latest_wins`
- Используем `astream` для получения результата
- SupersededRunError → suppress + log, без HTTP-ошибок клиенту

### 3. `main_graph.py` — интеграция
- Поддержка `checkpoint_id` в config для fork от HEAD
- Каждый fork выполняется на том же `thread_id`, но с указанием `checkpoint_id`

## Unit-тест: `tests/test_thread_runs_isolation.py`
- **Fake graph**: mock, который явно создаёт checkpoint'ы на каждом node
- **Test scenario**:
  1. Запрос A запускается, создаёт checkpoint'ы c1, c2, c3
  2. Запрос B приходит, cancel A
  3. HEAD остаётся на предыдущем значении (не c1/c2/c3)
  4. Запрос C запускается — HEAD = last successful, не видит checkpoint'ы от A
- **Assertion**: `HEAD == checkpoint_id от последнего успешно завершённого запроса`

## Why Now
- UI timeout — внешнее ограничение, которое мы не можем изменить
- Корректная обработка параллельных запросов необходима уже сейчас
- Это временное решение до изменения UI API

## Expected Improvement
- Каждый запрос выполняется с чистым, предсказуемым state
- Параллельные запросы не мешают друг другу
- Последний запрос пользователя всегда "выигрывает"
- История сообщений в LLM остаётся консистентной

## Constraints
- HEAD и task в process memory (только 1 uvicorn worker)
- При отмене — только log, без HTTP-ошибок клиенту
- Multi-turn архитектура сохраняется (используем тот же `thread_id`, но с `checkpoint_id`)

## Risks
- `task.cancel()` не останавливает LibreOffice calculation уже running в `ProcessPoolExecutor`
- `asyncio.shield` может mask exceptions — нужно корректно обрабатывать
- Concurrency с несколькими uvicorn workers — требует migration на Pangolin/Postgres с advisory locks
