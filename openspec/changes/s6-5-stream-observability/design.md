# Design

## Context

Мотивация — proposal.md, «Why». Что есть сейчас:

- `invoke_agent_stream` (`api/v1/router.py`) строит события: для v2 — `AgentV2.stream()` → `TurnStream`, для v1 — `sse.one_shot`. Затем отдаёт `StreamingResponse(sse_body(events))`. `sse_body` (`api/v2/stream/sse.py`) — единственное место, через которое проходит каждый кадр и каждый ping обоих движков. Только здесь видно, что клиент ушёл: Starlette отменяет генератор, и срабатывает `finally` со `pending.cancel()`.
- Интервал heartbeat — константа `sse.HEARTBEAT_S = 15.0`. `sse_body(heartbeat_s=None)` подставляет её, тесты меняют её через `monkeypatch.setattr(sse, "HEARTBEAT_S", …)`.
- `log_requests` (S6.3) оборачивает тело потока в `_logged_stream` и пишет записи ответа в `finally`. По §13 п. 5 его метрики не несут исхода хода: исход — дело строки `STREAM`.
- `turn_id` создаёт `TurnContext.before_agent` (`uuid4().hex`) и кладёт в состояние; в `RequestContext` его нет. В потоке `updates` он приходит обновлением узла `TurnContext.before_agent`.
- Метаданные Langfuse задаёт `_turn_setup` (S4.3 D2): `langfuse_tags = ["agent_engine=<v1|v2>"]`. Функция общая для обоих endpoint и уже получает аргумент `endpoint`.
- Механизм метрик — `logger.metric(metric_name, metric_value)` (`logger/logger.py`), его использует `log_requests`. `metric_router.py` править нельзя.
- Схема `v = 1` — pydantic-модели `api/v2/stream/events.py` (`EVENT_TYPES`, `extra="forbid"`) и описание `StreamEvent*` в `openapi_copilot.yaml` (S6.3–S6.4). В OpenAPI у каждой схемы есть свойство `event` с одним значением в `enum`, хотя в `data` оно не передаётся: тип события идёт строкой `event:` кадра.
- Части U10 уже проверены по отдельности: `TestStreamContract.test_frames_and_same_text_as_zip` (ASGI) и `TestLiveStream.test_disconnect_mid_calc` (живой uvicorn, `TimedGate` с дочерним процессом `sleep` вместо `soffice`). Сквозного сценария U10 нет, а второй проход сейчас идёт по короткому сценарию, а не по U2.

## Goals / Non-Goals

**Goals:**
- Один объект на поток, который считает кадры и пишет строку `STREAM` и метрики. Он не знает о FastAPI и движках и не может сломать поток.
- Интервал heartbeat из `AgentSettings`, без глобальной константы.
- Контрактный тест, которому не нужна сеть: слепок схемы в репозитории и сверка с OpenAPI.
- U10 — один тестовый модуль на тех же стендах, что S6.3.

**Non-Goals:**
- Строка `STREAM` на момент конца хода, а не потока (D2).
- Метрики по каждому исходу `answer` отдельно, гистограммы, правка формата метрик.
- Проверка OpenAPI внешним валидатором (`openapi-spec-validator` и т. п.) — новая зависимость не нужна.

## Decisions

### D1. `StreamStats` в `api/v2/stream/observe.py`, его кормит `sse_body`

```python
class StreamStats:
    def __init__(self, trace_id: str, engine: str, started: float, clock=time.perf_counter): ...
    def event(self, event: StreamEvent) -> None   # первое событие → first_event_ms; счёт по типам; терминальное → outcome
    def ping(self) -> None
    def close(self, turn_id: str | None) -> None  # один раз: строка STREAM + метрики; никогда не бросает
```

`sse_body(events, heartbeat_s: float, stats: StreamStats | None = None)` вызывает `stats.event(e)` перед `yield encode(e)`, `stats.ping()` перед `yield PING`, а в своём `finally` — `stats.close(getattr(events, "turn_id", None))`. Разрыв = `close` без терминального события, поэтому отдельный флаг не передаётся: `disconnected = outcome is None`.

`started` берётся в `invoke_agent_stream` первой строкой (`time.perf_counter()`), до `_turn_setup` и захвата блокировки. Так время до первого события включает подготовку хода: именно его видит клиент (AR-Q16, ≤ 1 с). Время до приёма тела запроса не входит: оно уже есть в `log_requests`.

