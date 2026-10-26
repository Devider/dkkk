# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-planning/spec.md`. ADR-3, §8 правила 6–7 и 9, стек №10 и №19, §9 `todos` / `guard_counters`, spike S0.2 (C7), spike S0.3 (решение по FR-5).

Состояние:
- `TodoListMiddleware` (langchain 1.4.6): `Todo{content, status: pending|in_progress|completed}`, `PlanningState.todos` (`OmitFromInput`), инструмент `write_todos` из `_write_todos` / `_awrite_todos` с `args_schema=WriteTodosInput`; ответ инструмента — английский `Updated todo list to [...]`. `after_model` отклоняет параллельные вызовы `write_todos`.
- `ForcePlanFirst` (S2.3) считает план существующим при непустом `todos`; `TurnContext` (S2.2) сохраняет `todos` в начале хода, если есть шаг `blocked`, и обнуляет `guard_counters`.
- Ошибка проверки аргументов инструмента в `create_agent` возвращается модели как `ToolMessage` со `status="error"`; инструмент не выполняется, `Command` не применяется.
- `after_model` с `@hook_config(can_jump_to=["model"])` может вернуть `{"messages": [...], "jump_to": "model"}`.
- GigaChat: последним сообщением промпта не может быть ответ модели — продолжает его и выдумывает данные (S0.2 C7).

## Goals / Non-Goals

**Goals:**
- Модель может честно закрыть шаг (`skipped` / `blocked` с причиной), и ход не завершается при открытых шагах.
- Граница возвратов и детерминированный ответ со списком невыполненного.

**Non-Goals:**
- Продолжение `blocked` в следующем ходе (S2.6); лимиты вызовов (S2.5); проверка чисел (S4.1).

## Decisions

### D1. Схема плана

`api/v2/middleware/todos.py`:
- `PlanStep(BaseModel)`: `content: str` (непустой), `status: Literal["pending", "in_progress", "completed", "skipped", "blocked"]`, `reason: str | None`; `model_validator`: для `skipped` / `blocked` `reason` обязателен и непуст.
- `WritePlanInput(BaseModel)`: `todos: list[PlanStep]`.
- `PlanStepState(TypedDict)` + `PlanState(AgentState)`: `todos: Annotated[NotRequired[list[PlanStepState]], OmitFromInput]` — то же поле, что у встроенной схемы, поэтому `ForcePlanFirst` и `TurnContext` не меняются.
- В состояние шаги пишутся как словари без `None` (`model_dump(exclude_none=True)`).

### D2. `PlanTodoList(TodoListMiddleware)`

Наследник: `state_schema = PlanState`, в `__init__` после `super().__init__` инструмент заменяется на `StructuredTool` с тем же именем `write_todos`, `args_schema=WritePlanInput` и своими `func` / `coroutine`. Ответ инструмента — `План обновлён:` и строки `N. [status] content — reason`. Промпт и `after_model` (запрет параллельных вызовов) наследуются.

*Альтернатива:* свой инструмент `update_plan` (ADR-3 отверг) или patch встроенного `Todo` — отвергнуты: имя `write_todos` уже в промптах и в `ForcePlanFirst`, а patch модуля langchain ломается при обновлении.

### D3. `PlanCompletionGuard`

`after_model` / `aafter_model`, `@hook_config(can_jump_to=["model"])`:
1. Последнее сообщение — `AIMessage` без `tool_calls`; иначе `None`.
2. Открытые шаги — `pending` и `in_progress`; нет — `None` (`blocked` и `skipped` закрыты: ход завершается ответом с вопросом или с причиной пропуска).
3. `guard_counters.plan_returns < N` → `{"messages": [HumanMessage(напоминание)], "guard_counters": {..., "plan_returns": n + 1}, "jump_to": "model"}`. Ранний ответ остаётся в истории, за ним — напоминание: последним сообщением промпта становится сообщение пользователя (C7). Напоминание перечисляет открытые шаги и требует довести их или закрыть `skipped` / `blocked` с причиной.
4. Иначе ответ принимается: последний `AIMessage` заменяется (тот же `id`) текстом ответа + раздел «Не выполнено» со списком открытых шагов; открытые шаги → `skipped` с причиной «Не выполнено к концу хода».

Язык раздела и напоминания — по последнему сообщению пользователя (`detect_language`, S2.2): `en` → «Not done» / английское напоминание, иначе русский.

*Альтернатива:* удалять ранний ответ (`RemoveMessage`) — отвергнута: модель видит, что уже ответила, и напоминание объясняет, почему ответ не принят; история хода остаётся честной для журнала.

### D4. Настройка и сборка

`AgentSettings.plan_max_returns: int = 3` (`AGENT_PLAN_MAX_RETURNS`, ≥ 0). `AgentFactory`: `PlanTodoList` вместо `TodoListMiddleware` всегда (статусы нужны и в `off`); при режиме не `off` — `ForcePlanFirst` после него (№11) и `PlanCompletionGuard(max_returns)` последним в стеке (№19: `after_*` идут снизу вверх, поэтому guard проверяет ответ первым).

### D5. Промпты

`TODO_SYSTEM_PROMPT` и `TODO_TOOL_DESCRIPTION`: пять статусов; `skipped` — шаг не удался после попыток или не нужен, с причиной; `blocked` — не хватает данных от пользователя (год, цель, диапазон), с причиной, и ответ заканчивается вопросом о недостающем; финальный ответ — только когда открытых шагов нет.

## Risks / Trade-offs

- [Модель отвечает тем же текстом после каждого напоминания] → граница N и детерминированный раздел «Не выполнено»; доля доведённых планов — eval S5.1.
- [Модель закрывает шаги `skipped`, чтобы обойти guard] → причина обязательна и видна в ответе и журнале; качество — eval S5.1.
- [Ранние ответы и напоминания остаются в истории сессии] → сжатие истории — S2.7; `TurnSummary` — S2.6.

## Migration Plan

Только v2. Выключение без изменения кода — `AGENT_PLAN_MODE=off` (схема плана остаётся) или `AGENT_PLAN_MAX_RETURNS=0` (сразу ответ с «Не выполнено»).

## Open Questions

—
