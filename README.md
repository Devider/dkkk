# AI Gateway — Excel Cashflow Model Agent

FastAPI + LangGraph агент для сценарного анализа Excel-моделей движения денежных средств.

Загружает `.xlsx`, через LLM (Ollama или GigaChat) определяет входные ячейки (цена сырья, объёмы и т.д.) и выходные показатели (Debt/EBITDA, выручка и т.д.), запускает сценарии «что-если» с пересчётом формул через LibreOffice.

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

Первый запуск скачает Ollama-образ и модель `qwen3-coder:30b` (~18 ГБ). Модель загружается автоматически из `docker.env` (`OLLAMA_MODEL`).

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

curl -s -X POST http://localhost:8888/api/v1/upload \
  -F "file=@model.xlsx" \
  -H "x-trace-id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "x-client-id: CI12345678" \
  -H "x-session-id: 550e8400-e29b-41d4-a716-446655440001" \
  -H "x-user-id: $U" \
  -H "x-request-time: 2026-06-08T12:00:00Z"
# → {"content":"Файл был успешно сохранен.","filename":"user_file_$U.xlsx","save_dir":"/tmp"}

# 2. Запустить анализ (ответ — ZIP-архив с txt_response.txt + generated/agent_output.xlsx)
curl -s -X POST http://localhost:8888/api/v1/invoke-agent \
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

Ранняя версия открывала книгу в `data_only=True` для чтения значений, а при `save()` теряла все формулы (`<f>`-элементы). После LO round-trip формулы исчезали, результаты были плоскими.

### Решение: двухкнижечный паттерн

Модуль держит **два** экземпляра openpyxl Workbook:

| Атрибут | `data_only` | Для чего |
|---|---|---|
| `self._wb` | `False` | Редактирование и **сохранение**. Все `set_cell()` и `save()` — только через этот экземпляр. Формулы сохраняются. |
| `self._wbv` | `True` | **Чтение значений**. Все `get_cell()`, `get_all_data()` — через этот экземпляр. |

`calculate()` делает так:

1. **`self.save()`** — вызывает `self._wb.save()` (формулы-книга). Формулы гарантированно сохраняются в XLSX.
2. **Закрывает** обе книги.
3. **`_clear_cached_formula_values()`** — zip-патч:
   - Устанавливает `fullCalcOnLoad=1` в `xl/workbook.xml`.
   - Удаляет `<v>` (кешированные значения) во всех ячейках, у которых есть `<f>` (формула).
   - LibreOffice при открытии видит: "нет кешированных значений, fullCalcOnLoad=1, надо пересчитать всё".
4. **LO round-trip: XLSX → ODS → XLSX**:
   - Копирует файл в `/tmp/model_lo_XXXXXX.xlsx`.
   - `libreoffice --headless --convert-to ods` — LibreOffice открывает и пересчитывает формулы.
   - `libreoffice --headless --convert-to 'xlsx:Calc MS Excel 2007 XML'` — сохраняет обратно с новыми значениями.
   - Копирует результат обратно в `self.file_path`.
   - На каждый вызов используется уникальный временный `-env:UserInstallation`, чтобы избежать блокировок профиля.
5. **Переоткрывает** `self._wbv` с `data_only=True` — теперь `get_cell()` возвращает свежевычисленные значения.

### Схема вызовов

```
calculate()
  ├─ save()                           # _wb (formulas) → xlsx
  ├─ close()
  ├─ _clear_cached_formula_values()   # zip-patch: fullCalcOnLoad=1, clear <v>
  ├─ LO: xlsx → ods                   # LibreOffice пересчитывает
  ├─ LO: ods → xlsx                   # LibreOffice записывает значения
  ├─ copy back
  └─ _open()                          # _wbv = open(data_only=True) — теперь читаем значения
```

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

- **Модель**: `MODEL_TO_USE=OLLAMA | GIGACHAT` (из `docker.env`). По умолчанию Ollama c `qwen3-coder:30b`.
- **Store**: `STORE_TO_USE=MEMORY | PANGOLIN`. По умолчанию `MEMORY` — хранение имени загруженного файла в памяти сохраняется только на время жизни контейнера.

## Тесты

```bash
# Запустить тесты (из виртуального окружения хоста, не из контейнера)
pytest tests

# Тесты используют httpx.AsyncClient с app=app_main (ASGI-транспорт, без реального сервера)
# Требуют GIGACHAT_HOST и GIGACHAT_PORT в pyproject.toml [tool.pytest.ini_options.env]
```

## Требования

- **Docker + Docker Compose**
- **LibreOffice Calc** — установлен в контейнере (`apt-get install libreoffice-calc`)
- **Ollama** — контейнер загружает `qwen3-coder:30b` при первом запуске
- Сеть: контейнеры должны быть в одной Docker-сети (`dkkk_copilot_default` создаётся автоматически)

## Разработка

```bash
# После изменений в src/ (кэш докер-слоёв):
docker compose build app --no-cache
docker compose up -d

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

3. **LibreOffice профиль блокируется** — каждый вызов `calculate()` использует уникальный `-env:UserInstallation`, поэтому параллельные вызовы не конфликтуют.

4. **Модель не найдена в Ollama** — при первом запуске docker compose может не успеть загрузить `qwen3-coder:30b`. Проверьте:
   ```bash
   docker compose exec ollama ollama list
   # если пусто — загрузите вручную:
   docker compose exec ollama ollama pull qwen3-coder:30b
   docker compose restart app
   ```
