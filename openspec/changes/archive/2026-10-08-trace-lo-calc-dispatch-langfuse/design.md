# Design

## Context

See `proposal.md` - Why. Relevant current-state details (from reading the code and from an
empirical repro, not restated from the intent doc):

- The two dispatch call sites are `OrchestratorTools._run_ift` and `_run_ema`, each a single
  `await asyncio.to_thread(self._lo_pool.submit(_run_lo_calc, "<tool_name>", kwargs).result)`.
  `kwargs` is a plain JSON-serializable dict built just above the call; the returned `calc` dict
  (`status`/`result`/`content`) is already run through `_safe_content`, so both are safe to use
  directly as the tool run's `input`/`output`.
- `OrchestratorTools.dispatch()` is called from `AgentGraph._execute_single_call`
  (`main_graph.py`), itself called from the `tool_executor` graph node - a plain async method on
  `AgentGraph`, not a LangChain `Runnable.ainvoke()` call. LangGraph nodes can declare a second
  `config: RunnableConfig` parameter and LangGraph injects the current run's config into it
  automatically - this is how `config` (and therefore `config["callbacks"]`) reaches our dispatch
  code.
- `router.py` sets `"lang_fuse_user_id"` in the LangGraph call's `metadata`, but
  `langfuse.langchain.CallbackHandler` only reads the literal key `"langfuse_user_id"` - so today
  `user_id` never reaches the trace that the new tool observations nest under. Fixed as a one-line
  rename.
- **The original plan (raw `Langfuse.start_as_current_observation` guarded by inspecting
  `APP_CTX.tracing`) was built and then empirically found broken** - see Decisions below for the
  repro and root cause. It is not used in the final implementation.

## Goals / Non-Goals

**Goals:**
- One reusable helper (`_dispatch_lo_calc`), called from both `_run_ift` and `_run_ema`, that
  encapsulates the tool-run lifecycle (`on_tool_start`/`on_tool_end`/`on_tool_error`) and the
  pooled dispatch itself.
- The resulting Langfuse tool observation must actually nest under the request's trace - this is
  the whole point of the change, not just "a span exists somewhere."
- Fix the metadata key typo in the same change, since the new observations are of limited
  diagnostic value without correct user attribution on their parent trace.

**Non-Goals:**
- No change to `tools.py` logging, the `ProcessPoolExecutor`/caching design, or any attempt to trace
  inside the spawned worker process (a separate Python process has no access to the parent's
  OTEL/Langfuse context - this is a hard boundary, not a deferred nice-to-have).
- No AEF-tracing equivalent for these spans - AEF and Langfuse are mutually exclusive backends, and
  this change targets the Langfuse client specifically.
- No new child-span granularity (e.g. distinguishing cache-hit vs. real recalculation) - duration on
  the single flat span is sufficient per the proposal.

## Decisions

