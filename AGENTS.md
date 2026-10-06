# AGENTS.md

## Project

AI Gateway REST service (`aigw-rest-service`) — FastAPI + LangGraph agent that answers what-if
questions over an Excel cashflow model. Python 3.12, Poetry metadata, package `src/aigw_service`.

- `CLAUDE.md` carries a longer copy of these notes for Claude Code — edit both together.
- `README.md` is partly stale: it still describes the old `formualizer`/openpyxl two-workbook engine
  and an Ollama container. Trust the code and this file.

## Environment setup (breaks everything if skipped)

- Config is built **at import** (`aigw_service.config`) with `load_dotenv(override=True)` from the
  git-ignored `.env`. Keys with **no default** — if any is missing, the app *and* pytest die during
  collection with a pydantic `ValidationError`: `GIGACHAT_MODEL_NAME`, `DB_HOST/DB_PORT/DB_USER/
  DB_PASS/DB_DATABASE`, `LANGFUSE_HOST/LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY`.
  `.env.example` does not list the `DB_*` keys; `docker.env` has all of them. Merge it into your
  shell before running anything outside Docker:
  ```sh
  set -a; . <(sed 's/=10 MB$/=10MB/' docker.env); set +a   # LOG_ROTATION=10 MB is not sourceable as-is
  ```
- `LOCAL=True` (current default in `.env`/`docker.env`) → GigaChat requires **exactly one** of
  `GIGACHAT_CREDENTIALS` or the cert pair; neither/both raises `ValueError` at startup.
- **Poetry is unusable here**: `requires-poetry = "2.3.4"` vs installed 2.4.1 → `poetry install/add/update`
  exit 1. Use the existing venv directly: `V=$(echo ~/.cache/pypoetry/virtualenvs/aigw-rest-service-*/bin)`
  (it can lag pyproject — e.g. `langfuse` was missing; install with `$V/python -m pip install <pkg>`).
- `.env` and root `api.key` hold live credentials (`api.key` is *not* gitignored) — never commit them.
- Host needs LibreOffice with `uno.py` (`lo_backend` finds it in `/usr/lib64/libreoffice/program`,
  `LO_PROGRAM_DIR` overrides). Docker image bakes it in.

## Commands

```sh
V=$(echo ~/.cache/pypoetry/virtualenvs/aigw-rest-service-*/bin)   # all deps + ruff/pylint/pytest

# run — Docker (primary deployment; LibreOffice + entrypoint baked in)
docker compose up -d
curl http://localhost:8080/health     # only the `app` service; APP_PORT unset in .env → host 8080
docker compose build app --no-cache   # after src/ changes
docker compose restart app            # no code change
docker compose logs app -f
docker compose down                   # volume app_tmp (uploaded xlsx) persists

# run — host (verified: boots uvicorn on APP_PORT; needs env from "Environment setup" above)
$V/python -m aigw_service 2>&1 | tee server.log

# lint (baseline is NOT clean — see below)
$V/ruff check src        # ~59 pre-existing errors: W291 in prompts/, C901 complexity, F821 in context.py
$V/ruff format src       # 5 files currently unformatted
$V/pylint src            # 9.41/10

# tests — env must be set first (see Environment setup)
$V/python -m pytest tests                                 # addopts: -v -s --maxfail=1 --cov=src + junitxml
$V/python -m pytest tests/unit -q --no-cov
$V/python -m pytest tests/test_tools_performance.py -q --no-cov   # 7 tests, ~48 s, real LibreOffice recalc

# tool-call / name-resolution quality (needs a running server — not pytest)
$V/python scripts/run_tool_queries.py --subset 5 --url http://localhost:8080 --log server.log
$V/python scripts/analyze_results.py test_output/tool_query_results.json --top-n 25 --csv analysis.csv
```

There is no CI and no pre-commit config — lint/tests are local only. Don't try to burn down the
pre-existing ruff backlog; just don't add new violations in files you touch.

Known-broken: 6 tests in `tests/unit/test_stop_event.py` fail because `context.py:43` raises
`StopEventError` that is never imported (ruff F821). Not caused by your change.

## Architecture

- Entrypoint `__main__.py` → uvicorn(`app_main` from `api/__init__.py`); routers: service (`/health`),
  metric, v1 (`/api/v1`).
- **Agent graph** — `api/v1/main_graph.py:AgentGraph`, what `router.py` actually wires:
  `START → extract_excel_data → orchestrator ⇄ tool_executor → synthesizer → END`.
  - `orchestrator`: native tool-calling LLM (`llm.bind_tools`) picks
    `analyze_model_inputs_for_target` / `analyze_excel_model` / both / none per turn (max 4 loops).
  - `tool_executor` → `subagents/orchestrator_tools.py` → `AnalyzerSubAgent` (free-text alias →
    catalog name, with a lookup-more retry) → calculation fns in `tools.py`.
  - compiled with `APP_CTX.agent_memory.checkpointer`; multi-turn history keyed by
    `thread_id = f"{user_id}:{session_id}"` (`router.py`).
  - structured LLM outputs: `api/v1/schemas/llm_outputs.py`; state: `api/v1/states/`; prompts: `api/v1/prompts/`.
  - **`api/v1/services.py` is legacy — nothing imports it.** Extend `main_graph.py` / `subagents/`.
