# Tasks

## 1. Настройки и счётчик токенов (AR-Q5)

- [ ] 1.1 `config/agent/config.py`: `max_prompt_tokens` получает `ge=4000`; новые `summary_trigger` (`AGENT_SUMMARY_TRIGGER`, 0.7, `gt=0, lt=1`) и `summary_keep` (`AGENT_SUMMARY_KEEP`, 0.3, `gt=0, lt=1`), валидатор модели отклоняет `summary_keep >= summary_trigger`. README: строка `AGENT_MAX_PROMPT_TOKENS` уточнена («v2: бюджет запроса, от него считаются пороги сжатия»), добавлены две новые строки. Проверка: `tests/unit/v2/test_summarization.py::TestSettings` — значения по умолчанию, переопределение, `AGENT_MAX_PROMPT_TOKENS=3999`, доли `0` и `1`, `keep=trigger` отклоняются; `test_runtime.py::test_every_agent_variable_is_in_readme` зелёный.
- [ ] 1.2 `middleware/summarization.py`: `CHARS_PER_TOKEN = 3.0` и `context_tokens(messages, tools=None)` поверх `count_tokens_approximately` (D3). Проверка: тест — кириллический текст из 300 символов даёт ≈ 100 токенов плюс служебные; `tools` увеличивают счёт.

## 2. Сжатие истории (AR-Q6, D1, D2, D6)

- [ ] 2.1 `prompts/middleware.py`: `SUMMARY_PROMPT` (русский, плейсхолдер `{messages}` внутри маркера `<messages>`; требует сохранить числа с единицами, ID показателей, `ref` вынесенных результатов, решения и открытые вопросы пользователя, без новых чисел) и `SUMMARY_MESSAGE` (текст сообщения-сводки с путём к истории и без пути). Проверка: тест «русские тексты» — кириллица есть, латиница только в `{messages}`, `read_file`, `ref`.
- [ ] 2.2 `middleware/summarization.py`: `CopilotSummarization(SummarizationMiddleware)` с переопределённым `_build_new_messages_with_path` (D2) и `summarization_middleware(model, backend, budget)`: `trigger`/`keep` в токенах, `trim_tokens_to_summarize=max_prompt_tokens`, `token_counter=context_tokens`, backend — `file_backend(...)` фабрики. Проверка: `test_summarization.py` на `ScriptedChatModel` при бюджете 4000 — «Короткая сессия не сжимается» (число вызовов модели = число шагов хода), «Свежий хвост дословно» (первое сообщение запроса — сводка с `lc_source="summarization"` на русском, далее хвост, у каждого `tool_call` в хвосте есть `ToolMessage`), «Повторное сжатие» (в запросе одна сводка).
- [ ] 2.3 `tests/unit/test_dependency_pins.py`: наличие `SummarizationMiddleware._build_new_messages_with_path` и константы `SUMMARIZATION_EVENT_KEY` в `deepagents.middleware.summarization`. Проверка: тест зелёный на `deepagents==0.7.29`.
- [ ] 2.4 `factory.py`: параметр `prompt_budget` (D7, по умолчанию из `APP_CONFIG.agent`); `summarization_middleware` на месте №7 — после `StopEventGuard`, перед `skills_middleware`, с тем же backend, что у `read_file`. Порядок стека в `test_plan.py::TestFactory` и `test_api.py::test_place_in_stack` обновлён. Проверка: эти тесты зелёные.
- [ ] 2.5 Сценарии хранения и отказов. Проверка, тесты в `test_summarization.py`:
  - «Полная история по ссылке»: `read_file` пути из сводки возвращает сжатые сообщения в ходе того же пользователя; у другого `user_id` с тем же `x-session-id` — «файл не найден».
  - «StopEvent при вызове сводки»: шаг-сводка скрипта бросает `ForbiddenError` с маркером → `StopEventError`.
  - Вызов сводки не увеличивает счётчик `TurnLimits`.
- [ ] 2.6 «Длинная сессия не превышает порог». Проверка: тест — 8 ходов со скриптовыми расчётами (результаты около `AGENT_RESULT_MAX_CHARS`) при бюджете 4000; у каждого запроса к модели `context_tokens(...)` < 4000, все ходы завершаются ответом.

## 3. Что переживает сжатие (AR-Q6, FR-16, D4)

