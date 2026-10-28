# Design: S6.2 — схема событий, `StreamTranslator`, `AgentV2.stream()`

Мотивация и рамки — proposal.md. Требования — `specs/agent-stream/spec.md`. Решения опираются на ADR-14, ADR-15 и отчёт spike S6.1 (`archive/2026-10-28-s6-1-stream-spike/design.md`, «Results»: P1–P6, H3, таблица «узел / событие LangGraph → событие прогресса»).

## Context

- `api/v2/service.py:AgentV2.answer()`: `with TURN_LOCKS.hold(thread_id)` → дедлайн из `AGENT_TURN_TIMEOUT`, если его ещё нет → `agent.ainvoke(...)` → текст `result["messages"][-1]`. Метода `stream()` нет.
- `turn/lock.py:TurnLocks.hold()` — контекстный менеджер над множеством занятых `thread_id`. Проверка и захват происходят без `await`, поэтому гонки в одном event loop нет.
- S6.1, P1/P2: `agent.astream(stream_mode=["updates", "custom"], version="v2")` даёт словари `{"type", "ns", "data"}`. Для `updates` `data` — это `{узел: обновление}`. Узлы: `model`, `tools` и `<Middleware>.<hook>`. Хуки без изменений дают пустое обновление (`None`).
- S6.1, P3: отклонённый ответ сначала приходит целиком в обновлении `model`. Только следующее обновление `AnswerGroundingGuard.after_model` / `PlanCompletionGuard.after_model` с `jump_to` показывает, что он отклонён.
- Guards и лимиты переписывают принятый ответ **тем же `id`**. `AnswerGroundingGuard` заменяет неподтверждённые числа на «н/д» (`grounding.py:84`), `PlanCompletionGuard` дописывает «Не выполнено» (`plan.py:176`), `TurnLimits` пишет запасной текст (`limits.py:144`). В состоянии побеждает последнее сообщение с этим `id`. Ход, оборванный в `before_agent` / `before_model`, кладёт своё `AIMessage` и `jump_to="end"`: `TurnContext` («загрузите модель»), `LanguageGuard` (отказ), `Cancellation` (разрыв).
- Признаки исхода в состоянии: `guard_counters[LIMIT_HIT]` / `[TURN_TIMEOUT]` (`limits.py`, `cancellation.py`), `todos` со статусом `blocked`.
- Индекс: `RequestContext.model_index` (`ModelIndex.inputs` / `outputs`: `IN<n>` / `OUT<n>` → каноническое имя). `TurnContext` заполняет его в `before_agent`, то есть до первого вызова инструмента.
- Конверт ошибки — `{"status": "error", "error", "code", "hint"}` (`tools/envelope.py`, 14 кодов `ErrorCode`). `ERROR_TEXT` и `DEFAULT_HINTS` написаны для модели.
- `router.py` ловит `TurnBusyError` → 409, `StopEventError` → 503 (`e.user_message`).

## Goals / Non-Goals

**Goals:**
- Переводчик — чистый класс без I/O: на вход части `astream`, на выход события. Тестируется подачей частей без агента.
- `stream()` повторяет `answer()` во всём, кроме доставки: один код подготовки хода, текст `answer` равен возврату `answer()`.
- S6.3 и S6.4 добавляют своё без правки схемы и переводчика: S6.3 — кадры SSE поверх событий, S6.4 — `ProgressReporter` (пишет части `custom` в объявленном формате) и сводки.

**Non-Goals:**
- Сериализация в SSE, heartbeat, HTTP-статусы (S6.3).
- Наблюдаемость потока (S6.5).

## Decisions

### D1. Пакет `api/v2/stream/` и его части

| Модуль | Содержимое |
| --- | --- |
| `events.py` | pydantic-модели событий (D2), `SCHEMA_VERSION = 1` |
| `labels.py` | подписи вызовов (D6), таблица «код → исход и текст» (D7), `scrub()` (D8) |
| `translator.py` | `StreamTranslator` (D3–D5) |
| `turn.py` | `TurnStream`: задача хода, очередь, терминальное событие, отмена (D9–D11) |
| `hygiene.py` | `violations(event) -> list[str]` — проверка на запрещённое содержимое (D8) |

LangChain импортируется только здесь и в `service.py`, внутри `api/v2` (AR-Q3). v1 пакет не импортирует.

### D2. Схема событий

