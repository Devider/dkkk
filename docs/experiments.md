# Experiment Procedures

## Overview

Script `scripts/run_experiments.py` orchestrates running tests across multiple Excel model files. It scans `real_models/` for `.xlsx` files, matches them with query files in `test_data/` by hash, and sequentially invokes `scripts/new_graph_test.py` for each (model, query) pair.

## Data Structure

Models and queries are linked by a **hash** embedded in their filenames:

| Resource | Path | Hash |
|---|---|---|
| Model | `real_models/2poj431skazt_depersonalized.xlsx` | `2poj431skazt` |
| EMA Queries | `test_data/analyze_excel_model_2poj431skazt_depersonalized.csv` | `2poj431skazt` |
| IFT Queries | `test_data/analyze_model_inputs_for_target_2poj431skazt_depersonalized.csv` | `2poj431skazt` |

Each model may have **two** query files: one for `analyze_excel_model` (EMA) and one for `analyze_model_inputs_for_target` (IFT). If a query file is missing, that type is skipped with a warning.

## Query Format

### EMA Queries (analyze_excel_model)

EMA-запросы в `expected_call (JSON)` содержат поля:

| Поле | Тип | Описание |
|---|---|---|
| `input_names` | `list[str]` | Список входных параметров |
| `output_names` | `list[str]` | Список выходных показателей |
| `year` | `int` | Год расчёта |
| `ranges` | `list[list[float]]` | Диапазоны для каждого input: `[[start, end], ...]` |
| `steps` | `list[float]` | Шаги для каждого input: `[step1, step2, ...]` |

**Пример:**
```json
{
  "input_names": ["Курс рубля к доллару", "Метанол", "Аммиак"],
  "output_names": ["Net Debt/EBITDA", "Debt/EBITDA"],
  "year": 2029,
  "ranges": [[88, 92], [360, 420], [450, 475]],
  "steps": [2, 20, 5]
}
```

### Scenario Limit

Для типа `analyze_excel_model` автоматически рассчитывается количество сценариев как **декартово произведение** сетки параметров:

```
n_values_i = ceil((end_i - start_i) / step_i) + 1
total_combinations = n_values_1 × n_values_2 × ... × n_values_N
```

**Пример:** `ranges=[[88, 92], [360, 420], [450, 475]]`, `steps=[2, 20, 5]`
- Параметр 0: `ceil((92-88)/2) + 1 = 3` значения (88, 90, 92)
- Параметр 1: `ceil((420-360)/20) + 1 = 4` значения (360, 380, 400, 420)
- Параметр 2: `ceil((475-450)/5) + 1 = 6` значений (450, 455, ..., 475)
- **Итого: 3 × 4 × 6 = 72 сценария**

Если `total_combinations > MAX_TEST_SCENARIOS` (константа = **1000**), тестовый кейс **пропускается** без вызова агента:
- В столбец `comment` записывается причина с детализацией
- В консоли выводится: `[SKIP] {id}: Пропускаем: {N} сценариев (макс 1000). Диапазоны: {name}: {n_values}, ...`
- В итоговой статистике показывается процент пропущенных тестов

**Цель ограничения:** предотвращение чрезмерно длительных вычислений при широких диапазонах.

### IFT Queries (analyze_model_inputs_for_target)

IFT-запросы в `expected_call (JSON)` содержат поля:

| Поле | Тип | Описание |
|---|---|---|
| `input_names` | `list[str]` | Список входных параметров для анализа |
| `output_name` | `str` | Целевой выходной показатель |
| `output_year` | `int` | Год целевого показателя |
| `target_value` | `float` | Целевое значение |

**Пример:**
```json
{
  "input_names": ["Курс рубля к доллару", "Метанол", "Аммиак", "Инфляция"],
  "output_name": "Debt/EBITDA",
  "output_year": 2028,
  "target_value": 1.85
}
```

### IFT Input Limit

Для типа `analyze_model_inputs_for_target` вводится ограничение на количество входных параметров:

```
MAX_IFT_INPUTS = 5
```

Если `len(input_names) > MAX_IFT_INPUTS`, тестовый кейс **пропускается** без вызова агента:

**Причина:** при N > 4 экспоненциальный рост декартова произведения. Инструмент `generate_scenarios()` делит `max_scenarios=1000` на N inputs:
- N=4: `1000^(1/4) ≈ 5` шагов → 625 комбинаций (полные)
- N=5: `1000^(1/5) ≈ 3→4` шагов → 1 024 комбинации (обрезка до 1000, потеря 2.4%)
- N=6: 4 шагов → 4 096 комбинаций (обрезка до 1000, потеря 75.6%)
- N=10: 4 шагов → 1 048 576 комбинаций (обрезка до 1000, потеря 99.9%)

При N > 4 инструмент теряет более 25% пространства поиска случайной выборкой, что может приводить к пропуску подходящих сценариев.

## Usage

```bash
# Full run across all models
python scripts/run_experiments.py --run-name my_baseline

# Run only first 2 models (quick test)
python scripts/run_experiments.py --run-name test_two --max-models 2

# Dry run — preview what would be done
python scripts/run_experiments.py --run-name dry --dry-run

# Custom directories
python scripts/run_experiments.py --run-name test \
    --models-dir /path/to/models \
    --queries-dir /path/to/queries

# Force overwrite
python scripts/run_experiments.py --run-name my_baseline --force
```

> **Note:** `--batch` was removed — `new_graph_test.py` now always runs sequentially with a single `AgentGraph` instance.

### Overwriting Existing Runs

If a directory `experiments/{run_name}/` already exists, the script will:

- **Interactive mode (terminal)**: display a warning with file count and last modification time, then prompt for a new name. Enter `'q'` to exit, or an empty string is rejected.
- **Non-interactive mode (pipe, CI)**: exit with an error immediately, suggesting `--force` or a different name.
- **`--force` flag**: silently overwrite the existing directory without prompting.

```bash
# Interactive — prompts for a new name
$ python scripts/run_experiments.py --run-name my_baseline
⚠ ВНИМАНИЕ: Папка прогона уже существует: experiments/my_baseline/
   Файлов: 15, последняя модификация: 2026-09-11 12:30:45
Пожалуйста, введите новое имя прогона (или 'q' для выхода):
> my_baseline_2

# Non-interactive (CI) — error without --force
$ echo "" | python scripts/run_experiments.py --run-name my_baseline
ОШИБКА: Папка прогона уже существует: experiments/my_baseline/
Используйте --force для перезаписи или укажите другое имя через --run-name.

# Force — overwrite existing directory
python scripts/run_experiments.py --run-name my_baseline --force
```

### CLI Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `--run-name` | Yes | — | Name of this experiment run (creates `experiments/{run_name}/`) |
| `--models-dir` | No | `real_models/` | Directory with `.xlsx` model files |
| `--queries-dir` | No | `test_data/` | Directory with CSV query files |
| `--output-dir` | No | `experiments/` | Base output directory |
| `--dry-run` | No | `False` | Preview only, do not execute |
| `--max-models` | No | All | Limit number of models to process |
| `--force` | No | `False` | Overwrite existing run directory without prompting |

### `new_graph_test.py` CLI Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `--queries` | No | `tests/data/Methanex_tool_test_queries.xlsx` | Path to Excel/CSV file with prompts |
| `--model` | No | `models/model.xlsx` | Path to the Excel model file |
| `--start` | No | `0` | Row index to skip |
| `--n` | No | `20` | Number of prompts to test |
| `--suffix` | No | Timestamp | Suffix for the output CSV filename |
| `--output-dir` | No | `tests/test_results` | Directory to save result CSVs |
| `--sleep` | No | `0` | Delay execution by N seconds (countdown every 30s) |

> **Note:** `--batch` was removed. `new_graph_test.py` now always runs sequentially with a single `AgentGraph` instance reused for all queries.

## Output Structure

```
experiments/{run_name}/
├── orchestrator.log            # Full log (timestamps + subprocess stdout/stderr)
├── 2poj431skazt_2026-09-07_18-33-58_unified_results.csv   # EMA results (hash + timestamp)
├── 2poj431skazt_2026-09-07_18-33-59_unified_results.csv   # IFT results (hash + timestamp)
├── ...
└── summary.csv
```

Each `new_graph_test.py` call writes exactly one CSV: `{hash}_{timestamp}_unified_results.csv`. EMA runs before IFT, so their timestamps differ. The `summary.csv` maps each hash to its EMA/IFT result files.

### `unified_results.csv` Columns

**Common columns:**

