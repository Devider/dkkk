# Design

## Context

Мотивация — proposal.md, требования — `specs/agent-tools/spec.md`. Контракт — §10 п. 3, 4 и справочник кодов; поведение модели на кодах — §8 правило 5.

Состояние после S1.1: `build_tool` сериализует доменный результат как есть (`serialize`); неизвестный ID — `KeyError` из `ModelIndex`, наружу уходит исключением. Расчётные функции `excel_engine` возвращают `status` / `result` / `content`:

| Ветка домена | `status` | Текст `result` (начало) |
| --- | --- | --- |
| Нет входов | `ERROR` | «Не указаны входные параметры для анализа» |
| Нет файла | `ERROR` | «Файл … не найден» |
| Нет выхода в книге | `ERROR` | «Выходной параметр "…" не найден», «Ошибка при поиске выходного параметра…» |
| Нет ячейки входа | `ERROR` | «Не удалось найти ячейку для параметра "…"» |
| Нет года | `ERROR` | «Не удалось определить год из параметра …» |
| Сетка больше `MAX_SCENARIOS` | `ERROR` | «Диапазон слишком широкий…» |
| Любое исключение | `ERROR` | `_humanize_error(e)` (EMA — с префиксом «Ошибка при анализе: ») |
| IFT без найденных сценариев | `WARNING` | «Не найдено сценариев…» |
| Успех | `OK` | сводка |

Содержимое успеха: IFT — структурированный `content` (`matching_scenarios`, `all_scenarios`, `current_input_values`, `optimized_scenario`, `results_file`, `processing_time_seconds`); EMA — `content.result_df` (`pandas.DataFrame`: столбцы входов и `<выход>_<год>`) и пути `scenario_file`, `minmax_matrix_file`, `scenario_matrix_file`.

## Goals / Non-Goals

**Goals:**
- Один конверт для всех инструментов; модель никогда не получает голое исключение от известной ошибки.
- Классификация v1 без правки домена.

**Non-Goals:**
- Подключение конверта к исключениям вне адаптера (ошибки валидации аргументов в `ToolNode`, непредвиденные исключения) через `ToolErrorMiddleware` — S2.5; S1.2 даёт для него функцию «исключение → конверт».

## Decisions

### D1. Модели конверта

`envelope.py`, Pydantic:
- `Number {value: float, label: str, unit: str | None}`;
- `ToolSuccess {status: "ok", result: str, data: dict | list | None, numbers: list[Number], notes: list[str], hint: str | None, ref: str | None}`;
- `ToolFailure {status: "error", error: str, code: ErrorCode, hint: str}`.

Сериализация — JSON без `None`-полей и пустых списков, чтобы не тратить контекст (AR-Q6). `ErrorCode` — `StrEnum` из 13 кодов §10; `DEFAULT_HINTS[code]` — подсказка по умолчанию на русском.

### D2. `ToolError` и место перевода

`ToolError(code, message, hint=None)` — исключение, которое бросают адаптер и обёртки инструментов. `build_tool` ловит `ToolError` и возвращает `ToolFailure`; успех оборачивает в `ToolSuccess`. Перевод `KeyError` из `ModelIndex` — в `ModelIndex` (бросает `ToolError(unknown_id)` вместо `KeyError`), чтобы каждый инструмент получал это поведение бесплатно.

Непредвиденные исключения адаптер **не** глушит: их ловит `ToolErrorMiddleware` (S2.5) через `failure_from_exception(exc) -> ToolFailure` (`ToolError` → свой код, остальное → `internal_error`, текст исключения — только в журнал через `logger.opt(exception=True)`). Так стек повторов (`ToolRetryMiddleware` внутри `ToolErrorMiddleware`, §8) видит исключение и может повторить `engine_unavailable`.

*Альтернатива:* адаптер ловит всё — отвергнута: `ToolRetryMiddleware` не увидел бы исключения движка.

### D3. Классификация результатов v1 (`classify_v1`)

Порядок проверок по `result` (регистр не важен):

| Признак | Итог |
| --- | --- |
| `status == "OK"` | успех |
| `status == "WARNING"` и «Не найдено сценариев» | успех + `notes: ["цель не достигнута"]` (FR-11; `data.reachable` — S1.4) |
| «Не указаны входные параметры», «Не удалось определить год» | `missing_input` |
| «Диапазон слишком широкий» | `invalid_args`, hint — сузить диапазон или увеличить шаг |
| «не найден» про параметр, «Не удалось найти ячейку», «Ошибка при поиске выходного параметра» | `no_values`, hint — взять следующего кандидата из `resolve_terms` |
| «Файл … не найден» | `internal_error` (файл обеспечивает `TurnContext`, модель это не исправит) |
| `URP bridge`, `DisposedException`, `soffice`, `uno`, `connect` в тексте исключения | `engine_unavailable` |
| иначе `ERROR` / `WARNING` | `internal_error` |

Для `ToolFailure.error` берётся короткое описание кода, а не текст домена: доменный текст может содержать текст исключения (FR-14). Исключение — `missing_input`, `no_values`, `invalid_args`: их доменный текст понятен и без технических деталей, он идёт в `error`.

### D4. Конверты расчётных инструментов

- `find_input_for_target`: `result` — доменная сводка; `data` — `matching_scenarios` (до 10) и `current_input_values`; `numbers` — по каждому найденному сценарию значения входов (`label` = имя входа) и выхода (`label` = имя выхода); при недостижимой цели — ближайший сценарий из `all_scenarios[0]`.
- `run_scenarios`: строки `result_df` (`DataFrame.to_dict("records")`) → `data.rows` (уточнено при реализации: `result_df` — объект `DataFrame`, а не текст, разбор не нужен); `numbers` — значения выходов с подписью «выход год при вход = значение»; `result` — «N сценариев: …» с границами по каждому выходу.
- Серверные пути (`results_file`, `scenario_file`, `minmax_matrix_file`, `scenario_matrix_file`) и `processing_time_seconds` в `data` не попадают.

### D5. Совместимость с v1-логами и eval

Конверт меняет только текст `ToolMessage`. Строки `TOOL ARGS` для eval-скриптов — S4.3.

## Risks / Trade-offs

- [Классификация по тексту хрупка: изменение формулировки в домене ломает код] → домен заморожен (копия, AST-тест S0.7); контрактные тесты на каждую строку таблицы D3 с текстами из копии.
- [Нет `result_df` в результате EMA] → `rows` пусто, сводка `result` остаётся.
- [Ошибки валидации аргументов (`ToolNode`) пока не в конверте] → S2.5; в S1.2 фиксируется как известное ограничение.

## Migration Plan

Только `api/v2`, сервис его не вызывает. Откат — revert.
