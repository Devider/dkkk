Story S0.3 · ADR-2, ADR-4, ADR-7 · риски §16 «кандидаты с оценками без правки `AnalyzerSubAgent`», «слабая модель не читает skill»

## Why

Слой инструментов (E1) и skills (E3) опираются на два непроверенных допущения. Первое (ADR-2): `SkillsMiddleware` и `FilesystemMiddleware` из `deepagents` открывают инструменты skill после `read_file`, дают закрепление skill, держат `/results/` в store с изоляцией по пользователю и работают без инструментов записи. Второе (ADR-4): смысловой слой можно вызвать отдельным инструментом `resolve_terms`, который возвращает кандидатов с оценками, без правки `AnalyzerSubAgent`.

По коду видно, что второе допущение в лоб не выполняется. `AnalyzerSubAgent` получает весь запрос и возвращает по одному эквиваленту на термин (LLM выбирает `INid` из полного каталога в промпте); оценок в нём нет. `find_matching_cell` / `find_matching_outputs` (Jaccard) возвращают только лучший вариант и для английских алиасов дают ≈ 0. Spike измеряет варианты на эталоне Methanex и выбирает, из чего строить кандидатов и оценку.

## What Changes

- Одноразовый стенд `scripts/spikes/s0_3_skills_resolve/`:
  - прототип `ScriptedChatModel` (`BaseChatModel` с `bind_tools`, ответы по сценарию) и сквозной ход `create_agent` «план → сопоставление → сценарии → ответ» без сети и без LibreOffice;
  - `SkillsMiddleware` + `FilesystemMiddleware(tools=["read_file"])` на составном backend: `/skills/` — каталог skills, `/results/` — store с пространством пользователя; тестовые skills;
  - прототип `resolve_terms` поверх неизменённого `AnalyzerSubAgent` и Jaccard-матчера на книге `models/Methanex_FinModel.xlsx` и эталоне `tests/data/Methanex_tool_test_queries.xlsx`.
- Отчёт и решения — в `design.md`: механика skills и `/results/`, определение кандидатов и оценки для `resolve_terms`, точность против v1.
- Правки документа спецификации по итогам: ADR-2, ADR-4, ADR-7, §9, §10, §11, §16.

**Вне рамок:** код в `src/`; промышленные `ScriptedChatModel` (S2.1), адаптер (S1.1), `resolve_terms` (S1.3); правка промптов и схем смыслового слоя.

## Capabilities

### New Capabilities

—

### Modified Capabilities

—

Spike не меняет поведение системы (`skip_specs: true`); требования добавят S1.3, S1.6, S3.1.

## Impact

- Новый каталог `scripts/spikes/s0_3_skills_resolve/` (стенд, не часть приложения).
- `docs/Copilot cashflow analyst v2 project spec.md` — уточнения по итогам.
- Вызовы живого GigaChat (api_key) для смыслового слоя и одной проверки чтения skill моделью.
