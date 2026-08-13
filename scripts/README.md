# R&D: резолвинг имён параметров + каталог-инъекция

Скрипты для исследования качества резолвинга имён параметров инструментов
`analyze_excel_model` / `analyze_model_inputs_for_target` и эксперимента с
**каталог-инъекцией** в GigaChat. Прод-код (`tools.py`, `services.py`) не
затрагивается — правки только в скриптах и тестовых артефактах.

Направление исследования: `Jaccard` (45–55%) → `эмбеддинги` (70–71%) →
`hybrid` (потолок ~88.9%, oracle) → **каталог-инъекция** (79–81% на GigaChat,
без изменения резолвера).

---

## Скрипты

### `run_tool_queries.py` — прогон тестовых запросов через живой сервер

Проверяет, что LLM формирует корректные вызовы инструментов: шлёт запрос на
`/api/v1/invoke-agent` с уникальным `x-trace-id`, вытаскивает `TOOL ARGS` из
`server.log`, резолвит алиасы тем же Jaccard-пайплайном, что и прод, и
сравнивает с `expected_call` из `tests/data/Methanex_tool_test_queries.xlsx`.

```sh
# Сервер должен быть запущен: python3 src/aigw_service/__main__.py 2>&1 | tee server.log

# baseline (без каталога)
python scripts/run_tool_queries.py --url http://localhost:8080 --log server.log \
    --upload models/model.xlsx --subset 20 --output test_output/ab_baseline.json

# каталог-инъекция: каталог препендится к каждому сообщению
python scripts/run_tool_queries.py --url http://localhost:8080 --log server.log \
    --upload models/model.xlsx --subset 20 --catalog test_output/catalog.txt \
    --output test_output/ab_catalog.json
```

Ключевые флаги (полный список — `--help`): `--subset N` (N на лист → 2N запросов),
`--catalog FILE` (препенд каталога: `message = catalog + "\n\nЗапрос пользователя: " + prompt`),
`--output`, `--resume`, `--verbose`, `--csv`.

**Результат A/B (GigaChat, subset=20):**

| Метрика | Baseline (40) | + Каталог (40) | Δ |
|---|---|---|---|
| Per-param | 168/309 = **54.4%** | 245/309 = **79.3%** | **+24.9 pp** |
| Query-pass | 0/40 = **0%** | 17/40 = **42.5%** | +17 |

### `make_catalog.py` — генератор каталога доступных имён

Собирает из `models/model.xlsx` список канонических имён параметров: **503 имени**
(442 Inputs + 61 Outputs), только строки со значениями (строки-заголовки секций с
пустым `values` отсекаются — плановый фикс из AGENTS.md), разбитые на две секции
с инструкцией «используй ТОЛЬКО точные названия из списка, для input_names —
только Inputs, для output_names — только Outputs».

```sh
python scripts/make_catalog.py --model models/model.xlsx -o test_output/catalog.txt
```

> **ВНИМАНИЕ**: `catalog.txt` с 13.08.2026 правится вручную (добавлены блок ПРИМЕР
> с сигнатурами тулов и правила подстановки). Повторный запуск `make_catalog.py`
> перезапишет файл и **затрёт ручные правки**.

### `bench_resolution.py` — офлайн-бенчмарк резолверов

Сравнивает методы резолвинга без живого сервера: `jaccard` (прод-пайплайн as-is),
`embeddings` (bge-m3/Ollama по умолчанию, cosine по каноническим именам),
`hybrid` (эмбеддинги + LLM-fallback на неоднозначных). Тест-сеты: `xlsx`
(курируемые алиасы), `live` (реальные алиасы GigaChat из чекпоинта, см.
`extract_live_aliases.py`), `both`. Векторы и алиасы кэшируются на диск.

```sh
python scripts/bench_resolution.py --method jaccard --testset both
python scripts/bench_resolution.py --method embeddings --testset both \
    --emb-backend ollama --filter-headers
python scripts/bench_resolution.py --method hybrid --testset live --subset 20
python scripts/bench_resolution.py --compare bench_jaccard_xlsx.json bench_emb_xlsx.json
```

Измеренные результаты (60-query sample):
- Jaccard: 45.4% (xlsx) / 46.4% (live)
- Embeddings: 70.7% (xlsx) / 71.4% (live)
- Oracle-потолок (идеальный LLM): ~88.3–88.9% per-param при k=5, δ=0.15 —
  резолвер в одиночку до 95% не дотягивает; разрыв закрывает каталог-инъекция.

### `extract_live_aliases.py` — live-тестсет из чекпоинта

