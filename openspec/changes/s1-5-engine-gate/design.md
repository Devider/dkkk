# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-tools/spec.md`. ADR-6 и §12 п. 1, 2, 4, 5.

Состояние:
- `calc_tools.py` (S1.1–S1.4): проверки → доменная функция `excel_engine` → конверт; домен вызывается адаптером в `asyncio.to_thread`.
- `excel_engine.lo_backend.LibreOfficeSession.start` — `subprocess.Popen([...soffice...])` без новой сессии: `soffice` входит в группу процессов того, кто его запустил; `stop()` — `desktop.terminate()`, затем `terminate()` процесса. Если родителя убить сигналом, `stop()` не выполнится и `soffice` останется сиротой.
- v1: `ProcessPoolExecutor(max_workers=1, mp_context=spawn)`; результат возвращается без `DataFrame` (`_safe_content`). Конверту S1.2 нужны строки `result_df`; `DataFrame` передаётся между процессами через pickle без потерь.
- GigaChat делает один вызов инструмента за ответ (S0.2) — параллельные расчёты в одной сессии возможны только из параллельных ходов (их закроет блокировка хода, S2.8), но гейт обязан держать инвариант сам.

## Goals / Non-Goals

**Goals:**
- Ограниченный пул, очередь по сессии, таймаут без сирот, пересоздание пула.
- Проверяемо без LibreOffice (unit: фейковые функции в пуле) и на настоящем движке (Docker).

**Non-Goals:**
- Отмена по дедлайну хода и разрыву соединения (S2.8) — гейт только даёт для этого метод `shutdown()` и таймаут.
- Повтор `engine_unavailable` (S2.5).

## Decisions

### D1. `EngineGate`

```
gate = EngineGate(workers=APP_CONFIG.agent.calc_workers, timeout=APP_CONFIG.agent.calc_timeout)
result = await gate.run(session_key, func, **kwargs)   # func — функция верхнего уровня (pickle)
await gate.shutdown()
```

- Один экземпляр на процесс сервиса (`default_gate()`, ленивый), в тестах — свой.
- `run`: `async with lock(session_key)` → `loop.run_in_executor(pool, func, **kwargs)` под `asyncio.wait_for(timeout)`.
- Блокировки — словарь `session_key → (Lock, счётчик ожидающих)`, запись удаляется, когда счётчик обнуляется (без утечки на каждую сессию).

*Альтернатива:* процесс на каждый расчёт — отвергнута: холодный старт процесса с импортом домена (~1–3 с) на каждом расчёте и потеря LRU-кэша домена внутри воркера.

### D2. Группы процессов и уничтожение

- `ProcessPoolExecutor(max_workers, mp_context=spawn, initializer=os.setsid)`: каждый воркер — лидер своей сессии и группы; `soffice`, запущенный движком, наследует группу.
- При таймауте или `BrokenProcessPool`: `os.killpg(pid, SIGKILL)` для всех воркеров пула (их PID — из `pool._processes`, приватный атрибут `concurrent.futures`; проверяется тестом), `pool.shutdown(wait=False, cancel_futures=True)`, новый пул.
- Таймаут одного расчёта убивает весь пул: прерываются и расчёты других сессий в этот момент — они получают `engine_unavailable` (повтор — S2.5). Это осознанная цена: `Future.cancel` не останавливает работающий воркер (§12 п. 5), а убить только один воркер `concurrent.futures` не даёт.
- `shutdown()` при остановке сервиса — то же уничтожение групп (нет сирот после рестарта).

### D3. Расчётные инструменты через гейт

`calc_tools`: `call` → `acall`. Порядок: проверки S1.4 (в основном процессе, без движка) → `await gate.run(ctx.thread_id, analyze_excel_model, **kwargs)` → конверт S1.2. В воркер передаётся сама доменная функция `excel_engine` (функция верхнего уровня) — без обёртки, домен не меняется. `TimeoutError` → `ToolError(engine_timeout)`; `BrokenProcessPool` → `ToolError(engine_unavailable)`.

### D4. Контекст и настройки

- `ToolContext` получает `thread_id: str` (§9 `RequestContext.thread_id = "{user_id}:{session_id}"`).
- `config/agent`: `calc_workers` (`AGENT_CALC_WORKERS`, 2), `calc_timeout` (`AGENT_CALC_TIMEOUT`, 120 с). Значение 2 — временное до замера памяти `soffice` в S0.4.

### D5. Проверки

- Unit (без LibreOffice): функции-заглушки в `tests/unit/v2/engine_jobs.py` (спят, пишут время начала/конца, порождают дочерний `sleep` как стенд `soffice`, падают с `os._exit`). Очередь сессии, параллельность разных сессий, лимит пула, таймаут + дочерний процесс убит + пул работает дальше, сломанный пул → `engine_unavailable`, уборка словаря блокировок.
- Docker (LibreOffice): настоящий расчёт с таймаутом меньше длительности → `engine_timeout`, через 2 с `pgrep soffice` пуст; следующий расчёт успешен; задержка event loop во время настоящего расчёта < 1 с.

## Risks / Trade-offs

- [`pool._processes` — приватный API] → один метод `_worker_pids()`, тест на его работу; при изменении Python тест упадёт первым.
- [Пересоздание пула прерывает чужие расчёты] → см. D2; частота таймаутов — метрика (S4.3).
- [`os.setsid` / `killpg` — только POSIX] → сервис работает в Linux-контейнере, разработка — macOS; Windows не поддерживается.
- [Холодный старт воркера] → пул ленивый, воркеры переиспользуются; первый расчёт после старта или таймаута медленнее на 1–3 с.

## Migration Plan

Только `api/v2` и две настройки с дефолтами; сервис v2 ещё не вызывает. Откат — revert.
