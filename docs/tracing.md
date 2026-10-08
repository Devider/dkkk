# Tracing: how observability is wired in this service

This describes how request tracing actually works end to end — the two backends, how LangGraph's
execution gets traced, how the LibreOffice calc dispatches became traceable tool observations, and
the gotcha that cost a full debugging cycle. For local Langfuse dev setup (certs, Docker networking,
scheme resolution) see `src/aigw_service/api/README.md` — this doc doesn't repeat that. For the
general architecture of the agent graph, see `CLAUDE.md`.

## Two backends, mutually exclusive

`TracingManager.get_tracing()` (`core/tracing/tracing.py`) picks exactly one backend, decided once at
startup:

- **AEF** (`AEFTracingHandler`) — the private `aef_tracing` package's own tracer, used when
  `AEF_TRACING_ENABLED` is set. Not installed in local dev by default (`core/tracing/tracing.py`
  lazy-imports it and falls back to a no-op stub class if missing).
- **Langfuse** (`LangfuseClient`, `aigw_modules/hub_services/langfuse.py`) — used when
  `LANGFUSE_TRACING_ENABLED` is set and AEF isn't. This is explicitly documented as a **local-dev-only
  stand-in** (`src/aigw_service/api/README.md`); production swaps in a different private-package
  client with the same shape.

Whichever one wins is stored as `APP_CTX.tracing: AEFTracingHandler | LangfuseClient` (`context.py`).
If neither is enabled, `APP_CTX.tracing is None`.

AEF also has its own decorator-based tracing primitives (`core/tracing/tracing_spans.py` —
`trace_agent_call`, `trace_input_request`, `trace_output_request`, `trace_kafka_produce`,
`trace_custom_span`, all wrapping the private `aef_tracing` package's context managers). **Nothing in
`src/` currently calls any of them** — they exist for a future AEF integration, not an active code
path. Don't assume they're wired into the request flow.

## How the top-level request trace gets created

`router.py`'s `invoke_agent` builds a `get_cb_handler()` dependency that returns
`APP_CTX.tracing.callback_handler` whenever `APP_CTX.tracing` is a `LangfuseClient` (via
`hasattr(APP_CTX.tracing, "callback_handler")` — `AEFTracingHandler` doesn't have this attribute, so
AEF mode naturally yields `None` here). That handler — a `langfuse.langchain.CallbackHandler` — is
attached as `config["callbacks"] = [cb_handler]` on the single `agent.graph.ainvoke(..., config=config)`
call that runs the whole LangGraph agent for one request.

From there, LangGraph's own execution fires standard LangChain callback events
(`on_chain_start`/`on_chain_end`) for the graph itself and for each node, and the Langfuse
`CallbackHandler` turns those into a trace with one nested span per node — this is why individual
graph nodes (`classifier`, `extract_excel_data`, `tool_executor`, ...) show up as distinct steps in
the Langfuse UI without any tracing code of our own. This nesting is driven by LangChain's own
`parent_run_id` bookkeeping inside the `CallbackHandler` (keyed by LangChain run IDs), not by OTEL's
ambient "current span" — that distinction matters a lot below.

**Metadata key gotcha**: the request's `user_id`/`session_id` reach the trace only through specific
metadata keys the `CallbackHandler` recognizes literally: `langfuse_user_id` and
`langfuse_session_id` (set in `config["metadata"]` in `router.py`). A one-character-off key
(`lang_fuse_user_id`, which this codebase actually shipped with until it was caught) silently drops
the attribution — no error, the trace just has no `user_id`. There's no validation anywhere that
catches a typo'd metadata key; it just doesn't do anything.

## Tracing the LibreOffice calc dispatch as its own observation

The two LO calculation dispatches (`analyze_model_inputs_for_target`, `analyze_excel_model`) run
inside `OrchestratorTools` (`api/v1/subagents/orchestrator_tools.py`), dispatched via a
`ProcessPoolExecutor` from `AgentGraph.tool_executor` (`api/v1/main_graph.py`). They're the slowest,
most failure-prone steps in the graph, but a generic per-node chain span doesn't show their own
input/output/duration/errors as a distinct observation — hence `OrchestratorTools._dispatch_lo_calc`.

### The gotcha: raw Langfuse SDK calls don't nest correctly from inside a node body

The first implementation did the obvious thing: call
`langfuse_client.start_as_current_observation(name=..., as_type="tool", input=kwargs)` as a context
manager around the dispatch, guarded on `APP_CTX.tracing` being an active `LangfuseClient`. This
compiles, runs, and produces a span — but in the real Langfuse UI it showed up as **a disconnected
root trace**, with its own `trace_id`, no parent, and no `user_id`/`session_id`.

Root cause (confirmed with a minimal repro — a bare LangGraph node opening a Langfuse span under an
in-memory OTEL exporter, no real server needed): `start_as_current_observation` attaches to
whatever OTEL considers the *ambient current span* (`opentelemetry.trace.get_current_span()`).
Langfuse's `CallbackHandler.on_chain_start` does attach the node's span to that ambient context via
`opentelemetry.context.attach(...)` — but **LangGraph runs each node's actual coroutine inside a
freshly created `asyncio.Task`** (`create_task_in_config_context` in
`langgraph/_internal/_runnable.py`, used from `pregel/_retry.py`). That task's copied
`contextvars.Context` snapshot does not include the attach. So from inside a plain node-function
body, `get_current_span()` is always invalid — there is no ambient parent to find, and Langfuse just
starts a new trace. This is a structural property of how LangGraph schedules node execution, not a
bug in our original code's placement of the `with` block.

**The fix**: drive LangChain's own callback-manager machinery directly instead of relying on ambient
OTEL context. A LangGraph node that declares a second `config: RunnableConfig` parameter gets the
current run's config auto-injected by LangGraph; `config["callbacks"]` carries a callback manager
whose `parent_run_id` already correctly identifies the current node's run — this is the exact,
different mechanism that makes per-node chain spans nest correctly in the first place (LangChain's
run-id bookkeeping, not OTEL ambient context). So `_dispatch_lo_calc` does this:

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

`config` is threaded explicitly from `tool_executor(state, config)` → `_execute_single_call` →
`OrchestratorTools.dispatch` → `_run_ift`/`_run_ema` → `_dispatch_lo_calc`. There's no shortcut
around this: the whole point of the bug was that nothing ambient/implicit reaches the right parent,
so the per-request object has to be passed explicitly all the way down.

Two kwargs are load-bearing and easy to "clean up" by accident: `name=tool_name` makes the
observation's name the literal tool name (`CallbackHandler.get_langchain_run_name` prioritizes
`kwargs["name"]` over anything else); `inputs=kwargs` makes the observation's `input` the actual
structured dict rather than a flat string (`on_tool_start`'s `inputs` parameter is part of the
standard `BaseCallbackHandler` signature, and Langfuse's handler specifically reads it in preference
to the `input_str` positional).