Почему в `sse_body`, а не в `TurnStream` / `one_shot`: `sse_body` — общая точка обоих движков, и только она знает о ping и о разрыве. `TurnStream` разрыва не видит: он лишь получает отмену чтения, ход идёт дальше.

Почему не в `log_requests`: тогда пришлось бы разбирать кадры ещё раз и трогать модуль, характеризационные тесты которого должны проходить без правок. Там же нет ни `turn_id`, ни движка.

Логгер берётся в `close` через `APP_CTX.get_logger()`, а не на уровне модуля. Так тесты подменяют его через `recorder` (`tests/unit/test_log_requests.py:RecordingLogger`), как в S6.3. Строка: `logger.info("STREAM {}", json.dumps(record, ensure_ascii=False))` — JSON идёт аргументом, поэтому фигурные скобки не ломают loguru. Тело `close` обёрнуто в `try/except Exception` с `logger.opt(exception=True).error("STREAM record failed: {}", type(e).__name__)` и вызывается не больше одного раза.

Поля записи:

| Поле | Значение |
| --- | --- |
| `trace_id` | `x-trace-id` |
| `turn_id` | из `TurnStream.turn_id` (D3); `null` для v1 и хода, не дошедшего до `TurnContext` |
| `engine` | `v1` / `v2` |
| `first_event_ms` | от `started` до первого кадра события (ping не считается); `null`, если событий не было |
| `duration_ms` | от `started` до `close` |
| `events` | `{"turn.started": 1, "phase": 4, …}` — только отданные клиенту |
| `pings` | число `: ping` |
| `outcome` | `outcome` терминального `answer` или `kind` терминального `error`; `null` без терминального |
| `disconnected` | `outcome is None` |

«Отданные клиенту» — переданные в `yield`. Кадр, на котором Starlette оборвал генератор, может не дойти, но считается. Точнее изнутри генератора не узнать, а для эксплуатации расхождение в один кадр не важно.

### D2. Строка — в момент конца потока, а не хода

При разрыве ход дозавершается ещё секунды (AR-Q17). Если ждать конца хода, `sse_body` пришлось бы держать ссылку на задачу хода после `finally`, а строку писать из `done_callback` в другом контексте, без `ContextVar` запроса. Исход хода после разрыва и так есть в `TURN SUMMARY` и `LLM CALL` (S4.3), строки связываются по `turn_id`. Поэтому `STREAM` описывает поток — что получил клиент. Так и задано в §13: «исход пишется в строку `STREAM`», а для разрыва исход — сам разрыв.

### D3. `turn_id` — из обновления узла в `TurnStream`

`TurnStream._run` при каждой части `updates` ищет в обновлениях узлов ключ `turn_id` и запоминает первое значение в `self.turn_id: str | None`. Пример части: `{"type": "updates", "data": {"TurnContext.before_agent": {"turn_id": …}}}`. Имя узла не проверяется: ключ состояния уникален, и так проверка переживёт переименование middleware. `StreamTranslator` не меняется: он переводит события, а `turn_id` в поток не идёт. У `one_shot` (v1) атрибута нет, `getattr(..., None)` даёт `null`.

Альтернатива — писать `turn_id` в `RequestContext` из `TurnContext`. Отклонена: правка middleware ради журнала, и поле контекста, которое читал бы только журнал.

### D4. Метрики через `logger.metric`

| Метрика | Значение | Когда |
| --- | --- | --- |
| `aigw_service_streams_total` | 1 | каждый поток |
| `aigw_service_stream_first_event_ms` | `first_event_ms` | если было хоть одно событие |
| `aigw_service_stream_disconnects_total` | 1 | `disconnected` |
| `aigw_service_stream_errors_total` | 1 | терминальное `error` |

Пишет их `StreamStats.close` после строки `STREAM`. Префикс `aigw_service_` — как у метрик `log_requests`, поэтому существующий сбор их подхватит. Отдельной метрики длительности нет: `aigw_service_process_duration_ms` в `log_requests` уже считается за весь поток (S6.3).

### D5. Тег `stream=true` в `_turn_setup`

