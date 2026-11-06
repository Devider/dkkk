# Proposal

## Why

Сервис сценарного анализа возвращает ложные `0`/`None` для целевых показателей на «тяжёлых» Excel-моделях: принудительный полный пересчёт LO (`doc.calculateAll()`) валит ячейки `CHOOSE` с >30 аргументами в `#VALUE!`, который каскадно обнуляет выходы (Net Debt/EBITDA, FCFF). Режим пересчёта расходится с Excel/десктопным LibreOffice, которые считают те же модели в числа.

## What Changes

- **Инкрементальный пересчёт как основной** (`doc.calculate()` вместо `doc.calculateAll()`) на всех этапах LO-конвейера: при открытии книги, в `ExcelWorkbook.calculate()`, в `get_compiled_func().evaluate()` после записи входов.
- **`CalcBook.calculate()`** — новый «мягкий» метод (инкрементальный, только «грязные» ячейки); `calculate_all()` остаётся как отладочный вызов, вне пути сценариев.
- **Уведомление о непосчитанных выходах в ответе тула**: если выход после пересчёта не посчитался, `analyze_excel_model` (и связанные) перечисляет его в `ExcelAnalysisToolResult.content` — только при наличии проблем, при полном успехе ничего не пишет.
- **Различение причины непросчёта**: «формула дала ошибку (`#VALUE!` и т.п.)» vs «легитимный `0`/`None`» — через признак ошибки ячейки из LO-моста, донесённый гибридно (точечная допроверка `None`-ячеек в `evaluate()` через `cell.getError()`, т.к. `getDataArray` для ошибки даёт `None`; см. design D2).
- Не меняем HTTP-контракт; добавляется только поле о непосчитанных внутри `content`.

Никаких **BREAKING** изменений внешнего HTTP API — только внутренняя логика пересчёта и семантика `content`.

## Capabilities

### New Capabilities
- `excel-model-and-calc-backend`: пересчёт Excel-моделей через LO/Calc — инкрементальный `calculate()` как основной режим (поведение Excel/десктопного LO), `calculateAll()` только отладочно, и явное уведомление о непосчитанных выходах в результате аналитических инструментов с различением причины (ошибка формулы vs легитимный `0`/`None`).

### Modified Capabilities

_Нет — capability `excel-model-and-calc-backend` не существовала (удалена откатом git); требования вводятся как ADDED в новой спеки._

## Impact

- **Код**: `src/aigw_service/api/v1/lo_backend.py` (метод `CalcBook.calculate()` + документация разницы режимов), `src/aigw_service/api/v1/excel_handler.py` (замена `calculate_all()` → `calculate()` в 3 местах: `_open`/`calculate`/`evaluate`), `src/aigw_service/api/v1/tools.py` + структура `ExcelAnalysisToolResult.content` (поле о непосчитанных).
- **Модели (данные для проверки, не код)**: `models/06.10.2026.bak.xlsx` — «поломаная» (регрессионный кейс); `models/06.10.2026.xlsx`, `models/06.10.2026.fixed.xlsx` — исправленные (эталон acceptance).
- **Поведение**: на сценарии «курс RUB/USD 80–100 к 2027» тул возвращает меняющиеся числа, а не плоские `None`/`0.0`; при невозможности посчитать выход — явно это указывает.
- **Тесты/линтеры**: `pytest tests`, `ruff check src`, `ruff format src`; устаревший `tests/test_tools_performance.py` не затрагивается.
- **Вне скоупа**: патч `CHOOSE`→`INDEX` в памяти для «грязной» тяжёлой ячейки (отдельная задача); name-resolution; `test_tools_performance.py`.