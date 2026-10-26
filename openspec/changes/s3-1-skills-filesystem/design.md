# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-skills/spec.md` (FR-18, AR-Q14) и `specs/agent-platform/spec.md` (AR-Q15). ADR-1, ADR-2, §8 №8–9, §10 «Каталог» (`read_file`), §11, §13, spike S0.3.

Состояние:
- deepagents 0.7.29: `SkillsMiddleware(backend, sources, system_prompt, tools)` — каталог в промпте (шаблон со слотами `{skills_locations}`, `{skills_load_warnings}`, `{skills_list}`, по умолчанию английский), skill-инструменты из `tools` видны после чтения skill с `metadata.include_tools`. `FilesystemMiddleware(backend, system_prompt=None, custom_tool_descriptions, tool_token_limit_before_evict=20000, tools=[...])` — своего промпта не добавляет; описание `read_file` английское.
- `CompositeBackend(default, routes, artifacts_root)`, `FilesystemBackend(root_dir, virtual_mode=True)` (отклоняет `..`), `StoreBackend(namespace=fn(runtime), store)` — проверены в S0.3 (K4–K6).
- Невалидный skill deepagents пропускает с предупреждением в журнале (K8). Правила его разбора: фронтматтер `---…---` в начале, YAML-словарь, непустые `name` и `description`; `name` — строчные буквы, цифры и одиночные `-`, ≤ 64, равен папке (нарушение — только предупреждение); `description` > 1024 обрезается; `metadata.include_tools` — строка через пробел.
- `ToolCatalog.skill_tools()` (S1.1) — инструменты с `availability != BASE`; сейчас таких нет (появятся в S3.4).
- `ForcePlanFirst` (S2.3) уже пропускает до плана `read_file` с `file_path` в `/skills/`.
- `get_agent_v2()` собирает агента лениво, при первом запросе; старт сервиса — `lifespan` в `api/__init__.py` (`APP_CTX.on_startup`).

## Goals / Non-Goals

**Goals:**
- Каталог skills в промпте, чтение skill и открытие инструментов — механикой deepagents; никаких инструментов записи и исполнения.
- Невалидный skill не может попасть в работающий сервис незамеченным.

**Non-Goals:**
- Содержимое skills первой волны (S3.2–S3.4), закрепление (S3.5), вынос результатов адаптером (S1.6).

## Decisions

### D1. Каталог и упаковка

`SKILLS_DIR = Path(api/v2/__file__).parent / "skills"`. Каталог входит в пакет `aigw_service` (Poetry включает не-`.py` файлы пакета), значит и в wheel и образ. `README.md` в корне каталога описывает формат и не считается skill (skill — подпапка с `SKILL.md`).

### D2. Backend (`middleware/files.py`)

```python
CompositeBackend(
    default=_Denied(),                      # любой путь вне маршрутов — «не найден»
    routes={"/skills/": FilesystemBackend(root_dir=skills_dir, virtual_mode=True),
            "/results/": StoreBackend(namespace=lambda rt: ("results", rt.context.user_id), store=store)},
    artifacts_root="/results/",
)
```
`default` — не `StateBackend`: чтение произвольного пути не должно находить файлы состояния; достаточно `StateBackend` с пустым состоянием, но явный пустой backend проще проверить. *Если* у `BackendProtocol` слишком широкий контракт для своей заглушки — `StateBackend()` (в состояние никто не пишет: инструментов записи нет); выбор фиксируется в коде с комментарием. Store — тот же, что у `TurnContext` (`APP_CTX.agent_memory.store` по умолчанию).

### D3. Middleware