`_turn_setup` уже получает `endpoint`. Если `endpoint == "/invoke-agent/stream"`, в `langfuse_tags` добавляется `"stream=true"`. Тег ставится для обоих движков: у v1 тоже есть трасса Langfuse через тот же `cb_handler`. Строка endpoint берётся из константы, общей с декоратором маршрута, чтобы проверка не разошлась с путём. Тест — по `config["metadata"]["langfuse_tags"]`, как в S4.3 D2: живой Langfuse не нужен.

### D6. `AGENT_STREAM_HEARTBEAT`

`AgentSettings.stream_heartbeat: float = Field(default=15.0, gt=0, validation_alias="AGENT_STREAM_HEARTBEAT")`. Верхней границы нет: выбор зависит от таймаута прокси в контуре, а слишком большой интервал виден сразу по обрывам. У `sse_body` параметр `heartbeat_s` становится обязательным, `HEARTBEAT_S` удаляется. Роутер передаёт `APP_CONFIG.agent.stream_heartbeat` при каждом запросе, поэтому тесты меняют настройку через `monkeypatch.setattr(APP_CONFIG.agent, "stream_heartbeat", 0.2)`. Это уже сделано так для `turn_timeout` в `service.py`. Тест `test_stream_sse.py`, который проверял `HEARTBEAT_S == 15.0`, начинает проверять значение по умолчанию в настройках.

Отклонение от §13 «Кадр» нет: там прямо сказано, что S6.5 переносит константу в `AGENT_STREAM_HEARTBEAT`. Текст «каждые 15 с» в описании `openapi_copilot.yaml` и в `STREAM_DESCRIPTION` роутера меняется на «каждые `AGENT_STREAM_HEARTBEAT` с (по умолчанию 15)».

### D7. Контракт схемы: слепок + сверка с OpenAPI

Нормализованное представление схемы строится из pydantic-моделей без сети:

```python
def contract() -> dict[str, dict[str, dict]]:
    # {"tool.finished": {"call": {"type": "integer", "required": True}, "outcome": {"type": "string", "enum": [...], "required": True}, ...}, ...}
```

Источник — `cls.model_json_schema()` каждого класса из `EVENT_TYPES`. Поле `event` исключается: оно идёт строкой `event:` кадра, а не в `data`. Вложенные модели (`PlanStepView` в `plan.steps`) разворачиваются в `items`, `Optional` нормализуется как `nullable: true`. Хелпер — в тесте (`tests/unit/v2/test_stream_contract.py`), не в `src`: сервису он не нужен.

Тест 1, добавочность: `contract()` сравнивается со слепком `tests/unit/v2/contracts/stream_events_v1.json`.
- Каждый тип слепка есть в текущей схеме, и каждое его поле есть с тем же `type`, `nullable`, `enum` и `items`.
- Необязательное поле не стало обязательным.
- Новое поле — только необязательное. Новый тип события допустим.
- Сообщение об ошибке называет тип события и поле.

Тест на выявление проверяет сам сравнитель на подделанных слепках: удалённое поле, новый обязательный, смена `enum`, новый тип. Так проверяется, что сравнитель ловит каждую ломку из спеки, а не только что текущая схема совпадает со слепком.

Слепок обновляется только добавочно. Для этого есть скрипт-флаг `UPDATE_STREAM_CONTRACT=1 pytest tests/unit/v2/test_stream_contract.py`: он перезаписывает файл, только если сравнение добавочное. Ломающая правка требует `v = 2` — это отдельная story, не этот флаг.

Почему перечисление сравнивается на равенство: §13 и AR-Q18 разрешают только новые поля и новые типы. Новое значение `phase` или `outcome` строгий фронт не разберёт. Пример — `preparing`: его убрали в S6.4 до слепка, после слепка это было бы ломкой.

Тест 2, OpenAPI: `yaml.safe_load(openapi_copilot.yaml)`, затем для каждой схемы `StreamEvent*` из `StreamEventData.oneOf`. Схема собирается из `allOf` (`StreamEventCommon` + свои поля), тип события берётся из `properties.event.enum[0]`. Поля, их обязательность, тип и `enum` сверяются с `contract()`. Набор типов в `oneOf` должен совпасть с `EVENT_TYPES`. `nullable` в OpenAPI 3.0 и `anyOf: [..., null]` в pydantic приводятся к одному виду. Отклонение: `format: date-time` у `ts` и `minimum` у `seq` не сверяются — это ограничения значений, а не форма данных.

