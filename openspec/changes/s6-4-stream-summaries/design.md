# Design: S6.4 — сводки шагов, итог хода и `ProgressReporter`

Мотивация и рамки — proposal.md. Требования — `specs/agent-stream/spec.md`. Решения опираются на ADR-14 (события из `updates`, `ProgressReporter` пишет только то, чего там нет), ADR-15 (сводки без LLM), §8 №17а, §13 «Каталог событий», «Подписи и сводки», «Части `custom`» и на spike S6.1 (P4, таблица «узел → событие»).

## Context

- `api/v2/stream/translator.py:StreamTranslator` (S6.2) уже переводит части `custom` с ключом `progress`: `{"progress": "phase", "phase": …}` → `phase` (одна фаза подряд не повторяется, `_set_phase`); `{"progress": "retry", "tool_call_id": …, "code": …}` → `tool.finished` (`retry`, текст `labels.retry_text`) открытого вызова и новый `tool.started` того же `call`. Фазу `checking` переводчик ставит сам по текстовому обновлению `model`. Писать части `custom` пока некому.
- `ToolFinished.summary` и `TurnSummaryEvent` (`completed`, `skipped`, `blocked`, `text`) объявлены в `events.py`, но не заполняются. `StreamTranslator.finish()` возвращает один черновик `answer`; `TurnStream._run` кладёт его в очередь.
- `_tool_message` разбирает конверт (`_envelope`: JSON со `status`). У успешного конверта есть `result` — сводка на русском с единицами (`ToolSuccess.result`), и `numbers`. Длина `result` не ограничена: `read_tools._cut`, `offload._truncate` режут по-своему, `resolve_terms` даёт «Сопоставлено 2 из 3 терминов. С низкой уверенностью: …». После выноса в `/results/` (ADR-7) в `ToolMessage` остаются `result`, `numbers`, `ref`. Чтение `/skills/` конверта не даёт.
- Стек (`factory.py:AgentFactory.middleware()`): … `CalcCache` (№15) → `tool_errors()` (№16) → `engine_retry(engine_tools)` (№17, `ToolRetryMiddleware`, `max_retries = ENGINE_RETRIES = 1`, `retry_on = is_transient_tool_error`) → `extra_middleware` → guards → … `ToolRetryMiddleware.awrap_tool_call` вызывает внутренний `handler(request)` на каждую попытку с тем же `request`, после последней неудачи — `on_failure="error"`, исключение уходит в `ToolErrorMiddleware`, и тот делает конверт `engine_unavailable` (переводчик даёт `failed`).
- S6.1 P4: `stream_writer` работает из `abefore_model` (`runtime.stream_writer`) и `awrap_tool_call` (`request.runtime.stream_writer`); под `ainvoke` это функция без потребителя — вызов безопасен. Повтор движка виден только копии внутри `ToolRetryMiddleware`. Узел своего middleware в `updates` даёт пустое обновление, переводчик его отбрасывает.
- `guard_counters` (сбрасывает `TurnContext` в начале хода): `TurnLimits` считает `model_calls`, `tool_calls`, `calc_calls`, ставит `limit_hit`; `Cancellation` — `turn_timeout` вместе с `limit_hit`. План — `todos` (`Status`, `OPEN = {pending, in_progress}`). `TurnContext` оставляет план прошлого хода, если в нём есть шаг `blocked`.
- Тестовый стенд: `tests/unit/v2/test_stream_turn.py:Stack` (скриптовая модель, `SlowGate`, `both()` — тот же сценарий через `answer()` и `stream()`), `u2_steps`, сценарий с `engine_unavailable` дважды.

## Goals / Non-Goals

**Goals:**
- `ProgressReporter` — один класс без состояния хода, кроме счётчика попыток; ход и ответ `/invoke-agent` не меняются.
- Сводки и итог — чистые функции в `labels.py`; их проверка обходится без агента.
- Схема `v = 1`, кадры SSE и формат частей `custom` не меняются.

**Non-Goals:**
- Наблюдаемость и параметры потока (S6.5).

## Decisions

### D1. `ProgressReporter` — `api/v2/middleware/progress.py`

```python
class ProgressReporter(AgentMiddleware):
    def __init__(self, engine_tools: Iterable[str], max_retries: int = ENGINE_RETRIES) -> None: ...
    async def abefore_model(self, state, runtime) -> None   # пишет фазу, состояние не меняет
    def before_model(self, state, runtime) -> None          # то же для sync-пути
    async def awrap_tool_call(self, request, handler)       # исход retry
    def wrap_tool_call(self, request, handler)
```

