# Design

## Context

Мотивация — в proposal.md. Требования — в `specs/agent-platform/spec.md`.

Что есть сейчас (по состоянию на 2026-10-26):

| Пакет | `pyproject.toml` | `poetry.lock` (рабочее дерево) | Последний на PyPI |
| --- | --- | --- | --- |
| `langchain` | `>=1.3.4,<2.0.0` | 1.4.2 | 1.4.7 |
| `langchain-core` | транзитивно | 1.6.3 | 1.7.0 |
| `langgraph` | `>=1.2.4,<2.0.0` | 1.2.11 | 1.2.17 |
| `deepagents` | — | — | 0.7.32 |
| `langchain-gigachat` | `>=0.5.1` | 0.5.1 | 0.5.1 |
| `gigachat` | `>=0.1.39.post1` | 0.2.3 | 0.2.3 |
| `langfuse` | `>=3.11.2` | 4.15.4 | 5.0.4 |

- Во внутреннем зеркале есть `deepagents` 0.7.29 (релиз 2026-10-20). Он требует `langchain>=1.4.6`, `langchain-core>=1.6.12` и жёстко тянет `langchain-anthropic>=1.7.6`, `langchain-google-genai>=4.4.2`, `langsmith>=0.14.11`, `wcmatch>=11.0`. `langchain` 1.4.6 требует `langgraph>=1.2.12,<1.3`.
- `poetry.lock` в HEAD не соответствует `pyproject.toml` после `ee18ead`; в рабочем дереве лежит перегенерированный lock (`poetry check --lock` проходит), не закоммичен.
- `Dockerfile`: `poetry install --only main` с публичного PyPI, затем `pip install dist/*.whl` — второй шаг заново разрешает зависимости колеса по диапазонам из `pyproject.toml` и может поставить версии мимо lock.
- Источники `sberosc` / `nexus-release` в `pyproject.toml` закомментированы: сборка вне контура идёт с PyPI.
- Poetry 2.3.4 при `poetry install` не использует `PIP_INDEX_URL` и не позволяет подменить PyPI переменной окружения без объявления источника в `pyproject.toml`.

## Goals / Non-Goals

**Goals:**
- Один и тот же lock даёт одинаковое окружение локально, в CI и в образе внутри контура.
- Зеркало выбирается при сборке, файлы репозитория для этого не меняются.
- Поломка API фреймворка при обновлении ловится unit-тестом, а не на spike или в рантайме.

**Non-Goals:**
- Чистка неиспользуемых зависимостей v1 и фиксация версий остальных библиотек (они фиксируются lock, но не `==` в манифесте).
- Фиксация базового образа по digest.

## Decisions

### D1. Точные версии только для фреймворка агента

`==` в `pyproject.toml` для `langchain`, `langchain-core`, `langgraph`, `deepagents`, `langchain-gigachat`, `gigachat`, `langfuse`. `langchain-core` и `gigachat` добавлены к пяти пакетам из AR-Q4: это реальные поверхности совместимости (базовые классы middleware и сообщений; клиент GigaChat под коннектором). Остальное фиксирует lock.

*Альтернатива:* `==` для всех зависимостей — лишний шум при обновлениях без выигрыша, lock и так даёт воспроизводимость.

### D2. Целевой набор версий

| Пакет | Версия | Почему |
| --- | --- | --- |
| `deepagents` | 0.7.29 | Есть во внутреннем зеркале; ≥ 0.7.23 для skill-инструментов и закрепления |
| `langchain` | 1.4.6 | Минимум для `deepagents` 0.7.29 |
| `langchain-core` | 1.6.12 | Минимум для `deepagents` 0.7.29 |
| `langgraph` | 1.2.16 | Последняя в диапазоне `langchain` 1.4.6 (`>=1.2.12,<1.3`), вышедшая до `deepagents` 0.7.29 |
| `langchain-gigachat` | 0.5.1 | Единственная актуальная |
| `gigachat` | 0.2.3 | Уже в lock, в диапазоне коннектора (`<0.3`) |
| `langfuse` | 4.15.4 | Уже в lock; 5.x — мажорное обновление callback-handler, вне рамок |

