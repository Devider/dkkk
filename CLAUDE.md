# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

AI Gateway REST service (`aigw-rest-service`) — FastAPI + LangGraph multi-agent for Excel cashflow-model
scenario analysis. Python 3.12+, Poetry. This repo also has an `AGENTS.md` with earlier notes; where the
two disagree, trust this file and the code — the graph architecture and LLM backend changed since AGENTS.md
was last updated (LibreOffice replaced `formualizer`, and GigaChat replaced Ollama as the active default).

## Spec-driven development (OpenSpec)

This repo plans and documents behavior changes with [OpenSpec](https://github.com/Fission-AI/OpenSpec)
(`openspec/`, schema `spec-driven`). Use the `opsx:*` skills for anything that changes observable behavior:
`opsx:explore` to think through an idea, `opsx:propose` to create `openspec/changes/<name>/`
(proposal.md → specs/ → design.md → tasks.md), `opsx:apply` to work through `tasks.md`, `opsx:archive` to
fold an approved change's deltas into `openspec/specs/` once shipped. `opsx:sync` updates main specs
without archiving; `opsx:update` revises an in-flight change's own artifacts.

- **`openspec/specs/` is currently empty** — no capability has been backfilled yet. Before, or as part of,
  the first real feature proposal, write baseline specs for the capabilities it touches (e.g. `agent-graph`,
  `excel-analysis`, `name-resolution`) so the delta has something to diff against; don't let the first
  proposal silently assume behavior that was never captured.
- Specs are behavior contracts (SHALL requirements + WHEN/THEN scenarios) for externally observable
  behavior — not a restatement of this file. This file stays the place for implementation facts an agent
  can rediscover by reading code (commands, module layout, backend quirks); don't duplicate that into spec
  prose, and don't let specs leak internals like class/function names or library choices.
- `openspec/config.yaml`'s `context:` field carries constraints that OpenSpec artifact generation needs
  but can't discover from the code — keep it in sync with real constraints as they change. This file
  remains the source of truth for everything that *is* discoverable from the code.
- For a change with no spec-level behavior impact (pure refactor, tooling, docs, CI), set `skip_specs: true`
  in that change's `.openspec.yaml` rather than inventing a requirement just to satisfy `openspec validate`.

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

Two independently versioned, independently maintained API surfaces are mounted side by side in
`api/__init__.py` — `/api/v1` (legacy orchestrator) and `/api/v2` (deep agent). There is no
deployment-time flag that picks between them (the old `AGENT_GRAPH_IMPL` env var is gone): the
implementation is a URL choice, made per request.

```
POST /api/v1/upload
  saves the .xlsx to /tmp/user_file_{x-user-id}.{ext}
  records the filename in the agent memory store (namespace ("memories", user_id))

POST /api/v1/invoke-agent
  -> AgentGraph.graph.ainvoke(...)   (src/aigw_service/api/v1/main_graph.py)
  -> response is a ZIP (txt_response.txt + generated/agent_output.xlsx placeholder)

POST /api/v2/upload
  same contract as /api/v1/upload, independent handler, same memory-store namespace

POST /api/v2/invoke-agent
  -> DeepAgentGraph.graph.ainvoke(...)   (src/aigw_service/api/v2/main_graph_deep.py)
  -> same ZIP response envelope as /api/v1/invoke-agent
```

Both pairs require the same 5 headers, validated independently by each version's own
`utils.py` (`api/v1/utils.py` / `api/v2/utils.py`, byte-identical today): `x-trace-id` (UUID v4),
`x-client-id` (`^[A-Z]{2}\d{8}$`), `x-request-time` (RFC-3339), `x-session-id` (UUID, optional),
`x-user-id` (≤8 chars, required for upload/lookup). A header mismatch returns 422.

`/api/v1/upload` and `/api/v2/upload` write to the *same* `("memories", user_id)` store
namespace (keyed by `x-user-id`, not by API version) — a workbook uploaded via either version is
readable by both. Conversation history is **not** shared, despite both graphs compiling against
the same `APP_CTX.agent_memory.checkpointer` (`MemorySaver`) singleton: `/api/v1` keys it
`{user_id}:{session_id}`, `/api/v2` keys it `v2:{user_id}:{session_id}` — the version prefix is
load-bearing (not cosmetic), since the two graphs use incompatible state schemas and sharing a
thread would corrupt state, not just leak messages.

`api/v1/` and `api/v2/` hold fully independent copies of every module each graph needs
(`tools.py`, `excel_handler.py`/`lo_backend.py`, `subagents/`, `schemas/`, `states/`,
`prompts/`, `russian_stemmer.py`, `utils.py`, `schemas_file.py`) — zero cross-imports between
them in either direction, by design (see `openspec/changes/add-deep-agent-v2-api/design.md` if
still present, or `openspec/specs/deep-agent-api/`). This means a correctness fix to logic both
versions conceptually share (Jaccard/name-resolution matching, a LibreOffice/UNO quirk, GigaChat
stop-event handling) must be applied by hand to **both** `api/v1/`'s and `api/v2/`'s copies —
there is no shared module or generation step keeping them in sync, and the two may drift apart
over time. `api/v2/`'s copies are trimmed to only what `DeepAgentGraph` actually reaches (e.g.
`subagents/orchestrator_tools.py` drops the native-tool-calling-only `OrchestratorTools.TOOLS`;
`tools.py` drops the dependency-graph/business-summary functions neither graph calls).

