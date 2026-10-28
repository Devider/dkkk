# S6.1 spike: поток хода агента v2

Одноразовый стенд для OpenSpec change `s6-1-stream-spike`. Не входит в приложение и в `pytest`, `src/` не меняет.
Проверки и критерии — `openspec/changes/s6-1-stream-spike/design.md` (D2), итоги — там же, раздел «Results».

```sh
# из корня репозитория, venv проекта
python scripts/spikes/s6_1_stream/run.py --checks P1,P2,P3,P4,P5,P6   # скриптовая модель, без сети и LibreOffice
python scripts/spikes/s6_1_stream/run.py --checks G1,G2,G3,G4,G5      # живой GigaChat по .env (~40 вызовов)
python scripts/spikes/s6_1_stream/run.py --checks H1,H2,H3,H4         # живой uvicorn на свободном порту
```

Отчёт пишется в `results/<проверки>-<время>.json`; в `results/` лежат отчёты прогона 2026-10-28.

| Файл | Что внутри |
| --- | --- |
| `common.py` | Агент через `AgentFactory` со всем стеком на `ScriptedChatModel`, фейки движка (`SpikeGate`) и `resolve_terms`, запись частей `astream` |
| `progress.py` | Прототип `ProgressReporter` на `runtime.stream_writer` |
| `agent_checks.py` | P1–P6 |
| `gigachat_checks.py`, `raw_stream.py` | G1–G4, G5 (порции потока GigaChat напрямую) |
| `http_proto.py` | Прототип сервера: `log_requests` как есть / с веткой `text/event-stream`, SSE-endpoint, ход в генераторе / в задаче |
| `http_checks.py` | H1–H4: клиент SSE с отметками времени и обрывом по таймеру |

В контуре (задача 4.1 change):

- C1 — запустить `http_proto.py --log patched` за маршрутом AI GW и читать `/ticks?n=40&interval=10` и
  `/agent?mode=task&delay=300&heartbeat=15` клиентом со стороны фронта; кадры должны приходить по мере отправки,
  соединение — жить 5 мин на одних heartbeat.

C2 (режим сертификатов GigaChat для потока) снята: финальный ответ приходит целиком, модель агента не стримит.
