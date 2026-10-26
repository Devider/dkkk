# Tasks

## 1. Копия

- [x] 1.1 Скопировать `lo_backend.py`, `excel_handler.py`, `russian_stemmer.py` в `src/excel_engine/` (в `excel_handler.py` — импорт `lo_backend` из пакета). Проверка: `python -c "import excel_engine.excel_handler"` проходит без LibreOffice
- [x] 1.2 Создать `matching.py` и `calculations.py` из определений замыкания (D1) в исходном порядке, `__init__.py` с публичным API; меняются только импорты. Проверка: `python -c "from excel_engine import analyze_excel_model, analyze_model_inputs_for_target"` проходит
- [x] 1.3 Добавить `excel_engine` в `packages` в `pyproject.toml`; исключить `src/excel_engine` из `ruff` до S5.4 (D3, риски). Проверка: `poetry build -f wheel` содержит `excel_engine/`; `ruff check src` не сообщает о файлах пакета

## 2. Тесты

- [x] 2.1 `tests/unit/test_excel_engine_copy.py`: AST-идентичность замыкания и модулей движка, отсутствие импортов `aigw_service.api.v1` и `langchain`. Проверка: тест зелёный; ручная правка одной строки в копии делает его красным
- [x] 2.2 `tests/integration/excel_engine/test_parity.py`: паритет v1 и копии на Methanex (EMA и IFT), пропуск без `uno`. Проверка: локально — `skipped`; в Docker-образе — `passed`

## 3. Качество

- [x] 3.1 `ruff check --fix` и `ruff format` по новым тестам. Проверка: новых замечаний нет
- [x] 3.2 `pylint src` > 7 и `pytest tests/unit`. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
- [x] 3.3 Отметить S0.7 в `openspec/traceability.md`. Проверка: строка S0.7 отмечена