- `SkillsMiddleware(backend, sources=["/skills/"], system_prompt=SKILLS_SYSTEM_PROMPT_RU, tools=catalog.skill_tools())`.
- `FilesystemMiddleware(backend, tools=["read_file"], custom_tool_descriptions={"read_file": READ_FILE_RU}, tool_token_limit_before_evict=AGENT_EVICT_TOKENS)` — страховка встроенного выноса с высоким порогом (§8 №9; основной вынос — S1.6). Порог — константа 20000 токенов (значение deepagents по умолчанию), без новой настройки.
- Порядок в `AgentFactory`: `TurnContext`, `LanguageGuard`, `SkillsMiddleware`, `FilesystemMiddleware`, `PlanTodoList`, `ForcePlanFirst`, …, `PlanCompletionGuard`, `TurnSummary` (§8 №8–9 перед №10).
- Русский промпт skills: каталог, «прочитай `SKILL.md` через `read_file` до плана, если запрос совпадает с описанием; передавай `limit=400`», «инструменты skill откроются после чтения». Описание `read_file`: что читает (`/skills/`, `/results/`), `offset`/`limit`, без упоминаний редактирования и мультимедиа.

### D4. Валидация (`api/v2/skills_check.py`)

`validate_skills(root: Path, known_tools: set[str]) -> list[SkillInfo]`, при нарушениях — `SkillsError` со всеми нарушениями сразу (`<папка>: <нарушение>` построчно). Правила — надмножество правил deepagents, всё, что он пропускает или правит молча, у нас ошибка:
- каждая подпапка содержит `SKILL.md`; файлы в корне, кроме `README.md`, — ошибка;
- фронтматтер по тому же шаблону, что у deepagents; YAML-словарь;
- `name` непустой, ≤ 64, `[a-z0-9]+(-[a-z0-9]+)*` (только ASCII — строже deepagents), равен имени папки;
- `description` непустой, ≤ 1024;
- `metadata.include_tools`, если есть, — строка имён через пробел; каждое имя есть в `known_tools` (все инструменты каталога);
- тело (после фронтматтера) ≤ 300 строк (§11).

`SkillInfo(name, description, include_tools)` — для тестов и журнала.

Где вызывается:
1. `AgentFactory.build()` — до `create_agent` (сборка с невалидным skill невозможна; тесты и `get_agent_v2`).
2. `lifespan` при `AGENT_ENGINE=v2` — `get_agent_v2()` до `yield`: агент собирается на старте, ошибка skills (и любая ошибка сборки) останавливает старт. Побочно — первый запрос не платит за сборку.

`AgentFactory(..., skills_dir=None)` — параметр для тестов; по умолчанию `SKILLS_DIR`.

### D5. Запрет LangSmith

`check_tracing(env)` в `api/v2`? Нет: запрет общий для сервиса (§13), поэтому функция в `aigw_service/core/tracing/` — `forbid_langsmith(environ)`: `LANGSMITH_TRACING` или `LANGCHAIN_TRACING_V2` в `{"true", "1", "yes"}` (без учёта регистра) → `RuntimeError` с понятным текстом. Вызов — первым в `lifespan` (оба движка). AR-Q3 не нарушается: LangChain не импортируется.

### D6. Тесты (`tests/unit/v2/test_skills.py`)

Временные каталоги skills в `tmp_path`:
- валидный skill → в системном промпте каталог с путём; `read_file` читает его (скриптовая модель), `read_file("/etc/passwd")`, `read_file("/skills/../.env")` — отказ;
- skill-инструмент (тестовый `ToolSpec` с `availability` skill в своём каталоге) скрыт до чтения и виден после;
- новый skill подключается без кода — два прогона с разным содержимым каталога;
- список инструментов агента; вызов `write_file` → ошибка неизвестного инструмента;
- `/results/` своего пользователя читается, чужого — нет (запись в store напрямую);
- валидация: по сценарию на каждое нарушение, сбор всех нарушений, пустой каталог валиден;
- `lifespan` при v2 с невалидным skill падает; `forbid_langsmith`.

## Risks / Trade-offs

- [`FilesystemBackend` документация deepagents предостерегает от веб-серверов] → `virtual_mode=True`, каталог только на чтение из образа, выход за корень отклоняется (K5); запись невозможна — инструментов нет.
- [Сборка агента на старте увеличивает время старта] → ~секунды; падение на старте лучше, чем на первом запросе (AR-Q15).
- [Приватные правила deepagents изменятся при обновлении] → версия закреплена (S0.1), наша валидация строже.

## Migration Plan

Только v2; v1 не затрагивается, кроме общего запрета LangSmith (в `.env` его нет).

## Open Questions

—
