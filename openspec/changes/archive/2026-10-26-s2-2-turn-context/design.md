# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-loop/spec.md`. §8 правило 1, §9 (слои контекста, правила 1–2), §13 (`/upload` при v2), ADR-11.

Состояние:
- `/upload` пишет файл в `/tmp/user_file_{user_id}.{ext}` и `{"filename": ...}` в `APP_CTX.agent_memory.store` по `("memories", user_id)` / ключ `user_id` (асинхронный `aput`).
- `RequestContext` (S2.1) — изменяемый: `file_path=""`, `model_index=None`; без индекса адаптер отвечает `ToolContextError`.
- `build_model_index(path)` (S1.4) — один проход openpyxl, ~0,5 с на Methanex.
- `ToolsState.resolved_terms` — редьюсер «объединить словари»: очистить его обычным обновлением нельзя.
- `before_agent` с `@hook_config(can_jump_to=["end"])` может вернуть `{"messages": [...], "jump_to": "end"}` — ход завершается без вызова модели.
- GigaChat: системное сообщение — одно, в начале (S0.2) — карточку нельзя добавлять вторым системным сообщением.

## Goals / Non-Goals

**Goals:**
- Ход знает книгу и индекс без открытия книги в обычном случае; без книги — ответ без LLM.
- Детерминированный языковой фильтр без новых зависимостей.

**Non-Goals:**
- Сохранение состояния между ходами (checkpointer, S2.6): логика сброса пишется сейчас и становится заметной с checkpointer.

## Decisions

### D1. Реестр моделей

`ModelRegistry(store)`: пространство `("model_registry",)`, ключ — SHA-256 файла; значение — `{"index": {inputs, outputs, input_values, output_years}, "card": {...}}` (JSON-совместимо, годы — строками). `register(path) -> file_hash`: хэш и построение индекса — в `asyncio.to_thread`; если запись по хэшу есть — не перестраивается. `get(file_hash) -> (ModelIndex, card) | None`. Реестр общий для пользователей (одна и та же книга — один индекс), изоляцию даёт путь к файлу пользователя в `RequestContext` (AR-Q7).

*Альтернатива:* реестр в памяти процесса — отвергнута: store уже переключается на Pangolin конфигурацией (`STORE_TO_USE`), реестр получает это бесплатно.

### D2. `/upload` при v2

После `store.aput(... {"filename"})`: если `APP_CONFIG.agent.engine == "v2"` — `file_hash = await registry.register(path)` и `store.aput(... {"filename", "file_hash"})`. Ошибка регистрации не валит загрузку: пишется в журнал, ход построит индекс сам (D3). При `v1` код загрузки не меняется.

### D3. `TurnContext`

`before_agent` (`can_jump_to=["end"]`):
1. Запись `("memories", user_id)` из store; нет записи или файла на диске → `{"messages": [AIMessage(NO_MODEL_ANSWER)], "jump_to": "end"}`.
2. `file_hash` из записи; нет — посчитать (to_thread) и дописать в запись.
3. `registry.get(hash)`; нет — `registry.register(path)` (единственный путь, где книга открывается).
4. `context.file_path`, `context.model_index` ← путь и индекс; `context.card` — карточка (новое поле `RequestContext`, не видно инструментам).
5. Обновление состояния: `turn_id` (uuid4), `step_results=[]`, `guard_counters={}`; `todos=[]`, если в нём нет `blocked`; `model_ref={file_name, file_hash, replaced_in_turn}`. Если прошлый `model_ref.file_hash` есть и отличается: `resolved_terms` ← `RESET` (D4), `todos=[]`, `replaced_in_turn = turn_id`.

`wrap_model_call`: к системному сообщению запроса добавляется блок «## Загруженная модель» из карточки; если `model_ref.replaced_in_turn == turn_id` — строка «Модель заменена в этом ходе: прежние результаты и цифры относятся к другой книге». Состояние и сообщения не меняются — карточка живёт только в запросе (§9: слой «Карточка модели» — динамический промпт).

### D4. Сброс `resolved_terms`

`merge_terms(current, update)`: если в `update` есть ключ `"__reset__"` — результат = `update` без этого ключа; иначе объединение, как сейчас. `TurnContext` пишет `{"__reset__": True}`.

### D5. Карточка модели

`build_card(file_name, index)`: `{"file_name", "inputs": N, "outputs": M, "years": "2018–2050", "key_metrics": [...]}`; ключевые метрики — выходы со значениями, чьи имена содержат `DSCR`, `LLCR`, `ICR`, `Debt/EBITDA`, `Чистый долг`, `EBITDA`, `Выручка`, `Чистая прибыль`, `FCF` (до 12, без дублей). Текст для промпта — 4–6 строк, имена метрик — канонические (это данные, не ID).

### D6. `LanguageGuard` и определение языка

`detect_language(text) -> "ru" | "en" | "other" | "unknown"` по буквам сообщения:
- символы вне латиницы и кириллицы (CJK, арабица, греческий, …) составляют > 30 % букв → `other`;
- кириллица с буквами `і ї є ґ ў ә ғ қ ң ө ұ ү һ` → `other`; иначе кириллица ≥ 30 % букв → `ru`;
- латиница: диакритика `äöüßàâçéèêëîïôûùÿñœæ` или ≥ 2 стоп-слов немецкого / французского / испанского / итальянского / португальского при < 2 английских → `other`; ≥ 1 английское стоп-слово или ≥ 3 слов → `en`;
- меньше 3 букв или слов → `unknown`.
`before_agent`: последнее сообщение пользователя → `other` → `{"messages": [AIMessage(LANGUAGE_REFUSAL)], "jump_to": "end"}`; `unknown` и остальные — пропуск. Отказ — только при уверенном распознавании другого языка (сценарий «Короткий запрос»).

### D7. Порядок в стеке

`AgentFactory.middleware()`: `TurnContext`, `LanguageGuard`, `TodoListMiddleware`, остальные. `before_agent` идут сверху вниз: без книги ход заканчивается раньше языковой проверки — ответ «загрузите модель» полезнее отказа по языку.

## Risks / Trade-offs

- [Эвристика языка ошибается на смешанных запросах] → кириллица ≥ 30 % сразу `ru`; английские финансовые термины не мешают; отказ только при уверенном `other`. Набор тестовых фраз; доля ложных отказов — метрика eval S5.1.
- [Хэш файла считается в ходе, если загрузка была до v2] → один раз, дальше хранится в записи.
- [Реестр растёт без очистки] → решение §16: TTL не вводим; одна запись на уникальную книгу.
- [Без checkpointer (до S2.6) сброс по смене файла не наблюдаем в сервисе] → проверяется unit-тестом с переданным прошлым состоянием.

## Migration Plan

Только `api/v2` и ветка `AGENT_ENGINE=v2` в `/upload`. Откат — revert или `AGENT_ENGINE=v1`.
