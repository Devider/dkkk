Bugfix (вне story) · найдено при прогоне `pytest tests/unit` в S3.3 (`2026-10-27-s3-3-scenario-skills`, задача 4.2) · дефект S1.7 (`2026-10-27-s1-7-derive-values-describe`) · ADR-9

## Why

`derive` с операцией `crossing` может вернуть `x` чуть за краем отрезка, на котором ряд пересекает порог. Интерполяция `x1 + (t − y1)·(x2 − x1)/(y2 − y1)` при дроби, близкой к 1, из-за округления float даёт `x2 + 1 ulp`. Property-тест `test_derive.py::TestArithmetic::test_crossing_lies_in_its_segment` падает стабильно (hypothesis хранит пример: точки `(0, 0.01)`, `(936914848.8242188, 0)`, порог `6.59e-82` → `936914848.8242189`). Сценарий мастер-спеки `agent-tools` «Точка пересечения порога» требует `x` на этом отрезке.

На ответ аналитика это почти не влияет: расхождение в последнем разряде. Но красный unit-набор маскирует новые падения, а `AnswerGroundingGuard` сверяет числа ответа с `numbers` результата `derive` — значение вне сетки сбивает с толку.

## What Changes

- `tools/derive.py:crossing` — результат интерполяции ограничивается отрезком `[x1, x2]`.
- Регрессионный тест на пример из hypothesis рядом с property-тестом.

**Вне рамок:** другие операции `derive`, формат результата, точность интерполяции внутри отрезка.

## Capabilities

### New Capabilities

—

### Modified Capabilities

—

Восстановление поведения, уже описанного сценарием «Точка пересечения порога» в `openspec/specs/agent-tools/spec.md`; спецификации не меняются (`skip_specs: true`).

## Impact

- `src/aigw_service/api/v2/tools/derive.py` — одна строка в `crossing`.
- `tests/unit/v2/test_derive.py` — регрессионный пример.
