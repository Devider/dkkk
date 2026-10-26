# Design

## Context

Мотивация — proposal.md. Ограничения: §2 (тела функций не правятся, разрешено менять место и способ вызова), §7 правило 5 (v1 и v2 не импортируют друг друга; общий код — домен и инфраструктура), AR-Q3 (в расчётном коде нет новых импортов LangChain).

Замыкание зависимостей (AST-анализ `tools.py`, 2026-10-26) двух функций — 26 имён верхнего уровня:
- расчёт: `analyze_model_inputs_for_target`, `analyze_excel_model`, `generate_scenarios`, `test_scenarios`, `_formula_scalar`, `optimize_with_regression`, `get_output_cell_ref`, `generate_result_message`, `save_analysis_results`, `create_scenario_matrix`, `generate_min_max_scenarios`, `_humanize_error`, `get_store_file`, модели `ExcelAnalysisToolResult`, `ModelInputAnalysisToolResult`, константы `TEMP_DIR`, `MAX_SCENARIOS`, кэш `_ANALYSIS_CACHE*`, `logger`;
- смысловой слой: `normalize_text`, `jaccard_similarity`, `extract_year_from_query`, `create_input_mapping`, `create_output_mapping`, `find_matching_cell`, `find_matching_outputs` (+ `russian_stem` из `russian_stemmer.py`).

Внешние зависимости замыкания: `ExcelWorkbook` (`excel_handler.py` → `lo_backend.py`), `APP_CTX` (логгер, `agent_memory.store` в `get_store_file`), `numpy`, `pandas`, `openpyxl`, стандартная библиотека. `langchain` замыкание не использует (декоратор `@tool` — только у других функций `tools.py`). `uno` импортируется лениво, поэтому пакет импортируется без LibreOffice.

## Goals / Non-Goals

**Goals:**
- Пакет `excel_engine` даёт те же результаты, что v1, и не импортирует `aigw_service.api.v1`.
- Расхождение копии с оригиналом ловится автоматически, пока живы обе.

**Non-Goals:**
- Рефакторинг, типизация, линтинг старых замечаний в копии: копия должна совпадать с оригиналом по AST.

## Decisions

### D1. Раскладка пакета

| Файл | Содержимое |
| --- | --- |
| `excel_engine/__init__.py` | Публичный API: `analyze_model_inputs_for_target`, `analyze_excel_model`, модели результатов, `ExcelWorkbook` |
| `excel_engine/lo_backend.py` | Копия `api/v1/lo_backend.py` |
| `excel_engine/excel_handler.py` | Копия `api/v1/excel_handler.py`, импорт `lo_backend` из пакета |
| `excel_engine/russian_stemmer.py` | Копия `api/v1/russian_stemmer.py` |
| `excel_engine/matching.py` | Копия функций смыслового слоя из замыкания |
| `excel_engine/calculations.py` | Копия расчётных функций, помощников, моделей, кэша, `get_store_file` |

Порядок определений внутри файлов — как в оригинале. Меняются только строки импорта.

*Альтернатива:* импорт смыслового слоя из `tools.py` (вариант «расчёт + движок») — отвергнута разработчиком: пакет должен быть самодостаточным.

### D2. Инфраструктура остаётся общей

`calculations.py` импортирует `APP_CTX` из `aigw_service.context`, как оригинал: логгер и `get_store_file` — инфраструктура, общая для v1 и v2 (§7 п. 5). Адаптер v2 (S1.1) передаёт абсолютный путь к файлу в `file_name` и пустой `user_id`: тогда `get_store_file` не вызывается, а `os.path.join(TEMP_DIR, <абсолютный путь>)` даёт сам путь — тело функции менять не нужно. Это фиксируется для S1.1, в S0.7 не реализуется.

### D3. Проверки

1. **AST-идентичность** (`tests/unit/test_excel_engine_copy.py`, без LibreOffice): для каждого имени замыкания `ast.dump` определения в копии равен оригиналу; для `lo_backend.py`, `excel_handler.py`, `russian_stemmer.py` равны модули целиком без учёта строк импорта и докстринга модуля.
2. **Границы пакета** (тот же файл): ни один модуль `excel_engine` не импортирует `aigw_service.api.v1` и `langchain` (AST импортов).
3. **Паритет** (`tests/integration/excel_engine/test_parity.py`, пропуск без `uno`): на `models/Methanex_FinModel.xlsx` v1 и копия возвращают одинаковые `status`, `result` и `content` для фиксированного набора вызовов — EMA (1 вход × 3 точки, 2 выхода) и IFT (1 вход, достижимая цель). Кэш обеих копий сбрасывается перед сравнением. Запускается в Docker-образе (LibreOffice есть только там).

### D4. Сборка

`pyproject.toml`: `{ include = "excel_engine", from = "src" }` в `packages`. Dockerfile копирует `src/` целиком — правка не нужна; `PYTHONPATH=/app/src` уже есть.

## Risks / Trade-offs

- [Две копии смыслового слоя и движка расходятся при правке одной] → тест AST-идентичности падает при любом расхождении; правка разрешена только одновременно в обеих (или после S5.4 — только в копии).
- [Копия удваивает объём кода до S5.4] → принято разработчиком.
- [Тест паритета требует LibreOffice] → в CI без `soffice` пропускается; прогон в Docker — часть этой story.
- [Старые замечания ruff/pylint в копии] → копия не линтится с `--fix` (иначе разойдётся с оригиналом по AST); каталог исключается из `ruff` до S5.4.

## Migration Plan

Добавление пакета, v1 не меняется. Откат — удаление `src/excel_engine/` и строки в `packages`.
