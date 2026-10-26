Story S3.1 · FR-18, AR-Q14 (владелец), AR-Q15 (валидация skills) · ADR-1, ADR-2 · §8 стек №8–9 · §10 `read_file` · §11 «Структура», «Активация» п. 1, «Хранение» · §13 безопасность · spike S0.3 (K4, K5, K6, K8, K9)

## Why

Сложные сценарии (подбор цели, сценарная сетка, запас по ковенантам) должны описываться декларативно — skill в `SKILL.md`, — и добавляться без правки кода агента (FR-18, цель G4). Агент v2 пока не знает о skills: в промпте нет каталога, нет `read_file`, а skill-инструменты не открываются. Spike S0.3 подтвердил механику deepagents (`SkillsMiddleware`, `FilesystemMiddleware` только с `read_file`), но показал, что невалидный skill молча пропускается — нужна своя проверка при старте (AR-Q15). AR-Q14 требует, чтобы у агента не было инструментов записи файлов и исполнения кода.

## What Changes

- Каталог skills `src/aigw_service/api/v2/skills/` (входит в пакет и образ); в этой story — без skills первой волны (S3.2–S3.4), только каталог с README о формате.
- Backend файлов агента: `CompositeBackend` — `/skills/` → `FilesystemBackend(root_dir=каталог skills, virtual_mode=True)` (выход за корень отклоняется), `/results/` → `StoreBackend` в пространстве `("results", user_id)` из runtime context (ADR-2); прочие пути не читаются.
- `SkillsMiddleware` (№8): каталог skills в системном промпте на русском; skill-инструменты каталога (`ToolCatalog.skill_tools()`) видны только после чтения skill, который их открывает (`metadata.include_tools`).
- `FilesystemMiddleware` (№9) только с `read_file`, русское описание инструмента; встроенный вынос результатов — страховка с высоким порогом в `/results/`.
- Валидация skills (`api/v2/skills_check.py`): фронтматтер, `name` по правилам Agent Skills и равный имени папки, непустой `description` ≤ 1024, `metadata.include_tools` — строка имён известных инструментов, тело ≤ 300 строк. Ошибки собираются все сразу; невалидный skill останавливает сборку агента и старт сервиса при `AGENT_ENGINE=v2` с понятной ошибкой.
- Тест списка инструментов: у агента нет `write_file`, `edit_file`, `ls`, `glob`, `grep`, `execute`, `task`.
- Запрет LangSmith (AR-Q14): при `LANGSMITH_TRACING=true` (или устаревшем `LANGCHAIN_TRACING_V2=true`) сервис не стартует — проверка в `lifespan` для обоих движков.

**Вне рамок:** skills `economic-answer` (S3.2), `target-seeking` и `scenario-analysis` (S3.3), `covenant-headroom` и `sensitivity-drivers` с их инструментами (S3.4); `AGENT_PINNED_SKILLS` (S3.5); запись результатов в `/results/` адаптером (S1.6); теги Langfuse и телеметрия (S4.3); подсказка «какой skill прочитать» при вызове скрытого инструмента — проверяется в S3.4 вместе с первым skill-инструментом.

## Capabilities

### New Capabilities

- `agent-skills`: skills агента — декларативные сценарии в `SKILL.md`, каталог в промпте, чтение через `read_file`, открытие инструментов, закрепление, проверка при старте, отсутствие инструментов записи и исполнения. Эта story добавляет FR-18 и AR-Q14.

### Modified Capabilities

- `agent-platform`: AR-Q15 получает сценарий «невалидный skill останавливает старт».

## Impact

- Новые `api/v2/skills/` (README), `api/v2/skills_check.py`, `api/v2/middleware/files.py` (backend, `SkillsMiddleware`, `FilesystemMiddleware`); `factory.py` — две middleware и валидация; `prompts/middleware.py` — русские промпт skills и описание `read_file`; `api/__init__.py` — проверка skills на старте при v2 и запрет LangSmith.
- Тесты: `tests/unit/v2/test_skills.py`.
- `deepagents` 0.7.29 уже закреплён (S0.1); тест закреплённых версий проверяет точки входа.
