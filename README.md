# AI Gateway — Excel Cashflow Model Agent

FastAPI + LangGraph агент для сценарного анализа Excel-моделей движения денежных средств.

Загружает `.xlsx`, через LLM (Ollama или GigaChat) определяет входные ячейки (цена сырья, объёмы и т.д.) и выходные показатели (Debt/EBITDA, выручка и т.д.), запускает сценарии «что-если» с пересчётом формул через `formualizer` (Rust in-memory).

## Быстрый старт

```bash
# Убедиться, что нет висящих старых контейнеров
docker compose down

# Запуск
docker compose up -d

# Проверить, что сервис жив
curl http://localhost:8888/health
# → {"status":"running"}
```

Первый запуск скачает Ollama-образ и модель `qwen2.5:7b` (~5 ГБ). Модель загружается автоматически из `docker.env` (`OLLAMA_MODEL`).

## Запуск через poetry (без Docker)

Приложение сконфигурировано под Poetry: `pyproject.toml` описывает console-script `aigw-rest-service-sh` → `aigw_service.__main__:main`.

```bash
poetry install
poetry run python -m aigw_service        # или
poetry run aigw-rest-service-sh
```

Нюансы:
- Нужен локально установленный LibreOffice с Python-мостом `python3-uno` — иначе не загрузится `lo_backend` (Excel-пересчёт работает через UNO).
- Модель для агента берётся из `MODEL_TO_USE` (по умолчанию `OLLAMA` → локальный Ollama на `localhost:11434`, нужен запущенный сервис).
- Порт по умолчанию — `8080` (настраивается в `.env` через `APP_PORT`), либо из переменных окружения.

## Port mapping

Сервис внутри контейнера всегда слушает на `:8080`. Проброс порта наружу задаётся в `.env`:

```bash
APP_PORT=8888
```

Стандартный `.env` в репозитории использует `8888`. Если нужно сменить порт:

```bash
echo "APP_PORT=9999" >> .env
docker compose up -d
```

> `.env` в `.gitignore` — при свежем клоне порт по умолчанию `8080`, если файла нет.

## Обязательные заголовки

API гейта требует пять заголовков, описанных в `src/aigw_service/api/v1/utils.py`:

| Header | Формат | Пример | Обязательный |
|---|---|---|---|
| `x-trace-id` | UUID v4 `^([0-9a-f]{8}-…){4}$` | `550e8400-e29b-41d4-a716-446655440000` | Да |
| `x-client-id` | `^[A-Z]{2}\d{8}$` — 2 буквы + 8 цифр | `CI12345678` | Да |
| `x-request-time` | RFC-3339 | `2026-06-08T12:00:00Z` | Да |
| `x-session-id` | UUID v4 (такой же как trace) | `550e8400-e29b-41d4-a716-446655440001` | Нет |
| `x-user-id` | Строка до 8 символов | `usr12345` | Нет (нужен для upload) |

Если заголовок не совпадает с regex — вернётся 422 с описанием ошибки.

## Загрузка файла и анализ

```bash
# 1. Загрузить модель (используйте правильный port, см. выше)
U="$USER"  # или любая строка до 8 символов; если имя >8 — замените вручную

curl -s -X POST http://localhost:8080/api/v1/upload \
  -F "file=@models/model.xlsx" \
  -H "x-trace-id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "x-client-id: CI12345678" \
  -H "x-session-id: 550e8400-e29b-41d4-a716-446655440001" \
  -H "x-user-id: $U" \
  -H "x-request-time: 2026-06-08T12:00:00Z"
# → {"content":"Файл был успешно сохранен.","filename":"user_file_$U.xlsx","save_dir":"/tmp"}

# 2. Запустить анализ (ответ — ZIP-архив с txt_response.txt + generated/agent_output.xlsx)
curl -s -X POST http://localhost:8080/api/v1/invoke-agent \
  -H "Content-Type: application/json" \
  -H "x-trace-id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "x-client-id: CI12345678" \
  -H "x-session-id: 550e8400-e29b-41d4-a716-446655440001" \
  -H "x-user-id: $U" \
  -H "x-request-time: 2026-06-08T12:00:00Z" \
  -d '{"message": "Проанализируй model.xlsx со значением метанола 2025 (450,500) и шагом 5. Покажи мне значения debt/ebitda 2025 для этих значений."}' \
  --output response.zip && unzip -p response.zip txt_response.txt
```

