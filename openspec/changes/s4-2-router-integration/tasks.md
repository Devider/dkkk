# Tasks

## 1. Блокировка и StopEvent

- [x] 1.1 `api/v2/turn/lock.py` (`TurnLocks`, `TurnBusyError`, `TURN_LOCKS`), блокировка в `AgentV2.answer`, ответ 409 в `router.py` (D1). Проверка: тесты — повторный захват занятой сессии даёт `TurnBusyError`; исключение хода освобождает сессию; после хода занятых сессий нет; через API — параллельный запрос в ту же сессию 409, в другую 200, повтор после хода 200
- [x] 1.2 `api/v2/middleware/stop_event.py` (`StopEventGuard`) после `LanguageGuard` в `AgentFactory` (D2). Проверка: тесты — StopEvent модели и `resolve_terms` → 503 с сообщением о недоступности; 403 без маркера → 424; порядок стека

## 2. Контракт API

- [x] 2.1 Тесты API v2 (`tests/unit/v2/test_api.py`): ZIP того же состава, ответ без файла, изоляция пользователей с одинаковым `x-session-id` (D3). Проверка: тесты зелёные
- [x] 2.2 `tests/integration/v1_api` при `AGENT_ENGINE=v1` и `AGENT_ENGINE=v2` (D4). (2026-10-27: оба прогона 2 passed, `LANGFUSE_TRACING_ENABLED=False` в `.env` на время прогона — `load_dotenv(override=True)` перекрывает окружение; `.env` восстановлен из копии.) Проверка: оба прогона зелёные без правок тестов

## 3. Качество

- [x] 3.1 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [x] 3.2 `pylint src` > 7, `pytest tests/unit`, покрытие нового кода ≥ 80 %. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
