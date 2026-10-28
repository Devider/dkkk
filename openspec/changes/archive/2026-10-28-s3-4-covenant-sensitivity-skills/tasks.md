# Tasks

## 1. Код `skill_required` и отклонение скрытого инструмента

- [x] 1.1 `ErrorCode.SKILL_REQUIRED` с подсказкой по умолчанию «прочитай skill через read_file и повтори вызов»; не в `TRANSIENT_CODES` (D1). Проверка: тест справочника — конверт с кодом создаётся, `hint` по умолчанию непустой, код не временный; существующий тест «у каждой ошибки есть код и подсказка» зелёный
- [x] 1.2 `api/v2/middleware/skill_gate.py` — `SkillToolGate(tool_skills: Mapping[str, str])` с `wrap_tool_call` / `awrap_tool_call`, и метод `ToolCatalog.skill_of()` (или отображение «инструмент → skill») (D1). Проверка: unit-тест — при `request.tool is None` и имени skill-инструмента возвращается `ToolMessage(status="error")` с `skill_required` и путём `/skills/<skill>/SKILL.md`; при прикреплённом инструменте и при имени вне каталога вызывается `handler`
- [x] 1.3 Подключить `SkillToolGate` в `AgentFactory.middleware()` сразу после `skills_middleware`, с комментарием о месте в стеке (D1). Проверка: тест порядка middleware в фабрике; без плана вызов скрытого инструмента получает `skill_required`, а не `plan_required`

## 2. Обход формул `excel_engine/dependencies.py`

- [x] 2.1 Функция `trace()` (D2): Tokenizer, раскрытие диапазонов, BFS с наименьшей глубиной, листья на листе `Inputs`, предел `MAX_TRACE_CELLS`, `truncated` / `skipped` (внешние книги, именованные диапазоны, целые столбцы, `#REF!`); без LangChain, типизирована. Проверка: `tests/unit/v2/test_trace_dependencies.py` на синтетической книге — глубина, диапазон `SUM`, межлистовые ссылки, цикл, повторные пути, обрезка, пропуски; тест границ (AR-Q3): модуль не импортирует LangChain и v1
- [x] 2.2 `cell_map(path)` — `IN/OUT → row`, `year → column` теми же `create_input_mapping` / `create_output_mapping`, что в `index_builder.py`; кэш книги с формулами и карты по `(path, mtime_ns, size)`, `lru_cache(maxsize=2)` (D3). Проверка: тест — ID и строки совпадают с `build_model_index` на синтетической книге; повторный вызов не открывает файл заново; изменение файла сбрасывает кэш

## 3. Инструмент `trace_dependencies`

- [x] 3.1 `api/v2/tools/dependencies.py`: схема `output_id`, `year`; предпроверки `missing_input` / `unknown_id` / `no_values` / `invalid_args` до открытия книги; конверт `result` / `data.inputs` / `data.outputs` / `numbers` (счётчики) / `notes` / `hint` (D3). Проверка: тесты — входы с названиями из индекса и глубиной, промежуточные выходы с ID, оговорка при обрезке, каждая ошибка с кодом и подсказкой, книга не открывается при ошибке аргументов, файл не меняется (mtime и хэш)
- [x] 3.2 Регистрация в `default_catalog()` с `availability="sensitivity-drivers"`, `uses_engine=False` (D3). Проверка: тест каталога — инструмент в `skill_tools()`, не в `base_tools()` и не в `engine_tool_names()`; модель до чтения видит 8 инструментов; `validate_skills` знает имя `trace_dependencies`
- [x] 3.3 Замер на `models/Methanex_FinModel.xlsx`: время первого и повторного вызова `trace_dependencies` для DSCR и EBITDA, число пройденных ячеек, `truncated`. Проверка: цифры записаны в раздел «Результаты» design.md; при обрезке на основных выходах предел пересматривается там же

## 4. Skill `covenant-headroom`

