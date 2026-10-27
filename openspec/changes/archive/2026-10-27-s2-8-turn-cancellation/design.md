# Design

## Context

Мотивация — proposal.md, требования — дельты `specs/`. Здесь — то, что определяет реализацию.

- `RequestContext.deadline: float | None` уже объявлен (S2.1, «S2.8»), но никем не ставится и не читается. `AgentV2.answer` держит блокировку хода (`TurnLocks.hold`, S4.2) и вызывает `agent.ainvoke`.
- S2.5 дал механизм последнего вызова: флаг `guard_counters.limit_hit` → `TurnLimits.wrap_model_call` убирает инструменты, добавляет `LIMIT_PROMPT` в запрос и вырезает `tool_calls` из ответа; `PlanCompletionGuard` при флаге принимает ответ сразу с «Не выполнено» и `LIMIT_REASON`; `ForcePlanFirst` при флаге не сужает и не повторяет запрос. `guard_counters` сбрасывает `TurnContext` в начале каждого хода.
- Стек сейчас: `TurnContext`, `LanguageGuard`, `StopEventGuard`, …, `TurnLimits`, `ModelRetryMiddleware`, `CalcCache`, `ToolErrorMiddleware`, `ToolRetryMiddleware`, …; `Cancellation` по §8 — №3, между `LanguageGuard` и `StopEventGuard`.
- `EngineGate.shutdown()` есть (S1.5), но `lifespan` в `api/__init__.py` его не вызывает: при остановке сервиса воркеры пула и их `soffice` остаются.
- Роутер (`api/v1/router.py:invoke_agent`) не получает `Request`, а Starlette не отменяет обработчик при разрыве соединения — разрыв сейчас никак не замечается. Запрос проходит через `app_main.middleware("http")(log_requests)` (`BaseHTTPMiddleware`), который оборачивает `receive`.
- Расчёт нельзя прервать посреди (`Future.cancel` не останавливает воркер — §12 п. 5); его ограничивает `AGENT_CALC_TIMEOUT` (S1.5).

## Goals / Non-Goals

**Goals:**
- Таймаут хода переиспользует последний вызов S2.5, а не заводит второй механизм частичного ответа.
- Ни одна точка остановки не рвёт вызов посреди: проверки только между вызовами, поэтому чекпоинт всегда согласован.

**Non-Goals:**
- Жёсткий таймаут поверх идущего вызова LLM (`asyncio.wait_for` вокруг `ainvoke`): отмена посреди узла теряет его записи и может оставить `AIMessage` с вызовами без `ToolMessage`. Зависание вызова LLM ограничено таймаутом клиента GigaChat.
- Отмена, когда сервер сам отменяет задачу запроса (остановка uvicorn): её закрывает `lifespan` (D5).

## Decisions

### D1. Дедлайн ставит `AgentV2.answer`, а не `TurnContext`

`AgentV2.answer` до `ainvoke` ставит `context.deadline = time.monotonic() + AGENT_TURN_TIMEOUT`, если он ещё не задан. Отклонение от §8 №1 («`TurnContext` … дедлайн хода»): в бюджет входит и `TurnContext` (построение индекса в `to_thread`, если реестр пуст), и вызывающий код может задать свой дедлайн (тесты, будущий eval). `Cancellation` без дедлайна (`None`) его не проверяет.

### D2. Флаг отмены — `asyncio.Event` в `RequestContext`

Поле `cancel: asyncio.Event` (`field(default_factory=asyncio.Event)`), не сохраняется checkpointer'ом (runtime context, §9). Роутер для v2 запускает наблюдатель `watch_disconnect(request, event)` отдельной задачей: он ждёт `await request.receive()` — тело запроса FastAPI уже прочитал, поэтому следующим сообщением может быть только `http.disconnect`, — затем `event.set()` и выход; задача отменяется в `finally` после хода. Уточнено при реализации (задача 3.3): первоначальный опрос `request.is_disconnected()` раз в секунду за `BaseHTTPMiddleware` (`log_requests`) разрыв не видит никогда — его чтение с мгновенной отменой не успевает пройти обёртку; ожидание `receive()` проверено на живом uvicorn с `log_requests` в обеих его ветках (с `x-trace-id` и без), разрыв замечается за ~1 с. Альтернатива — `StreamingResponse` с проверкой при отправке: узнаёт о разрыве только после хода. Наблюдатель — в `api/v2/turn/disconnect.py`, роутер только подключает его.