- **LLM backend**: `context.py` always builds `core/llm_executor.py:Agent` (GigaChat). `MODEL_TO_USE`,
  `OLLAMA_*` and `OllamaSettings` are dead config — no code branch reads them, and compose has **no
  ollama service**. Don't wire "the Ollama path" expecting it to work.
- **Store**: `STORE_TO_USE=MEMORY|PANGOLIN`; Pangolin used only if `pangolin.enabled` + pool connects,
  else `InMemoryStore`.
- **Config**: `config/__init__.py:Secrets` instantiates every settings class at import — one missing
  env var breaks the whole process.
- **Excel**: `excel_handler.py` + `lo_backend.py` — LibreOffice Calc via UNO, replacing `formualizer`.
  - one headless `soffice` per `ExcelWorkbook` (spawn ~1 s, open+calc ~0.6 s), workbook opened
    **ReadOnly**, mutated in memory only, never saved back. No engine cache by design (shared docs
    break concurrency; one reused process leaks).
  - `URE_BOOTSTRAP` must be set before `import uno` (set at `lo_backend` import + Dockerfile ENV);
    without it the bridge dies with "Binary URP bridge disposed during call".
  - `get_compiled_func()` closures are invalid after `xl.close()` — close the workbook last.
  - `analyze_excel_model` / `analyze_model_inputs_for_target` results are LRU-cached (max 10) by
    `(file_path, …params…, user_id)` — stale-looking answers are often cache hits.
- **Name resolution (the accuracy limiter)**: `tools.py:find_matching_cell` / `find_matching_outputs`
  = Jaccard over `normalize_text` output, with a self-contained Snowball Russian stemmer in
  `api/v1/russian_stemmer.py` (NLTK removed from code, still listed in pyproject). English aliases vs
  Russian sheet names → Jaccard 0 across alphabets, overlap only via fragments like "(LME)"; measured
  **~55% per-param, ~1% full-query** accuracy (0.55⁸ ≈ 0.8% at 8 params/query). Judge by per-param,
  and skip Outputs rows whose `values` dict is empty (section headers).
- **Package split**: `src/aigw_modules/` = stubs for the private `sber-aigw` dep (3 imports, all in
  `context.py`); `src/aigw_modules_custom/` = customer-specific code (`AsyncAgentMemory`, …) — keep
  customizations there, not in `aigw_service`. All deps come from public PyPI.

## Testing quirks

- Tests use `httpx.AsyncClient(transport=ASGITransport(app=app_main))` — no server needed;
  `asyncio_mode = auto`, so no `@pytest.mark.asyncio`. Integration fixture calls `APP_CTX.on_startup()`.
- pytest's `env` in pyproject only sets `GIGACHAT_HOST/PORT`; everything else comes from
  `.env`/`docker.env`. SSL errors reaching the GigaChat IFT host are expected offline.
- 5 headers validated in `api/v1/utils.py`: `x-trace-id` (UUID), `x-client-id` (2 letters + 8 digits),
  `x-request-time` (RFC-3339), `x-session-id` (UUID, optional), `x-user-id` (≤8 chars, needed for upload).
- `scripts/run_tool_queries.py` / `analyze_results.py` are live-server eval tools, not pytest: they
  POST prompts, parse `TOOL ARGS` lines from `server.log` by `x-trace-id`, and re-resolve names with
  the same Jaccard pipeline. `tests/README.md` documents flags/output; `scripts/TESTING.md` documents
  `scripts/new_graph_test.py` (drives `AgentGraph` directly).
- Diagnostic markers for resolution debugging: `Found output cell for`, `OUTPUT RESOLVED (modify)`,
  `returned None` (cell exists but has no formula — section header).

## Code gotchas

- **Loguru + f-strings**: never `logger.error(f"...{str(e)}...", exc_info=True)` — loguru calls
  `.format()` on the message, and braces in exception text (e.g. `{'[file]OUTPUTS'!P143}`) raise
  `ValueError: expected ':' after conversion specifier`. Use
  `logger.opt(exception=True).error("...: {}", str(e))` in new `except Exception as e:` blocks.
- Do NOT edit `api/os_router.py` / `api/metric_router.py` (marked `!!!!!! НЕ РЕДАКТИРОВАТЬ !!!!!`).
- Pydantic monkey-patch: `_wrap_llm_with_stop_event` swaps `llm.invoke`/`ainvoke` via
  `object.__setattr__` — GigaChat is a `BaseModel` and rejects plain `setattr` on non-fields.
- `src/aigw_service/giga_test.py` is a standalone scratch script, not part of the app.
- Coverage goes to `coverage.xml` + `term-missing` (from `--cov=src` in addopts); `test-results.xml`
  is generated junit output — both are noise, don't commit them.
