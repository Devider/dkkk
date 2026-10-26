Story S0.7 · §2 «Неизменяемые компоненты», §7 правило зависимостей 5, §16 открытый вопрос «Организация кода» · AR-Q3

## Why

Агент v2 вызывает расчёт через свой адаптер и пул процессов (ADR-5, ADR-6), а v1 должен продолжать работать на своём коде без изменений, пока eval не покажет паритет (S5.3). Поэтому расчётные функции и движок пересчёта копируются в отдельный пакет `src/excel_engine/`, который импортирует адаптер v2. Код v1 не трогается и удаляется вместе с v1 в S5.4.

## What Changes

- Новый пакет `src/excel_engine/` с полным замыканием двух расчётных функций (решение разработчика 2026-10-26):
  - `lo_backend.py`, `excel_handler.py` — копии движка;
  - `russian_stemmer.py` — копия стеммера;
  - `matching.py` — копии функций смыслового слоя, нужных расчёту (`normalize_text`, `jaccard_similarity`, `extract_year_from_query`, `create_input_mapping`, `create_output_mapping`, `find_matching_cell`, `find_matching_outputs`);
  - `calculations.py` — `analyze_model_inputs_for_target`, `analyze_excel_model` и их помощники (сценарии, подбор, регрессия, сохранение результатов, сообщения, кэш, модели результатов, `get_store_file`).
- Тела функций копируются без изменений; меняются только строки импорта (`aigw_service.api.v1.*` → `excel_engine.*`).
- Пакет добавляется в сборку (`pyproject.toml`, `packages`).
- Тесты: идентичность копии оригиналу по AST, отсутствие импортов `aigw_service.api.v1` и `langchain` в пакете, паритет результатов на `models/Methanex_FinModel.xlsx` (с LibreOffice; без него — пропуск).

**Вне рамок:** адаптер и инструменты v2 (S1.1, S1.4); пул и блокировки (S1.5); изменение поведения расчётов; удаление кода v1 (S5.4).

## Capabilities

### New Capabilities

—

### Modified Capabilities

—

Поведение системы не меняется: копия кода без изменения логики (`skip_specs: true`).

## Impact

- Новый пакет `src/excel_engine/` (≈ 1 600 строк копии).
- `pyproject.toml` — `packages`.
- `tests/unit/test_excel_engine_copy.py`, `tests/integration/excel_engine/test_parity.py`.
- Пока v1 жив, смысловой слой и движок существуют в двух экземплярах; тест AST-идентичности ловит расхождение.
