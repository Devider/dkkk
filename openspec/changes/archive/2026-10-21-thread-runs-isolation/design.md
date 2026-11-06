# Design

## Context

См. proposal.md — Why. Текущая реализация `router.invoke_agent` вызывает `agent.graph.ainvoke({...}, config=config)` напрямую. `config` содержит `configurable.thread_id` (композитный ключ `f"{user_id}:{session_id}"`) и `configurable.user_id`, но НЕ содержит `checkpoint_id`. Граф скомпилирован с чекпоинтером `MemorySaver` (`APP_CTX.agent_memory.checkpointer`, общий для всех воркеров процесса). Ключевая проблема: два параллельных `ainvoke` на одном `thread_id` оба резюмятся от последнего checkpoint и пишут в один shared state — история смешивается.

Код-точки интеграции (установлено исследованием кода):
- `router.py#invoke_agent` — сборка `config`, вызов `agent.graph.ainvoke`, формирование ZIP-ответа, обработка ошибок (`StopEventError`→503, generic→424).
- `main_graph.py` — `AgentGraph` (lru_cache-синглтон через `@lru_cache(maxsize=1)`), `self.graph = workflow.compile(checkpointer=APP_CTX.agent_memory.checkpointer)`, состояние `AgentState`/`AgentInput`.
- `context.py` — `APP_CTX.agent_memory.checkpointer` = `MemorySaver`.

## Goals / Non-Goals

**Goals:**
- Изолировать параллельные запуски одного `thread_id` через run-latest-wins: последний запрос всегда выигрывает.
- Новые запуски форкаются от `checkpoint_id` HEAD последнего успешно завершённого запуска на том же `thread_id`.
- Superseded-запуск отменяется тихо (log), без HTTP-409/ошибки клиенту.
- HEAD обновляется только при успешном завершении; `CancelledError` не меняет HEAD.
- Внешний контракт `/invoke-agent` (ZIP-ответ, заголовки) не меняется.

**Non-Goals:**
- Персистентность реестра/HEAD в БД (Pangolin/Postgres) и multi-worker concurrency с advisory locks — будущий этап, вне scope.
- Изменение внешнего API, моделей запроса/ответа.
- Принудительная остановка уже запущенных LibreOffice-расчётов в `ProcessPoolExecutor` (leaked computation допускается и логируется).
- Устранение short-timeout UI-клиента (внешнее ограничение).

## Decisions

### D1: `ThreadRunRegistry` в process-memory (`api/v1/thread_runs.py`)
Новый модуль с классом `ThreadRunRegistry`: `dict[thread_id, RunInfo]`. `RunInfo` = `HEAD: str | None` (checkpoint_id последнего успешного) + `task: asyncio.Task`. Инстанс — process-wide (аналог `get_agent` через `@lru_cache` или синглтон), потому что checkpointer и так процесс-локальный (`MemorySaver`).
- **Rationale**: реестр должен разделять тот же жизненный цикл, что и checkpointer (процесс), иначе HEAD/задачи не согласованы. Один uvicorn worker — допущение из proposal.
- **Alternative**: хранить HEAD в checkpointer state — избыточно и усложняет чтение; process-словарь проще и прозрачнее для теста.

### D2: Запуск через `astream(stream_mode=["values", "checkpoints"])` + `asyncio.shield`
Вместо `graph.ainvoke` используем `graph.astream(input, config, stream_mode=["values", "checkpoints"])`. По событиям checkpoints определяется checkpoint, на котором завершился ход → `HEAD`; финальное состояние берётся из последнего `values`-события (закрывающее событие эмитится уже после остановки графа).
- **Rationale**: `astream` с mode `checkpoints` — единственный способ получить идентификаторы checkpoint'ов; `ainvoke` их наружу не отдаёт.
- **Семантика событий (проверено на langgraph 1.2.12)**: событие checkpoint `C` несёт `config` **C** (`checkpoint_id`, `next`), но `values` — состояние **до** задач `C` (смещение на один шаг). Поэтому:
  - checkpoint с непустым `next` — его задачи ещё не выполнены;
  - checkpoint с `next == []` — граф на нём остановился (paused/END), его `values` — состояние завершённого хода;
  - **HEAD** = последний checkpoint с `next == []`, если после последнего «незакрытого» checkpoint ход дописал сообщения (финальный узел с `messages`); иначе — последний checkpoint с непустым `next` (финальный узел графа агента — `synthesizer` — не пишет в state, и граф останавливается именно на нём: форк с него возобновляет ровно ту задачу и даёт состояние завершённого хода).
  - Реализовано в `_HeadTracker` (`thread_runs.py`), проверено интеграционным тестом на реальном `MemorySaver` (`tests/integration/test_checkpoint_fork.py`).
