# Tasks

## 1. Настройка `AGENT_PINNED_SKILLS`

- [x] 1.1 `config/agent/config.py`: поле `pinned_skills: Annotated[tuple[str, ...], NoDecode]`, по умолчанию `("economic-answer",)`, `validation_alias="AGENT_PINNED_SKILLS"`, `field_validator(mode="before")`. Валидатор делит по запятой, убирает пробелы, пустые элементы и повторы, сохраняет порядок; список или кортеж нормализует так же (design D1). Проверка: `tests/unit/v2/test_pinned_skills.py::TestSettings` — без переменной `("economic-answer",)`; `" economic-answer , target-seeking,economic-answer"` → `("economic-answer", "target-seeking")`; `""` → `()`; `","` → `()`
- [x] 1.2 README, таблица «Параметры агента»: строка `AGENT_PINNED_SKILLS` | `economic-answer` | v2: skills, закреплённые на все ходы (имена через запятую, пусто — ничего). Неизвестное имя останавливает старт. Без `economic-answer` структура ответа FR-12 не гарантируется. Каждый skill расходует бюджет `AGENT_MAX_PROMPT_TOKENS`. Проверка: строка есть в таблице, значение по умолчанию совпадает с `AgentSettings`

## 2. Сборка агента из настроек

- [x] 2.1 `api/v2/factory.py`: `pinned_skills: Sequence[str] | None = None`, `None` → `APP_CONFIG.agent.pinned_skills`; удалить `DEFAULT_PINNED`, добавить `ANSWER_SKILL = "economic-answer"` (design D2). Проверка: тест — `AgentFactory(...)` без аргумента берёт список из настроек (через `monkeypatch` на `APP_CONFIG.agent.pinned_skills`), явный аргумент имеет приоритет
- [x] 2.2 `build()`: если `ANSWER_SKILL` нет в списке, `logger.warning` (loguru, без f-строк с текстом исключения) о том, что структура FR-12 не гарантируется; проверка неизвестного имени не меняется (design D3). Проверка: тест — список `("target-seeking",)` собирается, предупреждение записано (перехват sink loguru); список с `no-such-skill` → `SkillsError` с этим именем; `()` собирается без ошибки и с предупреждением
- [x] 2.3 `tests/unit/v2/test_answer.py::TestSkill::test_shipped_and_valid`: вместо `DEFAULT_PINNED` проверить `AgentSettings().pinned_skills == ("economic-answer",)`. Проверка: `rg DEFAULT_PINNED src tests` пусто, `pytest tests/unit/v2/test_answer.py` зелёный
- [x] 2.4 Проверка размера (design D5): сумма `context_tokens` закреплённых `SKILL.md` больше `prompt_budget.keep_tokens` → `SkillsError` с размером каждого skill и хвоста. Проверка: тест — `TWO` при хвосте 2 000 не собирается, при хвосте 6 000 собирается; `lifespan` с хвостом 1 500 не стартует

## 3. Сценарии FR-19 на скриптовой модели

- [x] 3.1 Два закреплённых skill (`economic-answer`, `target-seeking`), два хода одной сессии. Проверка: в `model.calls[0]["messages"]` по одному сообщению `lc_source="pinned_skill"` на каждый skill, во втором ходе новых копий нет, вызова `read_file` в сценарии нет
- [x] 3.2 Закреплённый skill после сжатия истории: бюджет 20 000 (хвост 6 000 вмещает оба skill, design D5). Проверка: ни в одном вызове нет второй копии; вызов сразу после первого сжатия видит оба закреплённых skill
- [x] 3.3 Закреплённый `sensitivity-drivers`: модель сразу вызывает `trace_dependencies` без `read_file`. Проверка: инструмент выполнился (обычный конверт, кода `skill_required` нет); тот же сценарий без закрепления даёт `skill_required`
- [x] 3.4 Незакреплённый skill: при списке `("economic-answer",)` текста `target-seeking` в контексте нет до `read_file`. Проверка: в первом вызове модели нет сообщения с `<skill name="target-seeking"`, каталог в системном промпте содержит его путь
- [x] 3.5 Контракт API (design D4): `httpx.AsyncClient` + `ASGITransport`, `AGENT_ENGINE=v2`, скриптовая модель, один запрос при двух списках закрепления (по умолчанию и `economic-answer,target-seeking`). Проверка: одинаковые статус и заголовки ответа, в ZIP тот же набор файлов; в схеме тела `/api/v1/invoke-agent` нет поля про skills
- [x] 3.6 Неизвестное имя останавливает старт: `lifespan` при `AGENT_ENGINE=v2` и `pinned_skills=("no-such-skill",)` (сброс `get_agent_v2.cache_clear()` до и после). Проверка: исключение `SkillsError` с именем `no-such-skill`

## 4. Качество

- [x] 4.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` + новые файлы). Проверка: новых замечаний нет, старые попутно не правятся
- [x] 4.2 `pylint src` > 7, `pytest tests/unit`; новый код типизирован, покрытие изменённого кода `config/agent/config.py` и `factory.py` ≥ 80 % (AR-Q15). Проверка: оценка > 7, тесты зелёные, покрытие по двум модулям ≥ 80 %
