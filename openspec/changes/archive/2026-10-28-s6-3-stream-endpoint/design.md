# Design: S6.3 — endpoint `/invoke-agent/stream`, SSE, `log_requests`

Мотивация и рамки — proposal.md. Требования — `specs/agent-stream/spec.md`. Решения опираются на ADR-13 (SSE поверх `POST`, отдельный endpoint), ADR-14 (ход в задаче, `TurnStream`), ADR-15 (ответ целиком), §13 «Кадр», «Ошибки и отмена», «Журнал запросов и поток» и на spike S6.1 (H1–H4, прототип `scripts/spikes/s6_1_stream/http_proto.py`).

## Context

- `api/v2/service.py:AgentV2.stream(message, context, config) -> TurnStream` (S6.2). Метод синхронный: блокировка и дедлайн берутся при вызове, `TurnBusyError` возникает до первого события. `TurnStream` кладёт `turn.started` в очередь в конструкторе и запускает задачу хода. Итерация отдаёт `StreamEvent` до терминального `answer` / `error` включительно. Выход из итерации по любой причине (`_events` → `finally` → `stop()`) ставит `RequestContext.cancel`, если ход ещё идёт. StopEvent и прочие сбои хода `TurnStream` уже превращает в `error` (`unavailable` / `failed`).
- `StreamEvent.data()` — поля для `data:` без `event`. `EventFactory(trace_id)` нумерует события и ставит общие поля.
- `api/v1/router.py:invoke_agent` собирает `thread_id`, `config` (колбэки Langfuse, метаданные), `RequestContext` и вызывает `answer()` внутри `disconnect_watch` (S2.8). При v1 вызывается `agent.graph.ainvoke`. Ошибки: `TurnBusyError` → 409, `StopEventError` → 503, прочее → 424 (`FailedDependencyResponse`).
- `api/middleware.py:log_requests` подключён через `app_main.middleware("http")` (`BaseHTTPMiddleware`). В ветке с `x-trace-id` он читает `response.body_iterator` целиком, ставит заголовки `x-*` и `x-response-time` и пишет записи ответа, аудит и метрики. `logger` он берёт через `APP_CTX.get_logger()` на каждый запрос. В `finally` очищаются `gc_ctx.trace_id_cvar` и `request` в контейнере контекста.
- S6.1: за текущим `log_requests` кадры приходят одним куском в конце хода (H1). Ветка для `text/event-stream` отдаёт первый кадр через 5 мс (H2). Starlette 1.6 отменяет генератор ответа ≈ через 0,2 с после разрыва, а `http.disconnect` забирает слушатель Starlette, поэтому `disconnect_watch` разрыв не видит (H3). `: ping` по таймеру не мешает порядку кадров (H4).
- `httpx` 0.28.1 `ASGITransport` дожидается конца приложения и отдаёт тело целиком (проверено по исходнику). Поэтому `httpx`-тесты через ASGI проверяют состав и порядок кадров, а доставку по мере хода и разрыв можно проверить только на живом uvicorn.
- `openapi_copilot.yaml` описывает API со стороны шлюза: путь `/copilot_agent`, схемы `CopilotAgentRequest` / `CopilotAgentResponse` / `FailedDependencyResponse`.

## Goals / Non-Goals

**Goals:**
- Endpoint — тонкий слой над `AgentV2.stream()`: HTTP-статусы до потока, кадры, heartbeat. Ход, события и отмену делает S6.2.
- Ветка потока в `log_requests` не меняет ни строки поведения для прочих ответов. Это доказывают характеризационные тесты, написанные до правки.
- Сериализация и heartbeat — чистые функции над `AsyncIterator[StreamEvent]`. Их тесты обходятся без HTTP.

**Non-Goals:**
- Изменения схемы событий, `StreamTranslator`, `TurnStream` (S6.2) и событий прогресса (S6.4).
- Параметры и наблюдаемость потока (S6.5): `AGENT_STREAM_HEARTBEAT`, строка `STREAM`, тег `stream=true`.

## Decisions

### D1. Модуль `api/v2/stream/sse.py`

