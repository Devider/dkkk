# Design

## Context

Мотивация — в proposal.md, требования — в дельтах `specs/`. Здесь — только то, что определяет реализацию.

- Стек `AgentFactory.middleware()` сейчас: `TurnContext` → `LanguageGuard` → `StopEventGuard` → `SkillsMiddleware` → `FilesystemMiddleware` → `PlanTodoList` → `ForcePlanFirst` → *extra* → `PlanCompletionGuard` → `TurnSummary`. `wrap_*` вложены (первый — внешний), `after_model` идут снизу вверх; если нижний `after_model` делает `jump_to`, верхние в этом проходе не выполняются.
- `TurnContext.abefore_agent` уже сбрасывает `guard_counters = {}` в начале каждого хода; `PlanCompletionGuard` хранит там `plan_returns`.
- Адаптер (`tools/adapter.py`) ловит `ToolError` и возвращает конверт строкой; прочие исключения летят наружу. `EngineGate` поднимает `ToolError(engine_unavailable)` при `BrokenProcessPool` и уже пересоздаёт пул; сбой LibreOffice внутри воркера приходит как статус v1 `ERROR`, и `classify_v1` возвращает `ToolFailure(engine_unavailable)` без исключения. Значит, сегодня ни одно `engine_unavailable` не доходит до повторяющего middleware.
- `create_agent` (langchain 1.4.6) не выполняет вызовы инструментов, на которые в истории уже есть `ToolMessage` (`factory.py`: `pending_tool_calls`). На этом построена блокировка вызовов в `after_model` — так же, как во встроенном `ToolCallLimitMiddleware`.
- Встроенные `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`: при `exit_behavior="end"` ход кончается фиксированным английским `AIMessage` без вызова модели; при `"continue"` модель получает английский `ToolMessage` «Tool call limit exceeded…»; тексты зашиты в модуле.
- `ToolErrorMiddleware(on_error, aon_error, tools)`: `None` из обработчика — исключение летит дальше. `ToolRetryMiddleware` и `ModelRetryMiddleware` принимают `retry_on` (кортеж или предикат), `on_failure="error"` перевыбрасывает исключение.

## Goals / Non-Goals

**Goals:**
- Один middleware отвечает за все три лимита и за финальный вызов без инструментов, чтобы «ровно один вызов» не зависел от взаимодействия нескольких классов.
- Повторы — встроенными middleware langchain, своими остаются только предикаты.
- `engine_unavailable` видно повтору, но модель по-прежнему получает конверт.

**Non-Goals:**
- Счёт отдельных HTTP-запросов к GigaChat: лимит модели считает шаги цикла (узел `model`), а не повторы `ModelRetryMiddleware` и не второй запрос `ForcePlanFirst` в режиме `always`.
- Ограничение размера `CalcCache` и TTL (см. Risks).

## Decisions

### D1. Свой `TurnLimits` вместо `ModelCallLimitMiddleware` и `ToolCallLimitMiddleware` — отклонение от §8 №12–13

Причина: встроенные не дают «один вызов модели без инструментов для частичного ответа» (§8 правило 8) — `end` завершает ход без модели, `continue` оставляет модели инструменты; их тексты английские (AR-Q15, FR-14), а заменить их можно только подменой функций модуля. Три лимита потребовали бы трёх экземпляров, каждый со своим флагом остановки.

`TurnLimits(max_model_calls, max_tool_calls, max_calcs, engine_tools)` — на месте №12–13 стека:

