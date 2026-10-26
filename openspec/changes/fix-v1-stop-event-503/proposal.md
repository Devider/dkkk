Bugfix (вне story) · найдено в spike S0.2 (`s0-2-gigachat-agent-spike`, проверка C8) · AR-Q11

## Why

При StopEvent GigaChat (HTTP 403 с телом «temporarily unavailable due to technical reasons») сервис должен отвечать HTTP 503: `router.py` ловит `StopEventError` и отдаёт 503. Но `context.py:_wrap_llm_with_stop_event`, которая превращает такой `ForbiddenError` в `StopEventError`, нигде не вызывается: `AppContext` создаёт `self.llm = Agent(...)` без обёртки. Поэтому сейчас StopEvent уходит наружу как `ForbiddenError` и клиент получает 500 вместо 503. Тесты `test_stop_event.py` проверяют саму функцию и этого не видят.

Spike C8 подтвердил, что обёртка работает, если её применить: `create_agent` и `bind_tools` вызывают `ainvoke` модели, и `StopEventError` выходит после первого вызова API.

## What Changes

- В `AppContext.__init__` оборачивать `self.llm` через `_wrap_llm_with_stop_event(self.llm, self.logger)`.
- Тест: StopEvent при вызове `APP_CTX.llm` (и через `bind_tools`) поднимает `StopEventError`; интеграционный — `/invoke-agent` отвечает 503 при подменённом ответе GigaChat.

**Вне рамок:** StopEvent в v2 — решён в spike S0.2 отдельным middleware `StopEventGuard` (S2.5).

## Capabilities

### New Capabilities

—

### Modified Capabilities

—

Восстановление задуманного поведения инфраструктуры v1, спецификации не меняются (`skip_specs: true`).

## Impact

- `src/aigw_service/context.py` — одна строка.
- `tests/unit/test_stop_event.py` или новый тест; `tests/integration/v1_api` — сценарий 503.