| Имя | Что делает |
| --- | --- |
| `HEARTBEAT_S = 15.0` | Интервал heartbeat (AR-Q16). До S6.5 это константа модуля; S6.5 переносит её в `config/agent` как `AGENT_STREAM_HEARTBEAT` |
| `SSE_HEADERS` | `{"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}` |
| `PING = b": ping\n\n"` | Комментарий SSE |
| `encode(event) -> bytes` | `event: {event.event}\nid: {event.seq}\ndata: {json.dumps(event.data(), ensure_ascii=False)}\n\n`. JSON в одну строку, поэтому многострочного `data:` нет: переводы строк в тексте ответа экранирует JSON |
| `sse_body(events, heartbeat_s=None) -> AsyncIterator[bytes]` | Кадры по событиям плюс `PING` по таймеру (D2). Завершается после терминального события |
| `one_shot(trace_id, session_id, run) -> AsyncIterator[StreamEvent]` | Упрощённый поток одного ответа для v1 (D4) |

Модуль не знает ни о FastAPI, ни о v1. `run` — это корутина, возвращающая текст.

Кадры пишутся вручную, без `sse-starlette`. Библиотека добавила бы свою обработку разрыва и ping поверх нашей, а формат тривиален (S6.1 design).

### D2. Heartbeat без отмены ожидающего чтения

`sse_body` держит одну задачу `next_item = asyncio.ensure_future(anext(it))` и ждёт её через `asyncio.wait({next_item}, timeout=heartbeat_s)`. По таймауту отдаётся `PING`, и ожидание продолжается **на той же задаче**. Когда задача готова, кадр отдаётся, и создаётся следующая задача чтения. После терминального события генератор выходит, поэтому ping после него не бывает (H4).

**Почему не `asyncio.wait_for(anext(it), timeout)`.** `wait_for` по таймауту отменяет чтение. `CancelledError` попадает в генератор `TurnStream._events`, а его `finally` вызывает `stop()` и ставит `RequestContext.cancel`. Первый же heartbeat остановил бы ход как при разрыве.

**Разрыв.** Starlette отменяет `sse_body` (H3). В `finally` генератор отменяет висящую задачу чтения и вызывает `aclose()` у итератора событий. Для `TurnStream` это `_events` → `finally` → `stop()` → `RequestContext.cancel` (AR-Q17). Задача хода не отменяется и снимает блокировку сама, в конце хода (S6.2 D10).

### D3. Endpoint в `router.py`

```text
POST /api/v1/invoke-agent/stream (CopilotAgentRequest, common_headers → 422)
  setup = _turn_setup(headers, agent, cb_handler, endpoint="/invoke-agent/stream")
  v2: events = agent.stream(message, setup.context, setup.config)   # TurnBusyError → 409
  v1: events = one_shot(trace_id, session_id, lambda: _v1_answer(agent, message, setup))
  прочее исключение до потока → 424 (FailedDependencyResponse), как у /invoke-agent
  return StreamingResponse(sse_body(events), media_type="text/event-stream", headers=SSE_HEADERS)
```

- `_turn_setup` содержит общий с `invoke_agent` код: `user_id`, `session_id` (из заголовка или `uuid4`), `thread_id`, `config` с метаданными и колбэками, `RequestContext` для v2. `invoke_agent` переходит на неё без изменения поведения. Отличается только `metadata["endpoint"]`. Это защищают `tests/integration/v1_api` и `tests/unit/v2/test_api.py`.
- `disconnect_watch` в потоке не используется (H3): разрыв ловит `sse_body` (D2).
- 409 — тот же JSON, что у `/invoke-agent`: `FailedDependencyResponse(error_description=TURN_BUSY_MESSAGE)`.
- `StopEventError` до потока не возникает: `TurnContext` и вызовы модели идут внутри задачи хода. StopEvent приходит в поток как `error` (`unavailable`), а HTTP-статус уже 200 (§13 «Ошибки и отмена»).
- Starlette ставит `Content-Type: text/event-stream; charset=utf-8`. Ветка `log_requests` поэтому проверяет префикс, а не равенство (D5).
- Trace id GigaChat (`gc_ctx.trace_id_cvar`) задача хода получает копией контекста при создании, внутри обработчика. Очистка в `finally` у `log_requests` эту копию не трогает, поэтому вызовы GigaChat в ходе несут `x-trace-id` до конца.

### D4. Упрощённый поток v1

