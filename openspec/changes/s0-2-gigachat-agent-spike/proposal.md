Story S0.2 · AR-Q1, AR-Q11 · ADR-3, ADR-9, ADR-10 · риск §16 «совместимость GigaChat с механикой `create_agent`»

## Why

Вся разработка E1–E4 опирается на то, что GigaChat через `langchain-gigachat` работает внутри `create_agent` со стеком middleware: принудительный вызов `write_todos`, смена набора инструментов при открытии skill, повтор и StopEvent, длинный контекст с финальным ответом. Это главный риск проекта (§16). Spike проверяет его на живом GigaChat в обоих режимах подключения до начала основной разработки и фиксирует решения по ADR-9 и ADR-10.

Уже видно по коду (spike подтверждает вживую):
- `langchain-gigachat` 0.5.1 не поддерживает `tool_choice="any"` (ошибка или откат на `auto`); принудительно можно вызвать только конкретный инструмент по имени.
- Несколько tool calls в одном `AIMessage` библиотека не сериализует (`ValueError`): GigaChat — один вызов функции за ход.
- `create_agent` работает с результатом `bind_tools`, поэтому переопределённые `Agent.ainvoke` / `invoke` (A/B preview-модели) на этом пути, вероятно, не вызываются.
- `context.py:_wrap_llm_with_stop_event` нигде не вызывается: в v1 StopEvent GigaChat сейчас не превращается в HTTP 503.

## What Changes

- Одноразовый стенд `scripts/spikes/s0_2_gigachat/`: `create_agent` с GigaChat из `config/gigachat` и фиктивными инструментами; по одной проверке на пункт списка §12 плюс StopEvent, A/B и длинный контекст. Проверки запускаются одной командой и пишут JSON-отчёт.
- Отчёт и решения — в `design.md` этого change: результат по каждому пункту (работает / не работает / работает с обходом), решения по ADR-9 и ADR-10, решение по месту проверки StopEvent.
- Правки документа спецификации по итогам: §8 п. 2 (`ForcePlanFirst`), §12 (несколько вызовов, StopEvent), §16, ADR-9 / ADR-10.

**Вне рамок:** код в `src/` (стенд не импортируется приложением и не входит в CI); исправление StopEvent в v1 (заводится отдельным bugfix, если подтвердится); `ScriptedChatModel` (S2.1).

## Capabilities

### New Capabilities

—

### Modified Capabilities

—

Spike не меняет поведение системы (`skip_specs: true`); требования AR-Q1 и AR-Q11 добавит S2.1 / S2.5 по итогам spike.

## Impact

- Новый каталог `scripts/spikes/s0_2_gigachat/` (стенд, не часть приложения).
- `docs/Copilot cashflow analyst v2 project spec.md` — уточнения по итогам.
- Вызовы живого GigaChat по api_key (локально) и по сертификатам (в контуре, выполняет разработчик).