- [x] 4.1 `skills/covenant-headroom/references/covenants.md` (D4): показатель → смысл → направление → как читать запас; глоссарий; без числовых порогов. Проверка: тест — в файле есть DSCR, ICR, LLCR, Debt/EBITDA, Net Debt/EBITDA с направлением; в таблице порогов нет чисел (регулярное выражение по десятичным числам рядом с названиями показателей)
- [x] 4.2 `skills/covenant-headroom/SKILL.md` (D4): когда применять, шаблон плана в порядке §11, порог только из запроса (`blocked` + один вопрос), направление, база без стресса, нет года, порог не пересечён, разделы ответа, ссылка на `references/covenants.md`. Проверка: `validate_skills` проходит; тест — шаги шаблона в порядке §11, правила FR-10 для порога и года в теле, ≤ 300 строк
- [x] 4.3 `covenant-headroom/evals.yaml` (D6): CH1–CH5. Проверка: тест формата (общий с S3.3) — разбирается, `skill` и `tools` существуют
- [x] 4.4 Сквозные ходы U2 и «без порога» на скриптовой модели (D7). Проверка: `pytest tests/unit/v2/test_covenant_sensitivity_skills.py` — U2: `headroom` с порогом 1.2 из запроса, `crossing` по точкам сетки, `pct_change` до точки пробоя, все шаги `completed`, ответ не изменён `AnswerGroundingGuard`, `structure_issues` и `hygiene_issues` пусты; без порога: `derive` не вызывался, шаг `blocked`, ответ — вопрос без числа порога

## 5. Skill `sensitivity-drivers`

- [x] 5.1 `skills/sensitivity-drivers/SKILL.md` с `metadata.include_tools: trace_dependencies` (D5): ветка А (отбор драйверов, сетка из двух точек ±10 % на драйвер отдельным вызовом, `derive elasticity`, рейтинг, связи), ветка Б (выжимка или `get_values` + `trace_dependencies` + `pct_change`, без пересчёта), лимит расчётов, нет года, разделы ответа. Проверка: `validate_skills` проходит; тест — шаги ветки А в порядке §11, правило ±10 % с оговоркой, оговорка «не разложение вклада» в ветке Б, ≤ 300 строк
- [x] 5.2 `sensitivity-drivers/evals.yaml` (D6): SD1–SD5. Проверка: тест формата из 4.3 покрывает файл; SD1 и SD3 ожидают `trace_dependencies`
- [x] 5.3 Сквозные ходы на скриптовой модели (D7): скрытый инструмент (`skill_required` → `read_file` → успех; после сжатия снова `skill_required`; `write_file` — прежняя ошибка), рейтинг драйверов и «почему просел». Проверка: тесты зелёные; в «почему просел» расчётные инструменты не вызывались; эластичности и изменения в ответе проходят FR-13

## 6. Поставка и eval

- [x] 6.1 Тест состава поставки: пять skills валидны, все пути в системном промпте, до чтения 8 инструментов, после чтения `sensitivity-drivers` — 9; существующие тесты `test_skills` / `test_scenario_skills` обновлены под пять skills. Проверка: `pytest tests/unit/v2/test_skills.py tests/unit/v2/test_scenario_skills.py tests/unit/v2/test_covenant_sensitivity_skills.py`
- [x] 6.2 `scripts/eval/scenario_skills.py`: четыре сценарных skill и флаг `--skills` (D7). Проверка: `python scripts/eval/scenario_skills.py --scripted --skills covenant-headroom sensitivity-drivers` проходит без сети и пишет JSON в `scripts/eval/results/`
- [ ] 6.3 «В контуре»: живой прогон `scenario_skills.py` по `covenant-headroom` и `sensitivity-drivers` на GigaChat и Methanex. Итог — в «Результаты» design.md. Проверка: отчёт в design.md; без прогона сценарий «Свои eval-запросы проходят» на живой модели остаётся открытым до S5.1 и отмечается так в `openspec/traceability.md`

## 7. Качество

- [x] 7.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` + новые файлы). Проверка: новых замечаний нет, старые попутно не правятся
- [x] 7.2 `pylint src` > 7, `pytest tests/unit`, покрытие нового кода (`skill_gate.py`, `tools/dependencies.py`, `excel_engine/dependencies.py`) ≥ 80 %; новый код типизирован (AR-Q15). Проверка: оценка > 7, тесты зелёные, отчёт покрытия по трём модулям ≥ 80 %