Место — сразу после `engine_retry(engine_tools)` в `AgentFactory.middleware()`, до `extra_middleware`: так он внутри `ToolRetryMiddleware` (видит каждую попытку) и снаружи адаптера. Это то, что решил spike S6.1 (P4) и записано в §8 №17а. Ставится всегда: агент один на процесс, а без потока `stream_writer` ничего не делает. `engine_tools` — тот же `catalog.engine_tool_names()`, что у `engine_retry`; `max_retries` — та же константа `ENGINE_RETRIES`.

Хуки возвращают `None`: обновлений состояния нет, узел в `updates` пустой.

Альтернатива — подкласс `ToolRetryMiddleware`, который пишет `retry` перед паузой. Отклонена: дублирует цикл повтора langchain и требует второго класса для фаз.

### D2. Правило фазы в `before_model`

Фаза выбирается по состоянию, которое видит `before_model` (до него уже отработали `before_model` у `TurnLimits` и верхних middleware), первым подходящим правилом:

| № | Условие | Фаза |
| --- | --- | --- |
| 1 | `limit_hit` в `guard_counters` (лимит или время хода) | `answering` |
| 2 | в `todos` есть шаг `pending` / `in_progress` | `executing` |
| 3 | `todos` не пуст, все шаги закрыты, `tool_calls` этого хода > 0 | `answering` |
| 4 | `tool_calls` этого хода = 0 | `planning` |
| 5 | иначе (вызовы были, плана нет — режим `off`, или остался закрытый план прошлого хода) | `executing` |

Счётчик `tool_calls` берётся из `guard_counters` (сбрасывается каждый ход), а не из поиска последнего `HumanMessage`: после сжатия истории и закреплённых skills граница хода по сообщениям ненадёжна. Правило 3 требует вызовов в этом ходе, чтобы план прошлого хода с шагом `blocked` не давал `answering` на первом вызове модели. Повторы фазы гасит переводчик (`_set_phase`), поэтому `ProgressReporter` пишет фазу перед каждым вызовом модели без своей памяти. После `checking` и возврата ответа guard-ом правило 2 снова даёт `executing`.

Часть: `{"progress": "phase", "phase": <фаза>}` — формат S6.2 D4 без новых полей.

### D3. Исход `retry` в `wrap_tool_call`

```python
async def awrap_tool_call(self, request, handler):
    if tool_name(request) not in self.engine_tools:
        return await handler(request)
    key = (thread_id(request), request.tool_call["id"])
    try:
        result = await handler(request)
    except Exception as exc:
        attempt = self._attempts[key] = self._attempts.get(key, 0) + 1
        if is_transient_tool_error(exc) and attempt <= self.max_retries:
            request.runtime.stream_writer({"progress": "retry", "tool_call_id": id, "code": exc.code.value})
        else:
            self._attempts.pop(key, None)
        raise
    self._attempts.pop(key, None)
    return result
```

- Решение «будет повтор» повторяет логику `ToolRetryMiddleware`: тот же предикат, тот же список инструментов, попытка не последняя. Последняя неудача `retry` не пишет: её исход `failed` даёт конверт `engine_unavailable` (таблица `FAILURE_VIEW`). Итог по сценарию «движок недоступен дважды»: одна пара `retry` → `tool.started`, затем `failed`.
- Счётчик попыток — словарь на экземпляре (middleware общий на процесс), ключ `(thread_id, tool_call_id)`: ID вызова скриптовой модели («c4») совпадают у разных сессий. Запись удаляется при успехе, при неповторяемой и при последней ошибке, поэтому словарь не растёт. Конкурентность — один event loop, гонок нет.
- Исключение пробрасывается без изменений: ход тот же, что без `ProgressReporter`.
- Попадание `CalcCache` (он снаружи) до `ProgressReporter` не доходит — `retry` от повтора не бывает, пара событий идёт из `updates`, как в S6.2.

### D4. Сводка шага — `labels.step_summary(envelope) -> str | None`

- Источник — только `result` успешного конверта; без конверта, без `result` или при пустой строке после очистки — `None`. `numbers` в текст не превращаются: `result` уже содержит числа с единицами, а склейка списка `numbers` дала бы длинный и бессвязный текст.
- Очистка — `scrub` (FR-24): ID, пути, имена инструментов, коды.
- Предел `SUMMARY_MAX = 300`. Если длиннее: (1) берётся последняя граница предложения (`. ` / `; `) в окне `[150, 299]`; (2) иначе последний пробел до 299; (3) если последний оставшийся токен содержит цифру, а следующий в исходном тексте начинается с цифры или разделителя числа (`1 234 567`, `1 234,5`, неразрывный пробел) — токен отбрасывается, пока условие держится; (4) снимаются хвостовые `,;:—-` и добавляется `…`. Так ни одно число не режется, и каждое число сводки — подстрока `result` (критерий FR-23).
- Вызов: `_tool_message` при исходе `ok` кладёт `summary` в черновик `tool.finished`; `_finished` получает необязательный `summary`. Для `retry` / `failed` и для части `custom` — `None`.