Пример вывода:
```
На основе анализа модели из файла `model.xlsx` для различных значений цены метанола
в 2025 году, получены следующие значения коэффициента долга к EBITDA (debt/ebitda):

- При цене метанола 450.0: debt/ebitda_2025 = 1.927
- При цене метанола 455.0: debt/ebitda_2025 = 1.900
...
- При цене метанола 500.0: debt/ebitda_2025 = 1.686
```

curl -s -X POST http://localhost:8080/api/v1/invoke-agent \
  -H "Content-Type: application/json" \
  -H "x-trace-id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "x-client-id: CI12345678" \
  -H "x-session-id: 550e8400-e29b-41d4-a716-446655440001" \
  -H "x-user-id: $U" \
  -H "x-request-time: 2026-06-08T12:00:00Z" \
  -d '{"message": "Проанализируй model.xlsx со значениями метанола 2025 (450, 500) с шагом 5 и инфляции USD CPI 2025 (0,1 , 0,2) с шагом 0,1. Покажи изменения debt/ebitda, net debt/ebitda (ltm) и icr corr (ltm) 2025 для таких значений"}' \
  --output response.zip && unzip -p response.zip txt_response.txt

## Тестирование руками (прямой вызов ExcelWorkbook)

Проверить, что пересчёт формул работает — меняется Debt/EBITDA при разных ценах метанола:

```bash
# Положите исходный файл в /tmp (через upload или напрямую)
docker compose exec app python3 -c "
import shutil
from aigw_service.api.v1.excel_handler import ExcelWorkbook

shutil.copy2('/tmp/user_file_usr12345.xlsx', '/tmp/t.xlsx')
with ExcelWorkbook('/tmp/t.xlsx') as xl:
    xl.set_cell('Inputs','AH340', 450); xl.calculate()
    print(f'450 -> {xl.get_cell(\"Outputs\",\"O69\"):.4f}')
    xl.set_cell('Inputs','AH340', 500); xl.calculate()
    print(f'500 -> {xl.get_cell(\"Outputs\",\"O69\"):.4f}')
"
```

Ожидаемый вывод:
```
450 -> 1.9270
500 -> 1.6860
```

Если значения одинаковые — значит, пересчёт не сработал (см. раздел ExcelHandler ниже).

## ExcelHandler — как работает пересчёт

`src/aigw_service/api/v1/excel_handler.py` — единственный модуль, через который агент читает и пишет Excel.

### Проблема: открытие с `data_only=True` ломает формулы

openpyxl умеет открывать книгу в двух режимах:

- `data_only=False` — читает **формулы** (XML-элементы `<f>`). `save()` пишет корректный XLSX.
- `data_only=True` — читает **кешированные значения** (XML-элементы `<v>`). `save()` пишет XLSX **без формул** — только значения, пересчитывать нечего.

Ранняя версия открывала книгу только в `data_only=True` для чтения значений, а при `save()` теряла все формулы — движок не мог пересчитать модель.

### Решение: двухкнижечный паттерн + formualizer

Модуль держит **два** экземпляра openpyxl Workbook:

| Атрибут | `data_only` | Для чего |
|---|---|---|
| `self._wb` | `False` | Редактирование и **сохранение**. Все `set_cell()` и `save()` — только через этот экземпляр. Формулы сохраняются. |
| `self._wbv` | `True` | **Чтение значений**. Все `get_cell()`, `get_all_data()` — через этот экземпляр. |

`calculate()` использует `formualizer.Workbook.evaluate_all()` — инкрементальный пересчёт dirty-подграфа **в памяти** (Rust-движок), без внешних процессов. Загрузка модели `load_workbook()` ~0.44 с; кэш не нужен (Workbook мутабельный, per-instance).

### Поток сценариев в `analyze_excel_model`

`api/v1/tools.py:analyze_excel_model` — главный инструмент агента для сценарного анализа:

```python
for scenario in combinations:
    with ExcelWorkbook("/tmp/copy_of_model.xlsx") as xl:
        # 1. Установить входные параметры
        xl.set_cell("Inputs", cell_ref, scenario_value)
        # 2. Пересчитать
        xl.calculate()
        # 3. Прочитать выходные показатели
        result = xl.get_cell("Outputs", output_cell_ref)
```

**Каждый сценарий работает с отдельной копией оригинального файла**, поэтому сценарии независимы и изменения не накапливаются.

