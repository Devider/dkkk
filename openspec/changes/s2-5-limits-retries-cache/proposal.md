Story S2.5 · FR-9, FR-17, AR-Q11 (владелец) · AR-Q5, FR-7, FR-8 (уточнение) · ADR-1, ADR-6 · §8 правила 5 и 8, стек №12–17 · §10 коды `engine_unavailable`, `limit_exceeded`, `internal_error` · §12 «Кэши» п. 6, «Отказоустойчивость» · spike S0.2 (`on_failure`), spike S0.3 (429)

## Why

У агента v2 нет ни одного предохранителя хода: зацикливающаяся модель вызывает инструменты и LLM без конца, временный сбой GigaChat или LibreOffice роняет ход целиком, а повтор того же расчёта снова запускает движок (~2 с на книгу). FR-9 требует лимитов на ход с частичным ответом, FR-17 — кэша повторного расчёта в сессии, AR-Q11 — повторов LLM и движка без повтора StopEvent. Без этого нельзя включать v2 на реальном трафике (S4.2 уже подключил v2 к роутеру).

## What Changes

- **Лимиты хода (FR-9).** Свой middleware `TurnLimits` считает вызовы модели, инструментов и расчётных инструментов (`uses_engine`) в пределах хода: по умолчанию 40 / 30 / 10, переменные `AGENT_MAX_MODEL_CALLS`, `AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_CALCS`. Счётчики сбрасываются каждый ход и не накапливаются между ходами. Вызов сверх лимита не выполняется — модель получает конверт `limit_exceeded`. После исчерпания любого лимита делается ровно один вызов модели без инструментов с просьбой дать частичный ответ по полученным результатам, и ход завершается.
- **Частичный ответ по плану (FR-8, уточнение).** При исчерпанном лимите `PlanCompletionGuard` не возвращает модель к работе: ответ принимается сразу с разделом «Не выполнено», открытые шаги закрываются как `skipped` с причиной «исчерпан лимит хода».
- **Повтор LLM (AR-Q11).** `ModelRetryMiddleware` повторяет вызов модели агента при сетевых ошибках, 5xx и 429 GigaChat: до `AGENT_LLM_RETRY_ATTEMPTS` (3) повторов с паузой `AGENT_LLM_RETRY_WAIT` (2 с) × 2. 403 и StopEvent не повторяются, исключение после последней попытки не превращается в текст ответа (`on_failure="error"`, spike S0.2).
- **Ошибки инструментов (FR-7, AR-Q11).** `ToolErrorMiddleware` переводит любое исключение инструмента в конверт `{error, code, hint}`; неизвестное — в `internal_error` без текста исключения. StopEvent сквозь него проходит до `StopEventGuard` (HTTP 503). Внутри — `ToolRetryMiddleware`: 1 повтор расчётного инструмента при `engine_unavailable` (пул движка к этому моменту пересоздан), затем модель получает `engine_unavailable` и пропускает шаг.
- **`CalcCache` (FR-17).** Успешный результат расчётного инструмента сохраняется в store на уровне сессии по ключу «пользователь + сессия, хэш файла, инструмент, нормализованные аргументы». Повтор с теми же аргументами в той же сессии отдаётся из кэша без запуска LibreOffice; ошибки не кэшируются; другая книга или другая сессия — промах.
- Порядок в `AgentFactory` по §8: … `ForcePlanFirst` → `TurnLimits` → `ModelRetryMiddleware` → `CalcCache` → `ToolErrorMiddleware` → `ToolRetryMiddleware` → `PlanCompletionGuard` → `TurnSummary`.
- Новые параметры — в `config/agent` и таблице README (AR-Q5).

**Вне рамок:** `Cancellation`, дедлайн хода `AGENT_TURN_TIMEOUT` и код `cancelled` (S2.8); частичный ответ после исчерпания повторов LLM (§12: «затем частичный ответ, если есть результаты, иначе 503») — сейчас ошибка уходит в роутер как раньше (HTTP 424), отдельная story; `AnswerGroundingGuard` (S4.1); метрики хода и `Telemetry` (S4.x); вынос больших результатов в `/results/` (S1.6); повтор 429 смыслового слоя — уже есть в `resolve_terms` (S1.3) и не меняется; v1 (свой LRU-кэш `tools.py` и дедупликация не трогаются); доля частичных ответов на реальной модели — eval S5.1.

## Capabilities

### New Capabilities

—

### Modified Capabilities

- `agent-loop`: добавляются FR-9 (лимиты хода и частичный ответ), FR-17 (кэш повторного расчёта в сессии) и AR-Q11 (повторы LLM и движка, StopEvent без повтора) — владелец S2.5.
- `agent-planning`: FR-8 — при исчерпанном лимите хода ответ принимается сразу с разделом «Не выполнено».
- `agent-tools`: FR-7 — исключение инструмента в ходе агента превращается в конверт через `ToolErrorMiddleware`, StopEvent проходит насквозь.
- `agent-platform`: AR-Q5 — новые параметры лимитов в `config/agent` и README.

## Impact

- Новые модули: `api/v2/middleware/limits.py` (`TurnLimits`), `api/v2/middleware/resilience.py` (сборка `ModelRetryMiddleware`, `ToolErrorMiddleware`, `ToolRetryMiddleware` и их предикаты), `api/v2/middleware/calc_cache.py` (`CalcCache`).
- Изменения: `factory.py` (стек), `middleware/plan.py` (`PlanCompletionGuard` и `ForcePlanFirst` при лимите), `middleware/stop_event.py` (чистый предикат StopEvent для обработчика ошибок), `tools/adapter.py` (`engine_unavailable` выходит исключением, чтобы его увидел повтор), `tools/envelope.py` (набор временных кодов), `prompts/middleware.py` (русские тексты лимита), `config/agent/config.py`, README.
- Тесты: новый `tests/unit/v2/test_limits.py`, `test_resilience.py`, `test_calc_cache.py`; обновляются проверки порядка стека в `test_plan.py`; `tests/unit/test_stop_event.py` остаётся зелёным.
- Зависимости не меняются: всё нужное есть в закреплённом `langchain==1.4.6`. Поведение v1 не меняется.
