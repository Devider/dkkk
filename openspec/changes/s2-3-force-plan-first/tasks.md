# Tasks

## 1. Настройка

- [x] 1.1 `AgentSettings.plan_mode` (`AGENT_PLAN_MODE`, по умолчанию `auto`) и строка в README (D1). Проверка: тесты — значение по умолчанию, переопределение, неверное значение даёт ошибку с перечнем допустимых; тест README находит переменную

## 2. ForcePlanFirst

- [x] 2.1 `api/v2/middleware/plan.py`: `ForcePlanFirst(mode)`, `wrap_model_call` / `awrap_model_call` (D2, D3). Проверка: тесты со скриптовой моделью — без плана первый вызов видит только `write_todos`; после `write_todos` видны все инструменты; в `always` ответ без вызова отбрасывается и повторяется с `tool_choice="write_todos"` (в журнале 2 вызова, ответ без вызова в сообщениях отсутствует); в `auto` ответ без вызова — ответ хода, 1 вызов модели, `todos` пуст; план прошлого хода с `blocked` — набор не сужается
- [x] 2.2 `wrap_tool_call` / `awrap_tool_call` — `plan_required` (D4). Проверка: тесты — расчёт без плана получает конверт `plan_required`, инструмент не вызван, затем `write_todos` и тот же расчёт выполняется; `read_file` в `/skills/` пропускается, вне `/skills/` — `plan_required`
- [x] 2.3 `AgentFactory`: `ForcePlanFirst` после `TodoListMiddleware`, режим из настройки, `off` — без middleware (D5). Проверка: тесты — порядок middleware при `auto`; при `off` первый вызов видит все инструменты и расчёт без плана выполняется

## 3. Сквозные сценарии

- [x] 3.1 Сценарии `ScriptedChatModel` в `test_runtime.py` и `test_turn_context.py` начинаются с `write_todos` (D6). Проверка: тесты зелёные при режиме по умолчанию

## 4. Качество

- [x] 4.1 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [x] 4.2 `pylint src` > 7, `pytest tests/unit`, покрытие нового кода ≥ 80 %. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
