# Тесты AI Gateway

## Unit-тесты

`tests/unit/` — тесты без внешних зависимостей (mock'и), быстрые:

| Файл | Что тестирует |
|---|---|
| `test_stop_event.py` | ТН05 (Retry), ТН08 (StopEvent): `StopEventError`, `_wrap_llm_with_stop_event`, Pydantic-совместимость monkey-patching, конфиг retry-параметров |
| `test_tool_dedup.py` | Дедупликация повторных вызовов инструментов (fingerprint: tool_name + sorted args) |

```bash
pytest tests/unit/                        # все unit-тесты
pytest tests/unit/test_stop_event.py -v   # выборочно
```

## Быстрый запуск

```bash
pytest tests                            # все тесты
pytest tests/test_tools_performance.py -v -s --no-header --no-cov  # ~2.5 min
```

Тесты используют `httpx.AsyncClient` с `app=app_main` (ASGI-транспорт, без реального сервера).
Требуют `GIGACHAT_HOST` и `GIGACHAT_PORT` в `pyproject.toml [tool.pytest.ini_options.env]`.
`asyncio_mode = auto` — `@pytest.mark.asyncio` не нужен.

---

## Интеграционные тесты

Ожидают 5 заголовков (описаны в `src/aigw_service/api/v1/utils.py`):

| Header | Формат |
|---|---|
| `x-trace-id` | UUID v4 |
| `x-client-id` | 2 буквы + 8 цифр |
| `x-request-time` | RFC-3339 |
| `x-session-id` | UUID, опционально |
| `x-user-id` | ≤8 символов |

`tests/conftest.py` задаёт только session-scoped `event_loop` фикстуру; явного вызова `APP_CTX.on_startup()` в тестах нет.

---

## Перформанс-тесты

`tests/test_tools_performance.py` — 7 тестов, ~2.5 мин.

Замеряют скорость каждого этапа пайплайна:
- `loads().finish()` (~34s first call, затем кеш)
- `calculate()` (~5–35s)
- `get_cell()` (попадание в `_solution` кеш)

Тесты также проверяют корректность значений (сравнение с golden reference).

---

## Валидация вызовов инструментов

`scripts/run_tool_queries.py` — проверяет, что LLM формирует
корректные вызовы инструментов, а Jaccard-резолвинг приводит
английские алиасы из запроса к каноническим русским именам.

**Тестовые данные**: `tests/data/Methanex_tool_test_queries.xlsx`
(600 запросов: 300 analyze_excel_model + 300 analyze_model_inputs_for_target).

### Принцип работы

1. HTTP POST с уникальным `x-trace-id` на работающий сервер
2. Парсинг `server.log` — строка `"TOOL ARGS"` с matching `rqUId`
3. Алиасы → `find_matching_cell()` / `find_matching_outputs()` (тот же Jaccard-пайплайн, что в production)
4. Сравнение разрешённого канонического имени с `expected_call` из .xlsx

### Запуск

```bash
# Сервер должен быть запущен:
python src/aigw_service/__main__.py 2>&1 | tee server.log

# В другом терминале:
# 1. Только первые 5 запросов (быстрая проверка)
python scripts/run_tool_queries.py --subset 5

# 2. С детальным выводом по каждому полю
python scripts/run_tool_queries.py --subset 5 --verbose

# 3. С выгрузкой CSV для просмотра в таблице
python scripts/run_tool_queries.py --subset 5 --csv errors.csv

# 4. Загрузить модель перед тестами (если файла нет на сервере)
python scripts/run_tool_queries.py --subset 5 --upload models/model.xlsx

# 5. Продолжить с прерванного места
python scripts/run_tool_queries.py --resume test_output/results.json
```

### CLI-флаги

| Флаг | Описание |
|---|---|
| `--url` | URL сервера (по умолч. http://localhost:8080) |
| `--log` | Путь к server.log (по умолч. `server.log`) |
| `--queries` | Excel-файл с тестовыми запросами (по умолч. `tests/data/Methanex_tool_test_queries.xlsx`) |
| `--model` | Путь к .xlsx модели (для резолвинга имён, по умолч. `models/model.xlsx`) |
| `--subset N` | Первые N запросов **из каждого листа** (0 = все, по умолч.) |
| `--resume FILE` | Продолжить с JSON-чекпоинта |
| `--output FILE` | Сохранить результаты в JSON (авто-генерируется, если не задан) |
| `--timeout SEC` | Таймаут HTTP-запроса (по умолч. 600) |
| `--verbose` | Детальный вывод alias/resolved/expected по каждому полю |
| `--csv FILE` | Записать CSV с деталями сравнения |
| `--upload FILE` | Загрузить .xlsx на сервер перед тестами |
| `--delay SEC` | Задержка между запросами в секундах (по умолч. 0) |

### Формат вывода

#### Базовый (без флагов)

```
━━━ [  1/30] A001 ━━━ FAIL (3/8 params) ━━━
  Prompt: В модели Methanex_FinModel.xlsx посчитай CFI, ICR (LTM), EBITDA, net profit и minimum annual DSCR за 2026...
  year: MISMATCH — expected 2026, got None
  input_names: LENGTH_MISMATCH — expected 3, got 4
  output_names[3]: MISMATCH — resolved to 'Net Debt/EBITDA', expected 'Чистая прибыль'
```

`params` — количество корректно зарезолвленных параметров (year + input_names + output_names).
`FAIL (3/8 params)` — из 8 проверяемых сущностей 3 совпали, 5 нет.

#### С `--verbose`

```
━━━ [  1/30] A001 ━━━ FAIL (3/8 params) ━━━
  Prompt: В модели Methanex_FinModel.xlsx посчитай CFI, ICR (LTM), EBITDA, net profit и minimum annual DSCR за 2026...

  input_names[1]:
    alias:      'US CPI (USD) 2030'       ← что вернула LLM
    resolved:   'Табак (USD)'             ← к чему привёл Jaccard
    similarity: 0.250                     ← Jaccard score (0..1)
    expected:   'Инфляция - Рост индекса
                 потребительских цен в США,
                 в долларах США (USD, eop CPI)'  ← эталон из expected_call
    status:     MISMATCH

  output_names[1]:
    alias:      'net profit margin'       ← LLM написала по-английски
    resolved:   'Net Debt/EBITDA'         ← ближайшее совпадение (Jaccard 0.2)
    expected:   'Рентабельность чистой прибыли'  ← русское каноническое имя
    status:     MISMATCH

  input_names[2]:
    alias:      'urea price 2030'
    expected:   'Карбамид (FOB Южный)'
    detail:     No match found for query: urea price 2030
    status:     RESOLUTION_ERROR          ← резолвинг упал с exception

  output_names[2]:
    alias:      'cash balance (eop) 2029'
    expected:   'Остаток денежных средств на конец периода'
    similarity: 0.000                     ← нет пересечения символов
    status:     NO_MATCH                  ← не нашлось в маппинге
```

#### Поля в `--verbose`

| Поле | Что означает |
|---|---|
| `alias` | Сырой алиас, который LLM вернула в `input_names` / `output_names` |
| `resolved` | Каноническое имя из Excel, в которое Jaccard-резолвинг преобразовал alias |
| `expected` | Эталонное каноническое имя из `expected_call` (поле `expected` в .xlsx) |
| `similarity` | Jaccard similarity (0..1) между нормализованными alias и resolved. Если 0.0 — символы не пересекаются (обычно English vs Русский) |
| `status` | `MISMATCH` — resolved не совпал с expected; `NO_MATCH` — ничего не нашлось; `RESOLUTION_ERROR` — exception при резолвинге |
| `detail` | Доп. информация (текст ошибки, сырое значение) |

#### CSV

```
id,field,alias,resolved,expected,similarity,status
A003,output_names[2],cash balance (eop) 2029,,Остаток денежных средств на конец периода,0.000,NO_MATCH
A001,input_names[1],ав USD/RUB,Табак (USD),Средний за период курс рубля к доллару США (av USD/RUB),0.250,MISMATCH
```

---

## Сводка по инструментам

В конце выводится сводка — отдельно по каждому инструменту и общая:

```
=== analyze_excel_model (15 queries) ===
  PASS: 0/15 (0.0%)
  Params: 32/75 correct (42.7%)

=== analyze_model_inputs_for_target (15 queries) ===
  PASS: 0/15 (0.0%)
  Params: 28/60 correct (46.7%)

=== TOTAL (30 queries) ===
  PASS: 0/30 (0.0%)
  Params: 60/135 correct (44.4%)
```

`Params: X/Y correct (Z%)` — доля успешно зарезолвленных параметров (year + input_names + output_names) среди всех запросов инструмента. Это дополнительная метрика к PASS/FAIL: даже если все запросы висят в FAIL из-за хотя бы одной ошибки, можно видеть прогресс по отдельным алиасам.

---

## Результаты

Все форматы сохраняются в `test_output/`:
- `tool_query_results.json` — полные результаты с `diffs`, `comparison` и `tool_stats`
- `comparison_dump.csv` — плоский CSV (если указан `--csv`)

---

## Анализ результатов

`scripts/analyze_results.py` — принимает JSON с результатами (из `run_tool_queries.py`)
и выводит **11 секций** для понимания причин падений тестов. Скрипт «расплющивает»
все `comparison`-записи в плоский список `{id, tool, field, status, alias, expected,
resolved, similarity}` и агрегирует их по-разному в каждой секции.

### Запуск

```bash
python scripts/analyze_results.py test_output/tool_query_results.json
python scripts/analyze_results.py results.json --top-n 30
python scripts/analyze_results.py results.json --csv analysis.csv
```

### Параметры

| Параметр | Обязательный | Описание |
|----------|--------------|----------|
| `input` | да | Путь к JSON-файлу с результатами (`tool_query_results.json` из `run_tool_queries.py`) |
| `--top-n N` | нет | Сколько строк выводить в Confusion Matrix, NO_MATCH Aliases и Resolution Errors (по умолч. 15) |
| `--csv FILE` | нет | Выгрузить плоский CSV всех comparison-записей (колонки: `id, field, alias, resolved, expected, actual, similarity, status, detail`) |

### Секции вывода (11)

| № | Секция | Что показывает |
|---|--------|---------------|
| 1 | **Summary** | Итоговые метрики из `tool_stats`: `queries_passed`, `params_passed` по каждому инструменту. `params_passed > 0` даже при `queries_passed = 0` (отдельные поля верны, но запрос целиком — нет) |
| 2 | **Query Status Breakdown** | Разбивка **запросов** по категориям (см. ниже). Показывает, какая доля вообще не дошла до сравнения параметров (timeout / no tool call) |
| 3 | **Effective Comparison Analysis** | Консистентность: берёт только дошедшие до сравнения запросы и считает ожидаемый pass-rate = `per_param_acc ^ avg_params`, сравнивает с фактическим. `CONSISTENT` — наблюдаемый pass-rate объясняется per-param точностью; `ANOMALOUS` — скрытый баг в сравнении |
| 4 | **Error Type Distribution** | Распределение **статусов сравнения** (MISMATCH / NO_MATCH / LENGTH_MISMATCH / RESOLUTION_ERROR / MISSING) — см. таблицу статусов ниже |
| 5 | **Field-Level Accuracy** | Точность по каждому полю (year, input_names, output_names, target_value, …). `total` = сколько раз поле ожидалось, `pass` = `total − ошибки` |
| 6 | **Near-Miss Analysis** | Распределение числа провалившихся параметров среди `PARAM_MISMATCH`-запросов. Сколько запросов «в 1 ошибке от PASS» — кандидаты на быстрый фикс через починку топ-Confusion пар |
| 7 | **Confusion Matrix** | Систематические ошибки резолвинга: `expected → resolved` (count). Повтор одной пары десятки раз = систематическая проблема в Jaccard-маппинге или prompt'е |
| 8 | **NO_MATCH Aliases** | Алиасы, которые **ни разу** не зарезолвились (резолвер вернул None). Обычно чисто английские термины без пересечения с русскими каноническими именами |
| 9 | **Resolution Errors** | Алиасы, упавшие с серверной ошибкой («No match found for query: X») при резолве |
| 10 | **Similarity Distribution** | Гистограмма Jaccard similarity для MISMATCH: `sim < 0.2` = cross-lingual (разные алфавиты), `0.2–0.4` = низкое/случайное совпадение, `0.6–1.0` = почти правильное имя, но резолвер выбрал другое. Если большинство `< 0.2` — Jaccard не справляется, нужны синонимы/перевод |
| 11 | **Input Count vs Accuracy** | Группировка по числу `input_names`. Деградирует ли точность с ростом входов (LLM путает алиасы в большом контексте) |

### Типы ошибок и категорий

**Категории запроса** (секция Query Status Breakdown, `analyze_results.py:_categorize_query`):

| Категория | Значение |
|-----------|----------|
| `PASS` | Запрос прошёл полностью (все поля совпали) |
| `ERROR (timeout)` | Серверный таймаут — запрос не дошёл до сравнения |
| `ERROR (other)` | Прочая серверная ошибка |
| `NO_TOOL_ARGS` | LLM не вызвал инструмент (или аргументы не найдены в `server.log`) |
| `WRONG_TOOL` | LLM вызвал не тот инструмент |
| `PARAM_MISMATCH` | Дошёл до сравнения, но есть ошибки резолвинга (основная рабочая категория) |
| `PARAMS_OK_FAIL` | Все параметры зарезолвились, но запрос всё равно FAIL (напр. структурная ошибка) |
| `OTHER` | Не попал ни в одну из категорий выше |

**Статусы сравнения** (секция Error Type Distribution, по каждому полю):

| Статус | Значение |
|--------|----------|
| `MISMATCH` | alias зарезолвился, но **не в то** каноническое имя. Причина: Jaccard выбрал ближайшее, но неправильное (cross-lingual или похожие имена в листе) |
| `NO_MATCH` | `find_matching_cell` / `find_matching_outputs` вернул None — ни одно имя не прошло порог. Сервер **не упал**, просто нет совпадений (чаще всего английский алиас vs русское имя, similarity = 0) |
| `RESOLUTION_ERROR` | Сервер не смог найти ячейку по alias'у и бросил exception (напр. английский термин без совпадений). Отличается от `NO_MATCH` тем, что упал с ошибкой, а не вернул пустоту |
| `LENGTH_MISMATCH` | Разная длина списков expected и actual. В текущем прогоне это исключительно `ranges`/`steps` (LLM неверно считает число диапазонов/шагов). Ранее встречался как output_name vs output_names (старый баг, пофикшен) |
| `MISSING` | Поле есть в `expected`, но отсутствует в `actual` |

### На что смотреть в первую очередь

1. **Query Status Breakdown** — сначала отделите запросы, дошедшие до сравнения, от
   `NO_TOOL_ARGS` / `ERROR (timeout)`. Иначе кажется, что все N запросов провалили
   резолвинг, хотя часть не дошла до проверки вообще.
2. **Confusion Matrix** — если одна пара `expected → resolved` повторяется >20 раз,
   это систематическая ошибка. Нужно либо править синонимы в resolution pipeline,
   либо менять system prompt.
3. **Near-Miss Analysis** — запросы с 1 ошибкой = быстрые победы. Починка топ-пар
   Confusion Matrix может перевернуть их в PASS.
4. **Similarity Distribution** — если >40% ошибок с `sim < 0.2`, проблема в разнице
   языков (LLM пишет по-английски, канонические имена русские), Jaccard тут не поможет.
5. **NO_MATCH / Resolution Errors** — чистые English-термины (`revenue`, `ending cash`,
   `D&A`), которые не резолвятся. Либо править LLM, либо добавлять синонимы.
6. **Field-Level Accuracy** — если одно поле стабильно ниже других (например,
   `input_names` хуже `output_names`), фокус доработки на нём.

---

## Диагностика name resolution (новое)

После фиксов loguru (commit: `logger.opt(exception=True).error(...)`) в `server.log` появились маркеры:

| Марккер | Где | Что означает |
|---------|-----|--------------|
| `Found output cell for ...` | `analyze_excel_model` | Успешный резолв: `alias` → `canonical` → `cell_ref` = `value` |
| `OUTPUT RESOLVED (modify) ...` | `modify_excel_input_value` | Резолв output перед `calculate()` |
| `Output cell ... returned None` (WARNING) | оба инструмента | Ячейка существует, но нет формулы (section header) — приведёт к `Unreachable output-targets` |
| `Unreachable output-targets` (ERROR) | `get_compiled_func` | Ячейка не в графе зависимостей — нет формулы |

Используйте `rg "Found output cell\|OUTPUT RESOLVED\|returned None\|Unreachable" server.log` для анализа.
