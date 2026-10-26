# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-planning/spec.md` (FR-10) и `specs/agent-loop/spec.md` (FR-16). ADR-8, AR-Q7, §8 №20, §9 (слой «Память диалога», поля `turn_summaries` / `step_results`, правила 1, 4, 5), §12 «Сводка хода на сервере», §14 U5 и U8.

Состояние:
- `router.py` уже передаёт `config.configurable.thread_id = "{user_id}:{session_id}"`; `get_agent_v2()` собирает агента без checkpointer — каждый ход начинается с пустого состояния.
- `APP_CTX.agent_memory.checkpointer` — `MemorySaver` процесса; им же пользуется v1. Движки не работают одновременно (`AGENT_ENGINE` + перезапуск).
- `TurnContext` (S2.2) в начале хода сбрасывает `step_results` / `guard_counters` и сохраняет `todos` с шагами `blocked`; `ForcePlanFirst` (S2.3) считает такой план планом хода; `PlanCompletionGuard` (S2.4) считает `blocked` закрытым.
- `resolved_terms` (S1.3) — словарь сессии «термин → `{id, name, confidence}`», переживает ходы при checkpointer.
- `step_results` никто не пишет — это S1.6. Результаты хода есть только в `ToolMessage` (конверт `{status, result, numbers, code, …}` или текст `write_todos`).
- Внутри хода `PlanCompletionGuard` добавляет `HumanMessage`-напоминания: «последнее сообщение пользователя» не равно началу хода.
- Карточка модели уже добавляется блоком к единственному системному сообщению (`TurnContext._append`).

## Goals / Non-Goals

**Goals:**
- План, термины и выжимки переживают ход; U5 и U8 проходят на скриптовой модели.
- Выжимка детерминирована, без LLM, компактна.

**Non-Goals:**
- Внешний checkpointer, блокировка хода, Langfuse, `step_results` (см. proposal).

## Decisions

### D1. Checkpointer

`get_agent_v2()` передаёт `checkpointer=APP_CTX.agent_memory.checkpointer`. `AgentFactory` по-прежнему принимает любой checkpointer (тесты — `InMemorySaver`). Внешняя БД подключается позже заменой объекта за тем же интерфейсом (ADR-8).

### D2. Граница хода

`TurnContext._turn_update` пишет `turn_message_id` — `id` последнего `HumanMessage` во входном состоянии (сообщение пользователя этого хода; `add_messages` уже присвоил ему `id`). Поле — в `CopilotAgentState`. Сообщения хода — все после этого `id`. Напоминания guard и будущее сжатие истории (S2.7 сохраняет хвост) границу не ломают.

### D3. Выжимка хода (`TurnSummary.after_agent`)

```
{turn_id, question, plan: [{content, status, reason?}],
 results: [{tool, status, code?, summary, numbers: [{label, value}]}]}
```
- `question` — текст сообщения хода, до 200 символов.
- `results` — `ToolMessage` хода, кроме `write_todos`; конверт разбирается из JSON; `summary` = `result` (успех) или `error` (ошибка), до 300 символов; `numbers` — первые 8 чисел результата (`label`, `value`). Нечитаемый ответ инструмента → `{tool, status: "ok", summary: первые 300 символов}`.
- Всего чисел в выжимке — не больше 16.
- Выжимка не строится, если в ходе нет ни плана, ни результатов (приветствие, ответ из памяти, отказ) или ход уже подведён (`turn_summaries[-1].turn_id == turn_id` — ход завершился в `before_agent` без нового `turn_id`).
- `turn_summaries` = последние 5 (`[*old, new][-5:]`, перезапись поля).
- Журнал: `logger.info("Turn summary {}: {}", thread_id, json)` — без текстов исключений (в конвертах их нет, FR-14).

*Отклонение от §9 правила 4:* источник — `ToolMessage` хода, а не `step_results`: `step_results` появятся в S1.6. Формат выжимки совпадает с тем, что `step_results` даст, поэтому замена источника не меняет выжимку.

### D4. Блок памяти в промпте (`TurnSummary.wrap_model_call`)

К системному сообщению (через тот же `_append`, вынесенный из `turn_context.py` в общий модуль) добавляется, если есть что показать:

```
## Память диалога
Сопоставленные термины (ID используй без повторного resolve_terms):
- «метанол» → IN1 (Метанол)
Прошлые ходы:
1. «EBITDA 2026 при цене метанола 300–500?» План: 2 из 2 выполнено. run_scenarios — Сценариев: 3. … Числа: EBITDA при Метанол=300: 1 200; …
Ждут ответа пользователя:
- Сценарии EBITDA — нет года расчёта
```
Термины — из `resolved_terms` (сессия), выжимки — из `turn_summaries`, шаги — `blocked` из текущего `todos`. Блок строится из состояния при каждом вызове модели (§9 правило 5: план, термины и выжимки живут вне сообщений).

### D5. Промпт плана

`TODO_SYSTEM_PROMPT`: «Если в плане есть шаги `blocked` прошлого хода — по ответу пользователя переведи их в `in_progress` и доведи; новый план не строй». Системный промпт агента уже требует ID только из `resolve_terms`; блок памяти явно разрешает ID из памяти.

### D6. Место в стеке

`TurnSummary` — последним в списке (§8 №20): его `wrap_model_call` самый внутренний (блок памяти добавляется после карточки и правил плана), `after_agent` выполняется первым из `after_agent`-хуков — других пока нет. `PlanCompletionGuard` остаётся перед ним; порядок `after_model` (снизу вверх) не меняется, у `TurnSummary` `after_model` нет. В режиме `off` `TurnSummary` подключается тоже — память не зависит от режима плана.

### D7. Тесты

`tests/unit/v2/test_memory.py` на скриптовой модели с `InMemorySaver` и реальным каталогом (`InlineGate`, анализатор и движок подменены):
- U5: ход 1 — план, `resolve_terms`, `run_scenarios` без года → `missing_input`, `write_todos` с `blocked`, вопрос; ход 2 «2026» — первый вызов видит все инструменты и блок памяти с ID и шагом `blocked`, `run_scenarios` с годом, план закрыт, `resolve_terms` в ходе 2 нет.
- U8: ход 1 — расчёт; ход 2 «почему DSCR просел?» в `auto` — один вызов модели без инструментов, в промпте выжимка с числами хода 1.
- FR-16 «а если то же на 2027?» — расчёт без `resolve_terms`.
- Изоляция `thread_id`, лимит 5 выжимок, ход без плана не добавляет выжимку, журнал.

## Risks / Trade-offs

- [`MemorySaver` растёт без TTL (§16 решено: TTL не вводим)] → как в v1; внешняя БД — открытый вопрос §16.
- [Блок памяти растёт с числом терминов] → термины только текущей книги (сброс при смене файла), выжимок ≤ 5, чисел ≤ 16 на выжимку; размер слоёв замеряет S2.7.
- [История `messages` растёт между ходами] → `SummarizationMiddleware` (S2.7); до неё — как в v1.

## Migration Plan

Только v2. В v1 checkpointer уже есть; поведение v1 не меняется.

## Open Questions

—
