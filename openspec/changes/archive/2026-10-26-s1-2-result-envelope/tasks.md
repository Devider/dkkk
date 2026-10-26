# Tasks

## 1. Конверт и коды

- [x] 1.1 `api/v2/tools/envelope.py`: `ErrorCode` (13 кодов), `DEFAULT_HINTS`, `Number`, `ToolSuccess`, `ToolFailure`, `ToolError`, сериализация без пустых полей (D1). Проверка: тесты — у каждого кода есть непустая подсказка; ошибка без `hint` получает подсказку кода; неизвестный код отклоняется; сериализация не содержит `null` и пустых списков
- [x] 1.2 `failure_from_exception(exc)` (D2): `ToolError` → свой код, остальное → `internal_error` без текста исключения, исключение — в журнал через `logger.opt(exception=True)`. Проверка: тест — текст исключения с `{}` и путём не попадает в конверт и не роняет loguru

## 2. Адаптер и классификация

- [x] 2.1 `ModelIndex` бросает `ToolError(unknown_id)` вместо `KeyError`; `build_tool` возвращает `ToolSuccess` / `ToolFailure` (D2). Проверка: тест в `create_agent` — вызов с неизвестным ID даёт конверт `unknown_id`, домен не вызывается; непредвиденное исключение домена проходит наружу (его обработает `ToolErrorMiddleware`, S2.5)
- [x] 2.2 `classify_v1(status, result)` по таблице D3. Проверка: параметризованный тест на каждую строку D3 с текстами из `excel_engine/calculations.py`

## 3. Конверты расчётных инструментов

- [x] 3.1 `find_input_for_target` → конверт по D4 (сводка, сценарии, `numbers`, оговорка для недостижимой цели, без серверных путей). Проверка: тест на `content` из реального прогона IFT (S0.7) — `numbers` содержит Метанол = 379.233 и EBITDA = 1000; путей нет
- [x] 3.2 `run_scenarios` → конверт по D4 (строки `result_df`, `numbers` по выходам, без серверных путей). Проверка: тест на `result_df` из реального прогона EMA — 3 строки, значения EBITDA 714.427 / 1074.847 / 1435.268 в `numbers`; без `result_df` — пустые `rows`, сводка сохраняется
- [x] 3.3 Паритет на движке: `tests/integration/excel_engine/test_tool_parity.py` сравнивает `data` / `numbers` конверта с прямым вызовом `excel_engine`. Проверка: в Docker — `passed`, локально — `skipped`

## 4. Качество

- [x] 4.1 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [x] 4.2 `pylint src` > 7, `pytest tests/unit`, покрытие `api/v2/tools` ≥ 80 %. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
