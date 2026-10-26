# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-tools/spec.md`. Решения, на которые опирается story:
- ADR-5: доменные функции не меняются, адаптер делает `StructuredTool`, служебный контекст — из `ToolRuntime`.
- S0.7 D2: в копию движка передаётся абсолютный путь в `file_name` и пустой `user_id` — тогда `get_store_file` не вызывается и тело функции менять не нужно.
- S0.2: GigaChat делает один вызов инструмента за ответ — параллельных вызовов внутри хода нет.
- S0.3: `ToolRuntime` в сигнатуре инструмента скрыт от модели и даёт `runtime.context` (контекст хода), `runtime.store`.

Состояние кода: `api/v2/` содержит только пустые каталоги; доменные функции расчёта — `excel_engine.analyze_excel_model` и `analyze_model_inputs_for_target` (синхронные, открывают LibreOffice); индекс модели строится `subagents/utils.py:_build_catalog_with_ids` → `{"IN0": имя, ...}`, `{"OUT0": имя, ...}`. LangChain сейчас импортируют вне v2: `tools.py` (`langchain.tools.tool`) и `subagents/analyzer.py` (`langchain_core.messages`) — это базовая линия AR-Q3.

## Goals / Non-Goals

**Goals:**
- Один способ превратить доменную функцию в инструмент: схема для модели + функция «аргументы модели + контекст хода → вызов домена».
- Каталог, из которого агент (S2.1) и skills (S3.x) берут наборы инструментов.
- Проверяемая граница фреймворка (AR-Q3) и границы v1/v2 (§7 п. 5).

**Non-Goals:**
- Формат результата: в S1.1 доменный результат сериализуется как есть (`model_dump` → JSON); конверт и коды — S1.2.
- Предпроверки и коды ошибок: неизвестный ID в S1.1 — исключение адаптера; `unknown_id` и остальные коды — S1.2 / S1.4.
- Пул процессов и блокировка сессии — S1.5; в S1.1 синхронная функция идёт в `asyncio.to_thread`.

## Decisions

### D1. Раскладка

| Модуль | Ответственность |
| --- | --- |
| `api/v2/tools/context.py` | `ModelIndex` (неизменяемые словари `ID → имя` для входов и выходов, методы перевода) и протокол `ToolContext` (`user_id`, `file_path`, `model_index`) — то, что адаптер читает из `runtime.context` |
| `api/v2/tools/adapter.py` | `ToolSpec` (имя, описание на русском, схема аргументов, функция вызова, доступность, `uses_engine`) и `build_tool(spec) -> StructuredTool` |
| `api/v2/tools/catalog.py` | `ToolCatalog`: регистрация `ToolSpec`, `base_tools()`, `skill_tools()`, поиск по имени; `default_catalog()` с базовыми инструментами |
| `api/v2/tools/calc_tools.py` | `ToolSpec` для `run_scenarios` и `find_input_for_target`: схемы аргументов модели и перевод в аргументы `excel_engine` |

Пустые каталоги `api/v2/{prompts,schemas,states,subagents}` не трогаются (их наполнят S2.x).

### D2. Контекст хода через протокол

Адаптер не знает конкретного класса контекста: `ToolContext` — `typing.Protocol` с тремя атрибутами. `RequestContext` из S2.1 его реализует; в тестах — простой dataclass. Если в `runtime.context` нет нужного атрибута, адаптер падает с понятной ошибкой конфигурации (это ошибка сборки агента, а не модели).

*Альтернатива:* читать контекст из состояния графа — отвергнута: состояние сохраняется checkpointer’ом и видно модели через историю, а контекст хода — нет (§9).

### D3. Построение инструмента

`build_tool` создаёт `StructuredTool.from_function(coroutine=..., name, description, args_schema)`, где корутина принимает аргументы схемы и параметр `runtime: ToolRuntime`. LangChain внедряет `runtime` сам и не показывает его в схеме. Корутина: проверяет контекст → `spec.call(args, ctx)` в `asyncio.to_thread` → сериализует результат. Схема аргументов — Pydantic-модель с `extra="ignore"`: лишние поля от модели отбрасываются до домена.

### D4. Схемы расчётных инструментов (§10 «Каталог»)

| Инструмент | Аргументы модели | Вызов домена |
| --- | --- | --- |
| `run_scenarios` | `inputs: [{id, min, max, step}]`, `outputs: [id]`, `year` | `analyze_excel_model(file_name=<путь из контекста>, input_names, output_names, output_years=[year]*n, ranges=[[min, max]], steps=[step], user_id=None)` |
| `find_input_for_target` | `output_id`, `year`, `target`, `input_ids`, `tolerance=0.1` | `analyze_model_inputs_for_target(file_name=<путь>, output_name, output_year, target_value, input_names, tolerance, max_scenarios=1000)` |

`max_scenarios=1000` и `tolerance=0.1` — как в `OrchestratorTools._run_ift` v1, чтобы v1 и v2 считали одинаково. `user_id` в домен не передаётся (S0.7 D2): файл однозначно задан путём из контекста, который уже содержит пользователя.

### D5. Проверки границ

`tests/unit/v2/test_boundaries.py` (AST, без импорта модулей):
- `excel_engine/*.py` — нет `langchain*`, `langgraph*`;
- `tools.py`, `subagents/analyzer.py`, `subagents/utils.py`, `russian_stemmer.py`, `schemas/llm_outputs.py` — импорты LangChain/LangGraph совпадают с базовой линией;
- `api/v2/**` — нет импортов `api.v1.main_graph`, `api.v1.subagents.orchestrator_tools`, `api.v1.services` (§7 п. 5).

### D6. Тесты адаптера без сети и LibreOffice

Инструменты вызываются в настоящем `create_agent` со скриптовой тестовой моделью (`tests/unit/v2/fakes.py`, по образцу прототипа S0.3), а доменные функции подменяются фейком, записывающим аргументы. Это проверяет внедрение `runtime`, скрытие служебных полей и перевод ID. Паритет с настоящим движком — `tests/integration/excel_engine/test_tool_parity.py` (пропуск без `uno`, прогон в Docker): инструмент v2 и прямой вызов `excel_engine` с каноническими именами дают одинаковый результат на Methanex.

## Risks / Trade-offs

- [Тестовая скриптовая модель дублируется с будущим `ScriptedChatModel` (S2.1)] → она в `tests/`, S2.1 заменит её промышленной.
- [`asyncio.to_thread` не ограничивает число параллельных расчётов] → временно до S1.5 (`engine_gate`); один вызов инструмента за ход (S0.2) снижает риск.
- [Неизвестный ID падает исключением, модель получит текст исключения] → закрывается в S1.2 (конверт `unknown_id`); в S1.1 агент ещё не собирается в сервисе.

## Migration Plan

Только новый код в `api/v2/`, сервис его пока не вызывает (переключатель `AGENT_ENGINE` — S2.1). Откат — удаление файлов.
