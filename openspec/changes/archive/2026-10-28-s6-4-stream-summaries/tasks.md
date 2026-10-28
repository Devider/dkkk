# Tasks

## 1. Сводка шага и итог хода (`api/v2/stream/labels.py`)

- [x] 1.1 `labels.step_summary(envelope) -> str | None` по design D4: только `result` успешного конверта, `scrub`, `SUMMARY_MAX = 300`, обрезка по границе предложения → пробела, без разреза чисел (`1 234 567`, `1 234,5`, неразрывный пробел), хвостовая пунктуация снимается, `…` в конце. Проверка: `tests/unit/v2/test_stream_translator.py` (класс `TestStepSummary`, рядом с тестами подписей) — короткий `result` без изменений; длинный ≤ 300 и кончается `…`; каждое число сводки (регулярка чисел с разделителями) — подстрока `result`; число на границе 300 не режется; ID / путь `/results/` / имя инструмента удалены; конверт без `result`, ошибка, не-конверт → `None`
- [x] 1.2 `labels.turn_summary(steps) -> Draft | None` по design D5: счёт `completed` / `skipped` / `blocked`, текст «Выполнено N из M шагов[, K пропущен/о][, L ждёт/ждут ответа пользователя]» с русскими формами числа; пустой план → `None`. Проверка: `test_stream_translator.py::TestTurnSummaryText` — тесты на 1, 2, 5, 11, 21 шаг (формы числа), план 3/1/1 → поля и текст по спеке, без причин шагов, текст проходит гигиенические регулярки FR-24

## 2. `StreamTranslator` и `TurnStream`

- [x] 2.1 `translator.py`: `_finished(..., summary=None)`; `_tool_message` при исходе `ok` кладёт `step_summary(envelope)`, для `retry` / `failed` и части `custom` — `None`. Проверка: `test_stream_translator.py` — `tool.finished` с `ok` несёт сводку из `result` своего `ToolMessage`; `retry` / `failed` / чтение `/skills/` → `summary = null`; вынесенный в `/results/` результат (конверт с `result`, `numbers`, `ref`) даёт сводку
- [x] 2.2 `translator.py`: `finish() -> list[Draft]` — `[turn_summary(self._steps)?, answer]`; `turn.py:TurnStream._run` кладёт все черновики по порядку, ветки `error` итог не дают. Проверка: `test_stream_translator.py` — с планом `summary` перед `answer`, без плана только `answer`, отказ без `summary`; `test_stream_turn.py::TestTurnStream` — StopEvent / сбой → `error` без `summary`; существующие тесты, ожидающие `finish()` как черновик, обновлены

## 3. Каталог фаз без `preparing`

- [x] 3.1 `events.py`: `Phase` без `preparing`; `labels.py:PHASE_TEXT` без ключа `preparing`; `openapi_copilot.yaml`: enum `phase` — `planning`, `executing`, `checking`, `answering` (design D7). Проверка: тест в `test_stream_translator.py` — `PhaseEvent` с `preparing` падает с `ValidationError`, ключи `PHASE_TEXT` совпадают с `get_args(Phase)`; часть `custom` с `phase: preparing` событий не даёт; `python -c "import yaml; yaml.safe_load(open('openapi_copilot.yaml'))"` без ошибок и enum совпадает с `Phase`

## 4. `ProgressReporter` (`api/v2/middleware/progress.py`)

- [x] 4.1 Класс по design D1, `before_model` / `abefore_model` по правилу фаз D2 (`limit_hit` → `answering`; открытый шаг → `executing`; закрытый план и `tool_calls > 0` → `answering`; `tool_calls = 0` → `planning`; иначе `executing`), пишет `{"progress": "phase", "phase": …}` в `runtime.stream_writer`, возвращает `None`. Проверка: `tests/unit/v2/test_progress_reporter.py` — по случаю на каждую строку таблицы D2 с записывающим `stream_writer`, включая закрытый план прошлого хода с `blocked` при `tool_calls = 0` → `planning`; хук не возвращает обновлений
- [x] 4.2 `wrap_tool_call` / `awrap_tool_call` по design D3: только `engine_tools`; при исключении `is_transient_tool_error` и попытке ≤ `max_retries` пишет `{"progress": "retry", "tool_call_id", "code"}`; исключение пробрасывается; счётчик по `(thread_id, tool_call_id)` очищается при успехе, неповторяемой и последней ошибке. Проверка: `test_progress_reporter.py` — внутри настоящего `engine_retry(...)` с обработчиком, падающим `ToolError(engine_unavailable)` раз / дважды: одна часть `retry` в обоих случаях; `ambiguous_term` и не-расчётный инструмент — частей нет; после каждого сценария словарь попыток пуст; одинаковые `tool_call_id` в двух сессиях не путаются
- [x] 4.3 `factory.py`: `ProgressReporter(engine_tools)` сразу после `engine_retry(engine_tools)`, до `extra_middleware`, с комментарием §8 №17а. Проверка: в `tests/unit/v2/test_limits.py` (там уже собирается `AgentFactory(...).middleware()`) или новом тесте `test_progress_reporter.py` проверено — `ProgressReporter` стоит непосредственно после `ToolRetryMiddleware`; непотоковый U2 через `answer()` даёт тот же текст, что до изменения, и не падает без потребителя `stream_writer`