### `/api/v2` deep-agent architecture

Active development happens on `/api/v2`. Its architecture (deep agent + `task` subagents, process-pool
LO calcs, dedup, recursion limit, v1-vs-v2 differences) is documented in
`src/aigw_service/api/v2/README.md`. It is not loaded by default. Read it only when a task touches
`api/v2/` and you need details beyond this file, e.g. before changing the graph, subagents, prompts or
calc dispatch. Skip it for v1-only work or trivial v2 edits. Keep it up to date when changing `api/v2/`
architecture.

### The agent graph (current — supersedes the old single-file `services.py` state machine)

`src/aigw_service/api/v1/main_graph.py:AgentGraph` is what `router.py` actually wires up. `services.py`
is legacy/unused — nothing imports it; don't extend it, extend `main_graph.py` and `subagents/` instead.
There is no upfront `classifier` node anymore — tool selection happens inside a bounded ReAct-style loop,
not as a one-shot branch before the graph fans out.

Graph shape:
```
START -> extract_excel_data -> orchestrator <--> tool_executor
                                     |
                                     v (no tool_calls, or MAX_ORCHESTRATOR_ITERATIONS=4 hit)
                                 synthesizer -> END
```

- **extract_excel_data** (`get_available_inputs_outputs`): loads the uploaded workbook's filename from
  the memory store (keyed by `user_id`, namespace `("memories", user_id)`) and builds the input/output
  name catalog via `subagents/utils.py:_build_catalog_with_ids`. Also resets the per-turn loop counters
  (`tool_call_count`, `executed_tool_fingerprints`, `tool_results`) — the checkpointer persists state
  across `ainvoke` calls within a `thread_id`, so without this reset they'd leak from the previous turn.
- **orchestrator**: the single decision point. A tool-calling LLM (`llm.bind_tools(OrchestratorTools.TOOLS)`)
  sees the full message history each iteration and either emits `tool_calls` (can be more than one per
  turn) or doesn't. When it doesn't call a tool, its own draft text is deliberately **not** persisted to
  `messages` — only `synthesizer` produces the real final answer, to avoid two competing AIMessages per turn.
- **route_after_orchestrator**: `tool_executor` if the last AIMessage has `tool_calls` and
  `tool_call_count < MAX_ORCHESTRATOR_ITERATIONS`; otherwise `synthesizer` (iteration cap hit is logged
  as a warning and forces synthesis even with unresolved tool_calls).
- **tool_executor**: runs every tool_call from the last AIMessage through `OrchestratorTools.dispatch`
  (`subagents/orchestrator_tools.py`), which wraps the same IFT/EMA `AnalyzerSubAgent`s
  (`subagents/analyzer.py`, LLM structured output + a lookup-more retry step to resolve free-text aliases
  against the catalog) and the real calculation functions (`tools.py:analyze_model_inputs_for_target` /
  `analyze_excel_model`) that the old fixed-branch nodes used. Identical tool calls within the same turn
  are deduped by a fingerprint (`_fingerprint_call`) — a repeat gets a canned "already done" ToolMessage
  instead of re-executing. Always loops back to `orchestrator`, never forward to `synthesizer` directly.
- **synthesizer**: final LLM call that turns the accumulated `tool_results` plus message history into the
  natural-language answer (same role the old `reviewer` node had). Appends a filler `HumanMessage` when
  the history doesn't already end on one — e.g. the iteration cap forced synthesis mid-loop — because
  GigaChat returns an unparseable completion when the prompt's last message is an AIMessage with
  unresolved tool_calls.

Structured LLM outputs (`QueryAnalysisIFT`, `QueryAnalysisEMA`, `LookupResult`, `InputItem`, ...) live in
`api/v1/schemas/llm_outputs.py`; graph state TypedDicts live in `api/v1/states/agents.py`; prompts
(`orchestrator.py`, `synthesizer.py`, `ift_analizer.py`, `ema_analizer.py`, `lookup_more.py`) live in
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