- **Счётчики** — в `guard_counters` (`model_calls`, `tool_calls`, `calc_calls`, флаг `limit_hit`). Их уже обнуляет `TurnContext` в начале хода, и флаг виден `PlanCompletionGuard` / `ForcePlanFirst`. Альтернатива — `UntrackedValue`-поля, как `run_*_count` у langchain: тот же эффект «на ход», но второй механизм сброса и флаг, невидимый другим middleware, — отклонено.
- **`before_model`** (всегда выполняется перед узлом модели): `model_calls += 1`; если `model_calls >= max_model_calls` — ставит `limit_hit`. Поэтому вызов №`max_model_calls` уже финальный и лимит модели не превышается (сценарий «Последний вызов в пределах лимита модели»).
- **`after_model`**: перебирает вызовы инструментов последнего `AIMessage` по порядку; вызов, который превысил бы `max_tool_calls` или (для инструментов `uses_engine`) `max_calcs`, получает `ToolMessage(status="error")` с конвертом `limit_exceeded`, ставится `limit_hit`. Разрешённые увеличивают счётчики. Вызовы из кэша (`CalcCache`) считаются — счёт идёт до исполнения, когда исход кэша неизвестен; это консервативно и записано в спеке.
- **`wrap_model_call` / `awrap_model_call`**: при `limit_hit` запрос идёт с `tools=[]`, `tool_choice=None` и добавленным последним сообщением пользователя `LIMIT_PROMPT` (только в запрос, не в историю): «Лимит хода исчерпан. Не вызывай инструменты. Дай частичный ответ по уже полученным результатам с цифрами и перечисли, что не выполнено». Если ответ всё же содержит `tool_calls`, они вырезаются (тот же `id` сообщения); пустой текст заменяется фиксированным `LIMIT_FALLBACK`. Ответ без вызовов → `create_agent` идёт к концу.
- Тексты — в `prompts/middleware.py`, на русском.

### D2. `PlanCompletionGuard` и `ForcePlanFirst` уважают `limit_hit`

`PlanCompletionGuard.after_model`: при `limit_hit` и открытых шагах — без напоминания сразу ветка «исчерпаны возвраты» с причиной `LIMIT_REASON = "Исчерпан лимит хода"`. `ForcePlanFirst.awrap_model_call`: при `limit_hit` — `handler(request)` без сужения и без повторного запроса с `tool_choice=write_todos`, иначе в режиме `always` финальный вызов превратился бы в два. Альтернатива — переставить `TurnLimits` внешним к `ForcePlanFirst`: нарушает порядок §8 и всё равно требует знания о флаге в guard.

### D3. Повтор модели — `ModelRetryMiddleware` с предикатом (§8 №14)

`ModelRetryMiddleware(max_retries=AGENT_LLM_RETRY_ATTEMPTS, retry_on=is_transient_llm_error, on_failure="error", initial_delay=AGENT_LLM_RETRY_WAIT, backoff_factor=2.0, jitter=False)`. Предикат: `gigachat.exceptions.ServerError` (5xx), `RateLimitError` (429, клиент сам не повторяет — spike S0.3), `httpx.TransportError` (включая таймауты), `ConnectionError`, `TimeoutError`. Всё прочее — нет; `ForbiddenError` и `StopEventError` явно исключены, чтобы смена иерархии клиента не включила их. `jitter=False` — паузы детерминированы (2, 4, 8 с) и проверяемы. Стоит внутри `StopEventGuard` (№4), поэтому StopEvent доходит до guard с первого вызова. Переменные — существующие `AGENT_LLM_RETRY_ATTEMPTS` / `AGENT_LLM_RETRY_WAIT` (их уже читает `resolve_terms`, S1.3): одни настройки повторов на сервис.

### D4. Ошибки инструментов — `ToolErrorMiddleware` (§8 №16), повтор движка — `ToolRetryMiddleware` внутри (№17)

- `ToolErrorMiddleware(on_error=tool_error_content, aon_error=…)`: StopEvent (`StopEventError` или `ForbiddenError` с маркером — общий чистый предикат `is_stop_event`, выделенный из `stop_event()` без логирования) → `None`, исключение летит к `StopEventGuard` → 503. Остальное → `to_json(failure_from_exception(exc))`: `ToolError` — свой код, неизвестное — `internal_error` с traceback в журнале (уже реализовано в `envelope.py`, S1.2).
- `ToolRetryMiddleware(max_retries=1, tools=catalog.engine_tool_names(), retry_on=is_transient_tool_error, on_failure="error", initial_delay=ENGINE_RETRY_DELAY (1 с), jitter=False)`; предикат — `ToolError` с кодом из `TRANSIENT_CODES = {engine_unavailable}` (новая константа в `envelope.py`).
- **Адаптер**: если конверт (пойманный `ToolError` или возвращённый `ToolFailure`) имеет код из `TRANSIENT_CODES`, он поднимается как `ToolError` того же кода, текста и подсказки. Альтернатива — адаптер перестаёт ловить все `ToolError` и конверты делает только `ToolErrorMiddleware`: меняет поведение инструмента вне агента и ломает тесты S1.x без пользы — отклонено.
- `engine_timeout` не временный: повтор той же сетки снова упрётся в таймаут (§10 — исправляет модель).