## 5. Сквозные сценарии потока (`tests/unit/v2/test_stream_turn.py`)

- [x] 5.1 `TestStreamScenarios::test_u2` обновлён на последовательность из спеки: `turn.started` → `phase(planning)` → `plan` → `phase(executing)` → пары `tool.*` с `plan` между ними → `phase(answering)` → `phase(checking)` → `summary` → `answer`; у каждого `tool.finished` — `summary` не `null`; `summary.completed = 3`. Проверка: тест зелёный
- [x] 5.2 Тест «числа сводок из конверта»: U2 и U5 (шаг `blocked`) — каждое число в `summary` каждого `tool.finished` найдено в `result` или `numbers` конверта того же вызова (сопоставление по `tool_call_id` через `ToolMessage` из истории чекпоинта / записи стенда). Проверка: тест зелёный
- [x] 5.3 Повтор движка: сценарий «`engine_unavailable` на первой попытке, затем успех» — `tool.started`, `tool.finished(retry, текст о повторе)`, `tool.started`, `tool.finished(ok)` с одним `call`; существующий сценарий «дважды» — ровно одна пара `retry`, затем `failed`. Проверка: тесты зелёные
- [x] 5.4 Фазы: ранний ответ при открытом шаге → после `checking` снова `executing`; лимит хода → `answering` и `summary` → `answer(partial)`; отказ (нет модели, язык) → ни одного `phase` и `summary`. Проверка: тесты зелёные
- [x] 5.5 Число вызовов LLM: U2 через `Stack.both()` со счётчиком вызовов скриптовой модели — равен в `answer()` и `stream()`. Проверка: тест зелёный
- [x] 5.6 Гигиена FR-24 (`assert_clean` / сценарий со всеми потоковыми сценариями) распространяется на `summary` в `tool.finished` и событие `summary`; добавлен случай `result` с ID и путём `/results/`. Проверка: тест зелёный

## 6. Документация

- [x] 6.1 `docs/Copilot cashflow analyst v2 project spec.md` по design D7–D8: FR-22 (фазы без «подготовки»), §13 каталог `phase` без `preparing`, §8 №17а (место сразу после `engine_retry`, ставится всегда, правило фаз), §13 «Подписи и сводки» (правило сводки, шаблон итога), пример `summary` в каталоге — по шаблону. Проверка: `git diff` по файлу содержит только эти места
- [x] 6.2 `openspec/traceability.md`: строка FR-23 — capability `agent-stream`, владелец S6.4; в строке FR-22 S6.4 указана как участник (статус обоих — при архивации). Проверка: `openspec validate s6-4-stream-summaries --strict` проходит

## 7. Проверки

- [x] 7.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` плюс новые): новых замечаний ноль, старые замечания в затронутых файлах не блокируют и попутно не правятся. Проверка: `ruff check <файлы>` без новых замечаний относительно `HEAD`
- [x] 7.2 Новый код типизирован, покрытие нового кода ≥ 80 % (AR-Q15). Проверка: `pytest tests/unit/v2/test_progress_reporter.py tests/unit/v2/test_stream_translator.py tests/unit/v2/test_stream_turn.py --cov=src/aigw_service/api/v2 --cov-report=term-missing` — `progress.py` и новые строки `labels.py` / `translator.py` / `turn.py` покрыты ≥ 80 %
- [x] 7.3 `pylint src` > 7 и `pytest tests/unit` зелёные (падения, существующие и на `HEAD`, перечислить в итоге задачи) — Итог: pylint 9.65; `tests/unit` — 1085 passed, 3 падения есть и на `HEAD` (`test_runtime::TestSettings::test_defaults` — `AGENT_ENGINE=v2` в локальном `.env`; `test_stop_event.py::TestNoHardcodedRetry` ×2 — change `fix-context-undefined-imports`). Обновлены ожидания `test_plan.py` (порядок стека) и `test_stream_api.py` (фазы и `summary` в потоке)
