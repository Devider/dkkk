# Tasks

## 1. ThreadRunRegistry (новый модуль)

- [x] 1.1 Создать `src/aigw_service/api/v1/thread_runs.py` с dataclass `RunInfo` (`HEAD: str | None`, `task: asyncio.Task`) и классом `ThreadRunRegistry` (процесс-словарь `dict[thread_id, RunInfo]`) — проверить, что модуль импортируется (`python -c "import aigw_service.api.v1.thread_runs"`)
- [x] 1.2 В `thread_runs.py` определить служебные типы для событий checkpoint'ов (`_extract_checkpoint_info`, `_HeadTracker`) и фильтр по пустому `checkpoint_ns` — покрыть модуль-тестами на фильтрацию событий. `SupersededRunError` не нужен: отмена пробрасывается как `asyncio.CancelledError` (класс удалён)
- [x] 1.3 Реализовать `async def run_latest_wins(thread_id, user_id, config, stream_fn)`: отмена активной `task` при наличии (тихий `logger.warning`, без исключения клиенту), чтение `HEAD`, сборка config (`thread_id` + `checkpoint_id` при HEAD), запуск потока в `asyncio.Task` с `asyncio.shield`, возврат финального state для router — юнит-проверка на fake-graph, что при активном запуске предыдущая task отменяется
- [x] 1.4 В `run_latest_wins` по событиям checkpoints определить checkpoint, на котором завершился ход (`_HeadTracker`; учтено смещение событий LangGraph), и в блоке успешного завершения обновить `HEAD`; при `asyncio.CancelledError` HEAD не трогать и пробросить отмену — юнит-проверка, что HEAD остаётся на прежнем значении после отмены
- [x] 1.5 Добавить синглтон/фабрику экземпляра реестра (`get_thread_run_registry` с `@lru_cache(maxsize=1)`), чтобы router и тест разделяли один process-wide инстанс — проверить, что между вызовами сохраняется `HEAD`

## 2. Интеграция в router.py (invoke-agent)

- [x] 2.1 Заменить в `router.invoke_agent` прямой вызов `agent.graph.ainvoke(...)` на `ThreadRunRegistry.run_latest_wins(thread_id, user_id, config, stream_fn)`, сохранив существующую сборку `config` (metadata/langfuse, callbacks) и внеся `checkpoint_id` в `configurable` при HEAD — проверить, что `/api/v1/invoke-agent` возвращает ZIP с `txt_response.txt` прежнего формата
- [x] 2.2 Извлечь финальное сообщение из возвращённого `state` (последний `values`-уровень), как раньше `result["messages"][-1]` — проверить, что контент ответа не изменился
- [x] 2.3 Обработать superseded-запуск: отменённый запуск логируется и не отправляет клиенту **ничего** (ни ZIP, ни HTTP 409/424), победивший запуск обрабатывается штатно — HTTP-тест `tests/integration/v1_api/test_invoke_agent_latest_wins.py::TestSupersededRequestGetsNoResponse`
- [x] 2.4 Сохранить обработку `StopEventError`→503 и generic→424 только для победившего запуска — HTTP-тест `TestWinnerErrorMapping` + существующие ветки ошибок не сломались
- [x] 2.5 Пробросить `asyncio.CancelledError` через `AgentGraph._execute_single_call`: без этого отмена supersede-запуска глоталась (превращалась в `ToolMessage` «Ошибка выполнения»), ход доходил до `synthesizer`, и проигравший клиент получал ZIP с 200 — юнит-тест в `tests/unit/test_tool_executor_error_handling.py`

## 3. Поддержка checkpoint_id в main_graph.py

- [x] 3.1 Подтвердить, что `checkpoint_id` из `config["configurable"]` корректно интерпретируется `MemorySaver` при fork на том же `thread_id` — тест `tests/integration/test_checkpoint_fork.py`: запуск с `checkpoint_id=HEAD` форкается от HEAD, а не от последнего checkpoint
- [x] 3.2 Убедиться, что передача config вниз (`_execute_single_call`, `orchestrator_tools.dispatch`) не сбрасывает `checkpoint_id`/не ломает fork — `tests/unit/test_session_continuity.py`, `tests/unit/test_tool_executor_error_handling.py`, `tests/unit/test_tool_executor_error_integration.py` приведены в соответствие с сигнатурами (`config` обязателен) и зелёные

## 4. Тесты изоляции потоков

- [x] 4.1 Создать `tests/test_thread_runs_isolation.py` с fake-graph (mock, который на каждом node создаёт checkpoint'ы c1/c2/c3), воспроизводящий сценарий: A создаёт checkpoint'ы → B отменяет A → HEAD остаётся на прежнем значении → C стартует от последнего успешного и не видит checkpoint'ы A — утверждение `HEAD == checkpoint_id последнего успешно завершённого запуска`
- [x] 4.2 Добавить scenario на успешное завершение (HEAD обновляется финальным `checkpoint_id`) и на отмену (HEAD не меняется) — проверить, что оба проходят
- [x] 4.3 Добавить scenario multi-turn continuity: отменённый A не коммитит ход в историю, которую продолжает успешный C (история сообщений консистентна) — `tests/integration/v1_api/test_invoke_agent_latest_wins.py::test_superseded_run_does_not_reach_the_client`
- [x] 4.4 Прогнать новые и существующие тесты и убедиться, что все зелёные

## 5. Линт, формат, финальная проверка

- [x] 5.1 Прогнать `ruff check --fix src` и `ruff format src` на затронутых файлах (`thread_runs.py`, `router.py`, `main_graph.py`, тесты) — `thread_runs.py` чистый; остаётся 6× `PLE1205` в `main_graph.py` (ложное срабатывание на loguru-формате) и pre-existing `BLE001` в `router.py`
- [x] 5.2 Убедиться, что в новых `except Exception as e`-ветках используется loguru-формат `logger.opt(exception=True).error("... {}", str(e))`, а не f-string — `pylint src` = 9.53/10 (>7)
- [x] 5.3 Прогон `pytest tests` (unit + integration) — 169 passed, 10 skipped, 13 failed (pre-existing: LibreOffice/server not running). Требуют внешних условий и не связаны с изменением: `tests/test_agent.py` (нужен запущенный сервер), `tests/test_tools_performance.py` (LibreOffice), `tests/unit/test_correct_workbook.py` (LibreOffice)
