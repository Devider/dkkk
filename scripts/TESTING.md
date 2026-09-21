# new_graph_test.py — Тестовый скрипт для AgentGraph

Скрипт для автоматизированного тестирования агента `AgentGraph` (модуль `aigw_service.api.v1.subagents.graph`).

## Назначение

Загружает набор промптов из Excel-файла, пропускает их через граф агента и сравнивает фактические результаты с ожидаемыми, сохраняя отчёт в CSV.

## Использование

```bash
python scripts/new_graph_test.py [аргументы]
```

## Аргументы

| Аргумент | По умолчанию | Описание |
|----------|-------------|----------|
| `--queries` | `tests/data/Methanex_tool_test_queries.xlsx` | Путь к Excel-файлу с промптами для тестирования |
| `--model` | `models/model.xlsx` | Путь к файлу модели для построения каталога доступных input/output |
| `--start` | `0` | Индекс первой тестируемой строки (пропускает предыдущие строки) |
| `--n` | `20` | Количество промптов для обработки (по одному на каждый лист Excel) |

## Примеры

```bash
# Тест 10 промптов, начиная с 5-й строки
python scripts/new_graph_test.py --n 10 --start 5

# Кастомные пути к файлам
python scripts/new_graph_test.py --queries tests/data/my_prompts.xlsx --model models/my_model.xlsx
```

## Структура входного Excel-файла

Лист `analyze_model_inputs_for_target` (в текущей версии — один лист):

| Столбец | Описание |
|---------|----------|
| ID | Идентификатор тестового кейса |
| Запрос (prompt) | Текст промпта для отправки агенту |
| expected_call (JSON) | JSON с ожидаемыми результатами: `input_names`, `output_name`, `target_value`, `output_year` |

## Что проверяет

Для каждого тестового кейса:

1. **classification_result** — результат классификации
2. **ift_resolved_inputs** — фактические input-имена vs ожидаемые (`expected.input_names`)
3. **ift_analyzer_results** — фактические output-параметры:
   - `output_name` vs `expected.output_name`
   - `target_value` vs `expected.target_value`
   - `output_year` vs `expected.output_year`
4. **duration_sec** — время выполнения (секунды)
5. **comment** — текст ошибки, если тест упал

Несоответствия между фактическими и ожидаемыми input/output группируются:
- `only_in_actual` — что агент нашёл, но не ожидалось
- `only_in_expected` — что ожидалось, но агент не нашёл

## Выходные данные

Результаты сохраняются в `tests/data/test_results/{timestamp}_results.csv` с кодировкой UTF-8.

## Зависимости

- `langchain-core` (`HumanMessage`)
- `pandas`
- `openpyxl`
- Внутренние модули: `aigw_service.api.v1.subagents.graph`, `aigw_service.api.v1.subagents.utils`