- **Отмена через `asyncio.shield`**: внутренний прогон идёт в отдельной `asyncio.Task` и прикрыт `shield`, поэтому отмена *внешней* задачи не рвёт граф посередине — отмена приходит как `CancelledError` из `await shield(...)`, после чего `run_latest_wins` отменяет сам `run_task`, дожидается его завершения и **пробрасывает `CancelledError` дальше**. `CancelledError` — `BaseException`, поэтому он проходит мимо `except Exception` в `router.invoke_agent` и **HTTP-ответ не формируется вовсе** (ни 409, ни 424, ни ZIP): клиент проигравшего запроса не получает ничего.
- **Alternative**: собрать checkpoint_id из `graph.get_state()` после `ainvoke` — не отражает fork-семантику, и отменить прогон посередине было бы невозможно.

### D3: Fork от HEAD через `checkpoint_id` в config
Если `HEAD` есть — в `config["configurable"]` добавляется `"checkpoint_id": HEAD`; иначе используется `thread_id` без `checkpoint_id` (первый запуск потока). LangGraph при наличии `checkpoint_id` резюмится от указанного checkpoint (fork), а не от последнего — это и даёт изоляцию: запуск C форкается от HEAD и не наследует промежуточные checkpoint'ы отменённого A.
- **Rationale**: использование штатного механизма `checkpoint_id` LangGraph минимально и не требует костылей с перезаписью state.
- **Примечание**: `main_graph.py` не требует изменений для передачи `checkpoint_id` — LangGraph принимает его через `config["configurable"]["checkpoint_id"]`. Поддержка на уровне графа (в среду передачи config) уже есть: config пробрасывается вниз в `_execute_single_call` через `RunnableConfig`, а `checkpoint_id` интерпретирует сам checkpointer. Изменение в `main_graph.py` сводится к фиксации/проверке этой семантики (например, не сбрасывать `checkpoint_id` при пересборке config), при выявлении необходимости — явная передача в compile/state.

### D4: Обработка superseded-запуска в `router.py`
`run_latest_wins(thread_id, user_id, config, stream_fn)`:
1. Если для `thread_id` есть активный `task` → `task.cancel()` + `logger.warning` (тихо, без HTTP-409).
2. `base_checkpoint_id = HEAD` (или `None` при первом запуске).
3. Сборка `config`: при наличии HEAD → в `config["configurable"]` добавляется `checkpoint_id=HEAD` (остальные ключи, включая `thread_id`/`user_id`, переносятся без изменений).
4. Запуск `stream_fn(config)` (в роутере это `astream(stream_mode=["values","checkpoints"])`) в `asyncio.Task`, обёрнутом в `asyncio.shield`; task сохраняется в реестре.
5. На успешном завершении `_consume` выбирает HEAD через `_HeadTracker` (см. D2) и возвращает финальное `values`-состояние.
6. При `CancelledError` — HEAD не обновляется, `run_task` отменяется и дожидается, затем **`CancelledError` пробрасывается вызывающему**.

`router.invoke_agent` заменяет `agent.graph.ainvoke(...)` на `run_latest_wins(...)`, результат (`state`) — финальное `values`-состояние; сообщение берётся как `state["messages"][-1]`. Внешний response-код и формат ZIP не меняются. `StopEventError`/generic по-прежнему мапятся в 503/424 — но только для победившего запуска.

**Почему подавленный запуск не отвечает вообще**: `CancelledError` — наследник `BaseException`, `except Exception` в роутере его не ловит → ни `StreamingResponse` (ZIP), ни `JSONResponse` (503/424) не формируются, ASGI-запрос проигравшего прерывается. Спека (`specs/thread-runs-isolation/spec.md`, scenario «A superseded run sends nothing to its client») закрепляет именно это поведение.

