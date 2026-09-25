# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

AI Gateway REST service (`aigw-rest-service`) — FastAPI + LangGraph multi-agent for Excel cashflow-model
scenario analysis. Python 3.12+, Poetry. This repo also has an `AGENTS.md` with earlier notes; where the
two disagree, trust this file and the code — the graph architecture and LLM backend changed since AGENTS.md
was last updated (LibreOffice replaced `formualizer`, and GigaChat replaced Ollama as the active default).

## Commands

```sh
# run (default — python, no Docker; requires local LibreOffice + python3-uno, see below)
python3 src/aigw_service/__main__.py 2>&1 | tee server.log

# run (Docker — primary deployment, has LibreOffice baked in)
docker compose up -d
docker compose build app --no-cache   # rebuild after src/ changes (layer-cached otherwise)
docker compose restart app            # fast restart without rebuild
docker compose logs app -f            # live logs
docker compose down                   # stop (named volume app_tmp persists uploaded files)
curl http://localhost:8888/health     # port from .env APP_PORT (repo default 8888; 8080 if no .env)

# lint / format — MUST run ruff check --fix then ruff format before committing
ruff check src           # check only
ruff check --fix src     # auto-fix
ruff format src          # format
pylint src                # must score >7

# tests (from host venv, NOT from inside the docker container)
pytest tests                                                        # everything, addopts already include -v -s --cov=src --maxfail=1
pytest tests/unit/                                                  # unit only (fast, mocked)
pytest tests/unit/test_stop_event.py -v                             # single file
pytest tests/test_tools_performance.py -v -s --no-header --no-cov   # ~2.5 min, 7 perf tests against real LibreOffice recalcs

# tool-call / name-resolution quality validation (requires a running server, NOT pytest)
python scripts/run_tool_queries.py --subset 5 --url http://localhost:8080 --log server.log
python scripts/analyze_results.py test_output/tool_query_results.json --top-n 25 --csv analysis.csv

# deps
poetry install
poetry add <pkg>
poetry update             # after editing pyproject.toml directly
```

There is no single-file lint autofix distinct from the commands above — always run `ruff check --fix src`
then `ruff format src` on the whole `src` tree before finishing a change.

## Architecture

### Request flow

```
POST /api/v1/upload
  saves the .xlsx to /tmp/user_file_{x-user-id}.{ext}
  records the filename in the agent memory store (namespace ("memories", user_id))

POST /api/v1/invoke-agent
  -> AgentGraph.graph.ainvoke(...)   (src/aigw_service/api/v1/main_graph.py)
  -> response is a ZIP (txt_response.txt + generated/agent_output.xlsx placeholder)
```

Both endpoints require the 5 headers validated in `api/v1/utils.py`: `x-trace-id` (UUID v4),
`x-client-id` (`^[A-Z]{2}\d{8}$`), `x-request-time` (RFC-3339), `x-session-id` (UUID, optional),
`x-user-id` (≤8 chars, required for upload/lookup). A header mismatch returns 422.

### The agent graph (current — supersedes the old single-file `services.py` state machine)

`src/aigw_service/api/v1/main_graph.py:AgentGraph` is what `router.py` actually wires up. `services.py`
is legacy/unused — nothing imports it; don't extend it, extend `main_graph.py` and `subagents/` instead.

Graph shape:
```
START -> classifier -> extract_excel_data -> (route) -> analyze_model_inputs_for_target -> reviewer -> END
                                                       \-> analyze_excel_model             -> reviewer -> END
```

- **classifier** (`subagents/classifier.py`): LLM call that picks exactly one of the two tools
  (`analyze_model_inputs_for_target` / `analyze_excel_model`) and extracts the filename the user meant.
- **extract_excel_data**: loads the uploaded workbook's filename from the memory store (keyed by
  `user_id`, namespace `("memories", user_id)`) and builds the input/output name catalog via
  `subagents/utils.py:_build_catalog_with_ids`.
- **analyze_model_inputs_for_target** / **analyze_excel_model**: each first runs an `AnalyzerSubAgent`
  (`subagents/analyzer.py`) to resolve the user's free-text aliases against the catalog (LLM structured
  output + a lookup-more retry step), then calls the real calculation tool
  (`tools.py:analyze_model_inputs_for_target` / `analyze_excel_model`) with the resolved names.