`one_shot` создаёт `EventFactory(trace_id)`, отдаёт `turn.started` (`session_id`), запускает `run()` отдельной задачей и ждёт её через `asyncio.wait`. Это защищает задачу от отмены при разрыве: ход v1 дозавершается сам, как сейчас у `/invoke-agent` v1. Задача хранится в модульном множестве, как `_RUNNING` в `turn.py`. Исходы:
- текст → `answer` (`full`);
- `StopEventError` → `error` (`unavailable`, `e.user_message`);
- прочее → `error` (`failed`, `turn.FAILED_TEXT`) и запись через `logger.opt(exception=True)`.

`_v1_answer` в `router.py` — тот же вызов `agent.graph.ainvoke` и извлечение текста, что в `invoke_agent`; выносится в общую функцию. Пакет `api/v2/stream` графа v1 не импортирует.

**Альтернатива:** не давать поток при v1 (404 или 501). Отвергнута: FR-21 требует упрощённый поток, чтобы фронт работал при откате на v1.

### D5. Ветка потока в `log_requests`

Правка точечная, по §13 «Журнал запросов и поток»:

1. Блок записей ответа (от `Outgoing response to client system` до метрики статуса) выносится в функцию `_log_response(logger, request_path, headers_to_log, decoded_body, status_code, start_time)` **без изменения порядка и аргументов**. Непотоковая ветка вызывает её на прежнем месте, после сборки тела и заголовков.
2. После `response = await call_next(request)`, если `response.headers.get("content-type", "").startswith("text/event-stream")`:
   - заголовки `x-*` и `x-response-time` ставятся сразу, до первого байта; `x-response-time` — время начала ответа;
   - `response.body_iterator = _logged_stream(response.body_iterator, …)`, и ответ возвращается без сборки тела.
3. `_logged_stream` отдаёт каждый кусок сразу. Из кадров он запоминает только `data` терминального кадра: держит хвост текущего кадра до `\n\n`, разбирает строки `event:` / `data:`, если тип `answer` / `error`. Тело целиком не копится. В `finally` (окончание, исключение, отмена при разрыве) вызывается `_log_response(..., decoded_body = data терминального кадра или {}, status_code = response.status_code, start_time)`, то есть длительность считается за весь поток. Записи пишутся ровно один раз.
4. Записи запроса (`Incoming…`, метрики пользователя и запросов, аудит `BusinessRequestReceived`) остаются как есть: они пишутся до `call_next`.
5. Контекст: `_logged_stream` выполняется после `finally` самого `log_requests`. Переменные `x-*` в `ContextVar` при этом сохраняются: Starlette отправляет тело в той же задаче, где шёл `dispatch`. `request` уже очищен, поэтому декораторы `tracing_spans` в обёртке не используются (§13, п. 6). Тест проверяет, что поля записи ответа те же.

Исход хода (`error`, разрыв) в метрики статуса не попадает: метрика успеха или ошибки ставится по HTTP-статусу, как сейчас. Исход пишет строка `STREAM` в S6.5.

**Альтернатива:** чистый ASGI-middleware вместо `BaseHTTPMiddleware`. Отвергнута: это переписывание всего журнала, а §13 требует точечной ветки при неизменном поведении прочих ответов.

### D6. Тесты: характеризация, ASGI и живой uvicorn

- **Характеризационные тесты `log_requests`** (`tests/unit/test_log_requests.py`) пишутся и проходят **до** правки `middleware.py`. Стенд — маленькое FastAPI-приложение с тем же `log_requests` и маршрутами, которые отдают те же формы ответов: ZIP через `StreamingResponse`, JSON 200, JSON 409 / 424 / 503, 422 от `common_headers`, путь из `NON_LOGGED_ENDPOINTS`, запрос без `x-trace-id`. `APP_CTX.get_logger` подменяется записывающим логгером (как `RecordingLogger` в `http_proto.py`). Тест сверяет полный список записей: метод, сообщение / `metric_name` / `event_name`, `message`, `metric_value` (длительность — только тип). Отдельно сверяются заголовки `x-*` ответа. После правки эти тесты не меняются; тесты ветки потока добавляются отдельным классом.
- **ASGI-тесты endpoint** (`tests/unit/v2/test_stream_api.py`, стенд `Api` из `test_api.py` с методом `stream()`): разбор кадров из тела, состав и порядок событий, одно терминальное событие последним, равенство `answer.text` и `txt_response.txt` на одном сценарии, ранний ответ при открытых шагах и выдуманное число (FR-26), 409 / 422 / StopEvent / таймаут / нет модели, v1 через подменённый граф.
- **Живой uvicorn** в том же файле: `uvicorn.Server(Config(app_main, port=<свободный>, lifespan="off"))` запускается задачей в цикле теста, клиент — `httpx.AsyncClient().stream(...)` по TCP. Проверяются три вещи. (1) Доставка по мере хода: отметка `turn.started` раньше конца расчёта-заглушки и не позже 1 с. (2) Heartbeat: `sse.HEARTBEAT_S` подменяется на 0,2 с, во время паузы расчёта приходят `: ping`, после терминального кадра — нет. (3) Разрыв посреди расчёта: заглушка `calc_tools.analyze_excel_model` запускает дочерний `sleep 1` (как `soffice`), ждёт его и пишет pid. Клиент закрывает поток после `tool.started`. Проверяется, что расчёт завершён, дочерний процесс не жив, `model.calls` не растёт, во время дозавершения та же сессия получает 409, после конца хода блокировка свободна, следующий ход видит историю.

