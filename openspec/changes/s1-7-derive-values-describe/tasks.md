# Tasks

## 1. Индекс модели: значения выходов, единицы, типы (design D4)

- [ ] 1.1 Добавить в `tools/context.py:ModelIndex` поля `output_values`, `units`, `output_types` (по умолчанию пустые, неизменяемые как остальные). Существующие конструкторы в тестах работают без правок — `pytest tests/unit/v2` зелёный
- [ ] 1.2 `tools/index_builder.py`: книга открывается один раз, строки идут в те же `create_input_mapping` / `create_output_mapping`, по `row` из mapping и колонкам заголовка «Ед. измерения» / «Тип показателя» заполняются `units` и `output_types` (заглушка `[требуется заполнить]` и пустые пропускаются), из `values` — `output_values`. Тест на `models/Methanex_FinModel.xlsx` (openpyxl, без LibreOffice): ID и имена совпадают с `_build_catalog_with_ids`, у выручки единица `USD млн` и тип `Общий (обязательный)`, значения выхода за 2018 совпадают с ячейкой листа

## 2. Источники чисел (design D1)

- [ ] 2.1 Новый `tools/grounding.py`: `Source(value, label, unit)`, `collect_sources(state)` (`step_results.numbers`, `turn_summaries[*].results[*].numbers`, числа из всех `HumanMessage`) и `match(value, sources)` с допуском `min(0.5·10^(−d), 0.005·|s|)` и выбором ближайшего. Тесты в `test_derive.py`: разбор `3,5`, `1 200`, `15%`, `3.5x`, знак; `2.81` ↔ `2.8134` принят, `3` ↔ `2.6` отклонён, `0` совпадает только с `0`; число из выжимки и из сообщения пользователя находится
- [ ] 2.2 `ToolSpec.needs_state` в `tools/adapter.py`: при флаге `call(args, ctx, state)` получает снимок `runtime.state` только для чтения, вызов остаётся в `asyncio.to_thread`. Тест в `test_tool_adapter.py`: инструмент с флагом видит `step_results` текущего хода, в его схеме нет полей состояния, инструменты без флага вызываются как раньше

## 3. `derive` (design D3)

- [ ] 3.1 Новый `tools/derive.py`: схема `DeriveArgs` (`operation`, `a`, `b`, `threshold`, `direction`, `points`) с короткими описаниями на русском; нехватка поля для операции → `missing_input` с названием поля. Проверка всех операндов через `grounding.match` — непринятые перечисляются одним `invalid_args` с подсказкой из design D1
- [ ] 3.2 Операции `difference`, `pct_change`, `headroom`, `crossing`, `elasticity` по таблице design D3: единицы (`bad_units` для двух разных известных, `%` для процентов, без единиц для эластичности), `notes` «порог нарушен» / «порог не пересекается в диапазоне» с ближайшей точкой, деление на ноль → `invalid_args`. В `numbers` — полные значения с подписями из источников, в `data.operands` — использованные источники
- [ ] 3.3 Добавить `hypothesis` в dev-группу Poetry (`poetry add --group dev hypothesis`, `poetry.lock` обновлён). Property-тесты в `tests/unit/v2/test_derive.py` по списку design D7 плюс примерные тесты единиц и выдуманного операнда; `pytest tests/unit/v2/test_derive.py` зелёный

## 4. `get_values` и `describe_model` (design D4)

- [ ] 4.1 Новый `tools/read_tools.py`: `get_values(ids, years=None)` и `describe_model()` как синхронные `call` по индексу из контекста. Конверты: подписи `"<имя>, <год>"` с единицей, оговорка про доли для `%`, годы без значения в `notes`, `no_values`, `unknown_id`. Карточка: горизонт, число входов и выходов, группы по типу, число заголовков, `data.outputs`. Тесты в `tests/unit/v2/test_read_tools.py` на `FakeContext` и на индексе Methanex: все сценарии требований «ADR-5 Значения показателей по ID» и «ADR-5 Описание модели», включая вынос карточки Methanex в `/results/` с `ref`

## 5. Значения сетки в `run_scenarios` (design D5)

- [ ] 5.1 `calc_tools.scenarios_envelope`: после минимумов и максимумов выходов добавить значения входов сетки (каждое один раз, подпись `"<вход>, значение в сетке"`, единица из индекса), затем остальные значения выходов. Обновить тест порядка `numbers` (S1.6) и добавить проверку: `derive crossing` по точкам из результата `run_scenarios` принимает все `x` и `y`

## 6. Каталог и промпт (design D6)

- [ ] 6.1 Зарегистрировать `get_values`, `describe_model`, `derive` в `default_catalog()` как базовые без `uses_engine`. Тесты: в `base_tools()` они есть, в `engine_tool_names()` их нет; у собранного агента модель видит не больше 8 инструментов; `validate_skills` по базовому набору зелёный (`test_skills.py`, `test_runtime.py`)
- [ ] 6.2 Дополнить `prompts/system.py` правилом про `derive` (design D6). Существующие тесты промптов зелёные

## 7. Сквозные проверки (ADR-9, AR-Q9)

- [ ] 7.1 Тест на собранном агенте со скриптовой моделью и фейковым движком: `get_values` → `derive headroom` по полученному значению и порогу из сообщения пользователя → в `step_results` хода есть производные числа `derive`. Второй сценарий: `derive` с выдуманным операндом → `invalid_args`, движок не запускался
- [ ] 7.2 Тест AR-Q9: в ходе агента `get_values`, `describe_model` и `derive` выполняются не в потоке event loop (идентификатор потока внутри `call` отличается от потока цикла), а параллельная корутина успевает выполниться во время вызова

## 8. Качество и трассировка

- [ ] 8.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` плюс новые файлы): новых замечаний ноль, старые не блокируют и попутно не правятся
- [ ] 8.2 `pylint src` — оценка > 7
- [ ] 8.3 `pytest tests/unit` — зелёный; покрытие нового кода (`derive.py`, `grounding.py`, `read_tools.py`, правки адаптера, индекса и `calc_tools.py`) ≥ 80 %
- [ ] 8.4 README: описать три новых инструмента в разделе об инструментах v2, если раздел есть; новых переменных окружения нет