Базовая модель `StreamEvent`: `v: Literal[1]`, `seq: int`, `ts: str` (RFC-3339, UTC, миллисекунды), `trace_id: str`, `model_config = ConfigDict(extra="forbid", frozen=True)`. Тип события хранится в поле `event: Literal[...]`. В `data` S6.3 оно не попадает (`model_dump(exclude={"event"})`), а становится строкой `event:` кадра. Подклассы по каталогу §13:

| Класс | `event` | Поля |
| --- | --- | --- |
| `TurnStarted` | `turn.started` | `session_id` |
| `PhaseEvent` | `phase` | `phase: Literal["preparing", "planning", "executing", "checking", "answering"]`, `text` |
| `PlanEvent` | `plan` | `steps: list[PlanStepView]` (`n`, `title`, `status`, `reason: str \| None`) |
| `ToolStarted` | `tool.started` | `call: int`, `step: int \| None`, `label` |
| `ToolFinished` | `tool.finished` | `call`, `label`, `outcome: Literal["ok", "retry", "failed"]`, `elapsed_s: float`, `summary: str \| None` (S6.4), `text: str \| None` |
| `TurnSummaryEvent` | `summary` | `completed`, `skipped`, `blocked`, `text` (выпускает S6.4) |
| `AnswerEvent` | `answer` | `text`, `outcome: Literal["full", "partial", "clarify", "refusal"]` |
| `ErrorEvent` | `error` | `kind: Literal["unavailable", "failed"]`, `text` |

`extra="forbid"` реализует сценарий «схема отклоняет лишнее поле». Добавочные изменения схемы — новые необязательные поля и типы (AR-Q18, S6.5).

Нумерацию и общие поля ставит один `EventFactory(trace_id, clock)` внутри `TurnStream`: `seq` растёт на 1 при каждом выпуске. Переводчик возвращает «черновики» без `seq` / `ts` (словарь полей с типом). Так `seq` монотонен для всех источников событий, в том числе для терминального `error` из `TurnStream`.

**Альтернатива:** один класс с `data: dict`. Отвергнута: контрактный тест S6.5 и OpenAPI S6.3 строятся из моделей, а `dict` не ловит лишние поля.

### D3. Переводчик: части `updates`

`StreamTranslator(index_provider, clock)`. `index_provider` — функция, которая возвращает `RequestContext.model_index`: индекс заполняется уже после старта хода. Метод `feed(part) -> list[Draft]` разбирает `part["type"]`; для `updates` перебирает пары «узел → обновление», пропуская `None`:

| Узел | Обновление | Выпуск |
| --- | --- | --- |
| `model` | `AIMessage` с `tool_calls` | по `tool.started` на каждый вызов, кроме `write_todos`; запоминается `{tool_call_id: (call, label, t0)}`; кандидат ответа сбрасывается |
| `model` | `AIMessage` без `tool_calls` | кандидат ответа ← это сообщение; `phase=checking` («Проверяю ответ»), если предыдущая фаза была другой |
| `tools` | `ToolMessage` (по каждому) | `tool.finished` по `tool_call_id`; исход и текст — из конверта (D7); `write_todos` и вызовы, которых нет в таблице открытых, пропускаются |
| `tools` / любой | ключ `todos` | `plan`, если шаги отличаются от последнего выпущенного |
| `*.after_model` guard | ключ `jump_to` | кандидат отбрасывается |
| любой | `AIMessage` с тем же `id`, что у кандидата | кандидат ← новая версия (переписанный guard-ом / лимитом ответ) |
| `TurnContext.before_agent`, `LanguageGuard.before_agent` | `AIMessage` + `jump_to="end"` | кандидат ← это сообщение, флаг `refusal` |
| `Cancellation.before_model` | `AIMessage` + `jump_to="end"` | кандидат ← это сообщение (исход `partial`) |
| любой | ключ `guard_counters` | запоминается последнее значение (для исхода, D5) |
| прочие узлы и ключи | — | ничего (FR-24) |

`turn.started` переводчик не выпускает: его выпускает `TurnStream` сразу после захвата блокировки, до первой части. Это ближе к AR-Q16 («сразу после блокировки») и не зависит от наличия `TurnContext` в стеке. Здесь S6.2 отходит от строки S6.1 «`TurnContext.before_agent` → `turn.started`»: на скорость это не влияет, а терминальное событие тогда есть всегда, даже если ход упал до первого узла.

