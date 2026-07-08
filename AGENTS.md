# AGENTS.md

## Project
AI Gateway REST service (`aigw-rest-service`) — FastAPI + LangGraph agent for Excel cashflow model analysis. Python 3.12+, Poetry.

## Commands

```sh
# run (default — python, no Docker)
python3 src/aigw_service/__main__.py 2>&1 | tee server.log

# run (Docker — primary deployment)
docker compose up -d
docker compose logs app -f      # live logs
docker compose down             # stop

# lint / format — MUST run `ruff check --fix src` then `ruff format src`
ruff check src           # check only
ruff check --fix src     # auto-fix
ruff format src          # format
pylint src               # must score >7

# test (from host venv, NOT from docker container)
pytest tests             # -v -s --maxfail=1 --cov=src
pytest tests/test_tools_performance.py -v -s --no-header --no-cov  # ~2.5 min (no --maxfail, runs all 7 tests)

# tool query validation (requires running server)
python scripts/run_tool_queries.py --url http://localhost:8080 --log server.log [--subset N]

# deps
poetry install
poetry add <pkg>
poetry update            # after pyproject.toml changes
```

## Architecture

- **Package**: `aigw_service` in `src/` (`[tool.poetry.packages]` with `from = "src"`). Entrypoint: `__main__.py` → `uvicorn.run(app_main)`.
- **App init**: `api/__init__.py` creates FastAPI, mounts: service (health), metric, v1 (`/api/v1`).
- **Config**: `config/__init__.py:Secrets` bundles settings classes. `BaseAppSettings.local` (`LOCAL` env) switches protocol (http/https) and cert injection. Default `LOCAL=false` → no cert validation needed.
- **AppContext** (`context.py`): singleton holding loguru logger, LLM, agent memory. Created at import time.
- **Model backend**: `MODEL_TO_USE=OLLAMA` or `GIGACHAT` (env var). Default Ollama with `qwen2.5:7b` (docker.env).
- **Store backend**: `STORE_TO_USE=MEMORY` or `PANGOLIN` (env var). Default `MEMORY`.
- **Agent**: LangGraph state machine in `api/v1/services.py` — init → analyze → execute_tool → analyze (loop) → END. Tools in `api/v1/tools.py`.
- **Excel backend**: `api/v1/excel_handler.py` — cross-platform using **openpyxl** (I/O) + **formulas** (in-memory formula evaluation).
  - **Two-workbook pattern**: `self._wb` (data_only=False, formulas) for edits + saves; `self._wbv` (data_only=True, values) for reads. **Critical** — `save()` on a `data_only=True` workbook strips formulas.
  - **`calculate()`**: calls `formulas.ExcelModel.calculate(inputs=..., outputs=...)` — full dependency graph evaluation in memory (~5–35s). Tools should pass explicit `outputs=` to prune the graph and avoid the full evaluation cost. `get_cell()` caches results in `self._solution`; `set_cell()` invalidates it.
- **Private dep stub**: `sber-aigw` replaced with local stubs in `src/aigw_modules/`. Only 3 imports used (all in `context.py`). No auth needed.
- **Name resolution pipeline**: `tools.py:find_matching_cell` / `find_matching_outputs` используют `jaccard_similarity` + `normalize_text` для fuzzy-маппинга английских алиасов из запроса → канонические русские имена из листа Inputs/Outputs.
- **Agent examples**: `api/v1/agent_examples/` — reference LangGraph agents (react, memorizer, graph).
- **All deps from public PyPI** — both private repos (`sberosc`, `nexus-release`) were removed.

## Key quirks

- **Model cache**: `excel_handler._load_model` is `@lru_cache(maxsize=3)`. The `formulas.ExcelModel` loaded via `loads().finish()` (~34s) is cached by file path and shared across `ExcelWorkbook` instances in the same process.
- **`_solution` cache**: `get_cell()` stores results in `self._solution` and merges on each call; `set_cell()` invalidates it. Tools call `calculate(outputs=all_refs)` once, then individual `get_cell()` calls hit the cache — no redundant recalc.
- **Analysis cache**: `analyze_excel_model` results are cached (LRU, max 10 entries) keyed by `(file_path, input_names, output_names, output_years, ranges, steps, user_id)`.
- Tests use `httpx.AsyncClient` with `app=app_main` (ASGI transport, no real server). Integration conftest calls `APP_CTX.on_startup()`.
- `asyncio_mode = auto` in pytest config — no `@pytest.mark.asyncio` needed.
- Test env vars in `[tool.pytest.ini_options.env]` — `GIGACHAT_HOST` and `GIGACHAT_PORT` required.
- Integration tests expect 5 headers (validated in `api/v1/utils.py`): `x-trace-id` (UUID), `x-client-id` (2 letters + 8 digits), `x-request-time` (RFC-3339), `x-session-id` (UUID, optional), `x-user-id` (≤8 chars, needed for upload).
- Do NOT edit: `api/os_router.py`, `api/metric_router.py` — marked `!!!!!! НЕ РЕДАКТИРОВАТЬ !!!!!!`.
- **Tool query tests** (`scripts/run_tool_queries.py`): тестирует LLM + resolution через production-сервер. Читает 600 промптов из `.xlsx`, ловит `TOOL ARGS` в server.log по `x-trace-id`, резолвит имена через тот же Jaccard-пайплайн. Флаги: `--subset`, `--resume`, `--timeout`.
- `giga_test.py` at root of `aigw_service` — standalone script, not part of app.
- Coverage: `--cov=src`, output to `coverage.xml` + `term-missing`.
- `LOCAL` flag (default False): when False, cert paths in GigaChat/Pangolin config are empty strings, no cert files needed even with Ollama or MEMORY store.

## Docker workflow (primary deployment)

```sh
docker compose up -d                 # starts app + ollama
curl http://localhost:8888/health    # port from .env APP_PORT (default 8080)
docker compose build app --no-cache  # rebuild image after src/ changes
docker compose restart app           # fast restart without rebuild
docker compose logs app -f           # live logs
docker compose down                  # stop (volumes persist)
```


