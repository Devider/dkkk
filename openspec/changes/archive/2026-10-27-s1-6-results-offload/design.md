# Design — S1.6 Вынос результатов в `/results`

## Context

Мотивация — proposal.md. Что уже есть:

- `middleware/files.py` (S3.1): `CompositeBackend` с маршрутами `/skills/` (`FilesystemBackend`) и `/results/` (`StoreBackend`, пространство `("results", user_id)` из `runtime.context`), `read_file` — единственный файловый инструмент. Встроенный вынос `FilesystemMiddleware` включён с порогом 20 000 токенов (`EVICT_TOKENS`) и `artifacts_root="/results/"`.
- `tools/adapter.py:build_tool` строит конверт (`ToolSuccess` / `ToolFailure`), сериализует `to_json` и возвращает строку или `Command`, если у конверта есть `state_update` (так `resolve_terms` пишет `resolved_terms`).
- `state.py` уже объявляет `step_results`, `TurnContext` обнуляет его (`[]`) в начале хода, но никто туда не пишет. `turn_id` в состоянии есть с S2.2.
- `CalcCache` (S2.5) кэширует только простой успешный `ToolMessage`; `Command` с обновлением состояния не кэшируется.
- `TurnSummary` (S2.6) строит выжимку из `ToolMessage` хода, а не из `step_results`.
- `ToolRuntime` даёт `state`, `tool_call_id`, `context` и `store`; `store` сейчас `None`, потому что `create_agent` вызывается без `store=` (бэкенд получает store напрямую).

## Goals / Non-Goals

**Goals:** гарантированный потолок размера результата доменного инструмента; полный результат дочитывается `read_file` по частям; полные `numbers` хода доступны будущему `AnswerGroundingGuard` без чтения store; кэш расчёта продолжает работать.

**Non-Goals:** замер токенов по слоям (S2.7); чтение `step_results` в `TurnSummary` и guard'ах (S4.1); очистка `/results/`.

## Decisions

### D1. Вынос в адаптере, а не в middleware (ADR-7)

Новый модуль `tools/offload.py` вызывается в `build_tool` после получения конверта: `ToolSuccess` длиннее порога → запись полного конверта и компактный конверт; `ToolFailure` и короткий `ToolSuccess` — как есть. Адаптер знает структуру конверта (`numbers`, `data`), middleware видел бы только строку. Встроенный вынос `deepagents` остаётся страховкой для чужих инструментов без изменений (spike S0.3, K7).

*Альтернатива:* свой `wrap_tool_call` middleware — отвергнута: пришлось бы снова разбирать JSON и знать про конверт вне слоя инструментов.

### D2. Порог и сборка компактного конверта

Порог — длина `to_json(envelope)` в символах, `AGENT_RESULT_MAX_CHARS` (1500, `ge=500`). Компактный конверт: `status`, `result`, `notes`, `hint`, `numbers`, `ref` (без `data`). Если он длиннее порога, `numbers` урезаются с конца (бинарный поиск по длине префикса), в `notes` добавляется «Показаны первые N из M чисел; полный список — в ref»; если не помещается и без `numbers` — `result` обрезается по символам с «…». Последняя проверка длины — инвариант, на нём держится тест «ни один `ToolMessage` не больше порога». Нижняя граница 500 гарантирует, что `ref`, статус и короткая сводка всегда помещаются.

### D3. Порядок `numbers` в `run_scenarios`

`scenarios_envelope` ставит в начало минимум и максимум каждого выхода (с подписью сценария, на котором они достигнуты), затем остальные значения без повторов. Тогда урезание по D2 оставляет экстремумы, о которых обычно спрашивают и которые уже есть в сводке `result`. `find_input_for_target` и `resolve_terms` порядок не меняют: их результаты короткие.

### D4. Запись через тот же `StoreBackend`, store из `ToolRuntime`

Файл пишется методом `StoreBackend.awrite` экземпляра с явным пространством `("results", user_id)` — формат `FileData` тот же, что читает `read_file`, без копирования внутреннего формата `deepagents`. Store берётся из `runtime.store`; `AgentFactory.build` передаёт `store=` в `create_agent` (тот же объект, что получает бэкенд `/results/` и `CalcCache`). Функция пространства одна — `offload.results_namespace(user_id)`; `files.py` использует её же, чтобы запись и чтение не разошлись.

Путь — `/results/<turn_id>/<call_id>.json`; ключ в store — путь без префикса маршрута, как его кладёт `CompositeBackend` (проверяется тестом «записал адаптер → прочитал `read_file`»). `turn_id` — из `runtime.state`, `call_id` — `runtime.tool_call_id`; символы вне `[A-Za-z0-9_-]` заменяются `_`. Содержимое — `json.dumps(envelope, ensure_ascii=False, indent=1)`: одно значение на строку, поэтому `read_file` с `offset` / `limit` листает файл, а строки не упираются в `MAX_LINE_LENGTH` `deepagents`.

