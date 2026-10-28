# Design

## Context

Мотивация — proposal.md. Проверяемые решения: ADR-13 (endpoint потока рядом с `/invoke-agent`), ADR-14 (источник событий — `agent.astream` того же агента, ход в отдельной задаче), ADR-15 (ответ по токенам через `AnswerStreamGate`), AR-Q16 (поток без буферизации, первое событие ≤ 1 с, heartbeat). Связанные места спецификации: §8 №5а (`ProgressReporter`), §13 «Поток», §16 (риски AI GW, GigaChat streaming, `log_requests`).

Текущее состояние (по коду, 2026-10-28; `langchain` 1.4.6, `langchain-core` 1.6.12, `langgraph` 1.2.16, `deepagents` 0.7.29, `langchain-gigachat` 0.5.1, `gigachat` 0.2.3, `fastapi` 0.141.1, `starlette` 1.6.0, `uvicorn` 0.53.0):
- `api/v2/factory.py:AgentFactory.middleware()` собирает стек §8 (20 позиций, `ProgressReporter` пока нет); `build()` → `create_agent(..., context_schema=RequestContext, checkpointer, store)`. Агент один на процесс (`get_agent_v2`).
- `api/v2/service.py:AgentV2.answer()` — блокировка хода (`TURN_LOCKS`), дедлайн, `ainvoke`. Метода `stream()` нет.
- `api/v1/router.py:invoke_agent` — `disconnect_watch(http_request, context.cancel)` вокруг хода (S2.8): разрыв ставит флаг, `Cancellation` останавливает ход после текущего вызова. `Request.is_disconnected()` за `BaseHTTPMiddleware` разрыв не видит (S2.8, задача 3.3).
- `api/middleware.py:log_requests` (`BaseHTTPMiddleware` через `@app.middleware`) для запросов с `x-trace-id` читает `response.body_iterator` целиком, затем пишет журнал, аудит и метрики, длительность — до конца сборки тела.
- Вложенные вызовы LLM внутри хода: `resolve_terms` (`AnalyzerSubAgent` на `APP_CTX.llm`), сводка истории (`summarization.summarizer` — та же модель агента через `ainvoke`), preview-модель и судья (`PreviewModelCheck`, фоновая задача).
- `langchain_gigachat.GigaChat` реализует `_stream` / `_astream`; `function_call` и `functions_state_id` переносятся в `additional_kwargs` чанка (`_convert_delta_to_message_chunk`), `usage_metadata` выставляется на чанке, если пришёл `usage`.
- `ScriptedChatModel` (`api/v2/scripted.py`) — только `_generate`, без `_astream`. Фейки движка и реестра для хода без сети и LibreOffice — `tests/unit/v2/fakes.py`, `conftest.py` (`inline_gate`), пример полного хода — `tests/unit/v2/test_runtime.py:test_turn`.
- `sse-starlette` не установлен.

## Goals / Non-Goals

**Goals:**
- Выбрать режим потока и составить таблицу «узел / событие LangGraph → событие прогресса» для `StreamTranslator` (S6.2).
- Найти место `ProgressReporter` в стеке и подтвердить, что `runtime.stream_writer` работает в нужных хуках, а без потока — пустышка.
- Подтвердить или опровергнуть ответ по токенам на GigaChat (ADR-15) и выбрать способ отбора токенов основного вызова.
- Показать на живом uvicorn поведение SSE за `log_requests` и при разрыве; набросать правку `log_requests` для S6.3.

**Non-Goals:**
- Промышленный код в `src/` и тесты `pytest` — это S6.2–S6.6.
- Схема событий и тексты подписей (S6.2).
- Замеры качества ответа в потоке (eval, эпик 5).

## Decisions

### D1. Стенд вне приложения

`scripts/spikes/s6_1_stream/`: `run.py` (CLI `--checks`, JSON-отчёт в `results/`), `agent_checks.py` (P-проверки на скриптовой модели), `gigachat_checks.py` (G-проверки), `http_proto.py` (прототип приложения для H-проверок, запуск `uvicorn`), `client.py` (клиент SSE с отметками времени и обрывом), `README.md`. Фейки движка и реестра берутся из `tests/unit/v2` импортом, без копий. Стенд импортирует `src/` и ничего в нём не меняет.

### D2. Проверки