**Проброс отмены через `tool_executor`** (`main_graph.py`): пока `_execute_single_call` ловил `(Exception, asyncio.CancelledError)` и превращал отмену в `ToolMessage("Ошибка выполнения: ...")`, отмена supersede-запуска глоталась: волна вычислений доходила до `synthesizer`, и проигравший клиент получал **ZIP с 200**. Теперь `asyncio.CancelledError` явно пробрасывается (`except Exception` оставлен для остальных ошибок), что и даёт требуемое спецификацией поведение. Требование capability `robust-tool-execution` («каждый `tool_call_id` получает `ToolMessage`») по-прежнему выполняется для всех обычных исключений и переполнения лимита итераций; отменённый supersede-ход просто не коммитится.
- **Rationale**: держать всю конкурентную логику в одном модуле (`thread_runs.py`), а router оставляет за собой только HTTP-адаптацию и ZIP — single responsibility.
- **Alternative**: встроить отмену прямо в router — увеличивает связность и усложнит unit-тесты (`thread_runs` изолированно от FastAPI).

### D5: Тесты
- `tests/test_thread_runs_isolation.py` — fake-graph без LangGraph: отмена активного запуска, неизменность HEAD при отмене, фильтрация nested-checkpoint'ов по `checkpoint_ns`, синглтон реестра.
- `tests/integration/test_checkpoint_fork.py` — реальный `StateGraph` + `MemorySaver`, `stream_mode=["values","checkpoints"]`: запуск с `checkpoint_id=HEAD` форкается от HEAD (а не от последнего checkpoint'а), `checkpoint_id` доезжает до `stream_fn` без изменений.
- `tests/integration/v1_api/test_invoke_agent_latest_wins.py` — HTTP-уровень через настоящий ASGI-роутер и настоящий граф со скриптованным LLM: проигравший запрос не получает **никакого** ответа (ASGI-task отменён), победитель получает 200 + ZIP; `StopEventError`→503, generic→424.
- `tests/unit/test_tool_executor_error_handling.py` — `CancelledError` из `dispatch` пробрасывается, а не превращается в `ToolMessage`.
- **Rationale**: property-тест реестра держит его в изоляции, а реальный `MemorySaver` и HTTP-граница проверяются отдельными интеграционными тестами — смещение событий checkpoint'ов и поведение отмены нельзя воспроизвести моком.

## Risks / Trade-offs

- [`task.cancel()` не останавливает уже запущенный LibreOffice-расчёт в `ProcessPoolExecutor`] → Accept; расчёт-сирота умирает со своим процессом, результат отбрасывается, только log. Отдельная задача по cancellation-token'ам в LO-бэкенде — вне scope.
- [`asyncio.shield` может маскировать исключения / сложно корректно отрабатывать отмену] → Явное ветвление по `asyncio.CancelledError` внутри `run_latest_wins`; shield применяется только к внутреннему прогону, отмену внешней задачи обрабатываем отдельно.
- [Реестр/HEAD в process-memory — некорректно трактуется при >1 worker (каждый worker видит свой реестр, gunicorn/uvicorn multi-worker)] → Зафиксировано как ограничение (1 worker) в proposal; миграция на Pangolin/Postgres + advisory locks — отдельный баг/этап.
- [Смена результата: `astream` отдаёт state по шагам, а не один финальный `dict`] → Логика инварианта соответствует `ainvoke` (последний values-кусок с финальным messages); добавлен тест, проверяющий итоговое сообщение.
- [GigaChat-ограничение 422 по tool_call_id не должно нарушиться за счёт cancel] → Отменённый запуск не коммитит свой (частичный) state в HEAD; следующий запуск стартует от непротиворечивого HEAD, поэтому история остаётся валидной. Отдельный scenario в spec.
- [Смещение событий checkpoint'ов (langgraph 1.2.12)] → `values` события checkpoint относятся к предыдущему шагу, `next` — к текущему; HEAD определяется `_HeadTracker` по правилу из D2 и покрыт интеграционным тестом на реальном `MemorySaver`. При обновлении langgraph тест `tests/integration/test_checkpoint_fork.py` — первая линия проверки.
- [Проброс `CancelledError` из `tool_executor`] → отменяется не только граф, но и весь ход запроса; это ожидаемое поведение supersede. Отмена по таймауту клиента (не supersede) тоже прервёт выполнение — клиенту и так некому отдать ответ.

## Migration Plan

1. Feature-флаг/поэтапность не требуется — изменение внутреннее, внешний API стабилен.
2. Rollback: откат к прежнему `ainvoke`-вызову в `router.py` (без `ThreadRunRegistry`) — обратимое изменение, низкий риск; HEAD/реестр в process-memory исчезают вместе с процессом.

## Open Questions

- Решено: `MemorySaver` надёжно форкается по `checkpoint_id` из `config["configurable"]` — покрыто `tests/integration/test_checkpoint_fork.py` на реальном чекпоинтере (langgraph 1.2.12).