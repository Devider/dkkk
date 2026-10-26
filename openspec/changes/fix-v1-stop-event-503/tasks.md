# Tasks

## 1. Исправление

- [ ] 1.1 Обернуть `self.llm` в `AppContext.__init__` через `_wrap_llm_with_stop_event`. Проверка: новый unit-тест — `APP_CTX.llm.ainvoke` и `APP_CTX.llm.bind_tools(...).ainvoke` при подменённом `ForbiddenError` с маркером поднимают `StopEventError`
- [ ] 1.2 Интеграционный тест: `/api/v1/invoke-agent` при StopEvent отвечает 503 с текстом `user_message`. Проверка: тест зелёный

## 2. Качество

- [ ] 2.1 `ruff check --fix` и `ruff format` по изменённым и новым файлам. Проверка: новых замечаний нет
- [ ] 2.2 `pylint src` > 7 и `pytest tests/unit`. Проверка: оценка > 7, тесты зелёные (кроме устаревших `TestNoHardcodedRetry`)
