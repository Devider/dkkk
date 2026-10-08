# Intent: trace LO calc dispatch as Langfuse tool spans

## Problem

The two LibreOffice calculation dispatches — `analyze_model_inputs_for_target` and
`analyze_excel_model` — are the slowest, most failure-prone steps in the agent graph (they run a
real LO recalculation in a dedicated single-worker `ProcessPoolExecutor`, taking anywhere from
~1s to tens of seconds depending on scenario count). Today neither shows up in Langfuse as a
discrete unit: the LangChain `CallbackHandler` attached at `router.py`'s `graph.ainvoke()` call
only sees LLM/tool-calling-node boundaries. The useful timing/arg/result information that *does*
exist is split across plain loguru lines in the main process (`orchestrator_tools.py`) and the
spawned worker process (`tools.py`), invisible to Langfuse either way.

We want these two calls to show up in Langfuse as explicit **tool** observations (not generic
spans), created with the SDK's context-manager style (`with ... as span:`), reusing the Langfuse
client the app already constructs at startup — not a new client, not the LangChain callback path.

## Current State

- **Call sites**: `src/aigw_service/api/v1/subagents/orchestrator_tools.py:194-196` (`_run_ift` →
  dispatches `analyze_model_inputs_for_target`) and `:250` (`_run_ema` → dispatches
  `analyze_excel_model`):
  ```python
  calc = await asyncio.to_thread(
      self._lo_pool.submit(_run_lo_calc, "analyze_model_inputs_for_target", kwargs).result
  )
  ```
  ```python
  calc = await asyncio.to_thread(self._lo_pool.submit(_run_lo_calc, "analyze_excel_model", kwargs).result)
  ```
- Both go through `self._lo_pool` — a `concurrent.futures.ProcessPoolExecutor(max_workers=1,
  mp_context=multiprocessing.get_context("spawn"))` built once in `OrchestratorTools.__init__`
  (`orchestrator_tools.py:120-129`) — via `_run_lo_calc` (`orchestrator_tools.py:55-71`), which
  calls into the real synchronous functions in `tools.py` inside the spawned worker process.
- `kwargs` for each call is a plain JSON-serializable dict (file name, resolved input/output
  names, tolerance/ranges/steps, `user_id`) built just above each call site — safe to use directly
  as a span `input`.
- Existing loguru logging: `_run_ift`/`_run_ema` log a one-line "dispatch" summary *before* the
  call (`orchestrator_tools.py:174-180`, `:232-240`); no log wraps the `asyncio.to_thread` call
  itself. `tools.py` logs start/complete/elapsed *inside the worker process*
  (`tools.py:314,399,888,932`) — invisible to the main process and to any tracing done there.
- Results are already passed through `_safe_content` (strips non-JSON-serializable values, e.g.
  the raw `pandas.DataFrame` that `analyze_excel_model` puts in `content`) before crossing back
  from the worker process, so `calc` is safe to use directly as a span `output`.
- An in-process LRU-style cache (`tools.py:40-45`, hand-rolled dict, not `functools.lru_cache`)
  lives inside the persistent worker process and can make a "dispatch" return near-instantly on a
  cache hit. The span should reflect this naturally via its duration — no special-casing needed
  (see Desired Behavior).
- **Langfuse integration**: `langfuse==4.15.4` is installed (OTEL-based v3+ client;
  `pyproject.toml:41` / `poetry.lock:1729-1731`), exposed as `APP_CTX.tracing`
  (`context.py:144-148`), built via `TracingManager` → `LangFuse.get_tracing()`
  (`core/tracing/tracing.py:89-108`) into a `LangfuseClient` instance
  (`src/aigw_modules/hub_services/langfuse.py:15-381`). That `LangfuseClient` is explicitly
  documented (`src/aigw_service/api/README.md:21-26`) as a **local-dev-only stand-in** — production
  swaps in a different private-package client. The design must degrade gracefully when
  `APP_CTX.tracing` is `None`, or is an `AEFTracingHandler` instead of a `LangfuseClient` (AEF and
  Langfuse are mutually exclusive backends — `TracingManager.get_tracing` in
  `core/tracing/tracing.py:103-108` picks exactly one, never both), or when `.client` is `None`
  (not yet started / health check not run).
- `LangfuseClient.client` (`langfuse.py:202-204`) exposes the raw `Langfuse` instance, which has
  `start_as_current_observation(name=..., as_type=..., input=..., output=..., metadata=...)`.
  Confirmed against the installed package (`langfuse/_client/client.py`) that `as_type="tool"` is
  a valid literal, with a documented usage example matching exactly what's wanted:
  ```python
  with langfuse.start_as_current_observation(name="web-search", as_type="tool") as tool:
      ...
  ```
  The returned context manager is OTEL's `_AgnosticContextManager` — it auto-captures exceptions
  raised inside the `with` block (sets span status to `ERROR` and records the exception) and
  re-raises them, so no manual `try/except` is needed around the traced call.