- **reviewer**: final LLM call that turns the calculation results into the natural-language answer.

Structured LLM outputs (`QueryAnalysisIFT`, `QueryAnalysisEMA`, `LookupResult`, `InputItem`, ...) live in
`api/v1/schemas/llm_outputs.py`; graph state TypedDicts live in `api/v1/states/agents.py`; prompts live in
`api/v1/prompts/`.

### Config / bootstrap

- `config/__init__.py:Secrets` bundles per-domain `*Settings` classes (`gigachat`, `ollama`,
  `pangolin`, `idp`, `platform_v_search`, `langfuse`, `aef_tracing`, ...), each a `pydantic-settings`
  class reading its own env vars. `BaseAppSettings.local` (`LOCAL` env, default `False`) switches
  http/https and whether cert paths are required — default `False` needs no certs even with GigaChat.
- `context.py:AppContext` (singleton via `aigw_service.base.Singleton`) is built once at import time as
  `APP_CTX`. It owns the logger, the LLM, tracing, and agent memory, and drives `on_startup`/`on_shutdown`
  (called from the FastAPI `lifespan` in `api/__init__.py`).
- **LLM backend**: `context.py` always builds `core/llm_executor.py:Agent` (a `GigaChat` subclass) — there
  is no runtime branch on `MODEL_TO_USE`/`OLLAMA` in `context.py` despite those settings/env vars still
  existing (`base_config.py` default is `GIGACHAT`; Ollama env vars are legacy/unused by the live code
  path). `Agent` also spins up a `-preview` model and an `_llm_judge` GigaChat instance for a background
  A/B comparison task (`preview_model_check`, gated by `APP_CONFIG.app.preview_model_check`).
- **Store backend**: `STORE_TO_USE=MEMORY | PANGOLIN` (default `MEMORY`). `AppContext.on_startup` uses
  Pangolin only if `pangolin.enabled` and its pool connects; otherwise it falls back to
  `config.get_store()` → `InMemoryStore`.
- **Tracing**: Langfuse (`core/tracing/`), enabled via `langfuse.enabled`; the callback handler is
  injected into the LangGraph `config["callbacks"]` in `router.py:get_cb_handler` when present.
- **Pydantic monkey-patch**: `context.py:_wrap_llm_with_stop_event` replaces `llm.invoke`/`llm.ainvoke`
  via `object.__setattr__` (GigaChat/pydantic BaseModel rejects plain `setattr` on non-fields) to detect
  a GigaChat `ForbiddenError` "temporarily unavailable" body and raise `StopEventError` instead.
- **Private dep stub**: `sber-aigw` is replaced by local stubs in `src/aigw_modules/` (only 3 imports used,
  all from `context.py`) — no internal auth/network needed to run this locally.

### Excel backend — LibreOffice Calc via UNO (not `formualizer`, not openpyxl-only)

`api/v1/excel_handler.py` is the only module the agent uses to read/recalculate Excel. It wraps
`api/v1/lo_backend.py`, which replaced the old `formualizer`-based engine (removed, along with
`formualizer_ext.py` — LibreOffice natively supports HYPERLINK/CELL/SHEET/TODAY/XNPV/XIRR without patches).

- Each `ExcelWorkbook` spawns its own headless `soffice` process on a free port with a throwaway user
  profile and opens the workbook **ReadOnly** — two LO processes on the same file without ReadOnly
  deadlock on the file lock. Documents are mutated in-memory only; nothing is ever saved back to disk.
- `lo_backend.py:LibreOfficeSession` handles spawn/connect/stop; `CalcBook` handles
  `set_value`/`get_value`/`calculate_all`/bulk reads via `getDataArray`.
- **`URE_BOOTSTRAP` is critical** — without it the UNO bridge dies with "Binary URP bridge disposed during
  call". It's set at import time in `lo_backend.py` (before `import uno`) and again via `ENV URE_BOOTSTRAP`
  in the Dockerfile (`vnd.sun.star.pathname:/usr/lib/libreoffice/program/fundamentalrc`).
- No engine cache: spawning is ~1s, open+calc ~0.6s, per workbook. Deliberately not shared — a shared LO
  document would break concurrent requests, and a single reused process would leak memory over time.
