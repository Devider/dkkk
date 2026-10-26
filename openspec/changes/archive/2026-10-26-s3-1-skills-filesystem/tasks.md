# Tasks

## 1. Каталог и валидация

- [x] 1.1 Каталог `api/v2/skills/` с `README.md` (формат skill) (D1). Проверка: каталог попадает в wheel (`poetry build`, список файлов)
- [x] 1.2 `api/v2/skills_check.py`: `validate_skills`, `SkillInfo`, `SkillsError` (D4). Проверка: тесты — валидный skill; по тесту на каждое нарушение (нет `SKILL.md`, нет фронтматтера, битый YAML, не словарь, нет `name` / `description`, `name` не по правилам или не равен папке, длинное `description`, `include_tools` не строка или с неизвестным инструментом, тело > 300 строк, посторонний файл в корне); все нарушения в одной ошибке; пустой каталог валиден

## 2. Файлы и skills в агенте

- [x] 2.1 `api/v2/middleware/files.py`: составной backend, `SkillsMiddleware`, `FilesystemMiddleware` только с `read_file`; русские промпт skills и описание `read_file` (D2, D3). Проверка: тесты — каталог skills в системном промпте на русском; `read_file` читает skill; путь вне `/skills/` и `/results/` и `..` — отказ; `/results/` своего пользователя читается, чужого — нет
- [x] 2.2 `AgentFactory`: middleware №8–9 перед планом, валидация skills в `build()`, параметр `skills_dir` (D3, D4). Проверка: тесты — порядок стека; список инструментов без записи и исполнения, вызов `write_file` отклонён; skill-инструмент скрыт до чтения и виден после; новый skill подключается без правки кода; невалидный skill — `SkillsError` при сборке
- [x] 2.3 `lifespan`: запрет LangSmith для обоих движков и сборка агента v2 на старте (D4, D5). Проверка: тесты — `LANGSMITH_TRACING=true` и `LANGCHAIN_TRACING_V2=true` останавливают старт; при v2 невалидный skill останавливает старт; при v1 агент v2 не собирается

## 3. Качество

- [x] 3.1 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [x] 3.2 `pylint src` > 7, `pytest tests/unit`, покрытие нового кода ≥ 80 %. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
