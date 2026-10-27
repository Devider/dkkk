# Tasks

## 1. Числа ответа и сравнение (design D2, D3)

- [ ] 1.1 `tools/grounding.py`: `Mention(start, end, written, value, decimals, percent)` и `answer_numbers(text)` на `_NUMBER.finditer` с исключениями: годы 1900–2100, номера пунктов в начале строки, числа, примыкающие к буквам (кроме множителя `x`/`х`), даты, целые 0–10 без дробной части и `%`. Тесты в новом `tests/unit/v2/test_grounding_guard.py`: каждое исключение, а также `2,50` (2 знака), `1 200`, `−12`, `12,5 %`, `3.5x`, число в ячейке таблицы Markdown
- [ ] 1.2 `confirm(mention, sources)`: допуск по записанным знакам, сравнение по модулю, для `%` — также источник × 100. Тесты: `412,3` ↔ `412.31` да, `3` ↔ `2.6` нет, `30` ↔ `-30` да, `12,5 %` ↔ `0.125` да, `12,5` (без `%`) ↔ `0.125` нет, `0` только с `0`. Регресс в `test_derive.py`: `tolerance` / `match` ведут себя как раньше (знак значим)

## 2. Детектор для eval (design D4)

- [ ] 2.1 `answer_check.py`: `unconfirmed_numbers(text, sources)`; `_sections` → публичная `sections` (внутренние вызовы переименованы, `test_answer.py` зелёный). Тест: ответ из сценария «Годы, номера и счёт не проверяются» даёт пустой список, ответ с `57,4` — одно вхождение

## 3. Guard (design D1, D5, D6, D7)

- [ ] 3.1 `prompts/middleware.py`: `GROUNDING_REMINDER` и `GROUNDING_CAVEAT` на русском (design D5, D6)
- [ ] 3.2 Новый `middleware/grounding.py`: `AnswerGroundingGuard(max_returns)` с `after_model` / `aafter_model`, счётчики `grounding_returns` / `grounding_removed`, журнал через позиционные аргументы loguru без чисел ответа. Снятие: замена на «н/д» справа налево (с `%`), оговорка в «Оговорки» / новый раздел `## Оговорки` / последний абзац, `AIMessage` с тем же `id`. Unit-тесты на состоянии (как `test_plan.py`): все сценарии требования FR-13, кроме eval; ответ с вызовами инструментов и ответ без чисел пропускаются; уникальные числа в напоминании в порядке появления; в оговорке нет снятых чисел; `max_returns=0`; `limit_hit` и `turn_timeout`
- [ ] 3.3 `config/agent/config.py`: `grounding_max_returns` (`AGENT_GROUNDING_MAX_RETURNS`, 1, `ge=0`); тест: `-1` отклоняется. README: строка в таблице параметров рядом с `AGENT_PLAN_MAX_RETURNS`
- [ ] 3.4 `factory.py`: guard после `extra_middleware`, перед `PlanCompletionGuard`; ставится и при `AGENT_PLAN_MODE=off`. Тест в `test_runtime.py`: порядок стека в `auto` и наличие guard в `off`

## 4. Сквозные проверки на собранном агенте (ADR-9)

- [ ] 4.1 `ScriptedChatModel` и фейковый движок: ответ с выдуманным числом → напоминание последним сообщением промпта второго вызова → исправленный ответ отдаётся без изменений; второй сценарий — модель повторяет число, ответ отдаётся с «н/д» и оговоркой, вызовов модели ровно 2
- [ ] 4.2 Взаимодействие с `PlanCompletionGuard`: ответ при открытом шаге с выдуманным числом сначала получает только напоминание плана (без напоминания о числах); после закрытия шагов числа проверяются. Ответ с `derive headroom` по значению `get_values` и порогу из вопроса принимается без возврата

## 5. Качество и трассировка

- [ ] 5.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` плюс новые файлы): новых замечаний ноль, старые не блокируют и попутно не правятся
- [ ] 5.2 `pylint src` — оценка > 7
- [ ] 5.3 `pytest tests/unit` — зелёный; покрытие нового кода (`middleware/grounding.py`, правки `tools/grounding.py`, `answer_check.py`, `factory.py`, настроек) ≥ 80 %
