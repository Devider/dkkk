# Tasks — S1.6 Вынос результатов в `/results`

## 1. Параметр порога (AR-Q5)

- [x] 1.1 Добавить `result_max_chars: int = Field(default=1500, ge=500, validation_alias="AGENT_RESULT_MAX_CHARS")` в `config/agent/config.py` и строку в таблицу параметров README; проверить тестом в `tests/unit/v2/` (или рядом с существующими тестами настроек), что значение по умолчанию 1500, переменная окружения переопределяет его, а 499 даёт ошибку проверки

## 2. Модуль выноса (AR-Q6, AR-Q7; design D2, D4, D5)

- [x] 2.1 Создать `api/v2/tools/offload.py`: `results_namespace(user_id)`, `result_path(turn_id, call_id)` с заменой символов вне `[A-Za-z0-9_-]`, `compact(envelope, ref, limit)` по D2 (без `data`, урезание `numbers` с пометкой в `notes`, затем обрезка `result`, финальная проверка длины) и `async offload(envelope, *, store, user_id, turn_id, call_id, limit) -> ToolSuccess` — запись через `StoreBackend.awrite` с JSON `indent=1`, сбой записи по D5. Типизация полная; проверить `tests/unit/v2/test_offload.py`: короткий конверт без изменений и без записи; длинный — запись, `ref`, нет `data`, длина `to_json` ≤ порога на нескольких порогах (500, 1500, 5000) и на 400-точечной сетке; урезанные `numbers` с пометкой; сбой store — без `ref` с пометкой, без исключения
- [x] 2.2 Перевести `middleware/files.py` на `offload.results_namespace`, чтобы запись и чтение брали одно пространство; `tests/unit/v2/test_skills.py` остаётся зелёным

## 3. `step_results` и адаптер (design D6)

- [x] 3.1 Добавить reducer `merge_steps` (дописывает; `{RESET: True}` первым элементом сбрасывает) и аннотацию `step_results` в `tools/state.py`; `TurnContext` пишет `[{RESET: True}]`; убрать дублирующее объявление из `state.py`, если оно конфликтует с аннотацией. Проверить юнит-тестом reducer'а (дописывание двух параллельных обновлений, сброс) и `test_turn_context.py`
- [x] 3.2 В `tools/adapter.py:build_tool` после конверта вызывать `offload` для `ToolSuccess` (store — `runtime.store`, `turn_id` — `runtime.state`, порог — `APP_CONFIG.agent.result_max_chars`) и всегда возвращать `Command` с `ToolMessage`, записью `step_results` (полные `numbers`) и `state_update`; `ToolFailure` — без выноса, с записью `status`/`code`. Обновить `test_tool_adapter.py`: запись шага для успеха и ошибки, полные `numbers` в шаге при урезанных в сообщении, `resolved_terms` по-прежнему обновляется, транзитная ошибка по-прежнему бросает `ToolError`
- [x] 3.3 Передать `store=` в `create_agent` в `AgentFactory.build` (тот же объект, что у бэкенда и `CalcCache`); проверить, что `runtime.store` в инструменте не `None` в собранном агенте (тест в `test_runtime.py` или `test_offload.py`)

## 4. Порядок чисел сетки (design D3)

- [x] 4.1 В `tools/calc_tools.py:scenarios_envelope` ставить в начало `numbers` минимум и максимум каждого выхода с подписью сценария, затем остальные значения без повторов; проверить тестом на сетке из нескольких выходов, что первые `2 × число выходов` чисел — экстремумы, а множество всех чисел не изменилось (`test_tool_adapter.py` или тест конверта)

## 5. Кэш и выжимка (design D7, D8)

- [x] 5.1 `CalcCache`: кэшировать `Command`, в обновлении которого только `messages` (один успешный `ToolMessage`) и `step_results`; хранить `{tool, content, step}`; попадание возвращает `Command` с новым `tool_call_id` и копией шага с новым `call_id` и прежним `ref`; запись без `step` даёт только `ToolMessage`. Обновить `test_calc_cache.py`: второй идентичный вызов не запускает движок, получает тот же `ref`, шаг попадает в `step_results`; ошибка по-прежнему не кэшируется
- [x] 5.2 `TurnSummary._result`: переносить `ref` из конверта в результат выжимки; обновить docstring (источник — `ToolMessage`, `ref` для дочитывания); проверить в `test_memory.py`, что выжимка хода с вынесенным результатом содержит `ref`, а блок памяти диалога его показывает

## 6. Подсказки модели (design D9)

- [x] 6.1 Дополнить `READ_FILE_DESCRIPTION` («путь берётся из поля `ref` результата») и системный промпт правилом «нет нужного числа в `numbers`, есть `ref` — дочитай `read_file`, не пересчитывай»; тексты на русском; существующие тесты промптов и skills зелёные

## 7. Сквозные проверки (AR-Q6, AR-Q7)

- [x] 7.1 Тест на собранном агенте со скриптовой моделью и фейковым движком (`tests/unit/v2/test_offload.py`): `run_scenarios` с большой сеткой → `ToolMessage` ≤ порога с `ref` → `read_file` по `ref` возвращает полный конверт с `data`, `read_file` с `offset` дочитывает следующую часть
- [x] 7.2 Тест изоляции: тот же `ref` в ходе другого `user_id` с тем же `session_id` → «не найден»; путь с `..` и с чужим `user_id` в пути тоже не находит результат A; содержимое результата A не попадает в ответ
- [x] 7.3 Тест «ни один `ToolMessage` не больше порога»: прогон сценария с `resolve_terms`, `run_scenarios` и `find_input_for_target` на скриптовой модели; все `ToolMessage` доменных инструментов в состоянии ≤ `AGENT_RESULT_MAX_CHARS`

## 8. Качество и трассировка

- [x] 8.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` плюс новые файлы): новых замечаний ноль, старые не блокируют и попутно не правятся
- [x] 8.2 `pylint src` — оценка > 7
- [x] 8.3 `pytest tests/unit` — зелёный; покрытие нового кода (`offload.py`, правки адаптера, кэша, reducer'а) ≥ 80 %
  > 2026-10-27: 559 из 562 зелёные. 3 падения были до S1.6 и с ней не связаны: `test_runtime.py::TestSettings::test_defaults` (в локальном `.env` `AGENT_ENGINE=v2`) и 2 × `test_stop_event.py::TestNoHardcodedRetry` (устаревшие, см. `fix-context-undefined-imports`). Покрытие: `offload.py` 98 %, `adapter.py` 100 %, `tools/state.py` 100 %, `calc_cache.py` 92 %.