`step` в `tool.started` — номер (с 1) первого шага `in_progress` в последнем выпущенном плане, иначе `null`. `call` — порядковый номер вызова в ходе (с 1, без `write_todos`).

Длительность `elapsed_s` считается от `tool.started` до `tool.finished` по `clock` переводчика (монотонные часы, в тестах — подставные). Округление — до 0,1 с.

### D4. Переводчик: части `custom`

Формат части, которую пишут middleware через `runtime.stream_writer` (объявляется здесь, пишет `ProgressReporter` в S6.4): `{"progress": "phase", "phase": <значение>}` и `{"progress": "retry", "tool_call_id": <id>, "code": <ErrorCode>}`.

- `phase` → `PhaseEvent` с текстом фазы из `labels.PHASE_TEXT`. Повтор той же фазы подряд не выпускается.
- `retry` → `tool.finished` (`outcome=retry`, `text` по коду из D7) для открытого вызова, затем новый `tool.started` с тем же `call` и меткой времени. Итоговый `ToolMessage` закрывает вторую попытку.
- Части `custom` другого вида (без ключа `progress`) отбрасываются: в поток идёт только объявленный формат.

В S6.2 ветка `custom` проверяется подачей частей вручную. Сквозная проверка с `ProgressReporter` — в S6.4.

### D5. Исход хода и текст ответа

`finish() -> Draft` вызывается `TurnStream`, когда `astream` закончился без исключения:
- текст — содержимое кандидата (строка или `str(content)`, как в `answer()`); кандидата нет → `RuntimeError`, `TurnStream` превращает его в `error: failed`;
- исход: `refusal`, если кандидат пришёл из `TurnContext` / `LanguageGuard`; иначе `partial`, если в `guard_counters` стоит `LIMIT_HIT` или `TURN_TIMEOUT`, или ход оборвал `Cancellation`; иначе `clarify`, если в последнем плане есть шаг `blocked`; иначе `full`.

**Почему кандидат по `id`, а не финальное состояние.** Варианты: (а) добавить `"values"` в `stream_mode` и взять `messages[-1]` из последней части; (б) `agent.aget_state(config)` после хода; (в) вести кандидата по обновлениям. (а) расходится с ADR-14 и на каждом шаге отдаёт весь список сообщений. (б) зависит от checkpointer и `thread_id` в `config`, и это лишнее чтение. (в) следует таблице S6.1 и не требует ничего сверх `updates`. Равенство с `answer()` проверяют тесты на сценариях, где ответ переписан: «н/д» от `AnswerGroundingGuard`, «Не выполнено» от `PlanCompletionGuard`, запасной текст `TurnLimits`, отказ `TurnContext` / `LanguageGuard`.

### D6. Подписи вызовов

`labels.tool_label(name, args, index) -> str`. Имена берутся только из `index` по ID. ID, которого нет в индексе, пропускается. Значения форматируются без лишних нулей (`300`, `0.15`).

| Инструмент | Подпись |
| --- | --- |
| `resolve_terms` | «Сопоставляю показатели: <text₁>, <text₂>, …» (термины — слова пользователя, через `scrub`, не больше 5, дальше «…») |
| `run_scenarios` | «Считаю сценарии: <выходы> при <вход> от a до b» (по каждому входу с диапазоном; без диапазона — «при <вход>»; без имён — «Считаю сценарии») |
| `find_input_for_target` | «Подбираю <входы> под <выход> = <цель> в <год>» (части без значения опускаются) |
| `get_values` | «Беру базовые значения» |
| `describe_model` | «Изучаю структуру модели» |
| `derive` | «Считаю <операция>»: `difference` — «разницу», `pct_change` — «изменение в процентах», `headroom` — «запас до порога», `crossing` — «точку пересечения порога», `elasticity` — «эластичность» |
| `trace_dependencies` | «Смотрю, от чего зависит <выход>» (без имени — «Смотрю зависимости показателя») |
| `read_file` | путь `/skills/…` — «Изучаю методику», `/results/…` — «Перечитываю прошлый расчёт», иное — «Читаю материалы» |
| прочие | «Выполняю шаг расчёта» |

Подпись `tool.finished` повторяет подпись `tool.started` того же вызова.

### D7. Таблица «код → исход и текст для пользователя»

