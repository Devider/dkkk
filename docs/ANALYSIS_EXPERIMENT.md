# Experiment Analysis Script

Скрипт для сбора сквозной статистики по всем результатам прогона эксперимента.

## Назначение

Читает все `*_results*.csv` файлы из указанной подпапки `experiments/` и объединяет в единый отчёт со статистикой:

- Количество неизвестных (`unknown`) запросов
- Корректность классификации (`expected_class == actual_class`)
- **Perfect call rate** — корректная классификация + совпадение всех параметров
- Детальная статистика по каждому типу сценария
- Экспорт проблем в Excel (два листа: EMA и IFT)

## Использование

```bash
# Базовый вызов — только статистика
poetry run python scripts/explore_experiment.py full_run_real_models

# Без вывода ID провалившихся запросов
poetry run python scripts/explore_experiment.py full_run_real_models --no-mismatches

# Экспорт проблем в Excel (по умолчанию: experiments/<folder>/issues.xlsx)
poetry run python scripts/explore_experiment.py full_run_real_models --export-problems

# Экспорт с указанием пути
poetry run python scripts/explore_experiment.py full_run_real_models --export-problems --output /path/problems
```

**Параметры:**
| Аргумент | Описание |
|---|---|
| `folder` | Имя подпапки в `experiments/` (обязательный) |
| `--no-mismatches` | Скрыть списки ID провалившихся запросов |
| `--export-problems` | Экспортировать проблемные запросы в Excel (.xlsx) |
| `--output <path>` | Путь для выходного файла (по умолчанию: `experiments/<folder>/issues.xlsx`) |

## Экспорт проблем (--export-problems)

При использовании флага `--export-problems` создаётся Excel-файл с **двумя листами**:

| Лист | Содержимое | Префикс ID |
|---|---|---|
| `EMA` | analyze_excel_model — пересчёт модели при изменении входных параметров | `A001`, `A002`... |
| `IFT` | analyze_model_inputs_for_target — поиск входного параметра для целевого значения выхода | `T001`, `T011`... |

**Формат строк:**
- Каждая проблема — отдельная строка (например, у одной записи не совпали `inputs` и `outputs` → 2 строки)
- Типы проблем: `classification_mismatch`, `input_mismatch`, `output_mismatch`, `range_mismatch`, `step_mismatch`, `year_mismatch`, `output_value_mismatch`, `target_mismatch`
- Чистые IDs (без суффикса файла) — `A001`, `T011`
- Полные данные строки из оригинального CSV: `hash`, `problem_type`, `source_file`, `id`, `expected_class`, `actual_class`, `prompt`, `expected_inputs`, `actual_inputs`, `expected_outputs`, `actual_outputs`, `expected_year`, `actual_year`, `expected_ranges`, `actual_ranges`, `expected_steps`, `actual_steps`, `expected_output`, `actual_output`, `expected_target`, `actual_target`, `final_state`, `duration_sec`, `comment`

## Входные данные

Скрипт ищет файлы `*_results*.csv` в корне указанной папки:

```
experiments/full_run_real_models/
├── ema_2poj431skazt_2026-09-08_12-47-13_results.csv
├── ema_5ugust7zuuzn_2026-09-08_12-58-26_results.csv
├── ema_6m576vu02eud_2026-09-08_13-58-45_results.csv
├── ift_2poj431skazt_2026-09-08_12-54-57_results.csv
├── ift_5ugust7zuuzn_2026-09-08_13-02-24_results.csv
└── ift_6m576vu02eud_2026-09-08_14-05-03_results.csv
```

## Метрики

### Общая статистика

| Метрика | Описание |
|---|---|
| **Total queries** | Общее число строк (запросов) |
| **Ошибка обработки** | `actual_class == "unknown"` — ошибки обработки запроса |
| **Намеренный пропуск** | Строки с пустым `actual_class` и `comment`, содержащим `"Пропускаем:"` — тесты намеренно пропущены из-за превышения лимита (MAX_TEST_SCENARIOS / MAX_IFT_INPUTS) |
| **Correct classification** | `expected_class == actual_class` среди строк без `unknown` и без намеренного пропуска |
| **Incorrect classification** | `expected_class != actual_class` среди строк без `unknown` и без намеренного пропуска |

> **Важно:** Classification accuracy считается **только по валидным строкам** — исключаются `unknown` и строки с намеренным пропуском (actual_class пустой AND comment содержит `"Пропускаем:"`).

### analyze_excel_model (EMA)

Агент **пересчитывает модель** при изменении входных параметров.

| Метрика | Описание |
|---|---|
| **Perfect call rate** | Классификация верна + все параметры совпали целиком |
| **Classification rate** | Доля запросов с корректной классификацией |
| **Inputs/Outputs matched** | Запросы, где список inputs/outputs совпал целиком (каждый элемент = 1 параметр) |
| **Ranges matched** | Запросы, где все диапазоны `[start, end]` совпали |
| **Steps matched** | Запросы, где все шаги изменения совпали |
| **Year** | Запросы, где год совпал |

