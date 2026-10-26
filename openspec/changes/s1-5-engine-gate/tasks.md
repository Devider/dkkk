# Tasks

## 1. Гейт

- [x] 1.1 Настройки `calc_workers` (`AGENT_CALC_WORKERS`, 2) и `calc_timeout` (`AGENT_CALC_TIMEOUT`, 120) в `config/agent`. Проверка: тест — дефолты и переопределение переменными окружения
- [x] 1.2 `api/v2/tools/engine_gate.py`: пул `spawn` с `os.setsid`, блокировки по сессии с уборкой, `run` с таймаутом, уничтожение групп и пересоздание пула, `shutdown`, `default_gate()` (D1, D2). Проверка: unit-тесты D5 на функциях-заглушках — очередь сессии, параллельность сессий, лимит пула, таймаут убивает дочерний процесс и пул работает дальше, падение воркера → `engine_unavailable`, словарь блокировок пуст после расчётов

## 2. Инструменты

- [x] 2.1 `ToolContext.thread_id`; `calc_tools` через гейт (`acall`, D3), `TimeoutError` → `engine_timeout`, `BrokenProcessPool` → `engine_unavailable`. Проверка: тесты в `create_agent` с подменённым гейтом — проверки S1.4 до гейта, ключ блокировки — `thread_id`, коды таймаута и сбоя в конверте
- [x] 2.2 Docker: настоящий расчёт — таймаут без сирот `soffice`, следующий расчёт успешен, задержка event loop < 1 с; паритет S0.7/S1.1 зелёный. Проверка: `tests/integration/excel_engine/test_engine_gate.py` в Docker — `passed`, локально — `skipped`

## 3. Качество

- [x] 3.1 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [x] 3.2 `pylint src` > 7, `pytest tests/unit`, покрытие `api/v2/tools` ≥ 80 %, тест границ зелёный. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