- [ ] 3.1 `turn_context.py`: `_visible(state)` — `messages[cutoff_index:]` по `_summarization_event` (весь список, если события нет); `_pinned_in` в `abefore_agent` проверяет видимую часть; новые `before_model` / `abefore_model` пишут `pinned_skills` для закреплённых skills, которых нет в видимой части. Проверка: тест «Закреплённый skill возвращается» — после сжатия, вытеснившего `economic-answer`, следующий запрос модели содержит его текст ровно один раз в видимой части; без сжатия `pinned_skills` не пишется в `before_model` (тесты S3.2 в `test_answer.py` зелёные без изменений).
- [ ] 3.2 Проверка тестом «План, карточка и выжимки не теряются» и FR-16 «Память после сжатия истории»: сессия с двумя прошлыми выжимками и открытым планом, сжатие посреди третьего хода. Системный промпт запроса после сжатия содержит те же блоки карточки и памяти, а `todos`, `resolved_terms`, `turn_summaries` и `step_results` в состоянии не изменились; выжимка третьего хода строится по полному ходу (`turn_message_id` найден).
- [ ] 3.3 Проверка тестом «Инструменты незакреплённого skill скрыты»: `read_file /skills/target-seeking/SKILL.md` попал в сжатую часть, и следующий запрос не содержит инструментов skill. Вызов скрытого инструмента отклоняется с подсказкой; после нового `read_file` инструменты снова видны.
- [ ] 3.4 Проверка тестом «Числа ответа после сжатия»: результат расчёта ушёл в сводку, финальный ответ с его числом проходит `AnswerGroundingGuard` без перегенерации.

## 4. Замер слоёв (AR-Q6, D5)

- [ ] 4.1 Заголовки блоков системного промпта — константы, которые берут и генератор блока, и замер: `CARD_HEADER` (`turn/card.py`), `MEMORY_HEADER` (`turn_summary.py`), первые строки `SKILLS_SYSTEM_PROMPT` и `TODO_SYSTEM_PROMPT`. Текст блоков не меняется. Проверка: тесты карточки и памяти (`test_turn_context.py`, `test_memory.py`) зелёные без изменений.
- [ ] 4.2 `middleware/context_meter.py`: чистая `measure_layers(request) -> dict[str, int]` по таблице D5 (`total` — сумма слоёв). Проверка: юнит-тест на собранном вручную `ModelRequest` со всеми слоями — каждый ненулевой, сумма равна `total`; `HumanMessage` со сводкой идёт в `history_summary`, `pinned_skill` — в `skill_text`, вызов `write_todos` — в `plan`, `read_file /skills/...` — в `skill_text`, `read_file /results/...` — в `calls_results`.
- [ ] 4.3 `ContextMeter` (`wrap_model_call` / `awrap_model_call`): пишет `CONTEXT LAYERS` (JSON: `trace_id`, `turn_id`, `call`, слои) через `logger.info("CONTEXT LAYERS {}", ...)`. Если `total > max_prompt_tokens` — `logger.warning` с итогом и бюджетом, вызов модели всё равно идёт. `factory.py` ставит его последним в списке. Проверка: тесты на агенте со скриптовой моделью и перехватом loguru — «Строка на каждый вызов» (номера 1, 2, 3, один `turn_id`), «Слой сводки после сжатия», «Превышение бюджета», «Ход без LLM» (нет файла и неподдерживаемый язык — строк нет).

## 5. Документация и трассировка

- [ ] 5.1 `docs/Copilot cashflow analyst v2 project spec.md`:
  - §8 №7: хук `wrap_model_call`, сжимается запрос, а не состояние (D1);
  - §8 №5: замер слоёв — отдельный внутренний `ContextMeter` (D5);
  - §9 правило 5: уточнение формулировки.

  `openspec/traceability.md`: AR-Q6 — S2.7 ✓ с отсылкой к D1 и D5; AR-Q5 — S2.7 ✓ (бюджет и доли сжатия). Проверка: просмотр диффа.

## 6. Проверки качества

- [ ] 6.1 `ruff check --fix` и `ruff format` только по изменённым и новым файлам change (`git diff --name-only --diff-filter=AM HEAD -- '*.py'` плюс новые файлы); новых замечаний ноль, старые не блокируют и попутно не правятся. Проверка: `ruff check <файлы>` без замечаний.
- [ ] 6.2 `pylint src` — оценка > 7. Проверка: вывод pylint.
- [ ] 6.3 `pytest tests/unit` зелёный; покрытие `summarization.py` и `context_meter.py` ≥ 80 % (AR-Q15). Проверка: `pytest tests/unit --cov=src/aigw_service/api/v2 --cov-report=term-missing`.