- No code anywhere in `src/` currently uses `start_as_current_observation` /
  `start_as_current_span` outside one docstring example (`langfuse.py:91-112`) — this would be the
  first real usage in the codebase.
- Established pattern for reaching app singletons from subagent modules: a direct
  `from aigw_service.context import APP_CTX` import at module scope (see
  `orchestrator_tools.py:22`, `analyzer.py:11`, `main_graph.py:43` — all do this; there is no DI
  container in this codebase). The new tracing calls should follow the same convention rather than
  threading a tracing client through constructors.
- **Adjacent bug to fix in the same change**: `router.py:189` sets Langfuse trace metadata under
  the key `"lang_fuse_user_id"` (extra underscore), but the installed `CallbackHandler` only
  recognizes the literal `"langfuse_user_id"` (verified against
  `langfuse/langchain/CallbackHandler.py:501-504,1803-1805`) — so `user_id` never actually reaches
  the Langfuse trace today. Since the new tool spans are of limited value without correct user
  attribution on their parent trace, this change should also fix the one-line key rename
  (`"lang_fuse_user_id"` → `"langfuse_user_id"`).

## Desired Behavior

- Wrap each of the two `await asyncio.to_thread(self._lo_pool.submit(...).result)` calls in
  `orchestrator_tools.py` with a `with` block using
  `APP_CTX.tracing.client.start_as_current_observation(name=<tool_name>, as_type="tool",
  input=kwargs)` as the context manager, assigning the yielded span to a variable and calling
  `.update(output=calc)` once the awaited result is available inside the block.
- `name` should be the literal tool name string (`"analyze_model_inputs_for_target"` /
  `"analyze_excel_model"`) so it lines up with the existing `TOOL ARGS: {name}` loguru marker and
  with the LLM's own tool-calling vocabulary.
- One flat span per call — no child spans for cache-hit vs. real recalculation. Duration alone
  will show the difference, and the cache lives inside the worker process, which this tracing
  code can't reach anyway.
- Must no-op safely (no span created, no exception raised) when `APP_CTX.tracing` is `None`, is
  not a `LangfuseClient`, or has `.client is None`. This needs a small guarded helper shared by
  both call sites so the checks aren't duplicated.
- The new spans should nest naturally under the per-request trace that the top-level
  `graph.ainvoke()` call already starts via `config["callbacks"] = [cb_handler]`
  (`router.py:198-207`), since Langfuse's OTEL client propagates the active span via contextvars.
  `asyncio.to_thread` copies the calling context, and — more importantly — the span itself is
  opened and closed in the *main* process/async task around the `await`, not inside the worker
  process, so there's no cross-process context-propagation problem to solve. This nesting
  assumption should be verified empirically once implemented (confirm in the Langfuse UI that the
  tool span appears nested under the right trace) — it isn't fully provable from static reading
  alone.

## Out of Scope

- No change to `tools.py`'s internal logging.
- No change to the `ProcessPoolExecutor` or caching design.
- No attempt to trace *inside* the spawned worker process — a separate Python process has no
  access to the parent's Langfuse/OTEL context.
- No AEF-tracing equivalent. AEF and Langfuse are mutually exclusive backends per
  `TracingManager`, and this change is specifically about the Langfuse client.

## Files

- `src/aigw_service/api/v1/subagents/orchestrator_tools.py` — add the guarded span helper and
  wrap both call sites (`_run_ift` around lines 194-196, `_run_ema` around line 250).
- `src/aigw_service/api/v1/router.py:189` — fix the `"lang_fuse_user_id"` → `"langfuse_user_id"`
  metadata key typo.

## Verification

Run the app locally with `LANGFUSE_TRACING_ENABLED=true` against a local/dev Langfuse instance,
issue a request that triggers each tool (`analyze_model_inputs_for_target`,
`analyze_excel_model`) via `/api/v1/invoke-agent`, and confirm in the Langfuse UI that:

- Each call shows up as a `tool`-typed observation nested in the request's trace.
- `input` matches the resolved `kwargs`, `output` matches the `calc` dict, and duration is
  correct.
- A forced-error case (e.g. an unreachable output name) marks the span `ERROR` with the
  exception recorded.
- The trace's `user_id` field is now populated (post metadata-key fix).
