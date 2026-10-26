# Tasks

> **Отложено** 2026-10-26: реализация варианта (б) откачена по решению разработчика, вернуться позже. Сейчас интеграции выключены (`PVS_ENABLED=False`, `IDP_ENABLED` по умолчанию `False`) — на работу сервиса не влияет.

## 1. Исправление

- [x] 1.1 Подтвердить выбор варианта (а или б из proposal.md) с разработчиком. Проверка: выбор записан в proposal.md
- [ ] 1.2 Импортировать `LangfuseClient` из `aigw_modules.hub_services.langfuse` в `context.py`. Проверка: `ruff check src/aigw_service/context.py` не показывает F821 для `LangfuseClient`
- [ ] 1.3 Реализовать выбранный вариант для `PlatformVSearch` и `IDPService` и добавить unit-тест: при `enabled=True` старт даёт ожидаемую ошибку, не `NameError`. Проверка: тест зелёный, `ruff check src/aigw_service/context.py` без F821

## 2. Качество

- [ ] 2.1 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [ ] 2.2 `pylint src` > 7 и `pytest tests/unit`. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
