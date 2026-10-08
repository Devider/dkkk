# Proposal

## Why

The two LibreOffice calculation dispatches (`analyze_model_inputs_for_target`, `analyze_excel_model`)
are the slowest, most failure-prone steps in the agent graph, but neither shows up in Langfuse as a
discrete observation today — the LangChain `CallbackHandler` attached at `graph.ainvoke()` only sees
LLM/tool-calling-node boundaries, not the `ProcessPoolExecutor` dispatch inside `OrchestratorTools`.
Timing, args, and results for these dispatches currently live only in plain loguru lines split across
the main process and the spawned worker process. Making them explicit Langfuse `tool` observations
gives visibility into the actual cost/failure driver of each request.

## What Changes

- Add a small helper in `orchestrator_tools.py` (`_dispatch_lo_calc`) that drives an explicit
  LangChain tool run (`on_tool_start` / `on_tool_end` / `on_tool_error`) around each of the two
  `await asyncio.to_thread(self._lo_pool.submit(...).result)` calls, using the callback manager
  inherited from the graph node's own `RunnableConfig`. The already-registered Langfuse
  `CallbackHandler` turns this into a `tool`-typed Langfuse observation automatically.
  (Originally planned as a raw `Langfuse.start_as_current_observation(...)` call guarded by
  inspecting `APP_CTX.tracing` directly — changed after empirically finding that approach creates
  a disconnected root trace; see Design's Decisions for why.)
- `config: RunnableConfig` is threaded from the `tool_executor` graph node down through
  `dispatch` / `_run_ift` / `_run_ema` to reach `_dispatch_lo_calc`.
- No-op is automatic and requires no explicit guard: when no Langfuse (or other LangChain-compatible)
  callback handler is registered on `config["callbacks"]`, `on_tool_start` with no handlers does
  nothing and `_dispatch_lo_calc` degrades to a plain dispatch.
- One flat tool run per call, named after the literal tool name (`"analyze_model_inputs_for_target"` /
  `"analyze_excel_model"`) — no child runs for cache-hit vs. real recalculation.
- Fix a one-line metadata key typo in `router.py` (`"lang_fuse_user_id"` → `"langfuse_user_id"`) so
  `user_id` actually reaches the Langfuse trace that these new tool observations nest under.

## Capabilities

### New Capabilities

- `lo-calc-tracing`: Langfuse `tool`-observation spans wrapping the two LibreOffice calculation
  dispatches in `OrchestratorTools`, capturing input/output/duration/errors, degrading to a no-op
  whenever no tracing callback handler is registered on the request's `config` (e.g. Langfuse
  disabled or unreachable).

### Modified Capabilities

(none — no existing specs cover tracing or dispatch behavior; the `user_id` metadata-key fix is a bug
fix on code that never worked as intended, not a behavior change to an existing documented capability)

## Impact

- `src/aigw_service/api/v1/subagents/orchestrator_tools.py` — new `_dispatch_lo_calc` tracing
  helper; `dispatch`, `_run_ift`, `_run_ema` all gain a `config: RunnableConfig` parameter.
- `src/aigw_service/api/v1/main_graph.py` — `tool_executor` and `_execute_single_call` thread
  `config` through to `OrchestratorTools.dispatch`.
- `src/aigw_service/api/v1/router.py` — one-line metadata key rename.
- No changes to `tools.py`, the `ProcessPoolExecutor`/caching design, or tracing inside the spawned
  worker process (out of scope — a separate process has no access to the parent's OTEL context).
- No new dependencies: reuses the already-registered Langfuse `CallbackHandler` via the existing
  LangChain callback-manager machinery (`langchain_core.runnables.config
  .get_async_callback_manager_for_config`) — no direct `APP_CTX.tracing`/raw Langfuse SDK access
  needed in `orchestrator_tools.py` any more.
