Story S2.4 · FR-5, FR-8 (владелец) · ADR-3 · §8 правила 6–7, стек №10 и №19 · §9 `todos`, `guard_counters` · spike S0.2 (C7), spike S0.3 (схема плана)

## Why

План строится (S2.3), но ничто не заставляет довести его до конца: модель может ответить при открытых шагах, и часть запроса молча теряется. FR-8 требует не принимать финальный ответ, пока есть шаги `pending` или `in_progress`, а после N возвратов — отвечать со списком невыполненного. FR-5 требует статусов `skipped` (с причиной) и `blocked` (ждёт ответа пользователя), которых нет во встроенном `write_todos` (spike S0.3). Без них модель не может честно закрыть шаг, который не получился или ждёт данных, и guard возвращал бы её к работе впустую.

## What Changes

- Своя схема плана: шаг `{content, status, reason}`, статусы `pending`, `in_progress`, `completed`, `skipped`, `blocked`; у `skipped` и `blocked` причина обязательна, иначе вызов `write_todos` отклоняется проверкой аргументов. Инструмент по-прежнему называется `write_todos`, ответ инструмента — по-русски.
- `PlanTodoList` (`TodoListMiddleware` с этой схемой) заменяет встроенный в `AgentFactory`; русские промпт и описание инструмента дополнены новыми статусами.
- `PlanCompletionGuard` (`after_model`, `jump_to="model"`): ответ без вызова инструмента при открытых шагах (`pending` / `in_progress`) не принимается — модель получает напоминание отдельным сообщением пользователя со списком открытых шагов (S0.2 C7), счётчик `guard_counters.plan_returns` растёт. После N возвратов ответ принимается с разделом «Не выполнено», а открытые шаги закрываются как `skipped` с причиной.
- Настройка `AGENT_PLAN_MAX_RETURNS` (по умолчанию 3). В режиме `AGENT_PLAN_MODE=off` guard не подключается (§8 п. 9).
- Тест «план прошлого хода» (S2.3) переходит на статус `blocked`.

**Вне рамок:** продолжение плана с `blocked` в следующем ходе через checkpointer и `TurnSummary` (S2.6); перевод шага в `blocked` по `missing_input` и вопрос пользователю — поведение модели по промпту, проверяется eval (S5.1) и сценарием U5 (S2.6); `AnswerGroundingGuard` (S4.1); лимиты вызовов и частичный ответ по лимиту (S2.5); доля доведённых планов — eval S5.1.

## Capabilities

### New Capabilities

—

### Modified Capabilities

- `agent-planning`: добавляются FR-5 (статусы шагов) и FR-8 (завершение плана) — владелец S2.4.

## Impact

- Новый `api/v2/middleware/todos.py` (схема плана, `PlanTodoList`); `middleware/plan.py` — `PlanCompletionGuard`; `factory.py` — замена `TodoListMiddleware` и guard в конце стека; `prompts/middleware.py` — статусы и причина; `config/agent/config.py` — `plan_max_returns`; README.
- Тесты: `tests/unit/v2/test_plan.py` (расширение), `test_runtime.py` (порядок и тип middleware, если проверяется).
- Поведение v1 не меняется.
