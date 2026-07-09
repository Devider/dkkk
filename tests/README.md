# Тесты AI Gateway

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

Conftest вызывает `APP_CTX.on_startup()`.

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
| `--log` | Путь к server.log |
| `--queries` | Excel-файл с тестовыми запросами |
| `--model` | Путь к .xlsx модели (для резолвинга имён) |
| `--subset N` | Запустить только первые N запросов |
| `--resume FILE` | Продолжить с JSON-чекпоинта |
| `--output FILE` | Сохранить результаты в JSON |
| `--timeout SEC` | Таймаут HTTP-запроса (по умолч. 600) |
| `--verbose` | Детальный вывод alias/resolved/expected по каждому полю |
| `--csv FILE` | Записать CSV с деталями сравнения |
| `--upload FILE` | Загрузить .xlsx на сервер перед тестами |

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
- `tool_query_results_<timestamp>.json` — полные результаты с `diffs`, `comparison` и `tool_stats`
- `comparison_dump.csv` — плоский CSV (если указан `--csv`)
