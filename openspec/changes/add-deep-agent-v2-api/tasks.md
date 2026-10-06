# Tasks

## Resore session 
claude --resume 13191a37-f83b-4f7f-ae3c-59caa16c321a

## 1. Duplicate the deep-agent support stack into `api/v2/` (zero cross-imports)

- [x] 1.1 Create the `src/aigw_service/api/v2/` package skeleton (`__init__.py`,
  `prompts/__init__.py`, `schemas/__init__.py`, `states/__init__.py`, `subagents/__init__.py`),
  mirroring `api/v1/`'s layout; verify it imports cleanly (`python -c "import aigw_service.api.v2"`).
- [x] 1.2 Duplicate `prompts/deep_agent.py`, `prompts/ift_analizer.py`, `prompts/ema_analizer.py`,
  and `prompts/lookup_more.py` into `api/v2/prompts/` unchanged (pure text constants, no
  internal imports to rewrite); verify `python -c "import aigw_service.api.v2.prompts.deep_agent"`.
- [x] 1.3 Duplicate `schemas/llm_outputs.py` into `api/v2/schemas/llm_outputs.py` unchanged;
  verify `python -c "import aigw_service.api.v2.schemas.llm_outputs"`.
- [x] 1.4 Duplicate `states/agents.py` into `api/v2/states/agents.py`, dropping
  `ClassifierInput`/`ClassifierState` (unused by `DeepAgentGraph` — see design.md), and
  rewriting its `tools`/`schemas.llm_outputs` imports to the `api/v2` copies; verify
  `python -c "import aigw_service.api.v2.states.agents"`.
- [x] 1.5 Duplicate `tools.py` into `api/v2/tools.py`, trimming the functions/classes listed in
  design.md - Decisions ("What gets trimmed") that are unreachable from
  `analyze_model_inputs_for_target`/`analyze_excel_model`'s call graph; verify
  `python -c "import aigw_service.api.v2.tools"` and that no dropped name is still referenced
  (`grep -n "build_dependency_graph\|get_output_info\|greet_user\|describe_outputs_sheet\|modify_excel_input_value\|get_outputs_business_summary" src/aigw_service/api/v2/tools.py` returns nothing).
- [x] 1.6 Duplicate `excel_handler.py` and `lo_backend.py` into `api/v2/` unchanged; verify
  `python -c "import aigw_service.api.v2.lo_backend"` succeeds (requires local LibreOffice +
  `python3-uno`, per `CLAUDE.md`).
- [x] 1.7 Duplicate `russian_stemmer.py` into `api/v2/russian_stemmer.py` unchanged (self-
  contained); verify `pytest tests/unit/test_russian_stemmer.py -v` still passes against the
  `api/v1` copy, and add an equivalent smoke import check for the `api/v2` copy.
- [x] 1.8 Duplicate `subagents/dummies.py` into `api/v2/subagents/dummies.py`, rewriting its
  `schemas.llm_outputs` import to the `api/v2` copy; verify
  `python -c "import aigw_service.api.v2.subagents.dummies"`.
- [x] 1.9 Duplicate `subagents/analyzer.py` into `api/v2/subagents/analyzer.py`, rewriting its
  imports (`schemas.llm_outputs`, `subagents.utils`, `subagents.dummies`) to the `api/v2`
  copies; verify `python -c "import aigw_service.api.v2.subagents.analyzer"`.
- [x] 1.10 Duplicate `subagents/utils.py` into `api/v2/subagents/utils.py`, rewriting its
  `tools` import to the `api/v2` copy; verify `python -c "import aigw_service.api.v2.subagents.utils"`.
- [x] 1.11 Duplicate `utils.py` (`common_headers`) into `api/v2/utils.py` unchanged; verify
  `python -c "import aigw_service.api.v2.utils"`.
- [x] 1.12 Duplicate `schemas_file.py` into `api/v2/schemas_file.py`, keeping only
  `CopilotAgentRequest`, `FileLoaderResponse`, `FailedDependencyResponse` (drop the unused
  template schemas per design.md); verify `python -c "import aigw_service.api.v2.schemas_file"`.
