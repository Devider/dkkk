# Proposal

## Why

`analyze_model_inputs_for_target` sometimes returns no acceptable scenario: it builds a coarse
grid (4–10 steps per input, ±50% of current values, capped at `max_scenarios`, so with ~8 inputs
only 1000 randomly-shuffled grid points survive) and then hands the best point to an L-BFGS-B
regression refine that has no idea about the model's structure. Targets that lie between grid
nodes, or outside the fixed ±50% band, are simply not reached — the tool answers "не найдено"
even when the model can produce the value.

## What Changes

- **BREAKING**: `analyze_model_inputs_for_target` replaces the grid search
  (`generate_scenarios` + `test_scenarios` path) and `optimize_with_regression` with a single
  adaptive coordinate-search loop:
  1. **Monotonicity probe** — evaluate each input at `current` and `current + 10%` to learn the
     sign of its effect on the target output (componentwise-monotonicity assumption).
  2. **Iterate** — compare current output with `target_value`; pick the input whose adjustment
     minimizes the penalty (sum of relative deviations of all inputs from their original values);
     move it in the learned direction by the current step.
  3. **Step adaptation** — step starts at 10%; after an overshoot (the deviation grows / sign of
     the error flips) the step for that input becomes half of its current value.
  4. **Stop** — `|value - target_value| < e` or the iteration cap (`MAX_ITER`) is reached.
- Inputs are **not** hard-bounded (the previous ±50% clip is dropped); the penalty is only a
  selection criterion, so no scenario is rejected for being far from the original values.
- Result/`ModelInputAnalysisToolResult` shape, cache key, output-cell/input-cell resolution and
  the `matching_scenarios`/`all_scenarios` payload keys stay as they are — only how scenarios are
  produced changes.
- New verification script `scripts/check_ift_target.py` (modeled on `scripts/run_qa.sh`, minus
  the HTTP/LLM parts) that calls the tool directly with a **random** `target_value`, then prints
  target, achieved value and deviation.

## Capabilities

### New Capabilities

- `target-value-search`: how the service picks input-parameter values that drive a chosen model
  output to a requested target — probe, iterate, penalty-based input selection, stopping rules,
  and the reported result of that search.

### Modified Capabilities

<!-- none — openspec/specs is empty -->

## Impact

- `src/aigw_service/api/v1/tools.py`: `analyze_model_inputs_for_target` rewritten; helpers
  `generate_scenarios`, `test_scenarios`, `optimize_with_regression` lose their IFT callers
  (`analyze_excel_model` has its own combination path and is untouched). LRU cache key must
  cover the new parameters (step, `MAX_ITER`, epsilon).
- `scripts/check_ift_target.py`: new, direct-call verification harness (needs a running
  LibreOffice backend via `ExcelWorkbook`, like `tests/test_tools_performance.py`).
- LLM side: no change — the tool signature, description and result JSON keys are preserved, so
  prompts, schemas and `scripts/run_tool_queries.py` expectations keep working.
- Cost profile: one probe pass (`len(input_names) + 1` model evaluations) plus
  `≤ MAX_ITER` evaluations instead of up to `max_scenarios` + scipy calls — faster on large
  input sets, but the returned answer is a single best scenario rather than a list of matches.
