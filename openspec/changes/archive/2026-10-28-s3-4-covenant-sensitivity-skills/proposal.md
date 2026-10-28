Story S3.4 · FR-18 (также; владелец S3.1) · FR-7, FR-10, FR-13 · ADR-2, ADR-5, ADR-9 · §10 «Каталог» (`trace_dependencies`) · §11 «Первая волна» (`covenant-headroom`, `sensitivity-drivers`), «Активация» п. 3 · §14 U2, U8

## Why

Две последние методики первой волны — запас по ковенантам и чувствительность к драйверам — пока держатся только на общем промпте. Запросы «хватит ли запаса по DSCR», «при какой цене пробьём 1,2», «что сильнее влияет на EBITDA», «почему просел DSCR» модель решает без шаблона. Кроме того, механика «skill открывает инструмент» ни разу не проверена на настоящем инструменте: каталог не содержит ни одного skill-инструмента. Вызов скрытого инструмента сейчас падает как вызов неизвестного инструмента, без подсказки, какой skill прочитать (§11 «Активация» п. 3; в S3.1 проверка этой подсказки отложена до S3.4).

## What Changes

- Skill `api/v2/skills/covenant-headroom/` с `references/covenants.md`. Шаблон плана: «метрики и пороги → база → стресс-сетка по драйверу → точка пробоя → запас прочности». Порог ковенанта берётся **только из запроса пользователя**. Если порога нет, шаг `blocked` и задаётся один вопрос о пороге (FR-10). `references/covenants.md` даёт направление ковенанта («не ниже» / «не выше»), трактовку DSCR / LLCR / ICR / Debt/EBITDA и глоссарий, но не числовые пороги.
- Skill `api/v2/skills/sensitivity-drivers/` с `metadata.include_tools: trace_dependencies`. Две ветки:
  - «что сильнее влияет на Y»: «драйверы → ±N % по каждому → эластичности → рейтинг → связи в модели»;
  - «почему просел Y»: связи → изменение драйверов между годами без пересчёта.
- Новый skill-инструмент `trace_dependencies` (открывает `sensitivity-drivers`, движок не нужен). Он получает ID выхода и год, обходит формулы книги от ячейки выхода и возвращает входы модели (ID, название, глубина) и промежуточные выходы, через которые идёт связь. Это отклонение от §10 «Оборачивает `build_dependency_graph`»: нужен обход по ID с раскрытием диапазонов, без поиска по описанию и без PNG (design D3).
- Вызов skill-инструмента до чтения его skill (в том числе после сжатия истории) отклоняется конвертом с новым кодом `skill_required`. В подсказке указан путь `SKILL.md`, который нужно прочитать. Вызов других неизвестных инструментов не меняется.
- `evals.yaml` в обоих skills. Eval-скрипт `scripts/eval/scenario_skills.py` прогоняет запросы всех сценарных skills, режим `--scripted` работает без сети.
- Тесты: `trace_dependencies` на синтетической книге; отклонение скрытого инструмента и его открытие после чтения; сквозные ходы обоих skills на скриптовой модели; состав поставки.

**Вне рамок:** типовые пороги ковенант как источник чисел (решение: только порог пользователя; FR-13 не меняется); `point-what-if` и `set_inputs_and_recalc` (после первой волны); закрепление skills и `AGENT_PINNED_SKILLS` (S3.5); PNG-визуализация графа и рендер graphviz; правка ответов v1 и `build_dependency_graph` v1; полный eval-набор `eval_v2.yaml` и метрики (S5.1); живой прогон на GigaChat (в контуре, эпик 5); сравнение v1 / v2 (S5.3).

## Capabilities

### New Capabilities

—

### Modified Capabilities

- `agent-skills`: FR-18 — в поставке появляются `covenant-headroom` и `sensitivity-drivers`; вызов скрытого skill-инструмента отклоняется с подсказкой, какой skill прочитать; добавлены сценарии приёмки обоих skills.
- `agent-tools`: ADR-5 «Каталог инструментов» — `trace_dependencies` зарегистрирован как инструмент `sensitivity-drivers`; новое требование ADR-5 «Связи показателя в модели»; FR-7 «Справочник кодов ошибок» — новый код `skill_required`.

## Impact

- Новые файлы:
  - `api/v2/skills/covenant-headroom/{SKILL.md,evals.yaml,references/covenants.md}`;
  - `api/v2/skills/sensitivity-drivers/{SKILL.md,evals.yaml}`;
  - `api/v2/tools/dependencies.py` (инструмент);
  - `src/excel_engine/dependencies.py` (обход формул, без LangChain);
  - `api/v2/middleware/skill_gate.py`.
- Изменяемые файлы:
  - `tools/catalog.py` — регистрация `trace_dependencies`;
  - `tools/envelope.py` — код `skill_required`;
  - `factory.py` — `SkillToolGate` сразу после `SkillsMiddleware`;
  - `scripts/eval/scenario_skills.py` — список skills.
- Логика v1, `tools.py`, расчётные функции и движок LibreOffice не меняются. `ModelIndex` и реестр моделей тоже не меняются.
- Модель после чтения `sensitivity-drivers` видит 9 инструментов (8 базовых + `trace_dependencies`), как в §10. Каталог skills в промпте увеличивается на две строки.
- Открытие книги с формулами (openpyxl, `data_only=False`) на вызов `trace_dependencies` занимает секунды на больших моделях. Вызов идёт в рабочем потоке, разобранная книга кэшируется (design D3).