*Отклонение от §9 правило 3:* пространство `("results", user_id)`, а не `("results", user_id, thread_id)`. Причина: так решено в ADR-2 и уже работает в S3.1; `read_file` получает пространство до разбора пути и не знает о сессии без лишней фабрики; изоляцию между пользователями (AR-Q7) даёт `user_id`, а случайный `turn_id` (UUID) исключает коллизии между сессиями одного пользователя.

### D5. Сбой записи

Если `awrite` бросает исключение или возвращает ошибку, вызов не падает: журнал через `logger.opt(exception=True).error("...: {}", ...)`, модель получает компактный конверт без `ref` с пометкой в `notes` «полный результат не сохранён». Ход продолжается на сводке и числах — это лучше, чем `internal_error` после успешного расчёта.

### D6. `step_results` — append-reducer и `Command` на каждый вызов

`build_tool` теперь всегда возвращает `Command(update={"messages": [ToolMessage], "step_results": [entry], **state_update})`. Запись: `{call_id, tool, status, code, summary, ref, numbers}` с полным списком `numbers` из исходного конверта (состояние модели не показывается). Для параллельных вызовов в одном шаге нужен reducer: `merge_steps` дописывает в конец; сброс хода — маркер `{RESET: True}` первым элементом (тот же `RESET`, что у `resolved_terms`). `TurnContext` пишет `[{RESET: True}]` вместо `[]`.

Конверты ошибок, созданные middleware (`plan_required`, `limit_exceeded`, `cancelled`, `ToolErrorMiddleware`), записи не дают — до адаптера вызов не дошёл. Это зафиксировано в спеке.

*Альтернатива:* писать `step_results` из `TurnSummary` по `ToolMessage` — отвергнута: полные `numbers` уже потеряны в компактном конверте.

### D7. `CalcCache` хранит результат вместе с записью шага

Сейчас кэш принимает только `ToolMessage`; после D6 расчёт всегда возвращает `Command`. `CalcCache` кэширует `Command`, если в его обновлении только `messages` (один `ToolMessage` с `status: ok`) и `step_results`; в store кладёт `{tool, content, step}`. Попадание возвращает `Command` с новым `ToolMessage` (новый `tool_call_id`) и копией `step` с новым `call_id`; `ref` прежний — файл прошлого хода в пространстве того же пользователя, он не удаляется. Запись старого формата (без `step`) — `InMemoryStore` процесса, после перезапуска пуст, поэтому миграция не нужна; код терпит отсутствие `step`.

### D8. `ref` в выжимке хода

`TurnSummary._result` переносит `ref` из конверта `ToolMessage` в результат выжимки. Источник выжимки не меняется (перевод на `step_results` — вне рамок), блок памяти диалога показывает `ref`, и модель может дочитать прошлый результат без пересчёта.

### D9. Подсказки модели

`READ_FILE_DESCRIPTION` уже упоминает `/results/...`; дополняется: «путь берётся из поля `ref` результата». В системный промпт — одно правило: если нужного числа нет в `numbers`, а у результата есть `ref`, дочитай его через `read_file`, а не пересчитывай. Тексты на русском (AR-Q15).

## Risks / Trade-offs

- [Модель не дочитывает `ref` и отвечает по сводке] → экстремумы идут первыми (D3), сводка содержит диапазоны; поведение на реальной модели — eval S5.1.
- [`Command` вместо строки меняет то, что видят соседние middleware] → `ForcePlanFirst`, `TurnLimits`, `Cancellation`, `ToolErrorMiddleware` сами создают `ToolMessage` и результат адаптера не разбирают; `CalcCache` правится (D7). Тесты стека (`test_plan.py`, `test_limits.py`, `test_cancellation.py`) прогоняются целиком.
- [Файлы `/results/` копятся в памяти процесса] → объём на вызов — десятки КБ, история сессий тоже живёт в памяти; очистка — вместе с внешним store (ADR-8).
- [Рост состояния checkpointer'а из-за полных `numbers`] → сетка ≤ `AGENT_MAX_GRID` (400) точек, `step_results` сбрасывается каждый ход.
- [Ключ store не совпадёт с тем, что ищет `CompositeBackend`] → тест сквозного пути «адаптер пишет → `read_file` читает» через собранный агент на скриптовой модели.

## Migration Plan

Новый параметр со значением по умолчанию, контракт API не меняется. Откат — `AGENT_ENGINE=v1` или откат коммита; данные `/results/` живут только в памяти процесса.