Правило: `deepagents` — версия из зеркала; `langchain` и `langchain-core` — минимальные, которые она требует (меньше изменений для v1); `langgraph` — последняя совместимая, вышедшая не позже `deepagents`. Если какого-то пакета набора нет в зеркале, берётся ближайшая младшая совместимая версия, и таблица обновляется при реализации.

### D3. Установка в Docker через экспорт lock и `pip --require-hashes`

Builder-стадия ставит `poetry-plugin-export`, выгружает `poetry export --only main -f requirements.txt` (с хэшами) и ставит его `pip install --require-hashes -r requirements.txt`. Источник — аргумент сборки `PIP_INDEX_URL` (пусто → PyPI). Колесо приложения ставится `pip install --no-deps`, чтобы не было второго разрешения зависимостей. `docker-compose.yml` пробрасывает `PIP_INDEX_URL` из окружения в `build.args`.

Зеркало проксирует те же файлы, что PyPI, поэтому хэши из lock совпадают — это же проверяет, что зеркало отдаёт именно зафиксированные артефакты.

*Альтернативы:* (а) раскомментировать `sberosc` как источник в `pyproject.toml` — lock начинает ссылаться на источник контура, сборка вне контура ломается; (б) `poetry source add` во время сборки — меняет `pyproject.toml` внутри образа и требует `poetry lock`, что ломает воспроизводимость.

### D4. Тест фиксации и точек API

`tests/unit/test_dependency_pins.py`:
1. Читает `pyproject.toml` (`tomllib`), берёт все зависимости с `==` и сравнивает с `importlib.metadata.version`.
2. Импортирует точки API, на которые опирается v2 по §6 и §8 спецификации: `create_agent`, базовый класс middleware, `TodoListMiddleware`, `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware`, `ModelRetryMiddleware`, `ToolRetryMiddleware`; из `deepagents` — `SkillsMiddleware`, `FilesystemMiddleware`, `SummarizationMiddleware`; коннектор `GigaChat`; in-memory checkpointer. Точные пути импорта сверяются с установленной версией при реализации.

Тест не требует сети и LibreOffice (AR-Q2).

## Risks / Trade-offs

- [Обновление `langchain` / `langchain-core` / `langgraph` ломает v1 (`main_graph.py`, `bind_tools` в `llm_executor.Agent`)] → полный `pytest tests/unit` и интеграционные `tests/integration/v1_api`; ручной прогон одного запроса на GigaChat. При поломке — откатиться на минимальный `deepagents` (D2) и записать причину.
- [`langchain-gigachat` 0.5.1 несовместим с `langchain-core` 1.7] → выявится тестами v1 и spike S0.2; запасной путь — тот же откат по D2.
- [Зеркало контура отстаёт от PyPI: `deepagents` там 0.7.29 при 0.7.32 на PyPI] → набор D2 строится от версии из зеркала; наличие остальных пакетов проверяется до фиксации.
- [`deepagents` тянет `langchain-anthropic`, `langchain-google-genai`, `langsmith`] → лишний вес образа; LangSmith выключается в S4.3 (AR-Q14). Наличие пакетов в зеркале проверяется заранее.
- [`poetry-plugin-export` — ещё одна зависимость сборки] → ставится только в builder-стадии, версия фиксируется.

## Migration Plan

1. Обновить `pyproject.toml`, `poetry lock`, `poetry install` локально; прогнать тесты.
2. Обновить `Dockerfile` и `docker-compose.yml`; собрать образ с PyPI и с зеркалом.
3. Откат — revert коммита: старые lock и Dockerfile восстанавливаются целиком, данных и контракта API изменение не касается.

## Open Questions

- Точный URL зеркала PyPI для сборки в контуре (`sberosc` или `nexus-release`) — подставляется аргументом сборки и не влияет на подход.