| # | Что проверяем | Как | Критерий |
| --- | --- | --- | --- |
| P1 | Узлы в `updates` | Ход U2 (план → `resolve_terms` → сценарии → ответ) через `AgentFactory` со всем стеком на `ScriptedChatModel`; `astream(stream_mode=["updates", "custom", "messages"])`, каждая часть в журнал с отметкой времени | Список имён узлов (`model`, `tools`, `<Middleware>.<hook>`) и содержимое каждого обновления; видны ли `todos`, `AIMessage.tool_calls` и каждый `ToolMessage` отдельно; приходят ли обновления по ходу, а не пачкой в конце |
| P2 | Формат `version="v2"` | Тот же ход с `version="v2"` и без него | Есть ли параметр в `langgraph` 1.2.16, форма частей (кортеж или `StreamPart`), где `ns` и метаданные |
| P3 | Отклонённые ранние ответы | Скрипт: ответ с числом, которого нет в результатах (`AnswerGroundingGuard` → перегенерация), и ответ при открытом шаге плана (`PlanCompletionGuard` → `jump_to="model"`) | Попадает ли отклонённый `AIMessage` в `updates` узла `model` и в `messages`; по какому признаку его отличить (следующее обновление guard, напоминание) — правило гигиены FR-24 для S6.2 |
| P4 | `runtime.stream_writer` | Прототип `ProgressReporter` с `before_model`, `after_model`, `wrap_tool_call`; варианты места: (а) №5а по §8 — между `Telemetry` и сводкой, (б) внутри `ToolRetryMiddleware` (после `engine_retry`); скрипт с `engine_unavailable` на первом вызове расчёта | Пишет ли `stream_writer` из каждого хука; в каком месте виден повтор движка; что такое `stream_writer` при `ainvoke` (пустышка, без ошибки) |
| P5 | `messages` на скриптовой модели | Тот же ход; метаданные частей `messages` (`langgraph_node`, `tags`, `checkpoint_ns`) | Модель без `_astream` даёт один чанк на вызов; по каким метаданным отделяется основной вызов модели агента от сводки истории (та же модель, тот же узел) и от `resolve_terms` (`APP_CTX.llm` внутри инструмента) |
| P6 | Ход в отдельной задаче | Ход — `asyncio.Task`, части — в `asyncio.Queue`, потребитель отменяется посреди хода | Задача хода доживает до конца, блокировка снимается после конца хода, а не после отмены потребителя; итоговый текст совпадает с `ainvoke` |
| G1 | GigaChat streaming с функциями | Живой GigaChat (api_key), `astream` модели с привязанными инструментами агента, запрос, требующий вызова | `AIMessageChunk` после склейки содержит `tool_calls` с разобранными аргументами |
| G2 | `functions_state_id` | Тот же вызов, затем второй вызов с `ToolMessage` в истории | `functions_state_id` есть в `additional_kwargs` склеенного сообщения и уходит во второй запрос |
| G3 | `usage_metadata` | `astream` с `stream_usage` по умолчанию и с явным включением | Приходит ли `usage_metadata` (вход / выход) — нужно `Telemetry` (S4.3) |
| G4 | Ход агента на GigaChat в `astream` | Полный ход U2-подобного запроса через `AgentFactory` с GigaChat (фейки движка), `stream_mode=["updates", "custom", "messages"]`, 3 прогона | Ход завершается так же, как `ainvoke`; модель агента вызывается потоково (чанков > 1 на финальный ответ); время до первого токена ответа; строки `LLM CALL` с токенами в журнале |
| G5 | Порции потока GigaChat (добавлена по ходу: G4 показал, что чанки вызова приходят разом) | `astream` модели и SDK `GigaChat.astream` напрямую, ответ ≈ 250 и ≈ 900 слов, `GigaChat-3-Pro` и `GigaChat-2` | Сколько порций и когда приходит первая — копит LangGraph или сторона GigaChat |
| H1 | SSE за текущим `log_requests` | `http_proto.py`: приложение с `log_requests` как в сервисе и endpoint, отдающий события раз в 1 с; `client.py` на `httpx` с отметками времени; живой uvicorn | Ожидание: все кадры приходят одним куском в конце; фиксируем фактическое поведение и записи журнала |
| H2 | Прототип правки `log_requests` | Ветка для `text/event-stream`: тело не копится, `body_iterator` оборачивается, записи ответа, аудит, метрики и длительность пишутся после последнего кадра | Кадры приходят по мере отправки, первый ≤ 1 с; записи те же по составу; прочие ответы идут прежним путём |
| H3 | Разрыв соединения | Клиент рвёт соединение посреди «расчёта» (фейк движка с задержкой); варианты: (а) ход внутри генератора `StreamingResponse`, (б) ход в отдельной задаче + `disconnect_watch` | (а): отменяется ли генератор и что видит ход (`CancelledError` посреди вызова); (б): ход останавливается через `Cancellation` после текущего вызова, блокировка держится до конца хода, задача не висит |
| H4 | Heartbeat | Пауза событий 20 с при heartbeat 15 с (`: ping`) | Клиент получает комментарий; heartbeat не мешает порядку кадров и не продлевает поток после терминального события |
| C1 | **В контуре:** SSE через AI GW | Прототип H2 за шлюзом AI GW, клиент со стороны фронта | Кадры приходят по мере отправки; соединение живёт ≥ 5 мин на одном heartbeat; заголовки `X-Accel-Buffering`, `Cache-Control` доходят |
| C2 | **В контуре:** G1–G3 в режиме сертификатов | `run.py --checks G1,G2,G3` с `LOCAL=True` и сертификатами | Те же критерии, что G1–G3 |