- The compiled closure from `get_compiled_func()` is only valid while its `ExcelWorkbook` is open — close
  the workbook only after all calls into the closure are done.
- `analyze_excel_model` / `analyze_model_inputs_for_target` results are LRU-cached (max 10) by
  `(file_path, ...params..., user_id)` since LLMs often repeat identical queries.
- Running outside Docker requires a local LibreOffice with `python3-uno` installed — otherwise
  `lo_backend` fails to import.

### Name resolution (the accuracy-limiting piece)

`tools.py:find_matching_cell` / `find_matching_outputs` fuzzy-match the LLM's free-text aliases (often
English, e.g. "copper (LME)") against canonical Russian names in the workbook's Inputs/Outputs sheets,
using Jaccard similarity over `normalize_text` output. `api/v1/russian_stemmer.py` is a self-contained
pure-Python Snowball stemmer — NLTK was removed; this is a byte-for-byte port of
`nltk.stem.snowball.RussianStemmer`, needing no corpora.

- Jaccard is 0 across alphabets, so cross-lingual aliases only overlap through shared English fragments
  in parentheses (e.g. "(LME)", "(USD)") — this causes false matches (shortest matching name tends to win).
- Output rows with an empty `values` dict (section headers, no formula) can still be Jaccard-matched,
  which used to raise `Unreachable output-targets`; guard with `if not info.get("values"): continue`.
- Current measured accuracy (see `scripts/run_tool_queries.py` / `scripts/analyze_results.py`) is roughly
  55% per-parameter, ~1% full-query pass — consistent with `0.55^8 ≈ 0.8%` at ~8 params/query. Don't be
  surprised by a low query-level pass rate; check per-param accuracy instead.

## Key quirks / gotchas

- **Loguru + f-strings crash on exception text containing `{}`**: never write
  `logger.error(f"...{str(e)}...", exc_info=True)` — loguru calls `.format()` on the message, and braces
  in an exception string (e.g. a cell ref like `{'[file]OUTPUTS'!P143}`) raise
  `ValueError: expected ':' after conversion specifier`. Always use
  `logger.opt(exception=True).error("...: {}", str(e))` in new `except Exception as e:` blocks.
- Do NOT edit `api/os_router.py` or `api/metric_router.py` — marked "НЕ РЕДАКТИРОВАТЬ" (do not edit) in
  the code.
- Tests use `httpx.AsyncClient(app=app_main)` (ASGI transport, no real server needed).
  `asyncio_mode = "auto"` in `pyproject.toml` — no `@pytest.mark.asyncio` needed.
- Test env requires `GIGACHAT_HOST`/`GIGACHAT_PORT`, set in `[tool.pytest.ini_options.env]` in
  `pyproject.toml`.
- `giga_test.py` at `src/aigw_service/` root is a standalone scratch script, not part of the app.
- Coverage output: `coverage.xml` + terminal `term-missing`, from `--cov=src` in pytest addopts.
- `scripts/run_tool_queries.py` and `scripts/analyze_results.py` are quality-evaluation tools, not pytest
  — they hit a live server and parse `server.log` for `TOOL ARGS` lines keyed by `x-trace-id`. Full docs
  (output format, all 11 analysis sections) are in `tests/README.md`. `scripts/TESTING.md` documents the
  separate `scripts/new_graph_test.py` harness for exercising `AgentGraph` directly.
- Diagnostic log markers for name-resolution debugging in `server.log`:
  `Found output cell for` (successful output resolve), `OUTPUT RESOLVED (modify)` (resolve before
  `calculate()`), `returned None` (WARNING — cell exists but has no formula, likely a section header),
  `Unreachable output-targets` (old `formualizer`-era error; should not occur on the LibreOffice backend).

## Docker workflow (primary deployment)

```sh
docker compose up -d
curl http://localhost:8888/health
docker compose build app --no-cache   # after src/ changes
docker compose restart app            # fast restart, no rebuild
docker compose logs app -f
docker compose down                   # app_tmp volume (uploaded/cached xlsx) persists across down
docker compose down -v                # also drops app_tmp
```

Known issue: `app_tmp` persists previously uploaded/cached xlsx files across restarts. If a workbook's
formulas look stripped, re-upload the original via `/upload` rather than assuming a code bug.
