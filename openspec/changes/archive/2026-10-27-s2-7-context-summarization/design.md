# Design

## Context

Мотивация — proposal.md, раздел Why. Ниже то, что в коде и в `deepagents==0.7.29` определяет подход.

- Стек собирается в `factory.py:AgentFactory.middleware()`. Места №5 (`Telemetry`) и №7 (`SummarizationMiddleware`) пока пустые. `AGENT_MAX_PROMPT_TOKENS` (30 000) есть в `config/agent`, но код его не читает.
- `deepagents.middleware.summarization.SummarizationMiddleware` в 0.7.29 работает не в `before_model`, как сказано в §8 №7, а в `wrap_model_call`. Сообщения из состояния он **не удаляет**. Он пишет приватное поле `_summarization_event` = `{cutoff_index, summary_message, file_path}` и при каждом вызове собирает запрос как `[summary_message, *messages[cutoff_index:]]`. Полная сжатая часть пишется через backend в `<artifacts_root>/conversation_history/<session>.md`. Сводку строит прямой вызов `model.ainvoke` по `summary_prompt` с плейсхолдером `{messages}`, а не через цепочку `handler`. Текст сообщения-сводки (`_build_new_messages_with_path`) зашит по-английски.
- Отсюда следствия:
  - `turn_message_id` и `TurnSummary._turn_messages` по-прежнему видят полную историю (S2.6 не ломается);
  - `AnswerGroundingGuard` опирается на `step_results`, а не на сообщения (S4.1);
  - `TurnContext._pinned_in` ищет закреплённый skill в `state["messages"]` и находит его даже тогда, когда модель его уже не видит. Без доработки skill после сжатия не вернётся;
  - `SkillsMiddleware` (№8) открывает инструменты skill по `request.messages`. Он стоит внутри сжатия, поэтому сжатый `read_file` скрывает инструменты сам (§11 «Активация» п. 3).
- Блоки системного промпта дописывают пять middleware: `SkillsMiddleware` (каталог), `FilesystemMiddleware` (правила `read_file`), `PlanTodoList` (правила плана), `TurnContext` (карточка, `append_block`), `TurnSummary` (память диалога, `append_block`). `TurnSummary` стоит последним в списке, поэтому его `wrap_model_call` самый внутренний.
- `StopEventGuard` (№4) оборачивает всё, что внутри него, включая вызов сводки. `TurnLimits` (№12–13) и `ModelRetryMiddleware` (№14) стоят глубже и вызов сводки не видят.

## Goals / Non-Goals

**Goals:**
- Сжатие на месте №7 по бюджету из настроек: русская сводка, история в `/results/` пользователя.
- Закреплённый skill возвращается в том же ходе, если сжатие его вытеснило.
- Одна строка `CONTEXT LAYERS` на вызов модели: слои запроса, который модель получает на самом деле.

**Non-Goals:**
- Удаление сообщений из checkpointer (`RemoveMessage`).
- Точный токенизатор GigaChat.
- Свой алгоритм выбора точки среза.
- Время, токены ответа и Langfuse из `Telemetry` (S4.3).

## Decisions

### D1. Встроенный `SummarizationMiddleware` deepagents, бюджет в токенах (ADR-2)

Middleware собирается в новом `middleware/summarization.py:summarization_middleware(model, backend, settings)`:
- `trigger=("tokens", int(max_prompt_tokens * summary_trigger))`;
- `keep=("tokens", int(max_prompt_tokens * summary_keep))`;
- `trim_tokens_to_summarize=max_prompt_tokens`: в сводку идёт вся сжимаемая часть, а не последние 4000 токенов по умолчанию;
- `summary_prompt` — русский, из `prompts/middleware.py`.

Backend тот же, что у `read_file`: `file_backend(...)` с `artifacts_root="/results/"`. Поэтому история ложится в `StoreBackend` пространства `("results", user_id)` и читается `read_file`, а чужой пользователь её не видит (AR-Q7, без нового кода). Место — сразу после `StopEventGuard`, перед `skills_middleware`, как в §8 №7.

- **Отклонение от §8 №7:** хук `wrap_model_call`, а не `before_model`, и `messages` в состоянии не сжимаются, сжимается только запрос. Причина: так устроен deepagents 0.7. Состояние растёт, как и раньше; его размер — вопрос внешнего checkpointer (ADR-8). §9 правило 5 по смыслу выполняется: план, карточка, термины и выжимки живут вне `messages`.
- Альтернатива — `langchain.agents.middleware.SummarizationMiddleware`: он удаляет сообщения через `RemoveMessage`. Отклонена. Удаление сломало бы `turn_message_id` и выжимку хода (S2.6), потеряло бы полную историю и расходится с ADR-2.
- `fraction`-триггер не годится: у `GigaChat` нет `profile.max_input_tokens`, а бюджет задаём мы (AR-Q6).