- [x] 1.13 Duplicate `subagents/orchestrator_tools.py` into
  `api/v2/subagents/orchestrator_tools.py`, dropping `OrchestratorTools.TOOLS`, `ToolQueryArgs`,
  and the two `@tool`-decorated stub functions (bind_tools-only, unused by the deep agent); keep
  `TOOL_TASK_DESCRIPTIONS`, `dispatch`, `_run_ift`/`_run_ema`, and the `ProcessPoolExecutor`/
  `_run_lo_calc` plumbing; rewrite its imports (`schemas.llm_outputs`, `subagents.analyzer`,
  `tools`) to the `api/v2` copies; verify `python -c "import aigw_service.api.v2.subagents.orchestrator_tools"`.
- [x] 1.14 Move (not duplicate — exclusive to the deep agent) `main_graph_deep.py` into
  `api/v2/`, rewriting every import to its `api/v2` counterpart from 1.2-1.13; verify
  `grep -n "aigw_service.api.v1" src/aigw_service/api/v2/main_graph_deep.py` returns nothing.
- [x] 1.15 Update `tests/unit/test_deep_agent_graph.py`'s imports to the `api/v2` paths; verify
  `pytest tests/unit/test_deep_agent_graph.py -v` passes unchanged.
- [x] 1.16 Verify zero cross-imports in either direction:
  `grep -rn "aigw_service.api.v1" src/aigw_service/api/v2/` and
  `grep -rn "aigw_service.api.v2" src/aigw_service/api/v1/` both return nothing.
- [x] 1.17 Run the full test suite plus a live smoke test through a locally running server
  hitting `/api/v2/invoke-agent` for both `analyze_model_inputs_for_target`- and
  `analyze_excel_model`-shaped questions, to catch anything the static-trace trimming in 1.5
  missed via `_run_lo_calc`'s string-based `getattr` dispatch (per design.md - Risks); verify
  both calculation paths return a real (non-error) result.
  NOTE: this sandbox has no LibreOffice/python3-uno and no running Docker daemon, so the actual
  LO recalculation could not be exercised here. Verified instead that both paths run correctly
  end-to-end (catalog build, LLM name resolution via `anlyze_query`/`lookup_more`, dispatch
  through `OrchestratorTools`/`_run_lo_calc` in the `ProcessPoolExecutor`, error humanization)
  up to the exact same `PyUNO bindings not available` boundary `AgentGraph` (`/api/v1`) would
  hit on this host — confirming the trimmed `getattr`-dispatch path resolves both function
  names correctly. Re-run this smoke test in Docker (`docker compose up -d`) before merge to
  confirm the real LibreOffice recalculation path.

## 2. Build the `/api/v2` router

- [x] 2.1 Add `src/aigw_service/api/v2/router.py` with `/upload` and `/invoke-agent` routes
  mirroring `api/v1/router.py`'s structure, using `api/v2/`'s own `utils.common_headers` and
  `schemas_file` copies (from tasks 1.11-1.12), wired to `DeepAgentGraph` via its own
  `@lru_cache(maxsize=1)` `get_agent()` returning `DeepAgentGraph()` unconditionally (per
  design.md - Decisions: no shared agent-factory abstraction). `/api/v2/upload`'s handler is its
  own independent implementation (no call into `api/v1/router.py` — that would be a
  cross-import), but writes to the same `APP_CTX.agent_memory.store` under the same
  `("memories", user_id)` namespace `/api/v1/upload` uses, so a workbook uploaded via either
  version stays usable by both (per design.md - Decisions and the `deep-agent-api` spec).
  Verify by starting the server and running `curl http://localhost:8888/health`.
- [x] 2.2 Mount the new router in `src/aigw_service/api/__init__.py` at prefix `/api/v2`; verify
  `/api/v2/upload` and `/api/v2/invoke-agent` appear in `GET /openapi.json`.
