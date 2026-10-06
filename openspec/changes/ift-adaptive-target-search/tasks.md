# Tasks

## 1. Adaptive search core

- [x] 1.1 Add module constants `IFT_PROBE_STEP = 0.10` / `IFT_MAX_ITER = 200` and a pure helper that,
      given a `func`, baseline values, `target_value` and `tolerance`, returns the influence sign per
      input (`+1/-1/0`, baseline evaluated once, inputs with `|x0| < 1e-9` excluded); verify with a new
      `tests/unit/test_ift_search.py` that uses a fake `func` (no LibreOffice) and covers: N+1
      evaluations, neutral input excluded, zero-valued input excluded with reason reported.
      Verify: `$V/python -m pytest tests/unit/test_ift_search.py -q --no-cov` passes.
- [ ] 1.2 Implement the iteration loop helper (probe → candidate per eligible input with fixed
      `base_step_i = 0.1·|base_i|` → combined score `probe-predicted normalized target error +
      IFT_SELECTION_LAMBDA · quadratic penalty` → argmin evaluated once → miss halves that input's
      step fraction → stop on `|value - target| < ε` with `ε` derived from `tolerance`, or
      `MAX_ITER`), returning the evaluation trace, best point, `stop_reason` and final penalty;
      verify with fake-`func` unit tests for: convergence on monotonic linear/quadratic functions,
      step halving after an overshoot, `MAX_ITER` stop with `WARNING` semantics, distributed moves
      scoring cheaper than concentrated ones, and rotation onto a second input once the first
      accumulates penalty. Verify: `$V/python -m pytest tests/unit/test_ift_search.py -q --no-cov`
      passes.
- [ ] 1.3 Rewrite `analyze_model_inputs_for_target` to call the new search instead of
      `generate_scenarios`/`test_scenarios`/`optimize_with_regression`; map results to the existing
      contract (`matching_scenarios`, `all_scenarios` trace, `optimized_scenario` with
      `input_values/actual_output/deviation/deviation_percent`, `search_config` keeping
      `input_ranges` plus `probe_step/directions/max_iter/lambda/epsilon/iterations/stop_reason/
      excluded/penalty`), guard `target_value == 0` in the stop test, and fold
      `IFT_PROBE_STEP`/`IFT_MAX_ITER`/`IFT_SELECTION_LAMBDA` into the cache key. Verify: existing
      tool-level tests still pass and a direct call returns `status="OK"` with a scenario inside
      tolerance — `$V/python -m pytest tests/unit tests/test_tools_performance.py -q --no-cov`.

## 2. Remove the grid path

- [ ] 2.1 Delete `generate_scenarios`, `test_scenarios`, `optimize_with_regression` and replace
      `tests/test_tools_performance.py::TestSearchAndOptimize::test_search_and_optimize` (currently
      asserting grid+scipy reference values 378.22 / 0.01 / 1000.0) with an adaptive-search
      correctness test on `models/model.xlsx` asserting `deviation_percent <= tolerance` and printing
      per-phase timings. Verify: `$V/python -m pytest tests/test_tools_performance.py -q --no-cov`
      (7+ tests, ~1 min, real LibreOffice).
- [ ] 2.2 Drop now-unused imports (`itertools.product` usage elsewhere checked, `scipy` if orphaned)
      and confirm no remaining callers: `grep -rn "generate_scenarios\|test_scenarios\|optimize_with_regression"
      src tests scripts`. Verify: `$V/ruff check src` reports no new violations versus the pre-change
      baseline (59) and `$V/pylint src` stays ≥ 9.41.

## 3. Verification script

- [ ] 3.1 Create `scripts/check_ift_target.py` in the spirit of `scripts/run_qa.sh` but calling
      `analyze_model_inputs_for_target` directly (copy `models/model.xlsx`, resolve inputs/output the
      way `tests/test_tools_performance.py` does): pick a **random** reachable `target_value` around
      the baseline output, run the tool, print `target / achieved / deviation_percent / iterations /
      stop_reason`, and exit non-zero when the deviation exceeds tolerance. Verify:
      `$V/python scripts/check_ift_target.py` prints all four numbers and exits 0; run it twice to
      confirm different random targets are handled.

## 4. Integration checks

- [ ] 4.1 Run the full suite with the required env and confirm only the known-broken
      `tests/unit/test_stop_event.py` failures remain: `set -a; . <(sed 's/=10 MB$/=10MB/' docker.env);
      set +a; $V/python -m pytest tests -q --maxfail=100`.
- [ ] 4.2 Update the obsolete grid rationale for `MAX_IFT_INPUTS` in `docs/experiments.md`
      (§ «Причина» — the limit now reflects eval-harness coverage, not cartesian-grid cost) and
      verify the documented commands in the file still run as written.
- [ ] 4.3 Smoke-test the tool through the live path: start the server, run
      `$V/python scripts/run_tool_queries.py --subset 5 --url http://localhost:8080 --log server.log`
      and confirm IFT queries produce `TOOL ARGS` log lines and answers without new error types
      (per-parameter resolution accuracy is out of scope for this change).

## Workflow follow-up

- Archive the change after the project's review requirements are satisfied and verify the archived
  result (specs move into `openspec/specs/target-value-search/`).
