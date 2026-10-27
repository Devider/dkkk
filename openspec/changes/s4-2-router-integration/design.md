# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-api/spec.md` (FR-15, FR-20). ADR-12, §12 пп. 3 и 7, §13 «Контракт API», AR-Q7, AR-Q11, spike S0.2 (C8: StopEvent).

Состояние:
- `router.py:invoke_agent`: ветка v2 строит `RequestContext` и вызывает `AgentV2.answer(message, context, config)`; ответ упаковывается в тот же ZIP, что у v1. `StopEventError` → 503 (`FailedDependencyResponse`), прочие исключения → 424.
- `thread_id = "{user_id}:{session_id}"` (session — из `x-session-id`, иначе новый uuid).
- Ход без загруженного файла уже завершается в `TurnContext` фиксированным ответом без LLM (S2.2): HTTP 200.
- v2 строит GigaChat в `models.build_model()` без обёртки `_wrap_llm_with_stop_event`; смысловой слой (`resolve_terms` → анализатор v1) вызывает `APP_CTX.llm`, который в v1 тоже не обёрнут (bugfix `fix-v1-stop-event-503` отложен). `resolve_terms` не глушит `ForbiddenError` и `StopEventError` (S1.3) — они выходят из инструмента исключением.
- Маркер StopEvent — `context._STOP_EVENT_MARKER` (`b"temporarily unavailable due to technical reasons"`); `StopEventError(user_message, url, status_code, reason)` — `aigw_service.exceptions`.
- `tests/integration/v1_api` — `test_health`, `test_errors` (404 на неизвестный путь) через `httpx.AsyncClient` + ASGI; `APP_CTX.on_startup()` вызывается фикстурой.

## Goals / Non-Goals

**Goals:**
- Контракт `/invoke-agent` для v2: ZIP, 200 без файла, 409, 503.
- Блокировка и StopEvent — минимально, без механики S2.5 / S2.8.

**Non-Goals:**
- Дедлайн и отмена хода (S2.8), повторы LLM (S2.5), распределённая блокировка.

## Decisions

### D1. Блокировка хода (`api/v2/turn/lock.py`)

```python
class TurnBusyError(RuntimeError): ...
class TurnLocks:
    def __init__(self) -> None: self._busy: set[str] = set()
    @contextlib.contextmanager
    def hold(self, thread_id: str) -> Iterator[None]:
        if thread_id in self._busy: raise TurnBusyError(thread_id)
        self._busy.add(thread_id)
        try: yield
        finally: self._busy.discard(thread_id)
TURN_LOCKS = TurnLocks()
```
Множество занятых `thread_id` вместо `asyncio.Lock` на сессию: проверка и захват — синхронные строки без `await` между ними, поэтому в одном цикле событий гонки нет, а освобождённая сессия не оставляет объект в памяти (без утечки по числу сессий). Очереди нет (§16: «без очереди, 409»).

`AgentV2(agent=None, locks=TURN_LOCKS)`; `answer()` оборачивает `ainvoke` в `locks.hold(context.thread_id)`. Блокировка отдельна от блокировки движка по сессии в `engine_gate` (§12 п. 2–3).

`router.py`: `except TurnBusyError` → 409, `FailedDependencyResponse(error_description=TURN_BUSY_MESSAGE)`, где `TURN_BUSY_MESSAGE = "Предыдущий запрос ещё обрабатывается. Направьте следующий вопрос позже."`; ставится перед общим `except Exception`.

### D2. `StopEventGuard` (`api/v2/middleware/stop_event.py`)

`awrap_model_call` / `wrap_model_call` и `awrap_tool_call` / `wrap_tool_call`: вызов `handler`, на исключение — `stop_event(exc)`: `ForbiddenError` с маркером в `content` (без учёта регистра) → `StopEventError("Выбранная модель GigaChat временно недоступна. Попробуйте позже.", url, 403, reason)` `from exc`; иначе исключение пробрасывается как есть. Маркер импортируется из `aigw_service.context` (один источник с v1). Журнал — `logger.error` без тела ответа целиком.

Место — сразу после `LanguageGuard` (§8 №4): снаружи всех будущих `wrap_*` (повторы S2.5 окажутся внутри и StopEvent не повторят). Инструменты: StopEvent смыслового слоя выходит из `resolve_terms` исключением — `wrap_tool_call` guard превращает его в `StopEventError`, ход прерывается. Подмена методов модели (`object.__setattr__`) в v2 не нужна (S0.2).

### D3. Тесты (`tests/unit/v2/test_api.py`)

Через `ASGITransport(app_main)` с `dependency_overrides` для `get_agent` и `get_cb_handler` (как в S2.1/S2.2):
- ZIP: состав и текст; нет загрузки → 200, `NO_MODEL_ANSWER`, 0 вызовов модели.
- 409: скриптовая модель, шаг которой ждёт `asyncio.Event`; первый запрос стартует задачей, второй в ту же сессию → 409 мгновенно, в другую сессию → 200; после освобождения события первый → 200, а повтор в ту же сессию → 200. Ошибка хода освобождает блокировку.
- 503: модель бросает `ForbiddenError` с маркером → 503 и `user_message`; без маркера → 424; `resolve_terms` с анализатором, бросающим `ForbiddenError` с маркером → 503.
- AR-Q7: два `x-user-id` с одинаковым `x-session-id` и общим checkpointer — во втором ходе второго пользователя нет сообщений первого.
- `TurnLocks`: повторный захват, освобождение после исключения, пустое множество после хода.

### D4. Интеграционные тесты

`tests/integration/v1_api` без правок, два прогона: `AGENT_ENGINE=v1` и `AGENT_ENGINE=v2`, Langfuse выключен на время прогона (как в прошлых story; `.env` восстанавливается из копии).

## Risks / Trade-offs

- [Блокировка в памяти процесса] → как `MemorySaver`: сервис работает одним процессом uvicorn (§12 п. 7); для нескольких — общий checkpointer и распределённая блокировка (вне рамок).
- [Ход, зависший навсегда, держит сессию] → дедлайн хода — S2.8; до неё таймаут HTTP-клиента не освобождает блокировку, пока ход не завершится. Принято для среза.
- [StopEvent смыслового слоя вне хода v2] → v1 по-прежнему отдаёт 424 до bugfix `fix-v1-stop-event-503`.

## Migration Plan

Только v2; ветка v1 в `router.py` не меняется, кроме нового `except` (v1 `TurnBusyError` не бросает).

## Open Questions

—