## Параметры агента (`AGENT_*`)

Задаются переменными окружения (`.env`, `docker.env`), читаются `config/agent/config.py` (`AgentSettings`).
Переключение `AGENT_ENGINE` и `AGENT_MODEL` — переменная + перезапуск, без пересборки образа.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `AGENT_ENGINE` | `v1` | Движок агента: `v1` — граф `main_graph.py`, `v2` — `create_agent` (`api/v2/`) |
| `AGENT_MODEL` | `gigachat` | Модель v2: `gigachat` (api_key или сертификаты — по `LOCAL` и `GIGACHAT_*`) или `scripted` |
| `AGENT_SCRIPT_PATH` | — | JSON-сценарий скриптовой модели: `[{"tool": "имя", "args": {...}} \| {"content": "текст"}]` |
| `AGENT_PREVIEW_CHECK` | `False` | v2: фоновая A/B-проверка — доля вызовов модели агента повторяется на `<GIGACHAT_MODEL>-preview`, ответы сравниваются (вызовы инструментов — по именам и аргументам, тексты — LLM-судьёй), итог — строка журнала `PREVIEW CHECK`. Ответ и время хода не меняются. Только при `AGENT_MODEL=gigachat`; preview-модель есть только во внутреннем контуре. Независима от `ENABLE_PREVIEW_MODEL_CHECK` v1 |
| `AGENT_PREVIEW_CHECK_PERCENT` | `0.05` | v2: доля проверяемых вызовов модели, [0, 1]; вне интервала — ошибка при старте |
| `AGENT_STREAM_HEARTBEAT` | `15` | v2 и v1: интервал комментария SSE `: ping` в `/invoke-agent/stream`, с, пока нет событий (прокси не закрывает соединение во время долгого расчёта). Не больше 0 — ошибка при старте |
| `AGENT_LLM_RETRY_ATTEMPTS` | `3` | v2: повторы вызова модели агента при сетевых ошибках, 5xx и 429; повторы 429 смыслового слоя (`resolve_terms`) |
| `AGENT_LLM_RETRY_WAIT` | `2.0` | v2: первая пауза между повторами, с (дальше удваивается) |
| `AGENT_MAX_MODEL_CALLS` | `40` | v2: вызовов модели за ход, включая последний вызов для частичного ответа |
| `AGENT_MAX_TOOL_CALLS` | `30` | v2: вызовов инструментов за ход; сверх лимита — `limit_exceeded` и частичный ответ |
| `AGENT_MAX_CALCS` | `10` | v2: вызовов расчётных инструментов за ход (включая отданные из кэша) |
| `AGENT_TURN_TIMEOUT` | `300` | v2: время хода, с; затем частичный ответ. Идущий вызов не прерывается, поэтому ход может длиться до `AGENT_TURN_TIMEOUT` + `AGENT_CALC_TIMEOUT` + один вызов LLM |
| `AGENT_MAX_ITERATIONS` | `10` | v1: лимит итераций оркестратора |
| `AGENT_MAX_MESSAGES` | `30` | v1: лимит сообщений в истории |
| `AGENT_MAX_PROMPT_TOKENS` | `30000` | v2: бюджет запроса к модели, токенов (приближённый счёт); от него считаются пороги сжатия истории, размер слоёв пишется в журнал строкой `CONTEXT LAYERS`. Меньше 20000 — ошибка при старте: запрос без истории уже занимает около 5,6 тыс. токенов |
| `AGENT_SUMMARY_TRIGGER` | `0.7` | v2: доля бюджета, при которой старая часть истории заменяется в запросе русской сводкой; полная история — в `/results/conversation_history/` |
| `AGENT_SUMMARY_KEEP` | `0.3` | v2: доля бюджета — свежий хвост истории, который остаётся дословно. Должна быть меньше `AGENT_SUMMARY_TRIGGER` |
| `AGENT_PLAN_MODE` | `auto` | v2: режим плана — `auto` (модель решает, расчёт без плана → `plan_required`), `always` (план в каждом ходе, для eval и отладки), `off` |
| `AGENT_PLAN_MAX_RETURNS` | `3` | v2: сколько раз ответ при открытых шагах плана возвращается модели; затем ответ с разделом «Не выполнено» |
| `AGENT_GROUNDING_MAX_RETURNS` | `1` | v2: сколько раз ответ с числами, которых нет в результатах и сообщениях пользователя, возвращается модели; затем такие числа заменяются на «н/д» с оговоркой. `0` — сразу замена |
| `AGENT_PINNED_SKILLS` | `economic-answer` | v2: skills, закреплённые на все ходы, — имена через запятую, пустая строка — ничего не закреплять. Текст закреплённого skill всегда в контексте, его инструменты открыты без `read_file`. Неизвестное имя останавливает старт, как и закреплённые тексты, которые вместе не помещаются в дословный хвост `AGENT_SUMMARY_KEEP` × `AGENT_MAX_PROMPT_TOKENS` (по умолчанию 9 000 токенов: два-три skill, но не все пять). Без `economic-answer` структура ответа FR-12 не гарантируется (предупреждение в журнале). Каждый skill расходует бюджет `AGENT_MAX_PROMPT_TOKENS` (слой `skill_text` в `CONTEXT LAYERS`) |
| `AGENT_MAX_GRID` | `400` | v2: максимум сценариев в сетке `run_scenarios` (проверка до движка) |
| `AGENT_RESULT_MAX_CHARS` | `1500` | v2: порог размера результата инструмента (символы JSON); длиннее — полный результат в `/results/`, модели — сводка, числа и `ref`. Меньше 500 — ошибка при старте |
| `AGENT_CALC_WORKERS` | `2` | v2: размер пула процессов LibreOffice на сервис |
| `AGENT_CALC_TIMEOUT` | `120` | v2: таймаут одного расчёта, с; дальше — `engine_timeout` и пересоздание пула |
| `AGENT_DUP_TOOL_THRESHOLD` | `3` | v1: порог повторов одинакового вызова инструмента |
| `AGENT_STOP_ON_CRITICAL` | `True` | StopEvent GigaChat → HTTP 503 |
| `AGENT_STOP_ON_ERROR` | `False` | Останавливать ход на любой ошибке LLM |

