Story S2.3 · FR-3, FR-4 (владелец) · ADR-3 · §8 правила 2 и 9, стек middleware №11 · §10 код `plan_required` · spike S0.2 (C3)

## Why

План хода есть (`TodoListMiddleware`, S2.1), но ничто не заставляет модель его строить: она может сразу вызвать расчёт, и составной запрос теряет шаги. FR-3 требует, чтобы в режиме плана первым вызовом хода (не считая чтения skill) был `write_todos`. FR-4 требует хода без плана для вопросов без расчёта и выбора режима через `AGENT_PLAN_MODE`. Spike S0.2 показал, что GigaChat не поддерживает обязательный вызов «любого из набора», а вызов по имени на запросе без расчёта даёт выдуманный план. Поэтому механика — сужение набора инструментов до плана, а не принудительный `tool_choice` на каждом ходе.

## What Changes

- Настройка `AGENT_PLAN_MODE = always / auto / off`, по умолчанию `auto` (решение spike S0.2, §16).
- `ForcePlanFirst` (`api/v2/middleware/plan.py`), `wrap_model_call` + `wrap_tool_call`:
  - пока в состоянии нет плана, модели видны только `write_todos` и `read_file` при `tool_choice = auto`;
  - в `always` ответ без вызова инструмента отбрасывается, и модель вызывается повторно с `tool_choice = write_todos` (один раз);
  - в `auto` ответ без вызова принимается — это ход без плана;
  - вызов любого другого инструмента (или `read_file` вне `/skills/`) без плана получает конверт ошибки `plan_required`, инструмент не выполняется.
- `AgentFactory` ставит `ForcePlanFirst` сразу после `TodoListMiddleware` (§8 №11); в режиме `off` middleware не подключается.
- Сценарии существующих сквозных тестов начинаются с `write_todos`.

**Вне рамок:** `PlanCompletionGuard` и расширенные статусы `skipped` / `blocked` (S2.4); продолжение плана с `blocked` в следующем ходе (S2.6, здесь план прошлого хода с `blocked` уже считается существующим — правило S2.2); `read_file` и skills (S3.1, здесь только разрешение на имя и путь `/skills/`); покрытие действий запроса планом ≥ 90 % и ход без плана на GigaChat — eval S5.1; лимиты вызовов (S2.5).

## Capabilities

### New Capabilities

- `agent-planning`: план хода — декомпозиция запроса в шаги, режимы плана, статусы шагов и доведение плана до конца. Эта story добавляет FR-3 и FR-4.

### Modified Capabilities

—

## Impact

- Новый `api/v2/middleware/plan.py`; `factory.py` — `ForcePlanFirst` и режим плана; `config/agent/config.py` — `plan_mode`; README — строка `AGENT_PLAN_MODE`.
- Тесты: новые `tests/unit/v2/test_plan.py`; сценарии `ScriptedChatModel` в `test_runtime.py` и `test_turn_context.py` получают шаг `write_todos`.
- Поведение v1 не меняется.
