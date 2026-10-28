Story S6.4 · FR-23 (владелец) · FR-22 (также) · ADR-14, ADR-15 · §8 №17а (`ProgressReporter`) · §13 «Каталог событий», «Подписи и сводки», «Части `custom`» · spike S6.1 (P4, таблица «узел → событие») · S6.2 (`StreamTranslator`, схема `v = 1`)

## Why

Поток S6.2–S6.3 показывает план и пары `tool.started` / `tool.finished`, но фронт не видит, что шаг дал: поле `summary` в `tool.finished` всегда пустое, итога хода (`summary`) нет. Фаз `planning` / `executing` / `answering` тоже нет: их должен писать `ProgressReporter` через `runtime.stream_writer`, а его в стеке нет. Повтор движка при `engine_unavailable` не виден: пользователь ждёт, а в потоке тишина до `failed` или `ok`. Формат частей `custom` и их перевод уже готовы в S6.2, не хватает того, кто их пишет. Без S6.4 нельзя закрыть FR-23 и прогресс FR-22.

## What Changes

- **Сводка шага в `tool.finished`** (FR-23). При исходе `ok` событие несёт сводку ≤ 300 символов из поля `result` конверта этого вызова. Сводка очищается как прочий текст потока (FR-24) и обрезается так, чтобы ни одно число не разрезалось. Если конверта нет (чтение `/skills/`) или в нём нет `result`, `summary = null`. Вызов LLM на сводку не делается (ADR-15).
- **Итог хода — событие `summary`** (FR-23). Отдаётся перед `answer`, если в конце хода есть план: число шагов `completed`, `skipped`, `blocked` и русский текст по шаблону («Выполнено 3 из 5 шагов, 1 пропущен, 1 ждёт ответа пользователя»). Без плана, при отказе и при терминальном `error` событие не отдаётся.
- **`ProgressReporter`** (§8 №17а, новый middleware `api/v2/middleware/progress.py`). Стоит внутри `ToolRetryMiddleware`, как решил spike S6.1 (P4). Пишет в `runtime.stream_writer` только части `custom` формата S6.2:
  - `before_model` — фаза хода: `planning`, `executing` или `answering` по состоянию плана, вызовам этого хода и флагу лимита;
  - `wrap_tool_call` — исход `retry`, когда попытка расчёта упала с ошибкой, которую `ToolRetryMiddleware` повторит (тот же предикат `is_transient_tool_error`, тот же список расчётных инструментов, только если попытки ещё остались).
  Ход не меняет. При `ainvoke` `stream_writer` ничего не делает, поэтому `/invoke-agent` ведёт себя как раньше.
- **Фаза `preparing` убирается из каталога событий** (решение владельца 2026-10-28). Её никто не отдаёт: `ProgressReporter` стоит после `TurnContext`, а событие до `TurnContext` сломало бы отказ «`turn.started` → `answer`» (FR-25). Значение удаляется из `Phase` схемы, из текстов фаз, из OpenAPI и из FR-22 / §13 спецификации. Клиентов не ломает: значение ни разу не попадало в поток.
- **`StreamTranslator`**: заполняет `summary` в `tool.finished` и выпускает событие `summary` в `finish()` перед `answer`; `TurnStream` отдаёт оба. Поля `summary` и тип `summary` объявлены в S6.2; кроме удаления `preparing` схема `v = 1` не меняется.

**Вне рамок:** строка `STREAM`, тег Langfuse `stream=true`, метрика потоков, `AGENT_STREAM_HEARTBEAT`, контрактный тест схемы `v = 1`, сценарий U10 (S6.5); ответ по токенам (снят ADR-15); вызов LLM для сводок; правка схемы событий сверх удаления `preparing`, кадров SSE, endpoint и `log_requests`; изменения конвертов инструментов и `tools.py`; упрощённый поток v1; `api/os_router.py` и `api/metric_router.py`.

## Capabilities

### New Capabilities

Нет.

### Modified Capabilities

- `agent-stream`: ADDED — FR-23 «Сводки шагов и итог хода без LLM» (владелец S6.4). MODIFIED — FR-22 «План и вызовы инструментов в потоке» (фазы `planning` / `executing` / `answering`, каталог фаз без `preparing`, последовательность U2 с фазами и итогом) и FR-22 «Исход вызова инструмента» (исход `retry` при системном повторе движка).

## Impact

- Код: новый `src/aigw_service/api/v2/middleware/progress.py`; `api/v2/factory.py` (место в стеке — сразу после `engine_retry`); `api/v2/stream/translator.py` (сводка шага, итог хода, `finish()` возвращает список); `api/v2/stream/turn.py` (отдаёт итог перед `answer`); `api/v2/stream/labels.py` (обрезка сводки, текст итога, `PHASE_TEXT` без `preparing`); `api/v2/stream/events.py` (`Phase` без `preparing`); `openapi_copilot.yaml` (enum `phase`).
- Тесты: `tests/unit/v2/test_progress_reporter.py` (новый), дополнения `test_stream_translator.py` и `test_stream_turn.py` (U2 с фазами и сводками, повтор `engine_unavailable`, равенство числа вызовов LLM в `answer()` и `stream()`, гигиена сводок).
- Документы: FR-22, §8 №17а и §13 спецификации v2 (фазы без «подготовки», место `ProgressReporter`, правило фаз, шаблон итога), `openspec/traceability.md` (FR-23, статус FR-22).
- API: поля `tool.finished.summary` и событие `summary` начинают заполняться; из enum `phase` уходит никогда не отдававшееся `preparing`; SSE без изменений. `/invoke-agent` и его ZIP не меняются.
- Зависимости: нет.
