# Tasks

> **Note:** Group 1's original plan (a raw `Langfuse.start_as_current_observation` helper guarded
> on `APP_CTX.tracing`) was implemented, then found during task 4.3's manual verification to create
> a disconnected root trace instead of nesting. It was replaced with the callback-manager-driven
> approach described below after an empirical repro confirmed the root cause and the fix. See
> `design.md` Decisions for the full account. Task descriptions below describe what was actually
> built.

## 1. Tool-run tracing helper driven by the node's own callback manager

- [x] 1.1 In `src/aigw_service/api/v1/subagents/orchestrator_tools.py`, add
  `OrchestratorTools._dispatch_lo_calc(tool_name: str, kwargs: dict[str, Any], config:
  RunnableConfig | None) -> dict[str, Any]`: get the callback manager via
  `get_async_callback_manager_for_config(config or {})`, call `manager.on_tool_start({"name":
  tool_name}, str(kwargs), name=tool_name, inputs=kwargs)`, run the pooled dispatch, and call
  `run_manager.on_tool_end(calc)` on success or `run_manager.on_tool_error(e)` (then re-raise) on
  failure. Verify by unit test: with `config=None` (no callbacks), the helper runs the dispatch and
  returns its result without raising.
- [x] 1.2 Unit test the active-tracing path: with a fake `AsyncCallbackHandler` in
  `config["callbacks"]` recording calls, verify `_dispatch_lo_calc` drives exactly `on_tool_start`
  (with `name=tool_name` and `inputs=kwargs`) then `on_tool_end(calc)` on success.

## 2. Thread `config` from the graph node down to the dispatch helper

- [x] 2.1 In `src/aigw_service/api/v1/main_graph.py`, give `tool_executor` a second
  `config: RunnableConfig` parameter (LangGraph injects the node's own config when declared) and
  pass it through `_execute_single_call` (also given a `config` parameter) to
  `OrchestratorTools.dispatch(..., config=config)`.
- [x] 2.2 In `orchestrator_tools.py`, add `config: RunnableConfig | None = None` to `dispatch`,
  `_run_ift`, and `_run_ema`, and replace the previous span-wrapped
  `await asyncio.to_thread(self._lo_pool.submit(_run_lo_calc, "analyze_model_inputs_for_target",
  kwargs).result)` / `"analyze_excel_model"` calls in both methods with
  `await self._dispatch_lo_calc(<tool_name>, kwargs, config)`. Verify with a unit test per method
  that mocks `_dispatch_lo_calc` and asserts it's awaited with the right tool name, kwargs, and the
  same `config` object passed into `_run_ift`/`_run_ema`.
- [x] 2.3 Unit test that an exception raised by the pooled call still propagates unchanged out of
  `_run_ift`/`_run_ema` through the real (non-mocked) `_dispatch_lo_calc`, confirming no
  `try/except` was accidentally added around the dispatch itself.

## 3. Fix trace user-attribution metadata key

- [x] 3.1 In `src/aigw_service/api/v1/router.py:189`, rename the `metadata` key
  `"lang_fuse_user_id"` to `"langfuse_user_id"` (no other change to the `config["metadata"]` dict).
  Verify by unit test (extend or add to the router's existing metadata-construction test coverage)
  asserting the built `config["metadata"]` dict contains `"langfuse_user_id": user_id` and does not
  contain the old key.

## 4. Quality gates and manual verification

- [x] 4.1 Run `ruff check --fix src` then `ruff format src`, and `pylint src` (score must stay > 7)
  over the touched files; verify both commands exit clean.
- [x] 4.2 Run `pytest tests/unit/ -v` and confirm the new/updated tests from groups 1-3 pass and no
  existing test regresses.
- [x] 4.3 Per the proposal's Verification section: with `LANGFUSE_TRACING_ENABLED=true` against a
  local/dev Langfuse instance, run the app and issue one `/api/v1/invoke-agent` request that
  triggers `analyze_model_inputs_for_target` and one that triggers `analyze_excel_model`. In the
  Langfuse UI, confirm: each call appears as a `tool`-typed observation nested under the request's
  trace (same `trace_id` as the root); `input`/`output`/duration are correct; a forced-error
  request (e.g. an unreachable output name) marks its observation `ERROR` with the exception
  recorded; the trace's `user_id` field is populated. (First attempt at this task surfaced the
  nesting bug that groups 1-2 were rewritten to fix — re-run against the corrected implementation.)
