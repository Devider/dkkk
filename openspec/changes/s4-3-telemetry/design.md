# Design

## Context

Что уже есть (наблюдаемо в коде):

- `ContextMeter` (S2.7, `api/v2/middleware/context_meter.py`) — самый внутренний `wrap_model_call`, пишет `CONTEXT LAYERS` с `trace_id`, `turn_id`, номером вызова (`call_number`) и слоями. Требование AR-Q6 «Замер слоёв контекста» в `agent-loop` уже закрыто.
- `forbid_langsmith` (S3.1, `core/tracing/langsmith_guard.py`) останавливает старт при `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2`; покрыт тестами в `tests/unit/v2/test_skills.py`.
- `router.py` кладёт в `config["metadata"]` `langfuse_session_id = thread_id`, `lang_fuse_user_id` (опечатка — Langfuse 4.15 читает `langfuse_user_id`) и для v2 `agent_engine = "v2"` (метаданные, не тег). Callback-handler — общий `APP_CTX.tracing.callback_handler`.
- Журнал — loguru с JSON-сериализацией; `rqUId` берётся из contextvar `x_trace_id_var`, выставленного на запрос. Contextvars копируются в задачи LangGraph и в `asyncio.to_thread`, поэтому строки из middleware v2 уже несут `rqUId` (так же, как `CONTEXT LAYERS`).
- `resolve_terms` вызывает смысловой слой (`_ema_analyzer().ainvoke(...)`) без явного `config`; LangChain подхватывает родительский run config через contextvar `var_child_runnable_config`, поэтому callbacks хода должны дойти до вложенного вызова — это нужно закрепить тестом.
- Справка для будущей story `TOOL ARGS` (вне рамок): `scripts/run_tool_queries.py` ждёт строки `TOOL ARGS: <имя> | <dict>` с полями движка v1 (`input_names`, `output_names`, `ranges`, …), а v1 (`main_graph.py:224`) сейчас пишет аргументы оркестратора (`reformulated_query`); v1-форма аргументов в v2 есть только в `tools/calc_tools.py` перед вызовом движка.

## Goals / Non-Goals

**Goals:**
- Строки `LLM CALL` / `TOOL CALL` на каждый вызов модели и инструмента v2: время, токены, коды ошибок, без текстов исключений.
- Корректные `langfuse_user_id` и тег `agent_engine=<v1|v2>`; вложенные вызовы смыслового слоя в дереве хода.

**Non-Goals:**
- Строки `TOOL ARGS` в формате v1 и работа `run_tool_queries.py` на v2 (отложено решением разработчика); метрики хода через `metric_router.py`; отдельные события Langfuse на каждое срабатывание guard; перенос `ContextMeter` или объединение его строки с `LLM CALL`; A/B preview-модели (S4.4).

## Decisions

### D1. `Telemetry` — §8 №5, сразу после `StopEventGuard`

Middleware с `wrap_model_call` / `awrap_model_call` и `wrap_tool_call` / `awrap_tool_call`. Место в стеке по ADR-1 / §8: внутри `StopEventGuard`, снаружи `SummarizationMiddleware`, лимитов, `ModelRetryMiddleware`, `CalcCache` и `ToolErrorMiddleware`.

Следствия места (принимаем):
- Одна строка `LLM CALL` на логический вызов модели: повторы `ModelRetryMiddleware` и вызов сводки `SummarizationMiddleware` входят во время этой строки. Отдельные попытки видны в Langfuse как отдельные generation.
- Для инструмента Telemetry видит итог после `ToolErrorMiddleware`: исключение уже превращено в конверт, код берётся из JSON `ToolMessage` (`code`). Хит `CalcCache` даёт обычный `TOOL CALL` со статусом `ok` и малым временем: `_replay` не ставит признака в сообщение, а сам хит уже пишется строкой `CalcCache hit` — отдельного поля `cached` не вводим.
- Исключения, которые проходят насквозь (`StopEventError`, отмена хода), пишутся с кодом = имя класса исключения и пробрасываются.

Поля `LLM CALL`: `trace_id`, `turn_id`, `call` (тот же `call_number`, что у `CONTEXT LAYERS` — строки связываются по `turn_id` + `call`), `elapsed_s`, `token_usage` (`input_tokens`, `output_tokens`, `total_tokens` из `AIMessage.usage_metadata`; пустой словарь, если нет), `tool_calls` (имена), `error` (код или `null`). Форма повторяет `build_diagnostic_data` v1 (`node`, `elapsed_s`, `token_usage` + доп. поля), но функция пишется в v2 заново: v1 и v2 не импортируют друг друга (AR-Q3). Формат сообщения — `logger.info("LLM CALL {}", json.dumps(...))`, как у `CONTEXT LAYERS`; стек исключения не нужен — пишем только код.

Отклонение от §13 («один JSON на вызов LLM с размером слоёв»): размеры слоёв остаются в отдельной строке `CONTEXT LAYERS`. Причина: слои верно меряются только самым внутренним `wrap_model_call` (после сжатия и всех блоков промпта, S2.7 D5), а время и токены — снаружи повторов. Связка по `turn_id` + `call` даёт тот же анализ без передачи данных между middleware.

### D2. Метаданные Langfuse в `router.py` для обоих движков

В `config["metadata"]` добавляются `langfuse_user_id = user_id` и `langfuse_tags = [f"agent_engine={engine}"]`, где `engine` — `v2` для `AgentV2`, иначе `v1`. `lang_fuse_user_id` и `agent_engine` в метаданных остаются (ничего не ломают, их может читать существующий дашборд). Для v1 это аддитивная правка метаданных трассы; ответ и поведение v1 не меняются (FR-20). Путь к файлу и ключи в метаданные не кладутся — их там и сейчас нет; тест это фиксирует.

### D3. Дерево хода — проверка, а не новый код

Узлы `before_*` / `after_*` middleware — узлы графа `create_agent`, вызовы модели и инструментов — runs LangChain; всё это handler Langfuse получает сам. Вложенный вызов смыслового слоя наследует callbacks через contextvar. Новый код здесь не пишется; тест на фейковом `BaseCallbackHandler` (собирает `run_id` / `parent_run_id` / имена / метаданные) проверяет: корень хода с метаданными и тегом → вызовы модели → `resolve_terms` → дочерний LLM-run смыслового слоя; есть узел `TurnSummary`. Если тест покажет, что вложенный вызов теряет родителя (например, из-за `asyncio.to_thread` в адаптере), фикс — передать `config` из `runtime` в вызов смыслового слоя внутри обёртки v2 (тела смыслового слоя не меняются).

### D4. LangSmith — без изменений

`forbid_langsmith` уже стоит в `lifespan` для обоих движков и покрыт тестами S3.1. S4.3 только ссылается на него; требование AR-Q14 в `agent-skills` не меняется.

## Risks / Trade-offs

- **Критерий приёмки S4.3 закрыт не полностью**: без `TOOL ARGS` скрипт `run_tool_queries.py` на v2 вызовов не найдёт, сравнение v1 / v2 (S5.3) им невозможно до отдельной story. При архивации это отражается в `traceability.md`.
- **Время `LLM CALL` включает повторы и сводку** → p95 вызова модели по этой строке выше «чистого» времени GigaChat. Чистое время каждой попытки — в Langfuse.
- **Токены есть не всегда**: если GigaChat не вернул `usage_metadata`, `token_usage` пуст; строка всё равно пишется.
- **Проверка в живом Langfuse** возможна только в контуре с GigaChat; локально — тестами с фейками. Задача помечена «в контуре».
