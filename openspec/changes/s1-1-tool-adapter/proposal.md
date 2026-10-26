Story S1.1 · AR-Q3 · ADR-5 · §7 «Компоненты» (адаптер, каталог), §9 «Runtime context», §10 «Контракт» п. 1, 2 (схема), 5

## Why

Агент v2 вызывает доменные функции (расчёт, смысловой слой, чтения) как инструменты LangChain. Если писать инструменты прямо в `tools.py` через `@tool`, фреймворк проникает в домен, а модель видит служебные аргументы (`user_id`, `file_name`, `thread_id`) и может их подменить. ADR-5 решает это адаптером: доменная функция остаётся как есть, адаптер делает из неё `StructuredTool` со схемой только для модели, берёт служебный контекст из `ToolRuntime` и переводит ID индекса модели в канонические имена. Это фундамент всего слоя инструментов E1.

## What Changes

- `src/aigw_service/api/v2/tools/adapter.py`: построение `StructuredTool` из доменной функции — схема аргументов для модели (Pydantic), контекст хода из `ToolRuntime.context` (пользователь, путь к файлу, индекс модели), перевод ID индекса в канонические имена, вызов синхронной доменной функции вне event loop.
- `src/aigw_service/api/v2/tools/catalog.py`: реестр инструментов с признаками доступности (базовый / открывается skill) и `uses_engine`.
- `src/aigw_service/api/v2/tools/context.py`: протокол контекста хода, который адаптер читает из runtime (реализует `RequestContext` в S2.1).
- Первые базовые инструменты на копии движка `src/excel_engine/`: `run_scenarios` (→ `analyze_excel_model`) и `find_input_for_target` (→ `analyze_model_inputs_for_target`) — по ID, с прежними аргументами доменных функций; результат возвращается как есть.
- Проверка импортов: LangChain — только в `api/v2/`; в `tools.py`, смысловом слое и `excel_engine` новых импортов LangChain нет.

**Вне рамок:** конверт результата и коды ошибок (S1.2); `resolve_terms` (S1.3); предпроверки аргументов, `unknown_id`, `missing_input`, «недостижимо» как успех (S1.4); `engine_gate` и пул процессов (S1.5); вынос в `/results/` (S1.6); `derive`, `get_values`, `describe_model` (S1.7); строки `TOOL ARGS` (S4.3); сборка агента (S2.1).

## Capabilities

### New Capabilities

- `agent-tools`: инструменты агента v2 — адаптер доменных функций, единый контракт, каталог и доступность, сопоставление терминов, доступ к движку, вынос результатов. Эта story добавляет AR-Q3 и механику адаптера (ADR-5).

### Modified Capabilities

—

## Impact

- Новый код в `src/aigw_service/api/v2/tools/` и `api/v2/__init__.py`.
- Домен, смысловой слой и `excel_engine` не меняются.
- Новые unit-тесты в `tests/unit/v2/` на скриптовых данных без LibreOffice и без сети.