| Column | Description |
|---|---|
| `id` | Test case identifier |
| `prompt` | Original prompt text |
| `expected_class` | Expected agent type (`analyze_excel_model` / `analyze_model_inputs_for_target`) |
| `actual_class` | Classified agent type (null if skipped) |
| `duration_sec` | Execution time in seconds (0.0 if skipped) |
| `comment` | Error message or skip reason |
| `final_state` | Full agent state (JSON) |

**EMA-specific columns** (for `analyze_excel_model`):

| Column | Description |
|---|---|
| `expected_inputs` / `actual_inputs` | Expected / resolved input names |
| `expected_outputs` / `actual_outputs` | Expected / resolved output names |
| `expected_year` / `actual_year` | Expected / resolved year |
| `expected_ranges` / `actual_ranges` | Expected / actual value ranges |
| `expected_steps` / `actual_steps` | Expected / actual step values |

**IFT-specific columns** (for `analyze_model_inputs_for_target`):

| Column | Description |
|---|---|
| `expected_inputs` / `actual_inputs` | Expected / resolved input names |
| `expected_output` / `actual_output` | Expected / resolved output name |
| `expected_target` / `actual_target` | Expected / resolved target value |
| `expected_year` / `actual_year` | Expected / resolved output year |

**Skipped tests:** When a test case exceeds `MAX_TEST_SCENARIOS` (100 combinations), the row contains:
- `actual_class`: null
- `duration_sec`: 0.0
- `comment`: reason string (e.g., `"Пропускаем: 360 сценариев (макс 100). Диапазоны: param_0: 5, param_1: 4, ..."`)
- All scenario-specific columns: null

### `summary.csv` Columns

| Column | Description |
|---|---|
| `hash` | Model hash (unique identifier) |
| `model_file` | Original `.xlsx` filename |
| `ema_count` | Number of EMA test rows (null if skipped) |
| `ift_count` | Number of IFT test rows (null if skipped) |
| `ema_result_file` | Result CSV filename for EMA |
| `ift_result_file` | Result CSV filename for IFT |
| `total_duration_sec` | Total wall-clock time for this model |
| `status` | `ok` or `failed` |

## Logging

The `orchestrator.log` contains:
- **Config section** — directories, dry-run flag
- **Scan section** — discovered models and their query matches (EMA=YES/NO, IFT=YES/NO)
- **Execution section** — per-model tasks with timestamps
- **Per-type headers** — `[EMA]`, `[IFT]` sections for each model
- **Full subprocess output** — complete stdout/stderr from each `new_graph_test.py` invocation
- **Status lines** — `OK` (exit 0), `FAIL` (non-zero exit)

### Async streaming output

`run_experiments.py` uses **asyncio-based streaming** for subprocess I/O. Each `new_graph_test.py` run is executed via `asyncio.create_subprocess_exec` with `PYTHONUNBUFFERED=1` to ensure line-buffered Python output.

**Implementation details:**
- `stdout` and `stderr` are read **in parallel** via `asyncio.gather()` + `pipe.readline()`
- Each line is decoded and written to both the log file and console **immediately** (no buffering)
- Uses a 100 MB `limit` on StreamReader to prevent `LimitOverrunError` on long log lines
- Subprocess completes only when both pipes are exhausted (`await proc.wait()`)

**Benefit:** output appears in real-time on console and in `orchestrator.log` simultaneously — no delayed blocks of text after subprocess exits.

**Skip logging:** When tests are skipped, the subprocess output includes:

*EMA — too many scenarios:*
```
[SKIP] A002: Пропускаем: 360 сценариев (макс 100). Диапазоны: param_0: 5, param_1: 4, ...
```

*IFT — too many inputs:*
```
[SKIP] T003: Пропускаем: 5 input-параметров (макс 4). При N > 4 экспоненциальный рост декартова произведения.
```

At the end of each test run, a summary is printed:
```
Results saved to experiments/{run_name}/xxx_unified_results.csv (14 rows)
  ✅ Выполнено: 12 (86%)
  ⏭️  Пропущено: 2 (14%) — превышение MAX_TEST_SCENARIOS=1000

Пропущенные тесты:
  A002: Пропускаем: 360 сценариев (макс 1000). Диапазоны: ...
  T003: Пропускаем: 5 input-параметров (макс 5). При N > 4 ...
```

Log output is written to both the log file and the console simultaneously.

## Performance Notes

### Single `AgentGraph` per run

`new_graph_test.py` creates **one** `AgentGraph` instance at the start of the run and reuses it for all test cases. This avoids the overhead of compiling the graph and two `AnalyzerSubAgent` subgraphs (each with their own compiled LangGraph) for every single query.