### D2. Русский текст сообщения-сводки

Подкласс `CopilotSummarization(SummarizationMiddleware)` переопределяет `_build_new_messages_with_path`. Текст: «Начало диалога сжато. Полная история — `read_file <путь>`. Сводка: …». `lc_source="summarization"` сохраняется, на нём держится повторное сжатие. Метод приватный, поэтому его сигнатуру проверяет `tests/unit/test_dependency_pins.py`: при смене версии deepagents тест упадёт. Альтернатива — английский текст в русском промпте. Она нарушает AR-Q15 (промпты на русском) и §16 «смешение языков».

### D3. Подсчёт токенов: `count_tokens_approximately` с `chars_per_token=3.0`

Один счётчик `context_tokens` (частичное применение) у сжатия и у `ContextMeter`, поэтому решение о сжатии и строка журнала согласованы. Значение 3.0 вместо 4.0 по умолчанию выбрано потому, что русский текст в токенизаторах BPE дробится мельче английского. Это запас в сторону раннего сжатия. Константа `CHARS_PER_TOKEN` калибруется по `usage_metadata` GigaChat в S5.1, во внутреннем контуре.
- Альтернатива — `tokens_count` API GigaChat: сетевой вызов перед каждым шагом нарушает AR-Q2 и добавляет задержку.

### D4. Повторное закрепление skill по видимой истории

`TurnContext` получает `abefore_model` (и `before_model`). Если в видимой модели части истории нет сообщения `pinned_skill` с нужным именем, он пишет `pinned_skills=[...]`, и `SkillsMiddleware` (№8, его `before_model` идёт позже — порядок сверху вниз) добавляет текст skill перед вызовом модели. Видимая часть — `messages[cutoff_index:]` из `state["_summarization_event"]`. Поле приватное, но внутренним узлам графа доступно, как `jump_to`. Ключ берётся константой `SUMMARIZATION_EVENT_KEY` из deepagents. `_pinned_in` в `abefore_agent` переходит на ту же видимую часть, чтобы проверка в начале хода и перед вызовом модели совпадала.

Сообщение-сводка без новых сообщений после неё не создаёт повторного закрепления, пока сжатие не случилось, поэтому лишних копий нет.

- Альтернатива — закреплять skill только в начале следующего хода (как было задумано в S3.2). Отклонена: сжатие посреди хода оставило бы финальный ответ без правил `economic-answer` (FR-12).
- Альтернатива — исключить закреплённое сообщение из сжатия. API deepagents этого не даёт, понадобился бы свой алгоритм среза.

### D5. `ContextMeter` — самый внутренний `wrap_model_call`

`ContextMeter` в новом `middleware/context_meter.py` стоит последним в списке, после `TurnSummary`. Поэтому он видит запрос со всеми блоками промпта и после сжатия, то есть ровно то, что уйдёт в модель. Хуков `after_*` у него нет, поэтому место в списке не меняет порядок guard'ов.

- **Отклонение от §8 №5:** замер слоёв делается не во внешнем `Telemetry`, а в отдельном самом внутреннем middleware. Причина: внешний middleware видит запрос до того, как внутренние допишут карточку, каталог skills и память. `Telemetry` из S4.3 может забрать данные `ContextMeter` или стоять рядом.

Разбор слоёв (функция `measure_layers(request) -> dict[str, int]`, чистая и тестируемая без агента):

| Слой | Откуда |
| --- | --- |
| `instructions` | блок 0 системного сообщения и нераспознанные блоки (правила `read_file`) |
| `card` | блок, начинающийся заголовком карточки (`turn/card.py`) |
| `skills_catalog` | блок `SKILLS_SYSTEM_PROMPT` |
| `plan` | блок правил плана, вызовы `write_todos` и их `ToolMessage` |
| `memory` | блок памяти диалога (`TurnSummary`) |
| `skill_text` | `HumanMessage` с `lc_source="pinned_skill"`, вызовы `read_file` по `/skills/` и их результаты |
| `calls_results` | остальные вызовы инструментов (аргументы) и их `ToolMessage` |
| `history_summary` | `HumanMessage` с `lc_source="summarization"` |
| `dialog` | реплики пользователя, напоминания guard'ов, текст ответов модели |
| `tools` | схемы `request.tools` |
| `total` | сумма слоёв |

