# Tasks

## 1. Настройки A/B-проверки

- [x] 1.1 `config/agent/config.py`: поля `preview_check: bool` (`AGENT_PREVIEW_CHECK`, `False`) и `preview_check_percent: float` (`AGENT_PREVIEW_CHECK_PERCENT`, `0.05`, `ge=0, le=1`) с комментарием в стиле соседних полей (design D8). Проверка: `tests/unit/v2/test_preview_check.py::TestSettings` — без переменных `False` / `0.05`; `AGENT_PREVIEW_CHECK_PERCENT=-0.1` и `=1.5` → `ValidationError`; `0` и `1` принимаются; переменные v1 `ENABLE_PREVIEW_MODEL_CHECK` / `PREVIEW_MODEL_CHECK_PERCENT` на `AgentSettings` не влияют
- [x] 1.2 README, таблица «Параметры агента»: строки `AGENT_PREVIEW_CHECK` | `False` (v2: фоновая A/B-проверка на `<GIGACHAT_MODEL>-preview`, только с `AGENT_MODEL=gigachat`, итог — строка `PREVIEW CHECK`; preview-модель есть только во внутреннем контуре) и `AGENT_PREVIEW_CHECK_PERCENT` | `0.05` (доля проверяемых вызовов модели, [0, 1]). Проверка: обе строки в таблице, значения по умолчанию совпадают с `AgentSettings`

## 2. Сравнение и строка журнала

- [x] 2.1 `api/v2/middleware/preview_check.py`: снимок вызова — `PreviewJob` (frozen dataclass: `system_message`, `messages` (кортеж), `tools`, `tool_choice`, `model_settings`, `main: AIMessage`, `trace_id`, `turn_id`, `call`) и функция `snapshot(request, response)` с `call_number` из `context_meter` (design D3). Проверка: тест — снимок из `full_request` из `test_context_meter` содержит те же системное сообщение, сообщения, инструменты и `tool_choice`; изменение исходного списка сообщений после снимка снимок не меняет
- [x] 2.2 Сравнение вызовов без LLM: `compare_calls(main, preview) -> (same_tools, same_args)` по упорядоченным именам и парам (имя, `json.dumps(args, sort_keys=True)`) (design D5). Проверка: тесты — одинаковые `resolve_terms` → `(True, True)`; тот же инструмент с другими аргументами → `(True, False)`; `run_scenarios` против `resolve_terms` → `(False, False)`; вызов против текста → `(False, False)`
- [x] 2.3 Судья: русский промпт, `parse_similarity(text) -> int | None` по «Итоговый процент схожести: N%», ограничение 0–100 (design D5). Проверка: тесты — `"… | Итоговый процент схожести: 85%"` → 85; `"150%"` → 100; текст без числа → `None` и `error="judge_unparsed"`
- [x] 2.4 `preview_record(job, ...) -> str`: JSON из design D6, тексты обрезаются до 1000 / 500 символов, `ensure_ascii=False`. Проверка: тест — поля из D6 на месте; в записи с ошибкой есть `error` и `exception` (имя типа), текста исключения нет; ответ длиной 5000 символов обрезан

## 3. Фоновый обработчик

- [x] 3.1 `PreviewChecker(preview, judge, queue_size=PREVIEW_QUEUE_SIZE)`: ленивое создание очереди и задачи в текущем loop с `context=contextvars.Context()`, `submit(job)` через `put_nowait`, при `QueueFull` — строка `PREVIEW CHECK` с `error="queue_full"` (design D2). Проверка: тесты — после `submit` фейковая preview-модель вызвана один раз; при `queue_size=1` и зависшей preview-модели третья проверка отбрасывается с `queue_full`, `submit` возвращается сразу; в новом event loop создаётся новая задача
- [x] 3.2 Обработка одной проверки: `preview.bind_tools(tools, tool_choice=..., **model_settings)` (или `bind(**model_settings)` без инструментов) → `ainvoke([system, *messages], config={"callbacks": []})`; судья — только когда оба ответа без вызовов инструментов; любое `Exception` → код `preview_failed` / `stop_event` / `judge_failed`, задача продолжает работу (design D3, D5). Проверка: тесты на фейковых моделях — preview получила те же сообщения, инструменты и `tool_choice`; судья не вызван при вызовах инструментов и вызван при тексте; после `StopEventError` из preview следующая проверка выполняется; строка `LLM CALL` для preview-вызова не пишется
- [x] 3.3 `shutdown()`, модульная `default_preview_checker()` и `shutdown_preview_checker()`; `api/__init__.py:lifespan` после `APP_CTX.on_shutdown()` вызывает `await shutdown_preview_checker()` — он останавливает обработчик сервиса, только если тот был создан, поэтому на v1 и при выключенной проверке модели GigaChat не строятся (design D2). Проверка: тест — `shutdown()` при непустой очереди отменяет задачу, после него фейковая preview-модель больше не вызывается; `shutdown()` без созданной задачи — no-op; `lifespan` на v1 стартует и останавливается без ошибок

## 4. Middleware и сборка агента