### D3. `Cancellation` (§8 №3): `before_model` и `wrap_tool_call`

- `before_model` (`can_jump_to=["end"]`):
  - `cancel` поднят → `{"messages": [AIMessage(CANCELLED_ANSWER)], "jump_to": "end"}` без вызова модели. Перед `before_model` все вызовы прошлого ответа уже имеют `ToolMessage` (`create_agent` вызывает модель только после узла инструментов), поэтому история согласована. `CANCELLED_ANSWER` — короткая русская фраза о прерванном ходе: сообщение остаётся в истории, и следующий ход видит, что прошлый не закончен.
  - дедлайн истёк и `limit_hit` ещё нет → `guard_counters` с `limit_hit = 1` и `turn_timeout = 1`. Дальше работает последний вызов S2.5: стоит выше `TurnLimits`, поэтому флаг уже в состоянии, когда `TurnLimits.wrap_model_call` строит запрос.
- `awrap_tool_call`: дедлайн истёк или `cancel` поднят → `ToolMessage(status="error")` с конвертом `cancelled`, обработчик не вызывается (движок не стартует, кэш и повторы не трогаются). Ставить флаг здесь не нужно: следующий `before_model` увидит то же время.
- Отклонение: на разрыв соединения — без «частичного ответа» из §10 (`cancelled` → «частичный ответ»): читать его некому, а вызов LLM — лишняя нагрузка; таймаут частичный ответ даёт.
- Место — №3 по §8, внешний ко всем `wrap_tool_call` (`CalcCache`, `ToolErrorMiddleware`, повторы), поэтому отменённый вызов не запускает ни движок, ни повтор.

### D4. Последний вызов и причина для таймаута

`TurnLimits.wrap_model_call` выбирает текст по флагу: `turn_timeout` → `TIMEOUT_PROMPT` («Время на этот ход истекло…»), иначе `LIMIT_PROMPT`; пустой ответ → `TIMEOUT_FALLBACK` / `LIMIT_FALLBACK`. `PlanCompletionGuard` берёт причину `TIMEOUT_REASON = "Истекло время хода"` при `turn_timeout`, иначе `LIMIT_REASON`. Хелперы `limit_hit(state)` и новый `turn_timeout(state)` — в `limits.py`, чтобы флаги читались в одном месте. Финальный вызов после дедлайна — сверх `AGENT_TURN_TIMEOUT` на длину одного вызова LLM; это и есть «после текущего вызова» (AR-Q12).

### D5. Пул движка гасится в `lifespan`

После `APP_CTX.on_shutdown()` — `await default_gate().shutdown()` (уже убивает группы процессов воркеров вместе с `soffice`, S1.5). `default_gate()` кэширован и пул создаёт лениво, поэтому без расчётов вызов ничего не запускает. Это и закрывает отмену, при которой задача запроса снята сервером посреди расчёта: воркер доработает, а при остановке будет убит.

### D6. Настройка

`AgentSettings.turn_timeout: float = 300.0`, `gt=0`, `AGENT_TURN_TIMEOUT`; строка в README.

## Risks / Trade-offs

- [`BaseHTTPMiddleware` (`log_requests`) прячет разрыв от `is_disconnected()`] → подтвердилось на живом uvicorn (задача 3.3); наблюдатель ждёт `receive()`, который обёртка пробрасывает, — `log_requests` не меняется.
- [Последний вызов после дедлайна сам долгий] → ограничен таймаутом клиента GigaChat; жёсткий предел — Non-Goal.
- [Расчёт, начатый за секунду до дедлайна, держит ход до `AGENT_CALC_TIMEOUT` (120 с)] → верхняя граница хода ≈ `AGENT_TURN_TIMEOUT` + `AGENT_CALC_TIMEOUT` + один вызов LLM; записано в README. Урезать таймаут расчёта до остатка хода — отдельное решение, не здесь.
- [Фоновая задача на каждый ход v2] → одна задача, которая спит в `receive()` и отменяется после хода; нагрузки нет.
- [`CANCELLED_ANSWER` в истории сбивает модель в следующем ходе] → короткая нейтральная фраза; `TurnSummary` строит выжимку хода без LLM.

## Migration Plan

Только v2 за `AGENT_ENGINE=v2`, кроме гашения пула в `lifespan` (при v1 пул не создаётся — вызов пустой). Данных и миграций нет. Откат — `AGENT_ENGINE=v1` или откат коммита.
