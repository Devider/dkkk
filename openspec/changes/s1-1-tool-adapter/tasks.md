# Tasks

## 1. Контекст и адаптер

- [x] 1.1 `api/v2/__init__.py`, `api/v2/tools/__init__.py`, `api/v2/tools/context.py`: `ModelIndex` (перевод ID входов и выходов в имена, `KeyError` на неизвестный ID) и протокол `ToolContext`. Проверка: unit-тест перевода ID и ошибки на неизвестном ID
- [x] 1.2 `api/v2/tools/adapter.py`: `ToolSpec` и `build_tool` (D3): `runtime: ToolRuntime` скрыт из схемы, контекст из `runtime.context`, вызов в `asyncio.to_thread`, лишние поля отбрасываются. Проверка: тест в `create_agent` со скриптовой тестовой моделью — схема без служебных полей, доменная функция получает контекст хода, лишний `user_id` от модели до неё не доходит

## 2. Каталог и расчётные инструменты

- [x] 2.1 `api/v2/tools/catalog.py`: `ToolCatalog` (регистрация, `base_tools()`, `skill_tools()`, поиск по имени). Проверка: тест — инструмент skill есть в `skill_tools()` и отсутствует в `base_tools()`
- [x] 2.2 `api/v2/tools/calc_tools.py` и `default_catalog()`: `run_scenarios`, `find_input_for_target` по D4 на `excel_engine`, `uses_engine=True`. Проверка: тест с фейковым доменом — ID переведены в канонические имена, аргументы домена совпадают с v1 (`max_scenarios=1000`, `tolerance=0.1`, `output_years`), путь к файлу — из контекста; во всех схемах каталога нет служебных полей
- [x] 2.3 `tests/integration/excel_engine/test_tool_parity.py`: инструменты v2 и прямой вызов `excel_engine` с каноническими именами дают одинаковый результат на Methanex (пропуск без `uno`). Проверка: локально — `skipped`, в Docker-образе — `passed`

## 3. Границы

- [x] 3.1 `tests/unit/v2/test_boundaries.py` по D5 (LangChain только в `api/v2`, базовая линия домена, v2 не импортирует `main_graph` / `orchestrator_tools` / `services`). Проверка: тест зелёный; добавление `import langchain` в `excel_engine` или импорта `main_graph` в `api/v2` делает его красным

## 4. Качество

- [x] 4.1 `ruff check --fix` и `ruff format` по новым файлам. Проверка: новых замечаний нет
- [x] 4.2 `pylint src` > 7, `pytest tests/unit`, покрытие нового кода `api/v2/tools` ≥ 80 %. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`), `pytest --cov=src/aigw_service/api/v2` ≥ 80 %