Сеть и LibreOffice не нужны: скриптовая модель (`ScriptedChatModel`), реестр в памяти (`uploaded_store`), расчёт-заглушка.

### D7. OpenAPI

- Декоратор FastAPI: `responses` с `200` (`content: text/event-stream`, описание кадров и ссылка на §13), `409`, `424`. Автоматический `/openapi.json` показывает endpoint.
- `openapi_copilot.yaml`: путь `/copilot_agent/stream` (`operationId: invoke_copilot_stream`) с теми же параметрами и телом, что `/copilot_agent`. Ответ `200` — `text/event-stream`: формат кадра, heartbeat, правило одного терминального события. Компоненты `StreamEvent*` для каждого типа каталога `v = 1` (поля по `api/v2/stream/events.py`) и `oneOf` по ним для `data`. Ответы `409` и `422`. Контрактный тест «модели ↔ YAML» — в S6.5 (AR-Q18).

### D8. Правки спецификации

§13 «Кадр»: интервал heartbeat в S6.3 — константа 15 с, параметр — S6.5. §13 «Контракт API»: путь шлюза для потока в `openapi_copilot.yaml`. §16, риск `log_requests`: отметка, что ветка сделана в S6.3. `openspec/traceability.md`: строки FR-21, FR-25, FR-26, AR-Q16, AR-Q17, FR-13 (поток).

## Risks / Trade-offs

- [`BaseHTTPMiddleware` разобьёт или склеит кадры, и `_logged_stream` не распознает терминальный] → разбор по хвосту до `\n\n`, а не по кускам. Тест с кадром, разрезанным на два куска.
- [Вынос `_log_response` незаметно поменяет порядок записей] → характеризационные тесты пишутся до правки и сверяют полный список записей.
- [Разрыв не доходит до `sse_body` за другими прокси] → локально проверено (H3). В контуре heartbeat держит соединение, а AI GW проверяется в C1, который блокирует выпуск E6.
- [Живой uvicorn в unit-тестах нестабилен по времени] → свободный порт, ожидание `server.started`, интервалы с запасом (расчёт ≥ 1 с, порог `turn.started` 1 с), явная остановка сервера в `finally`.
- [Задача хода v1 после разрыва никем не ждётся, её исключение уходит в «Task exception was never retrieved»] → исключение ловится внутри `one_shot` и превращается в событие. Задача хранится в множестве до конца.
- [`x-response-time` у потока означает начало ответа, а у прочих — конец сборки тела] → так задано в §13, п. 3. Описано в OpenAPI потока.

## Migration Plan

Изменение добавочное: новый маршрут и ветка `log_requests`, которую выбирает только `text/event-stream`. Сейчас таких ответов в сервисе нет. Порядок внедрения: характеризационные тесты → правка `log_requests` → endpoint. Откат: удалить маршрут и ветку, характеризационные тесты остаются. Выпуск E6 на фронт по-прежнему ждёт C1 (SSE через AI GW в контуре).

## Open Questions

- Имя пути потока на стороне шлюза (`/copilot_agent/stream`) подтверждается с командой AI GW при подключении маршрута (C1). На код сервиса и спеки это не влияет: меняется только `openapi_copilot.yaml`.