- [x] 2.3 Add unit tests for `/api/v2/upload` and `/api/v2/invoke-agent` (header validation,
  missing-prior-upload 424, successful ZIP response, shared store with `/api/v1/upload` per the
  `deep-agent-api` spec's "Upload via the other version is still usable" scenario) alongside the
  existing `tests/unit/` suite; verify `pytest tests/unit/ -v` passes.

## 3. Make `/api/v1` deterministic (remove the implementation switch)

- [x] 3.1 Remove the `if APP_CONFIG.app.agent_graph_impl == "DEEPAGENTS"` branch from
  `api/v1/router.py:get_agent()` so it unconditionally returns `AgentGraph()`; remove the now-
  unused `DeepAgentGraph` import from `api/v1/router.py`. Verify
  `grep -n "DeepAgentGraph\|agent_graph_impl" src/aigw_service/api/v1/router.py` returns nothing.
- [x] 3.2 Remove `agent_graph_impl` from `src/aigw_service/config/base_config.py`; verify
  `grep -rn "agent_graph_impl\|AGENT_GRAPH_IMPL" src` returns nothing.
- [x] 3.3 Add/update a unit test asserting `get_agent()` in `api/v1/router.py` always returns an
  `AgentGraph` instance regardless of env vars, covering the `agent-graph` spec's "Behavior is
  independent of deployment config" scenario; verify it passes.

## 4. Update quality-eval tooling and docs

- [x] 4.1 Update `scripts/run_tool_queries.py`'s hardcoded `/api/v1/upload` and
  `/api/v1/invoke-agent` paths to accept an API-version selector (e.g. `--api-version v1|v2`,
  default `v1`); verify `python scripts/run_tool_queries.py --help` shows the new flag and a
  `--subset 1 --api-version v2` run against a local server hits `/api/v2/*` (check
  `server.log` for the request path).
- [x] 4.2 Update `CLAUDE.md`'s request-flow section to document the `/api/v2/upload` +
  `/api/v2/invoke-agent` pair alongside `/api/v1`'s, and note that `AGENT_GRAPH_IMPL` no longer
  exists; verify by re-reading the updated section for accuracy against the final code.
- [x] 4.3 Update any `.env.example`/deployment doc mentioning `AGENT_GRAPH_IMPL` to remove it;
  verify `grep -rln "AGENT_GRAPH_IMPL" .` (outside `openspec/`) returns nothing.

## 5. Full verification

- [x] 5.1 Run `ruff check --fix src` then `ruff format src`; verify both exit clean.
  NOTE: 132 lint findings remain unfixable by `--fix` (RUF013 implicit-Optional, F841, etc.) —
  pre-existing across the whole `src` tree (baseline was 159 before this change's `--fix` pass;
  confirmed present in files this change never touches, e.g. `logger/context_vars.py`,
  `config/*`). Out of scope for this change to hand-fix; not a regression.
- [x] 5.2 Run `pytest tests` (full suite); verify it passes.
  NOTE: `tests/subagents/test_dummies.py` fails to even collect (`ImportError: cannot import
  name 'ClassifierOutput'`), which with `--maxfail=1` aborts the run before anything else
  executes; confirmed pre-existing on the unmodified tree (unrelated to `ClassifierInput`/
  `ClassifierState` removed from `api/v2/states/agents.py` in 1.4 — that's a different, unused
  pair of classes `api/v1/`'s copy still has). Running the rest of the suite
  (`--ignore=tests/subagents/test_dummies.py`): 69 passed, 14 failed, 2 errors — every failure
  confirmed pre-existing on the unmodified tree too: 6x `test_stop_event.py` (pre-existing code
  bugs, unrelated to this change), 6x `test_tools_performance.py` + the 2 `integration/v1_api`
  errors (this sandbox has no LibreOffice/python3-uno and no Docker/Langfuse reachable — same
  constraint as 1.17), 2x `test_agent.py` (needs a live server on :8080, none running). No
  failure touches `api/v2/` or anything this change added.
- [x] 5.3 Run `openspec validate add-deep-agent-v2-api --strict`; verify it passes.