Из JSON-чекпоинта `run_tool_queries.py` извлекает реальные алиасы, которые LLM
подставлял в tool calls, и сохраняет как live-тестсет для `bench_resolution.py`.

```sh
python scripts/extract_live_aliases.py test_output/tool_query_results.json -o test_output/live_aliases.json
```

### `zond_catalog_prompt.py` — эксперимент catalog-prompt на локальной LLM

Одиночный LLM-вызов (без agent-цикла): промпт = каталог + запрос, сравнивает
выданные имена с эталоном. Валидировал концепцию каталога на слабой локальной
модели (`qwen3.6:35b-a3b`, n=30 → **96.7% per-param**; все ошибки — семейная
неоднозначность `ISCR (LTM)` vs `ISCR`, `Этилен` vs `Этилен, CFR Китай` и т.п.).

```sh
python scripts/zond_catalog_prompt.py --queries tests/data/Methanex_tool_test_queries.xlsx \
    --n 30 --llm-backend ollama --ollama-model qwen3.6:35b-a3b
```

### `analyze_results.py` — анализ результатов прогона

Принимает JSON из `run_tool_queries.py` и выводит 11 секций (Summary, Confusion
Matrix, NO_MATCH Aliases, Similarity Distribution и др.). Подробно описан в
`tests/README.md`.

```sh
python scripts/analyze_results.py test_output/tool_query_results.json --top-n 25 --csv analysis.csv
```

---

## Дамп промптов (как увидеть, что именно видит GigaChat)

Инструментация для просмотра полных промптов, уходящих в LLM:

1. `.env`: `DEBUG=true` → `log_lvl=DEBUG` и `full_message_print` (печать полного
   входящего body до маскировки).
2. `src/aigw_service/api/v1/services.py` (analyze_step): активные debug-логи
   `LLM MESSAGES SENT: [...]` (роль + content + tool_calls каждого сообщения —
   loguru-safe, плейсхолдер `{}`) и `LLM TOKEN USAGE: {...}`.
3. Перезапустить сервер, прогнать `run_tool_queries.py --subset 3 --catalog ...`.
4. Разобрать лог: строки `LLM MESSAGES SENT` → полный массив `[SystemMessage,
   HumanMessage(catalog+prompt), AIMessage(tool_calls), ToolMessage(results)]`.

**Находки по дампу:**
- Модель **эхом возвращает английские алиасы** из промпта (`methanol`, `Revenue`,
  `USD/RUB eop`) вместо точных каталог-имён (`Метанол`, `Выручка`, `Курс рубля…
  (eop USD/RUB)`). Каталог-правило лежит в HumanMessage, SystemMessage его не
  дублирует.
- Там, где модель копирует каталог-имя, она **отрезает закрывающую `)`**
  (`ICR (LTM`, `Карбамид (FOB Южный`) — Jaccard это прощает, но для exact-match
  это латентный риск.
- GigaChat кэширует длинный префикс (`precached_prompt_tokens` ~10K из ~10.5K) —
  повторные вызовы почти не платят за каталог.
- Баг (не промптовый): `analyze_model_inputs_for_target` может упасть с
  `float division by zero`.

**Дописывание каталога вручную** (ПРИМЕР с сигнатурами тулов + правила
подстановки) дало небольшое улучшение: 77.6% → 81.6% per-param (subset=3),
но English-эхо для target-тула сохранилось.

## Выводы и следующий шаг

- Каталог-инъекция — самый сильный рычаг без правки резолвера: **+25 pp per-param**,
  query-pass 0% → 42.5%.
- Каталога в HumanMessage недостаточно: GigaChat игнорирует его часть запросов.
  Планируемый шаг **A+B**: правило «дословная копия имён, включая скобки» в
  SystemMessage (`services.py`) + few-shot примеры «алиас → точное имя» в начале
  каталога.
- Для продуктивизации нужен подъём `max_length` сообщения: `schemas.py`
  `CopilotAgentRequest.message` `2000 → 60000` (уже сделан).

## Артефакты (`test_output/`)

- `catalog.txt` — каталог 503 имён (редактируется вручную).
- `ab_baseline.json`, `ab_catalog.json` — A/B GigaChat (subset=20).
- `prompt_dump.json`, `prompt_dump2.json` — прогоны дампа (subset=3).
- `gigachat_prompt_dump.md` — полный дамп промптов (SystemMessage + 6 запросов).
- `A-B_gigachat_2026-08-13.md` — отчёт A/B.
- `live_aliases.json`, `live_20260715/` — live-тестсет из июльского чекпоинта.
- `bench/` — результаты `bench_resolution.py`.