**Why the original raw-OTEL plan was abandoned - empirical repro.** The first implementation
followed the intent doc literally: a `@contextlib.contextmanager` helper guarded on
`APP_CTX.tracing`, opening a span via `tracing.client.start_as_current_observation(name=...,
as_type="tool", input=kwargs)` around the `await asyncio.to_thread(...)` call. Manual verification
against a real Langfuse instance (the proposal's own Verification step) showed the tool span
landing as a **separate root trace** with its own `trace_id`, no parent, and no `user_id`/
`session_id` - not nested under the request trace at all. A minimal repro (a bare LangGraph node
opening a Langfuse span, run under an in-memory OTEL exporter, no real server needed) confirmed
why: printing `opentelemetry.trace.get_current_span()` *inside the node body*, before opening the
span, showed `valid=False` - no ambient span attached - even though the graph's root trace and the
node's own chain-run span (`tool_executor`, correctly parented under the root `LangGraph` trace)
both existed with correct linkage. The root cause: Langfuse's `CallbackHandler.on_chain_start` does
attach the node's span to the ambient OTEL context via `opentelemetry.context.attach(...)`
(`langfuse/langchain/CallbackHandler.py:708-709`), but LangGraph's Pregel scheduler runs each
node's actual coroutine inside a **freshly created `asyncio.Task`**
(`create_task_in_config_context` in `langgraph/_internal/_runnable.py`, used from
`pregel/_retry.py:460`), whose copied `contextvars.Context` does not include that attach. So any
code relying on OTEL *ambient* context from inside a plain node-function body gets no parent -
this is a structural property of this LangGraph version's node execution, not something fixable
by tweaking where our `with` block sits.

**The actual fix: drive LangChain's own callback manager, not raw OTEL context.** `tool_executor`'s
own span *does* nest correctly under the graph root - via a completely different mechanism:
LangChain's `parent_run_id`-based bookkeeping inside `CallbackHandler` (`self._runs[parent_run_id]`
lookups), which flows through `config["callbacks"]` independent of OTEL contextvars. A second repro
confirmed this path works: a node declaring `config: RunnableConfig` as its second parameter (a
signature LangGraph recognizes and auto-injects into), calling
`get_async_callback_manager_for_config(config)` and then `manager.on_tool_start(...)` /
`run_manager.on_tool_end(...)`, produced a tool span correctly nested under the node's own span,
under the graph root, all sharing one `trace_id`. `_dispatch_lo_calc` implements exactly this:
```python
manager = get_async_callback_manager_for_config(config or {})
run_manager = await manager.on_tool_start({"name": tool_name}, str(kwargs), name=tool_name, inputs=kwargs)
try:
    calc = await asyncio.to_thread(self._lo_pool.submit(_run_lo_calc, tool_name, kwargs).result)
except Exception as e:
    await run_manager.on_tool_error(e)
    raise
await run_manager.on_tool_end(calc)
return calc
```
Passing `name=tool_name` makes `CallbackHandler.get_langchain_run_name` use the literal tool name
(it takes priority over `serialized["name"]`); passing `inputs=kwargs` makes
`CallbackHandler.on_tool_start` use the dict directly as the observation's structured `input`
rather than falling back to the `input_str` positional (confirmed by reading
`CallbackHandler.on_tool_start`'s `structured_input = kwargs.get("inputs")` branch). The observation
type is unconditionally `"tool"` for the `on_tool_start` callback family
(`_get_observation_type_from_serialized`), satisfying the spec's "tool-typed observation"
requirement without any `as_type=` argument of our own.

**No more `APP_CTX.tracing`/`LangfuseClient` guard in `orchestrator_tools.py`.** The three-way guard
(`None` / wrong backend / client not started) is gone - it's no longer needed. `on_tool_start` on a
callback manager with no handlers is simply a no-op (LangChain's own documented behavior), and
`config["callbacks"]` only carries a Langfuse handler when `router.py`'s `get_cb_handler()`
produced one in the first place. This also means the mechanism isn't Langfuse-specific: any
LangChain-compatible callback handler attached to `config["callbacks"]` (including a future
AEF-side one, if it ever exposes a LangChain `CallbackHandler`) would get the same nested tool
observation for free - broader than the original design's explicit Langfuse-only guard, but not a
behavior regression against the spec (which only commits to the Langfuse-specific scenarios).

**`config` is threaded through four call layers.** `tool_executor(state, config)` (LangGraph injects
`config` because the node declares it) → `_execute_single_call(..., config)` →
`OrchestratorTools.dispatch(..., config=config)` → `_run_ift`/`_run_ema(..., config)` →
`_dispatch_lo_calc(tool_name, kwargs, config)`. This is more call-site churn than the original
single-helper plan, but it's the only way to reach the node's own callback manager - there is no
ambient/global way to get it, by the same finding that broke the OTEL-context approach.

**Metadata key fix is unchanged: a one-line value change.** `router.py`'s `"lang_fuse_user_id"` key
becomes `"langfuse_user_id"` with no other change to the `metadata` dict shape -
`langfuse.langchain.CallbackHandler` reads the corrected key directly.

## Risks / Trade-offs

- **This relies on LangGraph continuing to inject `config: RunnableConfig` into node functions that
  declare it, and continuing to populate `config["callbacks"]` with a manager whose `parent_run_id`
  matches the current node's run** → this is documented, stable LangGraph/LangChain behavior (not
  an internal/private API), and is exactly the same mechanism `tool_executor`'s own chain-run span
  already depends on for correct nesting today.
- **Langfuse SDK version drift in `on_tool_start`/`on_tool_end`'s kwarg handling** (e.g. `inputs=`
  no longer being read) → would degrade to the observation using `str(kwargs)` as a flat string
  input instead of the structured dict; still visible and still nested, just less readable. Pinned
  `langfuse==4.15.4` in `pyproject.toml`, so no drift without a deliberate dependency bump.
- **`on_tool_end(calc)` called with an already-`_safe_content`-filtered dict** → no new
  serialization risk, since this is the same dict already proven JSON-serializable for the
  cross-process return in `_run_lo_calc`.
- **Broader applicability than originally scoped (any LangChain-compatible handler, not just
  Langfuse)** → not a risk in practice; `config["callbacks"]` is only non-empty when
  `get_cb_handler()` actually produced a handler, which today only happens for Langfuse.