Отклонение от формулировки §15 («контрактный тест схемы `v = 1`»): тестов два, второй закрывает «описана в OpenAPI» из AR-Q18. Без него OpenAPI расходится с моделями незаметно — так в S6.4 `preparing` пришлось вручную удалять в трёх местах.

### D8. U10 — `tests/unit/v2/test_stream_u10.py`

Хелперы — из `test_stream_api.py` и `test_stream_turn.py`: `StreamApi`, `fresh`, `parse_sse`, `live`, `read_live`, `TimedGate`, `alive`, `wait_free`, `u2_steps`, `ANSWER_U2`, `violations`. Если для импорта их нужно перенести в общий модуль, они переносятся в `tests/unit/v2/stream_stands.py` без изменения поведения.

- **Проход 1 (ASGI)** — `stream_and_zip(monkeypatch, tmp_path, u2_steps)` с `recorder`. Проверки по сценарию FR-26 «U10, первый проход»: `plan`, у каждого `tool.finished` с `ok` есть `summary`, `summary` перед `answer`, текст `answer` равен ZIP и `ANSWER_U2`, `violations` по событиям без текста `answer` пуст. Строка `STREAM` одна, `outcome = "full"`, `events` равны счёту кадров тела.
- **Проход 2 (живой uvicorn)** — U2 со сценариями через `TimedGate(delay, child_s=1.0)` вместо движка. Чтение до `tool.started` вызова `run_scenarios`, пока `gate.pids` не пуст; затем клиент уходит. Дальше проверки как в `test_disconnect_mid_calc`: 409 во время дозавершения, `gate.finished` = 1, `not alive(pid)`, новых вызовов модели нет, следующий ход — `answer`. Строка `STREAM` первого потока: `disconnected = true`, `outcome = null`. События до разрыва без нарушений гигиены.

Без сети и LibreOffice: модель скриптовая, расчёт идёт через `TimedGate`, `sleep` заменяет `soffice`. Это тот же уровень, что «Поток» в таблице тестов §14. Живой прогон с LibreOffice остаётся в интеграционном слое (вне рамок).

### D9. Документы

- README: строка `AGENT_STREAM_HEARTBEAT` в таблице «Параметры агента», короткий раздел «Поток: журнал и метрики» с полями `STREAM` (таблица D1) и метриками (D4).
- §13 спецификации: «Кадр» — без «до S6.5 … константа», вместо этого `AGENT_STREAM_HEARTBEAT`; «Наблюдаемость → Поток» — поля строки, момент записи (D2), имена метрик (D4), слепок контракта (D7).
- `openspec/traceability.md`: AR-Q18 — S6.5 ✓ при архивации; S6.5 — в участниках AR-Q5, AR-Q16, AR-Q17, FR-24, FR-26.

## Risks / Trade-offs

- [Описание потока в `openapi_copilot.yaml` не закоммичено: в рабочем дереве ≈ 300 строк S6.3–S6.4] → Тест 2 D7 сверяется с файлом рабочего дерева и пройдёт локально, но на чистом клоне упадёт. Перед архивацией S6.5 разработчик коммитит `openapi_copilot.yaml` (отдельная задача). Агент файл не коммитит.
- [Кадр, на котором оборвался генератор, считается отданным] → Расхождение не больше одного события, указано в README.
- [`first_event_ms` измеряется до `yield`, а не до доставки в сокет] → Буферизацию между генератором и сокетом проверяет `TestLiveStream` (S6.3). Это время подготовки хода, его и смотрит эксплуатация.
- [Слепок схемы можно обновить вместе с ломающей правкой] → Флаг обновления пишет файл только при добавочном сравнении; иначе тест падает с названием поля. Ревью видит дифф слепка.
- [Новые имена метрик могут не попасть в дашборды контура] → Имена в README; дашборды вне рамок.
- [`close` в `finally` генератора, который Starlette отменил] → `close` синхронный, без `await`: отмена его не прервёт. Исключение внутри перехвачено (D1).

## Migration Plan

Совместимо в обе стороны: клиент потока и `/invoke-agent` изменений не видят. `AGENT_STREAM_HEARTBEAT` по умолчанию — прежние 15 с, переменную в `.env` / `docker.env` добавлять не обязательно. Откат — откат коммита: строка `STREAM`, метрики и тег пропадут, поток останется прежним.