`labels.FAILURE_VIEW: dict[ErrorCode, tuple[Outcome, str]]` покрывает все коды; тест сверяет ключи с `ErrorCode`. Правило: `retry` — модель или система исправит вызов, `failed` — шаг пропускается или ждёт пользователя (§13).

| Код | Исход | Текст |
| --- | --- | --- |
| `invalid_args` | retry | «Уточняю параметры расчёта» |
| `unknown_id` | retry | «Уточняю показатели» |
| `ambiguous_term` | retry | «Показатель назван неоднозначно — выбираю подходящий» |
| `term_not_found` | retry | «Показатель не найден — ищу под другим названием» |
| `no_values` | retry | «У показателя нет значений — беру другой» |
| `bad_units` | retry | «Сверяю единицы диапазона» |
| `engine_timeout` | retry | «Расчёт слишком долгий — сужаю сетку» |
| `plan_required` | retry | «Сначала составляю план» |
| `skill_required` | retry | «Сначала изучаю методику» |
| `engine_unavailable` | failed (из `ToolMessage`), retry (из части `custom`) | «Движок пересчёта недоступен — пропускаю шаг» / «Движок пересчёта не ответил — повторяю» |
| `missing_input` | failed | «Не хватает данных — задам уточняющий вопрос» |
| `limit_exceeded` | failed | «Лимит хода исчерпан — готовлю ответ по полученным результатам» |
| `cancelled` | failed | «Ход остановлен — готовлю ответ по полученным результатам» |
| `internal_error` | failed | «Шаг не удался — пропускаю его» |

`engine_unavailable` в `ToolMessage` значит, что системный повтор уже исчерпан (`ToolRetryMiddleware` внутри `ToolErrorMiddleware`), поэтому исход `failed`. Конверт, который не разбирается как JSON, или ответ без `status` (не наш инструмент) → `ok`. Это то же правило, что у `TurnSummary._result`.

### D8. Гигиена: очистка и проверка

`labels.scrub(text) -> str` применяется к любому тексту от модели или пользователя, который попадает в событие: названия и причины шагов, термины `resolve_terms`. Удаляются:
- ID индекса: `\b(?:IN|OUT)\d+\b`;
- имена инструментов: имена из `default_catalog()` плюс `write_todos`, `read_file`, как отдельные слова;
- коды `ErrorCode`, содержащие `_` (`cancelled` — обычное слово, в тексте модели его не трогаем);
- пути `/results/\S*` и `/skills/\S*`.

После удаления схлопываются пробелы и висящие знаки препинания. Пустое название шага → «Шаг <n>». Канонические имена индекса не чистятся: это текст книги, а не служебный.

`hygiene.violations(event)` проверяет все строковые поля события, кроме `AnswerEvent.text`, на те же шаблоны плюс `Traceback`, `Error:` / `Exception`, `"code"` / `"hint"`. Возвращает список нарушений. В S6.2 функция используется в тестах (сценарий FR-24). S6.5 может вызывать её в журнале потока.

Текст `answer` не чистится: он обязан совпадать с `txt_response.txt` (FR-21, FR-26). Что модель пишет в ответе, определяют промпт и skill `economic-answer`, а не поток.

**Альтернатива:** чистить все события на выходе одним фильтром. Отвергнута: подписи и тексты D6/D7 собраны из шаблонов и канонических имён, их чистка бессмысленна, а фильтр поверх готовых шаблонов маскировал бы ошибки в них. Тест `violations` ловит такие ошибки явно.

### D9. `AgentV2.stream()` и `TurnStream`

```python
def stream(self, message: str, context: RequestContext, config: dict[str, Any]) -> TurnStream
```

Метод синхронный: он захватывает блокировку и проставляет дедлайн сразу, при вызове. `TurnBusyError` поэтому возникает до первого `await` потребителя, и S6.3 вернёт 409 до открытия потока. Общий с `answer()` код (дедлайн) выносится в `_prepare(context)`.

