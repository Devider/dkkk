Story S6.3 · FR-21, FR-25, FR-26, AR-Q16, AR-Q17 (владелец) · FR-13 (также) · ADR-13, ADR-14, ADR-15 · §13 «Потоковый режим», «Журнал запросов и поток» · spike S6.1 (H1–H4) · S6.2 (`AgentV2.stream()`, `TurnStream`)

## Why

S6.2 добавил поток хода как объекты: `AgentV2.stream()` возвращает `TurnStream` с событиями схемы `v = 1`. Но наружу его ничего не отдаёт: HTTP endpoint нет, кадров SSE и heartbeat нет. Кроме того, `log_requests` собирает тело любого ответа целиком (`[chunk async for chunk in response.body_iterator]`), поэтому SSE доходит до клиента одним куском в конце хода (spike S6.1, H1), а при разрыве записи ответа не пишутся вовсе (H3). Без S6.3 фронт не видит ход по мере работы агента, и эпик E6 не даёт пользователю ничего.

## What Changes

- **Endpoint `POST /api/v1/invoke-agent/stream`** (FR-21, ADR-13). Те же 5 заголовков и тело `CopilotAgentRequest`, что у `/invoke-agent`, тот же `thread_id`, `RequestContext`, `config` (колбэки Langfuse, метаданные). Ответ — `text/event-stream` с заголовками `Cache-Control: no-cache` и `X-Accel-Buffering: no`. `/invoke-agent` и его ZIP не меняются: общая сборка `config` и контекста выносится в функцию роутера, поведение `/invoke-agent` прежнее.
- **Ошибки до потока — обычные HTTP-ответы**: 422 (заголовки, тело), 409 с `TURN_BUSY_MESSAGE` (сессия занята: `AgentV2.stream()` бросает `TurnBusyError` до первого события), 424 при сбое до старта хода.
- **Кадры SSE** (§13 «Кадр»): `event: <тип>`, `id: <seq>`, `data: <JSON>` + пустая строка; `data` — поля события без `event`, кириллица без экранирования. Heartbeat — комментарий `: ping` каждые 15 с, пока нет событий (AR-Q16). Поток закрывается сразу после терминального `answer` / `error`; после него ни кадров, ни ping (FR-25).
- **Терминальные события в HTTP** (FR-25, FR-26): StopEvent посреди хода → `error` (`unavailable`), статус уже 200; прочий сбой → `error` (`failed`); таймаут или лимит → `answer` (`partial`). Текст `answer` — финальный ответ целиком после `PlanCompletionGuard` и `AnswerGroundingGuard`, равен `txt_response.txt` того же сценария через `/invoke-agent`; отклонённые ранние ответы и ответы с выдуманными числами в поток не попадают.
- **Упрощённый поток v1** (FR-21): при `AGENT_ENGINE=v1` — `turn.started`, затем `answer` (`full`) с текстом графа v1 или `error`. Прогресса нет. Ход v1 тоже идёт в отдельной задаче, чтобы разрыв не обрывал его посреди расчёта.
- **Разрыв соединения** (AR-Q17): генератор ответа в `finally` закрывает `TurnStream`, а тот ставит `RequestContext.cancel`. Ход дозавершается после текущего вызова, расчёт не обрывается, блокировка держится до конца хода: запрос в сессию в это время получает 409. `disconnect_watch` (S2.8) в потоке не используется (S6.1, H3).
- **`log_requests` пропускает `text/event-stream` без буферизации** (§13 «Журнал запросов и поток», пункты 1–6). Ветка выбирается только по `Content-Type` ответа. Запрос журналируется как сейчас; заголовки `x-*` и `x-response-time` ставятся до первого байта; тело не копится, обёртка над `body_iterator` запоминает только `data` терминального события. Записи ответа (`Outgoing response…`, аудит `BusinessRequestFinished`, `responses_total`, длительность за весь поток, метрика успеха / ошибки по HTTP-статусу) пишутся в `finally` обёртки, в том числе при разрыве (тогда тело аудита `{}`). Для всех остальных ответов код и порядок записей прежние.
- **Характеризационные тесты `log_requests` — первая задача, до правки.** Они фиксируют записи журнала, аудита, метрик и заголовки для ZIP, JSON, 409 / 422 / 424 / 503, `NON_LOGGED_ENDPOINTS` и запросов без `x-trace-id`, а после правки проходят без изменений.
- **OpenAPI**: путь потока и схемы событий в `openapi_copilot.yaml`; описание ответа `text/event-stream` и 409 в декораторе FastAPI.
- Документация: §13 спецификации (heartbeat — константа до S6.5, путь в `openapi_copilot.yaml`), `openspec/traceability.md` — строки FR-21, FR-25, FR-26, AR-Q16, AR-Q17, FR-13 (поток).

**Допущения** (записаны в design): интервал heartbeat в S6.3 — константа модуля 15 с; параметр `AGENT_STREAM_HEARTBEAT` в `config/agent` добавляет S6.5 по своей строке §15. Путь в `openapi_copilot.yaml` повторяет принятое там имя шлюза: `/copilot_agent/stream` рядом с `/copilot_agent`.

**Вне рамок:** `ProgressReporter`, фазы `planning` / `executing` / `answering`, исход `retry` при повторе движка, сводки шагов и событие `summary` (S6.4); строка `STREAM`, тег Langfuse `stream=true`, метрика потоков, параметр `AGENT_STREAM_HEARTBEAT`, контрактный тест схемы `v = 1`, сценарий U10 (S6.5); проверка SSE через AI GW в контуре (C1, эпик 5 / контур); ответ по токенам (снят ADR-15); правка схемы событий, `StreamTranslator` и `TurnStream` сверх нужного endpoint; изменения ZIP-контракта `/invoke-agent`; `api/os_router.py` и `api/metric_router.py`.

## Capabilities

### New Capabilities

—

### Modified Capabilities

- `agent-stream`: ADDED требования-владельцы S6.3 — FR-21 «Потоковый endpoint», FR-25 «Завершение потока», FR-26 «Финальный ответ целиком», AR-Q16 «Доставка без буферизации и журнал потока», AR-Q17 «Разрыв потока». Требования S6.2 (ADR-14, FR-22, FR-24) не меняются.

## Impact

- Новые файлы:
  - `src/aigw_service/api/v2/stream/sse.py` — кадр SSE, heartbeat поверх итератора событий без отмены ожидающего чтения, упрощённый поток одного ответа (для v1);
  - `tests/unit/test_log_requests.py` — характеризационные тесты `log_requests` (до правки) и тесты ветки потока;
  - `tests/unit/v2/test_stream_sse.py` — кадры, heartbeat без отмены хода, упрощённый поток;
  - `tests/unit/v2/test_stream_api.py` — `httpx`-тесты endpoint (ASGI) и тесты на живом uvicorn (доставка по мере хода, heartbeat, разрыв посреди расчёта).
- Изменяемые файлы:
  - `src/aigw_service/api/v1/router.py` — endpoint `/invoke-agent/stream`, общая сборка `config` / `RequestContext` с `/invoke-agent`;
  - `src/aigw_service/api/middleware.py` — ветка `text/event-stream`, записи ответа вынесены в функцию (порядок прежний);
  - `openapi_copilot.yaml`; `docs/Copilot cashflow analyst v2 project spec.md` (§13); `openspec/traceability.md`.
- Зависимости не добавляются (кадры пишутся вручную, без `sse-starlette` — S6.1 design).
- API: добавочный endpoint; существующие контракты не меняются, `tests/integration/v1_api` проходят без правок.
