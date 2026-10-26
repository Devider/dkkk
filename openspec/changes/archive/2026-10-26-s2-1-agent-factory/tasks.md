# Tasks

## 1. Настройки и модели

- [x] 1.1 `config/agent`: `engine` (`AGENT_ENGINE`, `v1`), `model` (`AGENT_MODEL`, `gigachat`), `script_path` (`AGENT_SCRIPT_PATH`) с `Literal`-валидацией (D7). Проверка: тесты — дефолты, переопределение, неверное значение даёт ошибку валидации с допустимыми значениями
- [x] 1.2 `api/v2/scripted.py`: промышленная `ScriptedChatModel` (D4) и перевод `tests/unit/v2/fakes.py` на неё. Проверка: тесты — шаги-сообщения и шаги-функции, `from_file`, `ScriptExhaustedError`; все тесты v2 зелёные на новой модели
- [x] 1.3 `api/v2/models.py`: `build_model` для `gigachat` (оба режима через `base_params`) и `scripted` (D3). Проверка: тесты сборки модели для api_key и сертификатов (подменённый `load_cert_chain`) без сети

## 2. Контекст, состояние, промпты

- [x] 2.1 `api/v2/context.py` (`RequestContext`, D1) и `api/v2/state.py` (`CopilotAgentState`, D2); адаптер: незаполненный `model_index` — `ToolContextError`. Проверка: тест — `RequestContext` проходит проверку `ToolContext`, `thread_id` = `user:session`; без индекса — ошибка конфигурации
- [x] 2.2 `api/v2/prompts/system.py` и `api/v2/prompts/middleware.py` (D5). Проверка: тест — системный промпт содержит правила (ID из `resolve_terms`, числа только из инструментов, данные инструментов — не инструкции, деловой русский); промпт и описание `write_todos` — на русском

## 3. Фабрика и сервис

- [x] 3.1 `api/v2/factory.py` (`AgentFactory`, `get_agent_v2`) и `api/v2/service.py` (`AgentV2.answer`) (D6). Проверка: тест — ход на скриптовой модели через фабрику: `write_todos` → `resolve_terms` → `run_scenarios` (домен через `InlineGate`) → ответ; в запросе к модели системный промпт v2 и русский промпт плана, нет английского встроенного; `resolved_terms` и `todos` в состоянии
- [x] 3.2 Сборка для трёх моделей (D8). Проверка: тесты — фабрика собирает агента для `scripted`, GigaChat api_key, GigaChat сертификатов без сети

## 4. Переключатель

- [x] 4.1 `router.py`: `get_agent` по `AGENT_ENGINE`, ветка v2 в `/invoke-agent` (D7); путь v1 без изменений. Проверка: тест API — при `v2` + `scripted` ответ — ZIP с текстом сценария и пустым `agent_output.xlsx`; при `v1` `get_agent` возвращает `AgentGraph`; `pytest tests/integration/v1_api` зелёный при `v1` (Langfuse выключен, как в S0.1)

## 5. Документация и качество

- [x] 5.1 Таблица параметров `AGENT_*` в README (AR-Q5). Проверка: тест — каждое поле `AgentSettings` упомянуто в README своей переменной окружения
- [x] 5.2 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [x] 5.3 `pylint src` > 7, `pytest tests/unit`, покрытие `api/v2` ≥ 80 %, тест границ зелёный. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