## Поток: журнал и метрики

`/invoke-agent/stream` пишет на каждый поток одну строку журнала `STREAM {json}`. Строка пишется после последнего кадра, а если клиент ушёл, — в момент окончания потока. Она описывает то, что получил клиент. Итог хода после разрыва — в `TURN SUMMARY`, строки связываются по `turn_id`. Тексты событий, план и термины в строку не попадают.

| Поле | Значение |
| --- | --- |
| `trace_id` | `x-trace-id` запроса |
| `turn_id` | ID хода; `null` для v1 и для хода, не дошедшего до начала (отказ «загрузите модель») |
| `engine` | `v1` / `v2` |
| `first_event_ms` | от приёма запроса endpoint до первого события, мс (ping не считается) |
| `duration_ms` | длительность потока, мс |
| `events` | число отданных событий по типам; кадр, на котором клиент оборвал соединение, тоже считается |
| `pings` | число комментариев `: ping` |
| `outcome` | `full` / `partial` / `clarify` / `refusal` (из `answer`) или `unavailable` / `failed` (из `error`); `null`, если клиент ушёл до терминального события |
| `disconnected` | `true`, если терминального события не было |

Метрики (через тот же механизм, что метрики журнала запросов):

| Метрика | Значение |
| --- | --- |
| `aigw_service_streams_total` | 1 на каждый поток |
| `aigw_service_stream_first_event_ms` | `first_event_ms`, если было хоть одно событие |
| `aigw_service_stream_disconnects_total` | 1, если клиент ушёл до терминального события |
| `aigw_service_stream_errors_total` | 1, если поток закончился `error` |

Записи и метрики журнала запросов для потока прежние: успех считается по HTTP-статусу, а он всегда 200. В Langfuse у трассы потокового хода есть тег `stream=true` рядом с `agent_engine=<v1|v2>`. Схема событий `v = 1` меняется только добавочно. Контрактный тест `tests/unit/v2/test_stream_contract.py` сверяет её со слепком `tests/unit/v2/contracts/stream_events_v1.json` и с `openapi_copilot.yaml`. Новое необязательное поле или новый тип события записывается в слепок командой `UPDATE_STREAM_CONTRACT=1 pytest tests/unit/v2/test_stream_contract.py`.

## Архитектура

