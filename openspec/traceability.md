# Трассировка требований v2

Источник: `docs/Copilot cashflow analyst v2 project spec.md`.

**Владелец** — story, чья дельта добавляет требование (`ADDED`) в мастер-спеку capability.
**Также** — story, которые уточняют требование (`MODIFIED`) или проверяют его.
Мастер-спеки в `openspec/specs/` появляются по мере архивации story.

## Функциональные требования

| ID | Кратко | Capability | Владелец | Также | Статус |
| --- | --- | --- | --- | --- | --- |
| FR-1 | Книга и индекс из store / реестра; нет файла — ответ без LLM | agent-loop | S2.2 | S4.2 | S2.2 ✓, S4.2 ✓ (HTTP-контракт) |
| FR-2 | Только ru / en и тема модели; отказ без инструментов | agent-loop | S2.2 | — | S2.2 ✓ (тема — eval S5.1) |
| FR-3 | Декомпозиция в план 1–7 шагов | agent-planning | S2.3 | S5.1 | S2.3 ✓ (покрытие ≥ 90 % — eval S5.1) |
| FR-4 | Режимы плана `always` / `auto` / `off` | agent-planning | S2.3 | — | S2.3 ✓ |
| FR-5 | Статусы шагов плана | agent-planning | S2.4 | — | S2.4 ✓ |
| FR-6 | Явное сопоставление: расчёт только по ID индекса | agent-tools | S1.3 | S1.4 | S1.3 ✓ (входы −3 п. п. на IFT — наблюдение для S5.1) |
| FR-7 | Коррекция по `code` / `hint` | agent-tools | S1.2 | S5.1 | S1.2 ✓ (eval — S5.1) |
| FR-8 | Завершение: ответ не принимается при открытых шагах | agent-planning | S2.4 | — | S2.4 ✓ (доля доведённых планов — eval S5.1) |
| FR-9 | Лимиты на ход (`run_limit`) и частичный ответ | agent-loop | S2.5 | — | — |
| FR-10 | Недостающие данные → `blocked`, продолжение плана | agent-planning | S2.6 | S1.4 | S2.6 ✓ (вопрос только о недостающем — eval S5.1) |
| FR-11 | Недостижимая цель — результат, а не ошибка | agent-tools | S1.4 | — | S1.4 ✓ |
| FR-12 | Структура ответа | agent-answer | S3.2 | — | S3.2 ✓ (оценка аналитика ≥ 4 из 5 — eval S5.1) |
| FR-13 | Числа ответа только из источников | agent-answer | S4.1 | S1.7, S5.1 | — |
| FR-14 | Деловой русский, без кодов и traceback | agent-answer | S3.2 | — | S3.2 ✓ |
| FR-15 | Контракт API (ZIP) без изменений | agent-api | S4.2 | — | S4.2 ✓ (блокировка хода — в памяти процесса; дедлайн хода — S2.8) |
| FR-16 | Многоходовость через выжимки | agent-loop | S2.6 | — | S2.6 ✓ |
| FR-17 | Кэш повторного расчёта в сессии | agent-loop | S2.5 | — | — |
| FR-18 | Декларативные skills без правки кода | agent-skills | S3.1 | S3.3, S3.4 | S3.1 ✓ (skills первой волны — S3.3, S3.4) |
| FR-19 | Активация skill моделью или `AGENT_PINNED_SKILLS` | agent-skills | S3.5 | — | — |
| FR-20 | Переключатель `AGENT_ENGINE = v1 / v2` | agent-api | S2.1 | S4.2 | S2.1 ✓, S4.2 ✓ (`v1_api` на обоих движках) |

## Архитектурные требования

| ID | Кратко | Capability | Владелец | Также | Статус |
| --- | --- | --- | --- | --- | --- |
| AR-Q1 | GigaChat api_key / сертификаты / скриптовая модель | agent-platform | S2.1 | S0.2 | S2.1 ✓ (eval по режимам — S5.1) |
| AR-Q2 | Тесты без сети и LibreOffice | agent-eval | S5.2 | — | — |
| AR-Q3 | LangChain только в рантайме и адаптере | agent-tools | S1.1 | — | S1.1 ✓ |
| AR-Q4 | Зафиксированные версии, сборка из зеркала | agent-platform | S0.1 | — | S0.1 ✓ (сборка в контуре не проверена) |
| AR-Q5 | Параметры агента через `config/agent` | agent-platform | S2.1 | S2.5, S2.8, S3.5 | S2.1 ✓ |
| AR-Q6 | Бюджет контекста, вынос результатов | agent-tools | S1.6 | S2.7 | — |
| AR-Q7 | Изоляция пользователей | agent-tools | S1.6 | S4.2 | S4.2 ✓ (история на уровне API; `/results/` — S1.6) |
| AR-Q8 | Конкурентность движка | agent-tools | S1.5 | — | S1.5 ✓ (размер пула — по замеру S0.4) |
| AR-Q9 | Event loop не блокируется | agent-tools | S1.5 | S1.7 | S1.5 ✓ (чтения — S1.7) |
| AR-Q10 | Производительность, таймаут хода | agent-loop | S2.8 | S0.4, S5.3 | — |
| AR-Q11 | Повторы LLM и движка, StopEvent → 503 | agent-loop | S2.5 | S0.2, S4.2 | S4.2 ✓ (StopEvent → 503 в v2; повторы — S2.5; v1 — `fix-v1-stop-event-503`) |
| AR-Q12 | Отмена без сирот `soffice` | agent-loop | S2.8 | S1.5 | — |
| AR-Q13 | Журнал, Langfuse, `TOOL ARGS` | agent-api | S4.3 | — | — |
| AR-Q14 | Нет записи и `execute`; LangSmith запрещён | agent-skills | S3.1 | S4.3 | S3.1 ✓ (включая запрет LangSmith) |
| AR-Q15 | ruff, pylint, покрытие, русские промпты, валидация skills | agent-platform | S2.1 | S3.1 | S2.1 ✓, S3.1 ✓ |

## Story без дельты спек

| Story | Почему | Результат |
| --- | --- | --- |
| S0.2 | Spike | ✓ 2026-10-26: решения по ADR-3, ADR-9, ADR-10, StopEvent — `archive/2026-10-26-s0-2-gigachat-agent-spike/design.md`; режим сертификатов не проверен |
| S0.3 | Spike | ✓ 2026-10-26: решения по ADR-2, ADR-4, ADR-7, FR-5, FR-6, AR-Q15 — `archive/2026-10-26-s0-3-skills-resolve-spike/design.md` |
| S0.4 | Замер базовой линии v1 | Метрики и тестовая книга (`skip_specs`) |
| S0.5 | Каркас OpenSpec | `openspec/config.yaml`, этот файл; сделано без change |
| S0.6 | Инструменты разработки | MCP LangChain в `.mcp.json`, описание в CLAUDE.md / AGENTS.md (`skip_specs`) |
| S0.7 | Копия движка в `src/excel_engine/` | ✓ 2026-10-26: полное замыкание, AST-идентичность и паритет на Methanex (Docker) (`skip_specs`) |
| S5.4 | Удаление v1 | Чистка кода и документации (`skip_specs`) |
