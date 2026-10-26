# S0.3 spike: skills, `/results/` и `resolve_terms`

Одноразовый стенд для OpenSpec change `s0-3-skills-resolve-spike`. Не входит в приложение и в `pytest`.
Проверки K1–K9, R1–R4 и критерии — в `openspec/changes/.../design.md`, раздел D2.

```sh
# из корня репозитория, venv проекта
python scripts/spikes/s0_3_skills_resolve/run.py --checks K1,K2,K3,K4,K5,K6,K7,K8   # без сети
python scripts/spikes/s0_3_skills_resolve/run.py --checks K9                        # живой GigaChat, 6 ходов
python scripts/spikes/s0_3_skills_resolve/run.py --checks R1,R4,R2,R3              # смысловой слой, ~60 запросов
```

- `scripted.py` — прототип `ScriptedChatModel` (шаги — готовые `AIMessage` или функции от истории).
- `skills/` — тестовые skills; `broken-skill` намеренно невалиден (проверка K8).
- R1–R4 читают `models/Methanex_FinModel.xlsx` и эталон `tests/data/Methanex_tool_test_queries.xlsx`;
  запросы к GigaChat идут последовательно — при параллельных API отвечает 429.
