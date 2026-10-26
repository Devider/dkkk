Story S0.1 · AR-Q4 · ADR-1, ADR-2 (риск «быстрые изменения API `deepagents` 0.x», §16)

## Why

Агент v2 строится на `create_agent` и middleware из `langchain` и `deepagents` 0.x, у которых API меняется от релиза к релизу. Сейчас ключевые библиотеки объявлены открытыми диапазонами (`langchain-gigachat>=0.5.1`, `langfuse>=3.11.2`), `deepagents` в зависимостях нет вовсе, а `poetry.lock` в репозитории отстаёт от `pyproject.toml` после синхронизации с релизной веткой (`ee18ead`). Без жёстких версий spike S0.2 / S0.3 и вся разработка E1–E4 опираются на непредсказуемый набор пакетов, а сборка в закрытом контуре не воспроизводима.

## What Changes

- Добавить `deepagents` `0.7.29` — версию, которая уже есть во внутреннем зеркале (минимум по прототипу — `0.7.23`: skill-инструменты и закрепление skills).
- Зафиксировать точными версиями (`==`) фреймворк агента: `langchain`, `langchain-core`, `langgraph`, `deepagents`, `langchain-gigachat`, `langfuse`. **BREAKING (для окружения разработки):** `langchain` 1.4.2 → 1.4.6, `langchain-core` 1.6.3 → 1.6.12 и `langgraph` 1.2.11 → 1.2.16 по требованию `deepagents` 0.7.29; v1 должен пройти тесты на новых версиях.
- `langfuse` остаётся на ветке 4.x (`4.15.4`, уже в lock); переход на 5.x — вне рамок.
- Перегенерировать и закоммитить `poetry.lock`, согласованный с `pyproject.toml` (`poetry check --lock`).
- Docker-сборка устанавливает зависимости через внутреннее зеркало PyPI, адрес задаётся аргументом сборки; без аргумента сборка идёт с публичного PyPI, как сейчас.
- Добавить unit-тест, который проверяет, что установленные версии совпадают с зафиксированными и что используемые v2 точки импорта (`create_agent`, встроенные middleware, middleware `deepagents`) существуют.

**Вне рамок:** удаление неиспользуемых зависимостей v1 (`nltk`, `sqlalchemy`, `tavily`, `langchain-ollama`, `ipython`, `matplotlib`, `xlsxwriter`, `tenacity` — в коде не импортируются; это решение по релизной ветке); запрет LangSmith (AR-Q14, S4.3); обновление `langfuse` до 5.x; фиксация базового образа `python:3.12-slim` по digest.

## Capabilities

### New Capabilities

- `agent-platform`: сквозные свойства платформы агента v2 — воспроизводимая сборка, подключение провайдеров LLM, конфигурация, сопровождаемость. Эта story добавляет AR-Q4.

### Modified Capabilities

—

## Impact

- `pyproject.toml`: новые и зафиксированные версии; `poetry.lock` перегенерирован.
- `Dockerfile`: установка зависимостей через зеркало (аргумент сборки); `docker-compose.yml` — проброс аргумента.
- Транзитивно добавляются `langchain-anthropic`, `langchain-google-genai`, `langsmith`, `wcmatch` (жёсткие зависимости `deepagents`) — они должны быть в зеркале.
- Код v1 не меняется, но работает на обновлённых `langchain` / `langchain-core` / `langgraph` — регрессия проверяется существующими тестами.
- Новый тест `tests/unit/test_dependency_pins.py`.