Блоки системного промпта распознаются по заголовкам-константам, которые уже лежат в `prompts/`. Константы заголовков вынесены так, чтобы и генератор блока, и `ContextMeter` брали одну строку. Номер вызова — число `AIMessage` хода после `turn_message_id` плюс 1. Повтор после сетевой ошибки (№14 стоит снаружи) пишет строку с тем же номером, это допустимо. Строка журнала: `logger.info("CONTEXT LAYERS {}", json.dumps({...}))`. Если `total > max_prompt_tokens` — `logger.warning`.

- Альтернатива — метки слоёв в content-блоках (`{"type":"text","layer":...}`). Отклонена: неизвестный ключ блока уходит в конвертер `langchain-gigachat`, поведение не проверено.

### D6. Вызов сводки вне лимитов и повторов

Вызов сводки не считается в `AGENT_MAX_MODEL_CALLS` и не повторяется `ModelRetryMiddleware`: он идёт напрямую через модель внутри №7. Встроенный путь langchain оборачивает модель сводки в `with_retry`, который повторяет любые ошибки, StopEvent тоже. Поэтому в middleware передаётся свой `summarizer`: асинхронная функция, которая один раз вызывает модель по `SUMMARY_PROMPT` (уточнено при реализации). StopEvent при нём ловит `StopEventGuard` (№4 снаружи) → 503. Сетевая ошибка роняет ход так же, как ошибка модели после исчерпания повторов (HTTP 424, как в S2.5).
- Альтернатива — свой `Summarizer` с повтором. Отложена: частые сжатия не ожидаются, а повторы GigaChat стоит решать вместе с частичным ответом после исчерпания повторов, который S2.5 оставила открытым.

### D7. Тесты на скриптовой модели

Вызов сводки идёт в ту же модель. В тестах её подкласс `SummarizingModel` сам отвечает на промпт сводки и не сдвигает курсор скрипта, поэтому скрипт содержит только шаги агента (уточнено при реализации). Тесты задают маленький бюджет 12 000 через `ContextBudget` фабрики — ниже минимума настроек 20 000, который защищает рабочую конфигурацию (запрос без истории — около 5,6 тыс. токенов). `AgentFactory` получает параметр `prompt_budget: ContextBudget | None` (бюджет и доли), по умолчанию из `APP_CONFIG.agent`, чтобы тест не трогал окружение.

## Risks / Trade-offs

- [Приближённый счёт расходится с токенами GigaChat] → порог 70 % и `chars_per_token=3.0` оставляют запас; калибровка в S5.1. `ContextMeter` пишет итог, по которому видно расхождение с `usage_metadata` (сравнение — S4.3).
- [Хвост сам больше бюджета: огромный результат или skill в последних сообщениях] → результаты уже не больше `AGENT_RESULT_MAX_CHARS` (S1.6), skill ≤ 300 строк (S3.2). Если хвост всё равно не влезает, deepagents обрезает хвостовые результаты инструментов (`_clip_overflow_tail`) или поднимает `ContextOverflowError`, и ход падает с 424. Строка `CONTEXT LAYERS` покажет причину.
- [Сводка теряет число, нужное в ответе] → числа хода лежат в `step_results` и выжимках, `AnswerGroundingGuard` проверяет по ним, а не по сводке. Полная история читается `read_file`.
- [`messages` в состоянии растут без предела] → как до S2.7. Запрос модели ограничен, память процесса — нет. Вопрос внешнего checkpointer (ADR-8, §16).
- [Переопределение приватного `_build_new_messages_with_path` и чтение `_summarization_event`] → версия закреплена `==`; `test_dependency_pins.py` проверяет наличие метода и ключа.
- [Повторно закреплённый skill — `HumanMessage` сразу после `ToolMessage` посреди хода] → GigaChat принимает такой порядок (spike S0.2: смена набора инструментов и сообщения после `ToolMessage`). Тест на скриптовой модели проверяет форму запроса. Живая проверка на GigaChat — S5.1.
- [Лишний вызов LLM на сжатие удлиняет ход] → редкий (порог 70 %), время хода ограничено `AGENT_TURN_TIMEOUT`.

## Migration Plan

Поведение включается вместе с v2 (`AGENT_ENGINE=v2`), отдельного флага нет. Откат — `AGENT_SUMMARY_TRIGGER` близко к 1 (сжатие практически не срабатывает) или откат коммита. Миграции данных нет: `_summarization_event` — новое необязательное поле состояния.

## Open Questions

- Значение `CHARS_PER_TOKEN` для GigaChat — по замеру в S5.1, во внутреннем контуре; на подход не влияет.