### D3. Критерии решений

- **ADR-14, режим потока:** `updates` + `custom` подтверждаются, если P1 даёт по каждому вызову инструмента начало (`tool_calls` в обновлении `model`) и конец (`ToolMessage` в обновлении `tools`) по ходу хода, а P4 — запись из `before_model` и `wrap_tool_call`. Иначе — переход на `astream_events` для недостающих событий с обоснованием в отчёте.
- **Место `ProgressReporter`:** по P4. Если повтор движка виден только внутри `ToolRetryMiddleware`, `ProgressReporter` делится на две части (фазы и окно ответа — №5а; исход `retry` — внутри повторов) или исход `retry` берётся из конверта `ToolMessage` — выбирается вариант без второго класса, если он даёт тот же исход.
- **Гигиена отклонённых ответов (FR-24):** по P3 записывается правило, по которому `StreamTranslator` не выпускает отклонённый текст в прогресс.
- **ADR-15, ответ по токенам:** остаётся, если G1–G3 проходят, G4 даёт > 1 чанка на ответ, а P5 / G4 дают однозначный признак основного вызова (тег модели агента, `langgraph_node` + отсутствие вложенного `checkpoint_ns` или иной). Если G1 или G2 не проходят — по умолчанию `AGENT_STREAM_ANSWER=whole`, прогресс остаётся; если G3 не проходит — `Telemetry` в потоковом ходе берёт токены иначе (решение в отчёте).
- **Ход и разрыв (AR-Q17, AR-Q12):** вариант H3(б) принимается, если он проходит; H3(а) фиксируется как отвергнутый с наблюдённым поведением.
- **`log_requests`:** H2 — эскиз правки для S6.3 с перечнем сохраняемых записей; характеризационные тесты пишет S6.3 до правки.
- **Выпуск E6:** C1 блокирует выпуск E6 (§13 «Путь до фронта»), но не архивацию S6.1 — результат C1 заносится в отчёт, когда будет.

### D4. Модель и доступ к сети

P-проверки — только скриптовая модель и фейки, без сети и LibreOffice. G-проверки — живой GigaChat по `.env` (api_key, `LOCAL=False`), как в S0.2. H-проверки — локальный uvicorn на свободном порту, без GigaChat (скриптовая модель или генератор событий).

### D5. Кадры SSE без `sse-starlette`

Прототип пишет кадры вручную (`event:` / `id:` / `data:` + пустая строка, heartbeat `: ping`) через `StreamingResponse(media_type="text/event-stream")`. Новая зависимость не нужна: формат прост, а `sse-starlette` добавил бы свою логику разрыва и ping поверх нашей (H3, H4). Если H3 покажет, что без библиотеки разрыв не ловится, — отметить в отчёте как вопрос к S6.3.

## Risks / Trade-offs