`TurnStream` реализует `AsyncIterator[StreamEvent]`:
1. Конструктор получает агента, вход, `config`, `context` и освобождение блокировки. В очередь сразу кладётся `turn.started`.
2. Задача хода `asyncio.create_task(self._run())` запускается тут же, в конструкторе. `stream()` вызывается из async-обработчика, так что event loop уже запущен. Ход не зависит от того, читает ли кто-нибудь поток.
3. `_run()`: `async for part in agent.astream(input, config=config, context=context, stream_mode=["updates", "custom"], version="v2")` → `translator.feed(part)` → события в `asyncio.Queue()` без предела (AR-Q16: ход не ждёт потребителя). В конце — `translator.finish()` → `answer`. `StopEventError` → `error: unavailable` (текст `e.user_message`, как в 503). Прочее `Exception` → `error: failed` с фиксированным текстом и записью в журнал через `logger.opt(exception=True)`. В `finally` блокировка освобождается, затем в очередь кладётся маркер конца.
4. `__anext__` отдаёт события до терминального и после него завершает итерацию (`StopAsyncIteration`), даже если задача ещё что-то дописывает (не дописывает: терминальное событие кладётся последним).

**Почему блокировка снимается в задаче.** Блокировка должна жить до конца хода, а не потока (AR-Q17, S6.1 H3). Задача — единственное место, которое знает о конце хода. `TurnLocks` получает `acquire(thread_id) -> Callable[[], None]`: это та же проверка и тот же захват без `await`, что в `hold()`, но возвращается функция освобождения. `hold()` переписывается через `acquire()`, его поведение не меняется.

Если создание задачи падает (нет запущенного loop — ошибка разработки), `stream()` освобождает блокировку до того, как пробросить исключение. Повторный вызов освобождения безопасен (`discard`).

### D10. Уход потребителя

`TurnStream.aclose()` и выход из `async for` по исключению (`GeneratorExit` / `CancelledError` у потребителя, S6.3 — отмена генератора Starlette) делают одно: если задача хода не закончилась, ставят `context.cancel`. Задача не отменяется. `Cancellation` останавливает ход после текущего вызова, задача доходит до `finally` и снимает блокировку (S6.1, P6 и H3(б)). События, которые ход кладёт после ухода потребителя, остаются в очереди и пропадают вместе с объектом.

Задача хода хранится в атрибуте `TurnStream` и в модульном множестве `_RUNNING`, из которого удаляет себя через `add_done_callback`. Так сборщик мусора не соберёт задачу после ухода потребителя: это известная ловушка `asyncio.create_task`.

**Альтернатива:** `disconnect_watch` (S2.8). Не подходит: внутри потока `http.disconnect` забирает слушатель Starlette (S6.1, H3).

### D11. Конфиг и колбэки

`config` передаётся в `astream` тот же, что в `ainvoke` (колбэки Langfuse, `thread_id`). `version="v2"` и набор режимов — константы `turn.py`, а не параметры: их меняет ADR, а не конфиг.

### D12. Правки спецификации

§13 «Потоковый режим»:
- формат частей `custom` (D4);
- таблица «код → исход и текст» (D7) вместо перечисления в «Подписи и сводки»;
- правило исхода `answer` (D5);
- `turn.started` выпускается сразу после захвата блокировки (D3).

§8 и стек middleware не меняются.

## Risks / Trade-offs

- [Кандидат по `id` разойдётся с `messages[-1]`, если новый middleware перепишет ответ сообщением с другим `id`] → тест равенства `stream()` и `answer()` на сценариях с переписанным ответом (D5). Новый guard без него упадёт в этих тестах, а не тихо в проде.
- [Неограниченная очередь при ушедшем потребителе копит события до конца хода] → ход ограничен лимитами (30 вызовов инструментов, 40 вызовов модели) и таймаутом. Это сотни мелких объектов на ход, которые освобождаются вместе с `TurnStream`.
- [`scrub` вырежет из названия шага осмысленное слово, совпавшее с именем инструмента (`derive`)] → имена инструментов английские snake_case, а план пишется по-русски. Ложное срабатывание даёт только чуть беднее название шага.
- [Модель пишет ID или имя инструмента в тексте ответа] → ответ не чистится по контракту равенства с ZIP. Это вопрос промпта и eval (S5.1), а не потока. Отмечено в proposal «Вне рамок».
- [Формат части `custom` придуман до `ProgressReporter`] → формат минимален (`progress`, `phase` / `tool_call_id`, `code`) и описан в §13. S6.4 может только добавлять поля.

## Migration Plan

Добавочное изменение: `stream()` никто не вызывает до S6.3. `answer()` меняется только выносом общей подготовки, `TurnLocks.hold()` — переписыванием через `acquire()` с тем же поведением; существующие тесты блокировки и `answer()` проходят без правок. Откат — удалить пакет `stream/` и метод.