### D5. `CalcCache` (§8 №15)

- Оборачивает `wrap_tool_call` только для `catalog.engine_tool_names()`; стоит снаружи `ToolErrorMiddleware` и `ToolRetryMiddleware`, поэтому попадание не трогает ни движок, ни повторы, а внутри `ForcePlanFirst`, поэтому расчёт без плана отклоняется раньше кэша.
- Хранилище — store сервиса (как у `TurnContext`: аргумент конструктора или `APP_CTX.agent_memory.store`), namespace `("calc_cache", thread_id)` — сессия входит в namespace, пользователи и сессии не пересекаются (AR-Q7). Ключ — `sha256` от `json.dumps({"file": file_hash, "tool": name, "args": norm}, sort_keys=True, ensure_ascii=False)`, где `norm = tool.args_schema.model_validate(args).model_dump(mode="json")` — значения по умолчанию подставлены, `1` и `1.0` совпадают. `file_hash` — из `state.model_ref`. Аргументы, не прошедшие проверку схемы, кэш не трогает.
- Сохраняется только `ToolMessage` со `status != "error"` и конвертом `status: "ok"`; `Command` (результат с `state_update`) не кэшируется — у расчётных инструментов его нет. Значение — текст конверта; при попадании возвращается `ToolMessage(content, tool_call_id=<текущий>, name=…)` и пишется `CalcCache hit {tool} [{thread}]`.
- Заменяет fingerprint-дедупликацию v1 только для v2; LRU-кэш `tools.py` внутри воркера остаётся (§12 «Кэши» п. 6).

### D6. Порядок стека

`TurnContext`, `LanguageGuard`, `StopEventGuard`, `SkillsMiddleware`, `FilesystemMiddleware`, `PlanTodoList`, [`ForcePlanFirst`], `TurnLimits`, `ModelRetryMiddleware`, `CalcCache`, `ToolErrorMiddleware`, `ToolRetryMiddleware`, *extra*, [`PlanCompletionGuard`], `TurnSummary`. `TurnLimits` ниже `PlanCompletionGuard` по `after_model`-порядку не нужен: guard проверяет ответ без вызовов, а `TurnLimits.after_model` работает с вызовами — их проходы не пересекаются.

### D7. Настройки

В `AgentSettings`: `max_model_calls` (40), `max_tool_calls` (30), `max_calcs` (10) с `ge=1`; `llm_retry_attempts` получает `ge=0`, `llm_retry_wait` — `ge=0`. v1-поля `max_iterations`, `max_messages`, `duplicate_tool_call_threshold` не трогаются. README — три новые строки, у повторов LLM уточнено, что они покрывают модель агента и 429 смыслового слоя (v1 их не читает).

## Risks / Trade-offs

- [`ToolNode` сам перехватит исключение инструмента раньше `ToolErrorMiddleware`] → в 1.4.6 `ToolNode` по умолчанию обрабатывает только ошибки проверки аргументов; первым тестом группы 3 проверить, что `ZeroDivisionError` из инструмента доходит до обработчика.
- [`CalcCache` растёт без предела в `InMemoryStore` (до ~400 строк сценариев на запись)] → ключ включает хэш файла и сессию, пространство одно на сессию; TTL store и очистка при закрытии сессии — вместе с внешним checkpointer/store (ADR-8), не в этой story.
- [Кэш отдаёт результат после изменения кода расчёта при той же книге] → кэш живёт в памяти процесса и сбрасывается рестартом/релизом; для Pangolin-store — то же, что выше.
- [Модель в финальном вызове не перечисляет невыполненное] → раздел «Не выполнено» добавляет `PlanCompletionGuard` из плана детерминированно; без плана остаётся текст модели.
- [Счёт кэш-попаданий в лимит расчётов] → консервативно: при 10 одинаковых вызовах модель всё равно остановится, что и нужно при зацикливании.
- [Повтор LLM удлиняет ход до +14 с при 3 повторах] → в пределах `AGENT_TURN_TIMEOUT` 300 с (S2.8).

## Migration Plan

Только код v2 за `AGENT_ENGINE=v2`; данных и миграций нет. Откат — `AGENT_ENGINE=v1` и перезапуск либо откат коммита. Новые переменные имеют значения по умолчанию.