**Before:** 20 queries → 20 `AgentGraph` instances → 20 graph compilations (negligible CPU time, but adds ~50–100 ms each).

**After:** 20 queries → 1 `AgentGraph` → 1 compilation → reused via `graph.ainvoke()`.

### No concurrent `soffice` processes

Each `analyze_excel_model()` / `analyze_model_inputs_for_target()` call spawns a dedicated headless `soffice` process via `ExcelWorkbook`. Concurrent spawns (parallel `--batch > 1`) cause:
- Port search conflicts — each `soffice` searches for a free port; concurrent searches lead to retries and delays.
- Zombie processes — `proc.wait()` may not clean up immediately; rapid restarts inherit stale state.
- RAM pressure — N concurrent `soffice` × ~2 GB each.

Sequential execution eliminates all three issues.

### LibreOffice spawn cost

| Phase | Time |
|---|---|
| `soffice` spawn | ~1.0 s |
| Open document + first `calculateAll()` | ~0.6 s |
| Per-scenario `set_value() → calculateAll() → read_block` | ~23–40 ms |

For a query with 100 scenarios: ~1.6 s overhead + ~2.3–4.0 s compute = **~4–6 s total**.

### `run_experiments.py` orchestrator

The orchestrator runs each model sequentially. For each model, EMA queries run first, then IFT. The `--batch 1` flag was removed from `_build_cmd()` since `new_graph_test.py` no longer supports parallelism.

## Execution Model

- **Sequential** — each model is processed one at a time, in discovery order
- **Within each model** — EMA runs first, then IFT
- **Within each `new_graph_test.py` run** — test cases execute sequentially with a **single `AgentGraph` instance** reused for all queries (avoids repeated graph compilation overhead)
- **No parallelism** — `--batch` argument removed; all queries run one-at-a-time to prevent concurrent `soffice`-process conflicts
- **Missing queries** — skipped with a warning, model continues to the next type
- **No resume** — interrupted runs must be restarted from scratch
- **Direct output** — `new_graph_test.py` writes results directly to `experiments/{run_name}/` (no copy step)

## Test Data

Current dataset in `real_models/`:

| # | Model | EMA | IFT |
|---|---|---|---|
| 1 | `2poj431skazt_depersonalized.xlsx` | YES | YES |
| 2 | `5ugust7zuuzn_depersonalized.xlsx` | YES | YES |
| 3 | `6m576vu02eud_depersonalized.xlsx` | YES | YES |
| 4 | `8qytmh5jcl4k_depersonalized.xlsx` | YES | YES |
| 5 | `aps9fws6luy6_depersonalized.xlsx` | YES | YES |
| 6 | `bfb7rf884u6f_depersonalized.xlsx` | YES | YES |
| 7 | `bjs67r6v2hvp_depersonalized.xlsx` | YES | YES |
| 8 | `cbf5o5iu8wcp_depersonalized.xlsx` | YES | YES |
| 9 | `dd861re5nwns_depersonalized.xlsx` | YES | YES |
| 10 | `e6lb2z4nr453_depersonalized.xlsx` | YES | YES |
| 11 | `jiyuh1wldwzo_depersonalized.xlsx` | YES | YES |
| 12 | `kgvb7lsf5m0w_depersonalized.xlsx` | YES | YES |
| 13 | `l52xxao4toba_depersonalized.xlsx` | YES | YES |
| 14 | `l591qk04p0uc_depersonalized.xlsx` | NO | YES |
| 15 | `o2nzj0yi2mcd_depersonalized.xlsx` | NO | YES |
| 16 | `v9dajerqdnrn_depersonalized.xlsx` | NO | YES |
| 17 | `wz262ll70gku_depersonalized.xlsx` | NO | YES |

**13 models** have both query types. **4 models** have IFT-only queries.

## Workflow

1. **Scan** — discover all `.xlsx` files in `real_models/` and `.csv` files in `test_data/`
2. **Match** — extract hash from filenames, pair models with their query files
3. **Execute** (for each model, sequentially):
   a. Copy model to `/tmp/` and write to InMemoryStore (via `new_graph_test.py`)
   b. Run EMA queries → `new_graph_test.py --output-dir experiments/{run_name}/`
   c. Run IFT queries → `new_graph_test.py --output-dir experiments/{run_name}/`
4. **Summarize** — generate `summary.csv` with per-model statistics