**Параметрическая статистика (внутри Total params):**

| Метрика | Описание |
|---|---|
| **Input params** | Количество совпавших элементов во всех списках inputs |
| **Output params** | Количество совпавших элементов во всех списках outputs |
| **Range params** | Каждый диапазон `[start, end]` = 2 параметра (start + end) |
| **Step params** | Каждый шаг = 1 параметр |
| **Year** | 1 параметр на запрос |

> **Важно:** параметрическая статистика считается **только по корректно классифицированным** запросам. Строки с неверной классификацией исключаются из метрик.

### analyze_model_inputs_for_target (IFT)

Агент **находит входной параметр для достижения целевого значения выхода**.

| Метрика | Описание |
|---|---|
| **Perfect call rate** | Классификация верна + совпали: inputs, output, target, year |
| **Classification rate** | Доля запросов с корректной классификацией |
| **Total params** | Совпавшие элементы: все элементы из списков inputs + output + target + year |

> В IFT output, target, year — одиночные значения (не списки), поэтому count = количеству корректно классифицированных запросов.

## Пример вывода

```
============================================================
📁 Experiment run: experiments/full_run_real_models/
📄 Files: 6 (ema_2poj431skazt_..., ema_5ugust7zuuzn_..., ...)
============================================================

📊 analyze_excel_model (45 queries)

  ✅ Perfect call rate:    17/45 (37.78%)
  ✅ Classification rate:   40/45 (88.89%)
  ✅ Total params:         462/570 (81.05%)
      Input params:       55/111 (49.55%)
      Output params:      75/117 (64.1%)
      Range params:       198/198 (100.0%)
      Step params:        94/99 (94.95%)
      Year:               40/45 (88.89%)
---
📊 analyze_model_inputs_for_target (50 queries)

  ✅ Perfect call rate:   39/50 (78.0%)
  ✅ Classification rate:  50/50 (100.0%)
  ✅ Total params:         189/201 (94.03%)
      Input params:       45/51 (88.24%)
      Output params:      44/50 (88.0%)
      Target:             50/50 (100.0%)
      Year:               50/50 (100.0%)
---
=== General ===

	Total queries:            100
	Ошибка обработки:          5 (actual_class == 'unknown')
	Намеренный пропуск:        5
	Correct classification:   80/95 (84.21%)
	Incorrect classification: 15/95 (15.79%)

---
=== Mismatch Details ===

  EMA Inputs mismatch: ['T011_2poj431skazt', 'T018_2poj431skazt', ...]
  EMA Outputs mismatch: ['T013_2poj431skazt', 'T014_2poj431skazt', ...]

---
💾 EMA sheet: 45 rows written

📊 EMA problem type distribution:
   input_mismatch: 18
   output_mismatch: 15
   classification_mismatch: 11
   range_mismatch: 1

💾 IFT sheet: 15 rows written

📊 IFT problem type distribution:
   output_value_mismatch: 9
   input_mismatch: 6

📊 Total: 60 problems (EMA: 45, IFT: 15)
💾 Saved to: experiments/full_run_real_models/issues.xlsx
```

## Структура вывода

Вывод разделён на четыре блока, каждый блок отделён строкой `---`:

1. **Заголовок** — путь к данным / папка эксперимента, список файлов
2. **Per-scenario статистика** — EMA и IFT, каждая с `Perfect call rate`, `Classification rate` и `Total params` (включая разбивку по типам параметров)
3. **General** — общий сводный блок: total, error processing, intentional skip, correct/incorrect classification
4. **Mismatch Details** — deferred вывод: metric mismatches по типам (Inputs/Outputs/Ranges/Steps/Year), сгруппированные по EMA/IFT

> **Примечание:** блока `Schema mismatch` в выводе **нет**. Строки с неверной классификацией (`expected_class != actual_class`) учитываются только в `Incorrect classification` в General, но не выводятся отдельно.

## Различия между метриками

В скрипте **закомментированы** строки вывода на уровне запросов (`Inputs matched: 29/45`, `Outputs matched: 31/45` и т.д.) — выводятся только параметрические метрики (`Total params`, `Input params`, `Output params` и т.д.).

Если раскомментировать эти строки, скрипт выведёт две шкалы совпадений для `analyze_excel_model`:

| Метрика | Масштаб | Пример |
|---|---|---|
| `Inputs matched: 29/45` | **Запросов** — сколько запросов имеют полностью совпадающий список | Из 45 запросов 29 полностью совпали по inputs |
| `Input params: 55/111` | **Параметров** — сколько отдельных элементов из всех списков inputs совпало | Из 111 элементов inputs во всех запросах 55 совпали |

Обе метрики полезны: первая показывает стабильность агента на уровне запроса, вторая — точность на уровне конкретных параметров.