- [Скриптовая модель без `_astream` не показывает токены] → P5 проверяет только метаданные и отбор; поток токенов — G4 на GigaChat.
- [Фоновая задача preview-модели и судья пишут в `messages`] → в G4 `AGENT_PREVIEW_CHECK=false` (preview-модели вне контура нет), отбор preview-вызовов проверяется на P5 заменой preview-модели скриптовой.
- [Живой GigaChat нестабилен (429, StopEvent)] → 3 прогона G4, сбои записываются как результат, а не как провал критерия, если причина — сеть.
- [Поведение uvicorn локально ≠ за AI GW] → C1 в контуре; до C1 выпуск E6 не делается.
- [Стенд устаревает] → он в `scripts/spikes/`, как S0.2 и S0.3, в приложение и `pytest` не входит.

## Results

Прогон 2026-10-28, отчёты — `scripts/spikes/s6_1_stream/results/`. GigaChat — публичный `api.giga.chat`, api_key, `GigaChat-3-Pro`.

### Итоги проверок

| # | Результат |
| --- | --- |
| P1 | Обновления приходят по мере хода: `ToolMessage` расчёта — через 0,5 с задержки фейка, остальное — сразу после своего узла. Узлы: `model`, `tools` и узлы хуков `<Middleware>.<hook>` (`TurnContext.before_agent`, `LanguageGuard.before_agent`, `SkillsMiddleware.before_agent` / `.before_model`, `TurnContext` / `Cancellation` / `TurnLimits.before_model`, `PlanCompletionGuard` / `AnswerGroundingGuard` / `TurnLimits` / `PlanTodoList.after_model`, `TurnSummary.after_agent`). Хуки без изменений дают пустое обновление. Вызов инструмента: начало — обновление `model` с `AIMessage.tool_calls` (приходит до запуска инструмента), конец — обновление `tools` с `ToolMessage` того же `tool_call_id`; `todos` — в обновлении `tools` у `write_todos`. Закреплённый skill — `HumanMessage` в обновлении `SkillsMiddleware.before_model` и в `messages` |
| P2 | `version="v2"` есть в `langgraph` 1.2.16: часть — словарь `{"type", "ns", "data"}`; для `messages` `data = (сообщение, метаданные)` |
| P3 | Отклонённый ответ попадает и в `messages`, и в обновление `model` целиком, до решения guard. Признак отклонения — следующее обновление `AnswerGroundingGuard.after_model` или `PlanCompletionGuard.after_model` с `HumanMessage`-напоминанием и `jump_to`. Принятый ответ — обновление `model` без `tool_calls`, за которым guards дают пустые обновления, а затем `TurnSummary.after_agent` |
| P4 | `runtime.stream_writer` пишет из `abefore_model`, `aafter_model` и `awrap_tool_call` (`request.runtime.stream_writer`), части `custom` приходят сразу. Повтор движка (`engine_unavailable` → повтор) виден только копии внутри `ToolRetryMiddleware`: она получает исключение `ToolError` и второй `tool_start`; копия на месте №5а видит один вызов с успешным исходом. `awrap_tool_call` получает не `ToolMessage`, а `Command` адаптера — исход берётся из `Command.update["messages"]`. Под `ainvoke` `stream_writer` — функция `Pregel.astream` без потребителя: вызов безопасен, ничего не делает. Узел своего middleware в `updates` даёт пустое обновление |
| P5 | Основной вызов модели и вызов сводки истории неотличимы по метаданным: тот же `langgraph_node="model"`, тот же `checkpoint_ns`, тегов нет. Вложенный вызов внутри `resolve_terms` (`APP_CTX.llm`) приходит с `langgraph_node="tools"`. Preview-модель и судья в поток не попадают (`NO_CALLBACKS` и чистый `contextvars.Context`, S4.4). Закреплённый skill — `HumanMessage` узла `SkillsMiddleware.before_model`. Модель сводки с тегом `nostream` (`langgraph.constants.TAG_NOSTREAM`, `model.with_config(tags=["nostream"])`) из `messages` исчезает, остальное не меняется |
| P6 | Ход в отдельной задаче с очередью: отмена потребителя посреди расчёта не трогает ход, расчёт доходит до конца (`finished=1`, `cancelled=0`), блокировка снимается через 0,75 с после отмены — с концом хода |
| G1 | Проходит: в потоке вызов функции приходит одним чанком с `function_call`; после склейки `tool_calls` разобраны, `invalid_tool_calls` пуст — как у `ainvoke` |
| G2 | Проходит: `functions_state_id` есть в склеенном чанке и уходит в следующий запрос (`_build_payload`), модель продолжает цикл |
| G3 | Проходит: `usage_metadata` приходит в последнем чанке и для текста, и для вызова функции |
| G4 | Как собирает сервис сегодня, `GigaChat` вызывается **без** потока даже под `astream(..., "messages")`: класс явно ставит `streaming=False`, и `langchain_core` (`_streaming_disabled`) это уважает — в `messages` по одному целому сообщению на вызов. С `GigaChat(streaming=True)`: 3 хода из 3 завершаются, 15–25 чанков на текстовый ответ, склеенный поток совпадает с итоговым сообщением, во всех строках `LLM CALL` есть токены. Но все чанки одного вызова приходят в одну миллисекунду (G5). В каждом ходе было 3–4 текстовых ответа: ранние отклонялись guards, модель продолжала вызовами `derive` / `write_todos` |
| G5 | Порции копит сторона GigaChat: даже SDK напрямую (`aiter_lines`) получает ответ ≈ 250 слов одной пачкой через 2,7–3,1 с (`update_interval=0.1` не меняет); ответ ≈ 900 слов — 3 пачки: `GigaChat-3-Pro` 6,9 / 13,5 / 16,1 с, `GigaChat-2` 5,7 / 7,4 с |
| H1 | Подтверждено: за текущим `log_requests` 5 кадров с интервалом 1 с приходят одним куском через 5,0 с; записи ответа и длительность (5014 мс) — после конца |
| H2 | Ветка для `text/event-stream`: первый кадр через 5 мс, далее раз в секунду; запись ответа, аудит, метрики и длительность (5010 мс) — после последнего кадра. JSON-ответ через тот же middleware пишет прежний набор записей |
| H3 | Starlette 1.6 отменяет генератор ≈ через 0,2 с после разрыва, даже без отправки кадров. (а) Ход внутри генератора: `CancelledError` прилетает в граф посреди расчёта, граф обрывается, блокировка снимается сразу, а расчёт доживает в фоне без хозяина — новый ход сессии мог бы начаться, пока движок занят старым. (б) Ход в задаче: генератор в `finally` ставит `RequestContext.cancel`, ход доводит расчёт, `Cancellation` завершает его без вызова модели, блокировка снимается с концом хода (≈ 2,6 с после разрыва). `disconnect_watch` (S2.8) внутри потока разрыв не видит: `http.disconnect` забирает слушатель Starlette. Текущий `log_requests` при разрыве потока не пишет записей ответа вовсе; прототип H2 пишет их в момент разрыва |
| H4 | `: ping` приходит по таймеру, пока идёт расчёт; после терминального кадра поток закрывается, лишних ping нет |
| C1 | Пройдена в контуре (подтверждено владельцем 2026-10-28): SSE через AI GW до фронта приходит без буферизации; выпуск E6 не блокирует |
| C2 | Снята: модель агента не стримит (ADR-15 ниже), потоковый режим GigaChat на сертификатах проверять незачем |