```
HTTP POST /api/v1/upload
  └─ сохраняет файл в /tmp/user_file_{user_id}.xlsx
  └─ сохраняет имя файла в store (InMemoryStore или Pangolin)

HTTP POST /api/v1/invoke-agent
  └─ LangGraph Agent
       ├─ init_node — приветствие
       ├─ analyze_step — LLM определяет, какой инструмент вызвать
       │    ├─ Ищет файл: /tmp/{file_name} → get_store_file(user_id)
       │    └─ Вызывает analyze_excel_model (главный инструмент)
       └─ execute_tool — ExcelWorkbook.set_cell() → calculate() → get_cell()
  └─ Ответ: ZIP (txt_response.txt + generated/agent_output.xlsx)
```

### Компоненты

| Файл | Назначение |
|---|---|
| `api/v1/excel_handler.py` | ExcelWorkbook — openpyxl + LO recalc, двухкнижечный паттерн |
| `api/v1/services.py` | LangGraph агент (init → analyze → execute_tool → ... → END) |
| `api/v1/tools.py` | Инструменты агента: `analyze_excel_model`, `get_output_info` и др. |
| `api/v1/routers.py` | FastAPI роуты: `/upload`, `/invoke-agent` |
| `api/v1/utils.py` | Headers validation (x-trace-id и др.) |
| `api/v1/schemas.py` | Pydantic модели запросов/ответов |
| `context.py` | Singleton AppContext — логгер, LLM, agent memory |
| `logger/` | Loguru — консоль + файлы, маскирование K2-данных |

### Бекенды

- **Модель**: `MODEL_TO_USE=OLLAMA | GIGACHAT` (из `docker.env`). По умолчанию Ollama c `qwen2.5:7b`.
- **Store**: `STORE_TO_USE=MEMORY | PANGOLIN`. По умолчанию `MEMORY` — хранение имени загруженного файла в памяти сохраняется только на время жизни контейнера.

## Тесты

```bash
pytest tests
pytest tests/test_tools_performance.py -v -s --no-header --no-cov  # ~2.5 min
```

Подробнее о тестах, валидации вызовов инструментов и формате вывода — [tests/README.md](tests/README.md).

## Требования

- **Docker + Docker Compose**
- **formualizer** (Rust in-memory) — быстрая загрузка (~0.5 s) и пересчёт без внешних программ
- **Ollama** — контейнер загружает `qwen2.5:7b` при первом запуске
- Сеть: контейнеры должны быть в одной Docker-сети (`dkkk_copilot_default` создаётся автоматически)

## Разработка

```bash
# После изменений в src/ (кэш докер-слоёв):
docker compose build app --no-cache
docker compose up -d

# Сборка в закрытом контуре — зависимости из внутреннего зеркала PyPI
# (ставятся строго по poetry.lock с проверкой хэшей; без переменной — публичный PyPI):
PIP_INDEX_URL=<url зеркала>/simple docker compose build app --no-cache

# Быстрый рестарт (без пересборки):
docker compose restart app

# Логи:
docker compose logs app -f     # смотреть живые логи
docker compose logs app --tail 30  # последние 30 строк

# Остановка и очистка:
docker compose down
docker compose down -v          # с удалением томов (Ollama модель тоже удалится!)
```

## Известные проблемы

1. **Тома persist кешированные файлы** — `app_tmp` сохраняет предыдущие XLSX. Если файлы были повреждены (формулы стёрты), нужно перезагрузить оригинал через `/upload`. При `docker compose down` том **не удаляется** — удаляйте вручную `docker volume rm dkkk_copilot_app_tmp` только при необходимости.

2. **Старые Docker-сети** — если несколько стеков (`dkkk`, `dkkk_copilot`) создают сети, DNS между контейнерами может не работать. Решение: `docker compose down` для каждого стека, затем `docker network prune`.

3. **Модель не найдена в Ollama** — при первом запуске docker compose может не успеть загрузить `qwen2.5:7b`. Проверьте:
   ```bash
   docker compose exec ollama ollama list
   # если пусто — загрузите вручную:
   docker compose exec ollama ollama pull qwen2.5:7b
   docker compose restart app
   ```

4. **Качество резолвинга имён** — LLM передаёт английские алиасы ("copper (LME)", "net income"), канонические имена в Excel русские ("Медь (London Metals Exchange)", "Чистая прибыль"). Jaccard similarity = 0 на разных алфавитах. Текущая точность: ~55% per-param (тесты `run_tool_queries.py`). Пути решения: мультиязычные эмбеддинги, EN→RU словарь в `normalize_text`, LLM-перевод алиасов. См. анализ в `scripts/analyze_results.py`.
