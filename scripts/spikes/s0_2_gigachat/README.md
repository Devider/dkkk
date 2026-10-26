# S0.2 spike: GigaChat в `create_agent`

Одноразовый стенд для OpenSpec change `s0-2-gigachat-agent-spike`. Не входит в приложение и в `pytest`.
Проверки C1–C10 и критерии — в `openspec/changes/.../design.md`, раздел D2.

```sh
# из корня репозитория, venv проекта; режим подключения — как у сервиса (LOCAL + .env)
python scripts/spikes/s0_2_gigachat/run.py                      # все проверки (~60–80 вызовов GigaChat)
python scripts/spikes/s0_2_gigachat/run.py --checks C1,C3,C8    # выборочно
```

Отчёт пишется в `results/<api_key|certificate>-<время>.json`.

Режим сертификатов (в контуре): в `.env` `LOCAL=True`, `GIGACHAT_TLS_CERT_FILEPATH`, `GIGACHAT_KEY_FILEPATH`,
`GIGACHAT_CA_BUNDLE_FILEPATH`, без `GIGACHAT_CREDENTIALS`; затем
`python scripts/spikes/s0_2_gigachat/run.py --checks C1,C3,C4,C5,C6,C7,C8`.

C8 не ходит в сеть: ответ API подменяется на `ForbiddenError` с маркером StopEvent.