### Таблица «узел / событие LangGraph → событие прогресса» (для `StreamTranslator`, S6.2)

| Источник (`astream`, `version="v2"`) | Условие | Событие прогресса |
| --- | --- | --- |
| `updates` · `TurnContext.before_agent` | первое обновление хода | `turn.started` |
| `custom` · `ProgressReporter.before_model` | перед каждым вызовом модели | `phase` (`planning` до плана, `executing` при открытых шагах, `answering` при закрытом плане) |
| `updates` · `model` | `AIMessage.tool_calls`, имя не служебное | `tool.started` по каждому вызову (подпись по имени и аргументам) |
| `updates` · `model` | `tool_calls` = `write_todos` | ничего: план придёт в `tools` |
| `updates` · `tools` | `ToolMessage` инструмента | `tool.finished` (исход и сводка из конверта; длительность — от `tool.started` того же `tool_call_id`) |
| `updates` · `tools` | ключ `todos` | `plan` |
| `custom` · `ProgressReporter.wrap_tool_call` | исключение попытки, которое `ToolRetryMiddleware` повторит | исход `retry` для `tool.finished` этого вызова |
| `updates` · `model` | текст без `tool_calls` | кандидат ответа, в прогресс не идёт; `phase=checking` |
| `updates` · `AnswerGroundingGuard.after_model` / `PlanCompletionGuard.after_model` | есть `jump_to` | кандидат отклонён: отбросить |
| `updates` · `TurnSummary.after_agent` | конец хода | `summary`, затем последний кандидат — `answer` (FR-26) |
| `updates` · `SkillsMiddleware.*`, `LanguageGuard`, `TurnLimits`, `Cancellation`, `PlanTodoList`, свой `ProgressReporter` | всегда | ничего (служебное, FR-24) |
| исключение хода | `StopEventError`, `TurnBusyError`, таймаут, прочее | `error` (терминальное) |