### What this means for extending tracing to new call sites

- There is **no guard needed** anymore (`APP_CTX.tracing`, `isinstance(..., LangfuseClient)`, etc.).
  `on_tool_start` on a callback manager with zero handlers is simply a no-op — safety is automatic as
  long as `config["callbacks"]` only ever contains a handler when tracing is actually active, which
  is exactly what `router.py`'s `get_cb_handler()` guarantees.
- Any new code that wants a traced "tool" observation from inside a LangGraph node needs: (1) the
  node function to declare `config: RunnableConfig` so LangGraph injects it, and (2) that `config` to
  be threaded explicitly down to wherever `on_tool_start`/`on_tool_end`/`on_tool_error` is driven.
  Reaching for `Langfuse.start_as_current_observation()` directly from inside node code will silently
  produce disconnected traces — this is the mistake to not repeat.
- This mechanism isn't Langfuse-specific. It's generic LangChain tool-run tracing — whatever handler
  AEF or anything else registers on `config["callbacks"]` would get the same treatment for free.

## Failure-mode guarantees (why this can't crash a request)

Verified directly in `langchain_core/callbacks/manager.py` and by testing against a live-like setup:

- **No handler registered at all** (tracing disabled, or `APP_CTX.tracing is None`): `config` carries
  no callbacks, so `on_tool_start`/`on_tool_end` iterate zero handlers. Pure no-op.
- **A handler throws** (Langfuse unreachable mid-call, or a buggy custom handler): LangChain's
  `_ahandle_event_for_handler` wraps every handler invocation in `try/except Exception:
  logger.warning(...)`, and only re-raises if that handler's `raise_error` attribute is `True`
  (default `False`, and Langfuse's `CallbackHandler` doesn't override it). The exception is logged
  and swallowed — our dispatch code never sees it.
- **Langfuse server unreachable mid-operation**: span creation in the installed `langfuse` SDK
  (OTEL-based) is purely local/in-memory; only the background batch exporter talks to the network,
  asynchronously. A failed export just logs a line (`Failed to export span batch code: ...`) — it
  never blocks or fails the request that created the span.
- **One real, separate caveat**: if Langfuse is *enabled in config* but unreachable *at app startup*,
  `AppContext.on_startup()` (`context.py`) calls `self.tracing.on_startup()` with no try/except —
  the whole app refuses to boot. That's a pre-existing fail-fast design choice unrelated to the
  tool-tracing mechanism above; once the app has actually started, Langfuse was reachable at boot, so
  none of the per-request paths above can hit an uninitialized client.

## Known gaps

- There is no automated regression test for the actual end-to-end nesting property (a real
  LangGraph graph + Langfuse `CallbackHandler` + OTEL exporter, asserting the tool observation shares
  the root trace's `trace_id`). Unit tests cover `_dispatch_lo_calc`'s own logic with a fake handler,
  but not whether LangGraph's real config-injection behavior keeps working across a future
  LangGraph/LangChain upgrade. A regression here would only surface by manually checking the Langfuse
  UI again, the same way it was found the first time.
- Manual verification against a real Langfuse instance (nested tool observation, correct
  `input`/`output`/duration, `ERROR` marking on a forced failure, populated `user_id`) is tracked as
  an open task in `openspec/changes/trace-lo-calc-dispatch-langfuse/tasks.md` (4.3) — check there for
  current status before assuming it's been done.