### D5. Итог хода — `labels.turn_summary(steps) -> Draft | None` и `finish()`

- `StreamTranslator.finish() -> list[Draft]`: `[summary?, answer]`. `summary` — если у переводчика есть шаги плана (`self._steps`, последний `plan` этого хода). Без плана — только `answer`. `TurnStream._run` кладёт все черновики по порядку; ветки `error` итог не дают.
- Поля: `completed`, `skipped`, `blocked` — счёт по статусам. Текст по шаблону с русскими формами числа: «Выполнено 3 из 5 шагов», затем через запятую «1 пропущен» / «2 пропущено», «1 ждёт ответа пользователя» / «2 ждут ответа пользователя»; шаги, оставшиеся `pending` / `in_progress` (частичный ответ), в тексте не перечисляются — их видно по «3 из 5». Причины шагов в текст не идут: это текст модели, а числа итога должны быть только счётчиками.
- Отклонение от примера §13 («один ждёт уточнения года»): причина шага не попадает в итог — причины уже есть в событии `plan`, а число из причины модели нарушило бы правило «числа только из конверта». §13 правится на шаблон.

### D6. Число вызовов LLM

`ProgressReporter`, сводки и итог не вызывают модель. Тест: сценарий U2 через `Stack.both()` со счётчиком вызовов скриптовой модели — счётчики `answer()` и `stream()` равны (критерий FR-23).

### D7. Фаза `preparing` убирается из каталога

Решение владельца 2026-10-28. Источника у фазы нет: `ProgressReporter` стоит после `TurnContext`, а событие до `TurnContext` нарушило бы последовательность отказа «`turn.started` → `answer`» (FR-25); загрузку книги и индекса фронт и так видит как паузу между `turn.started` и `phase(planning)`.

- `events.py`: `Phase = Literal["planning", "executing", "checking", "answering"]`; `labels.py:PHASE_TEXT` — без ключа `preparing`.
- `openapi_copilot.yaml`: enum поля `phase` — те же четыре значения.
- Спецификация v2: FR-22 «фазу хода (план, исполнение, проверка ответа)», §13 каталог `phase` — без `preparing`.

Отклонение от AR-Q18 («изменения схемы только добавочные»): значение удаляется, а не добавляется. Причина — оно ни разу не отдавалось (S6.2 и S6.3 его не выпускают), поэтому ни один клиент не мог на него опереться; контрактного теста схемы ещё нет (S6.5), он фиксирует уже четыре фазы.

### D8. Документы

- FR-22 и §13 каталог `phase`: без `preparing` (D7).
- §8 №17а: место — сразу после `engine_retry` (№17), ставится всегда, без потока ничего не делает; правило фаз D2.
- §13: в «Подписи и сводки» — правило сводки D4 и шаблон итога D5; пример в каталоге `summary` — по шаблону.
- `openspec/traceability.md`: FR-23 — владелец S6.4; статус FR-22 и FR-23 — при архивации.

## Risks / Trade-offs

- [Счётчик попыток дублирует знание `ToolRetryMiddleware`] → одни и те же `ENGINE_RETRIES` и `is_transient_tool_error`; тест «движок недоступен дважды» ловит расхождение (две пары `retry` или ни одной).
- [`ProgressReporter` стоит внутри повторов и видит запрос до `extra_middleware`] → хуки не меняют ни запрос, ни ответ; тест непотокового U2 сравнивает ответ и ZIP до и после.
- [Правило фаз эвристическое: модель может вызвать инструмент без открытого шага] → фаза — подсказка для фронта, на ход не влияет; правило 5 даёт `executing`, а не ложный `answering`.
- [Обрезка сводки может отрезать смысл] → граница предложения в приоритете; полный текст есть в ответе агента.
- [`result` инструмента может содержать имя инструмента или код] → сводка проходит `scrub`, гигиенический тест FR-24 расширяется на `summary`.
- [Удаление значения из схемы `v = 1`] → значение не отдавалось ни разу; OpenAPI и модели меняются в одном коммите, контрактный тест S6.5 строится уже на новом каталоге.