### Решения

- **ADR-14 — подтверждён.** Источник: `agent.astream(stream_mode=["updates", "custom"], version="v2")`; `"messages"` не нужен (ответ целиком, см. ADR-15 ниже). Начало и конец вызова инструмента берутся из `updates` (P1), `astream_events` не нужен. Ход — отдельная `asyncio`-задача с очередью (P6, H3(б)); вариант «ход внутри генератора» отвергнут по H3(а).
- **Место `ProgressReporter` — внутри `ToolRetryMiddleware`, а не №5а.** Только там виден повтор движка (P4), а `before_model` там работает так же. Один класс: `before_model` — фаза; `wrap_tool_call` — только исход `retry` (решение «будет повтор» — тем же предикатом `is_transient_tool_error`, что у `engine_retry`). Начало и конец вызова идут из `updates`, поэтому попадания `CalcCache` (он снаружи) тоже дают `tool.started` / `tool.finished`. Фаза `checking` выводится переводчиком из текстового обновления `model`, `after_model` у `ProgressReporter` не нужен: guards не вызывают LLM. Отклонение от §8 №5а — причина в P4.
- **Гигиена отклонённых ответов (FR-24):** текст из обновления `model` — только кандидат до пустых обновлений guards и `TurnSummary.after_agent`; напоминания guards (`HumanMessage`) и закреплённые skills в прогресс не идут (P3, P5).
- **ADR-15 — финальный ответ целиком (решение владельца 2026-10-28 по этим итогам).** Ответ по токенам технически реализуем: модель агента нужно строить с `GigaChat(streaming=True)` (класс явно ставит `streaming=False`, G4), токены отбирать по `langgraph_node == "model"` и `AIMessageChunk`, вызов сводки истории помечать тегом `nostream` (P5); вызовы функций, `functions_state_id` и токены при этом сохраняются (G1–G3). Но финальность ответа во время генерации неизвестна — её решают guards по готовому тексту (P3), в G4 ранние ответы отклонялись 2–3 раза за ход, а публичный GigaChat отдаёт поток пачками, и типичный ответ приходит одной пачкой в конце вызова (G5). Выигрыша по времени почти нет, зато текст мелькал бы через `answer.reset`, а проверку чисел в потоке пришлось бы выключить. Поэтому `answer` — одно событие после `TurnSummary.after_agent`, `"messages"` в `stream_mode` не нужен, story S6.6 снята, C2 не нужна (модель агента не стримит).
- **Разрыв соединения (AR-Q17, AR-Q12):** признак разрыва — `CancelledError` / закрытие генератора ответа; генератор в `finally` ставит `RequestContext.cancel`, если ход ещё идёт. `disconnect_watch` для потока не используется (H3). Heartbeat для обнаружения разрыва локально не нужен — он нужен промежуточным узлам (AI GW, C1).
- **`log_requests` (эскиз для S6.3, прототип — `http_proto.py:log_requests_patched`):** при `content-type: text/event-stream` тело не копится; `body_iterator` оборачивается, и в `finally` обёртки пишутся запись ответа (тело — число кадров и исход: `completed` / разрыв), аудит `BusinessRequestFinished`, метрики `responses_total`, `process_duration_ms` за весь поток и статус. Записи запроса и заголовки `x-*` ответа — как сейчас, до первого кадра. В промышленной правке эта ветка не пишет записи ответа до потока (прототип пишет их дважды, потому что оборачивает неизменённый middleware). Прочие ответы идут прежним путём (H2).
- **AR-Q16:** первое событие — через 2–5 мс от начала хода (P1, H2) при пропуске без буферизации; на живом сервисе первое событие — `turn.started` после `TurnContext.before_agent`.
