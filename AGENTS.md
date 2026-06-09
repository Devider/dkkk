# AGENTS.md

## Project
AI Gateway REST template — FastAPI + LangGraph agent for Excel cashflow model analysis. Python 3.12+, Poetry.

## Commands

```sh
# run
python3 src/aigw_service/__main__.py

# lint / format — MUST run `ruff check --fix src` then `ruff format src`
ruff check src           # check only
ruff check --fix src     # auto-fix
ruff format src          # format
pylint src               # must score >7

# test
pytest tests             # verbose, coverage, fail-fast on 1st error

# deps
poetry install
poetry add <pkg>
poetry update            # after pyproject.toml changes
```

## Architecture

- **Entrypoint**: `src/aigw_service/__main__.py` → `uvicorn.run(app_main)`
- **Package**: `aigw_service` in `src/` (declared in `[tool.poetry.packages]` with `from = "src"`)
- **App init**: `api/__init__.py` creates FastAPI app, mounts routers: service (health), metric, v1 (`/api/v1`)
- **Config**: Pydantic-settings classes in `config/`, loaded from `.env` via `python-dotenv`
- **AppContext** (`src/aigw_service/context.py`): singleton holding logger (loguru), LLM, agent memory. Created at import time.
- **Logger**: loguru, writes to console + file. Masking config in `logger/utils.py`. K2 data must NOT appear in logs.
- **Model backend**: `MODEL_TO_USE=OLLAMA` or `GIGACHAT` (env var). `LOCAL` flag controls cert injection.
- **Store backend**: `STORE_TO_USE=MEMORY` or `PANGOLIN` (env var). Default is `MEMORY`.
- **Agent**: LangGraph state machine in `api/v1/services.py` — init → analyze → execute_tool → analyze (loop) → END. Tools in `api/v1/tools.py`.
- **Excel backend**: `api/v1/excel_handler.py` — cross-platform Excel handler using **openpyxl** (I/O) + **LibreOffice** (formula recalculation). Replaces xlwings (Windows-only).
  - **Two-workbook pattern**: `self._wb` (data_only=False, formulas) for edits + saves; `self._wbv` (data_only=True, values) for reads. This is **critical** — openpyxl `save()` on a `data_only=True` workbook strips formulas from XML, breaking subsequent recalculations.
  - **`calculate()` force-recalc**: save formulas-workbook → close → `_clear_cached_formula_values()` (zip-patch: `fullCalcOnLoad=1`, clear `<v>` on formula cells) → LO round-trip (XLSX→ODS→XLSX) → reopen both.
- **Private dep stub**: `sber-aigw` replaced with local stubs in `src/aigw_modules/`. Only 3 imports used from it (all in `context.py`). No auth needed.
- **All deps from public PyPI** — both private repos (`sberosc`, `nexus-release`) were removed.

## Key quirks

- Tests use `httpx.AsyncClient` with `app=app_main` (ASGI transport, no real server). `conftest.py` calls `APP_CTX.on_startup()`.
- `asyncio_mode = auto` in pytest config — no need for `@pytest.mark.asyncio`
- Test env vars are set in `[tool.pytest.ini_options.env]` in `pyproject.toml`. Must set `GIGACHAT_HOST`, `GIGACHAT_PORT` at minimum.
- Integration tests call `/upload` and `/invoke-agent` endpoints. Expect `x-trace-id` and `x-request-time` headers.
- Some `api/` files are marked "do not edit": `os_router.py`, `metric_router.py`, `schemas.py`, `middleware.py`, `logger/models.py`
- Duplicated `giga_test.py` at root of `aigw_service` — standalone script, not part of app.
- Coverage report: `--cov=src`, output to `coverage.xml` + `term-missing`. Run `pytest tests` to generate.
- Cert path validators in `config/gigachat/config.py` and `config/pangolin/config.py` skip empty strings — no cert files needed when using Ollama or MEMORY store.

## Linux Excel setup (required for formula recalculation)

Excel tools work without real Microsoft Excel. Formula recalculation uses **LibreOffice**:
```bash
sudo apt-get install libreoffice-calc graphviz
```
`graphviz` is optional — dependency graph tool falls back to saving `.dot` files if the `dot` command is missing.
No configuration needed — `ExcelWorkbook.calculate()` calls `libreoffice --headless --convert-to` automatically.
