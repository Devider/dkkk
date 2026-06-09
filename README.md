# AI Gateway — Excel Cashflow Model Agent

FastAPI + LangGraph агент для сценарного анализа Excel-моделей движения денежных средств. Загружает `.xlsx`, через LLM определяет входные ячейки (цена сырья, объёмы и т.д.) и выходные показатели (Debt/EBITDA, выручка и т.д.), запускает сценарии «что-если» с пересчётом формул через LibreOffice.

## Быстрый старт

```bash
# Запуск (Docker Compose)
docker compose up -d

# Проверить, что сервис жив
curl http://localhost:8888/health
```

## Загрузка файла и анализ

```bash
# 1. Загрузить модель
curl -X POST http://localhost:8080/api/v1/upload \
  -F "file=@model.xlsx" \
  -H "x-trace-id: test1" \
  -H "x-client-id: test" \
  -H "x-session-id: s1" \
  -H "x-request-time: 2026-06-08T12:00:00Z"

# 2. Запустить анализ
curl -X POST http://localhost:8080/api/v1/invoke-agent \
  -H "Content-type: application/json" \
  -H "x-trace-id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "x-client-id: CI12345678" \
  -H "x-request-time: 2026-06-08T12:00:00Z" \
  -d '{"message": "Проанализируй model.xlsx со значением метанола 2025 (450,500) и шагом 5. Покажи мне значения debt/ebitda 2025 для этих значений."}'
```

## Тестирование руками

Проверить, что пересчёт формул работает (меняется Debt/EBITDA при разных ценах):

```bash
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

## Архитектура

- **FastAPI** + **LangGraph** — агент с циклами init → analyze → execute_tool → analyze → END
- **ExcelHandler** (`api/v1/excel_handler.py`) — openpyxl (чтение/запись) + LibreOffice (пересчёт формул)
  - Два Workbook: `_wb` (data_only=False, формулы) для сохранения, `_wbv` (data_only=True, значения) для чтения
  - `calculate()`: clear cached `<v>` + `fullCalcOnLoad=1` → LO round-trip XLSX→ODS→XLSX
- **Модель**: Ollama (qwen2.5:7b локально) или GigaChat (внешний API)
- **Обязательные заголовки**: `x-trace-id`, `x-client-id`, `x-request-time`

## Требования

- Docker + Docker Compose
- LibreOffice Calc (установлен в контейнере)

```bash
docker compose build --no-cache app   # после изменений в src/
docker compose restart app            # перезапуск
```