- [x] 4.1 `PreviewModelCheck(checker, percent, rng=random.random)`: `awrap_model_call` — `response = await handler(request)`, при `rng() < percent` — `checker.submit(snapshot(...))`, возврат `response` без изменений; исключение `handler` пробрасывается без проверки; синхронный `wrap_model_call` — только `handler` (design D7). Проверка: тесты — `percent=0` → `submit` не вызван; `percent=1` → один `submit` на вызов; ответ — тот же объект; при исключении `handler` `submit` не вызван
- [x] 4.2 `api/v2/models.py`: `build_preview_model()` → `GigaChat(**base_params, model=f"{model}-preview")` и `build_judge_model()` (design D4). Проверка: тест — имя модели `<GIGACHAT_MODEL>-preview`, судья — основная модель; сеть не нужна
- [x] 4.3 `api/v2/factory.py`: аргумент `preview_check: PreviewModelCheck | None = None`; `None` — по настройкам: ставится при `preview_check=True` и `model="gigachat"`, при `scripted` — `logger.warning` и не ставится; место — сразу перед `ContextMeter` с комментарием `§8 №6 (S4.4 design D1)` (design D1, D4). Проверка: тесты — по умолчанию в `middleware()` нет `PreviewModelCheck` и список типов совпадает с прежним; переданный `PreviewModelCheck` стоит предпоследним, `ContextMeter` последним; `AGENT_PREVIEW_CHECK=true` + `AGENT_MODEL=scripted` → предупреждение, middleware нет

## 5. Сценарии ADR-10 на скриптовой модели

- [x] 5.1 Ответ не меняется: один ход (план, `resolve_terms`, расчёт, ответ) на `ScriptedChatModel` без проверки и с `PreviewModelCheck(percent=1)` и скриптовой preview-моделью. Проверка: одинаковые ответ, вызовы инструментов и `todos`; проверок ровно столько, сколько вызовов основной модели
- [x] 5.2 Тот же запрос: ход с `AGENT_PLAN_MODE=always` (набор инструментов до плана сужен `ForcePlanFirst`). Проверка: preview-модель первого вызова получила тот же набор инструментов и `tool_choice`, что и основная (`model.calls[0]`), и системный промпт с каталогом skills и промптом плана
- [x] 5.3 Повтор основной модели: первая попытка падает сетевой ошибкой, повтор успешен (`ModelRetryMiddleware`). Проверка: одна проверка на этот вызов
- [x] 5.4 Латентность и сбой: preview-модель `await asyncio.sleep(2)`, затем ошибка 404. Проверка: `ainvoke` хода завершается быстрее 0,5 с с тем же ответом, затем после ожидания очереди — строка `PREVIEW CHECK` с `error="preview_failed"`
- [x] 5.5 Лимиты и трасса: проверка с `percent=1` при `AGENT_MAX_MODEL_CALLS=2`; callback-handler хода из `test_telemetry`. Проверка: частичный ответ — как без проверки; в дереве хода нет запусков preview-модели и судьи
- [x] 5.6 Контракт API: `httpx.AsyncClient` + `ASGITransport`, `AGENT_ENGINE=v2`, скриптовая модель, внедрённая проверка с `percent=1`. Проверка: HTTP 200, тот же набор файлов ZIP и тот же текст ответа, что без проверки

## 6. Документация и трассировка

- [x] 6.1 `docs/Copilot cashflow analyst v2 project spec.md` §8 №6: место — перед `ContextMeter` (внутри повторов), включение `AGENT_PREVIEW_CHECK`, ссылка на S4.4 design D1. Проверка: строка таблицы §8 обновлена, порядок остальных строк не менялся
- [x] 6.2 `openspec/traceability.md`: строка ADR-10 в таблице архитектурных требований — capability `agent-platform`, владелец S4.4, также AR-Q5. Проверка: строка есть, статус заполняется при архивации

## 7. В контуре

- [ ] 7.1 «В контуре»: живой прогон с `AGENT_PREVIEW_CHECK=true`, `AGENT_PREVIEW_CHECK_PERCENT=1` на нескольких запросах eval. Проверка: строки `PREVIEW CHECK` без `preview_failed`, имя preview-модели в контуре верно (иначе — открытый вопрос design о переменной имени), время хода не выросло по сравнению с выключенной проверкой

## 8. Качество

- [x] 8.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` + новые файлы). Проверка: новых замечаний нет, старые попутно не правятся
- [x] 8.2 `pylint src` > 7 и `pytest tests/unit`; новый код типизирован, покрытие `preview_check.py` и изменённых строк `factory.py`, `models.py`, `config/agent/config.py` ≥ 80 % (AR-Q15). Проверка: оценка > 7, тесты зелёные, покрытие ≥ 80 %. Факт: pylint 9.62, покрытие `preview_check.py` 98 %, остальных — 100 %; 883 теста зелёные, 3 падения были и до change (`test_runtime::TestSettings::test_defaults` — `AGENT_ENGINE=v2` в локальном `.env`; два `test_stop_event::TestNoHardcodedRetry` — незавершённый `fix-context-undefined-imports`)
