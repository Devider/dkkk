# Tasks

## 1. Выбор версий

- [x] 1.1 `deepagents` 0.7.29 в зеркале подтверждён. Проверить наличие остального набора D2: `langchain` 1.4.6, `langchain-core` 1.6.12, `langgraph` 1.2.16, `langchain-anthropic` ≥ 1.7.6, `langchain-google-genai` ≥ 4.4.2, `langsmith` ≥ 0.14.11, `wcmatch` ≥ 11.0; при отсутствии — заменить по правилу D2 и обновить таблицу D2 в `design.md`. Проверка: таблица D2 отражает итоговый набор, и все пакеты из неё есть в зеркале (подтверждено разработчиком 2026-10-26: все пакеты набора D2 есть в зеркале)

## 2. Манифест и lock

- [x] 2.1 В `pyproject.toml` объявить `==` для `langchain`, `langchain-core`, `langgraph`, `deepagents`, `langchain-gigachat`, `gigachat`, `langfuse` по таблице D2. Проверка: `grep -E '"(langchain|langchain-core|langgraph|deepagents|langchain-gigachat|gigachat|langfuse)==' pyproject.toml` выдаёт 7 строк
- [x] 2.2 Перегенерировать `poetry.lock` (`poetry lock`) и установить окружение (`poetry install`). Проверка: `poetry check --lock` проходит, `poetry show deepagents langchain langchain-core` показывает версии из D2
- [x] 2.3 Добавить `tests/unit/test_dependency_pins.py` по D4: сверка установленных версий с `==` из `pyproject.toml` и импорт точек API v2 (пути импорта сверить с установленными пакетами). Проверка: `pytest tests/unit/test_dependency_pins.py -v --no-cov` зелёный; при ручной подмене версии в `pyproject.toml` на несуществующую тест падает

## 3. Регрессия v1 на новых версиях

- [x] 3.0 Вернуть в `src/aigw_service/context.py` импорт `StopEventError` (удалён в `30bfbd7`; без него StopEvent GigaChat даёт `NameError` вместо HTTP 503). Проверка: `pytest tests/unit/test_stop_event.py` зелёный
- [x] 3.1 Прогнать существующие тесты v1. (2026-10-26: unit 80 passed; 2 устаревших теста `TestNoHardcodedRetry` проверяют удалённые `AppContext.create_llm` / `_gigachat_max_retries` и игнорируются по решению разработчика; `v1_api` 2 passed с `LANGFUSE_TRACING_ENABLED=False` — локальный Langfuse с хоста недоступен при текущем `.env`. Те же результаты на прежних версиях `langchain` — регрессий от обновления нет.) Проверка: `pytest tests/unit` и `pytest tests/integration/v1_api` зелёные без правок кода v1; при падении — откат по D2 и запись причины в `design.md`
- [x] 3.2 Ручной прогон одного запроса v1 на GigaChat (`python3 src/aigw_service/__main__.py`, `/upload` + `/invoke-agent` на книге Methanex). Проверка: ответ получен, в `server.log` есть строки `TOOL ARGS` и нет ошибок импорта (2026-10-26: прогон в Docker — локально нет LibreOffice; запрос T001 из набора Methanex, HTTP 200 за 23 с, ZIP прежнего состава, `TOOL ARGS: analyze_model_inputs_for_target`, ошибок нет; Langfuse на время прогона выключен)

## 4. Docker-сборка через зеркало

- [x] 4.1 В `Dockerfile` (builder): аргумент `PIP_INDEX_URL`, установка `poetry-plugin-export` с фиксированной версией, `poetry export --only main -f requirements.txt` и `pip install --require-hashes -r requirements.txt`; колесо приложения — `pip install --no-deps`. Проверка: `docker compose build app --no-cache` без аргумента проходит с PyPI
- [x] 4.2 В `docker-compose.yml` пробросить `PIP_INDEX_URL` в `build.args` (пустое значение по умолчанию). Проверка: `docker compose config` показывает аргумент сборки
- [x] 4.3 Проверить образ: версии внутри совпадают с lock. Проверка: `docker compose run --rm --entrypoint python app -c "import importlib.metadata as m; print({p: m.version(p) for p in ['langchain','langchain-core','langgraph','deepagents','langchain-gigachat','gigachat','langfuse']})"` выдаёт версии из D2; `curl http://localhost:8888/health` после `docker compose up -d` отвечает 200
- ~~4.4~~ **Пропущено** по решению разработчика 2026-10-26; сценарий спеки «Сборка из внутреннего зеркала» в контуре не проверен. (в контуре) Сборка в контуре с `PIP_INDEX_URL=<зеркало>` без доступа к PyPI (выполняет разработчик в контуре). Проверка: сборка проходит, версии как в 4.3
- [x] 4.5 Описать аргумент `PIP_INDEX_URL` в разделе Docker в `CLAUDE.md` и `README.md`. Проверка: команда из документации собирает образ

## 5. Качество

- [x] 5.1 `ruff check --fix` и `ruff format` по изменённым файлам (`src/aigw_service/context.py`, `tests/unit/test_dependency_pins.py`). Проверка: новых замечаний нет (2026-10-26: остались только 5 старых F821 в `context.py` — вынесены в change `fix-context-undefined-imports`)
- [x] 5.2 `pylint src`. Проверка: оценка > 7 (2026-10-26: 9.43/10)
- [x] 5.3 `pytest tests/unit`. Проверка: все тесты зелёные (2026-10-26: 80 passed; 2 устаревших `TestNoHardcodedRetry` игнорируются по решению разработчика, см. 3.1)
- [x] 5.4 Отметить S0.1 выполненной в `openspec/traceability.md` (AR-Q4). Проверка: статус AR-Q4 — «S0.1 ✓»
