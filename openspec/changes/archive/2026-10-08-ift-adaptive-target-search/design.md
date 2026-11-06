# Design

## Context

`analyze_model_inputs_for_target` (src/aigw_service/api/v1/tools.py:170) resolves the output cell
and input cells, compiles a LibreOffice closure `func(*values) -> [output]` (≈30–40 ms per call),
then currently: `generate_scenarios` (±50% band, 4–10 steps/input, product shuffled down to
`max_scenarios`) → `test_scenarios` (per-point deviation) → `optimize_with_regression`
(scipy L-BFGS-B from the best grid point) → `save_analysis_results` + `generate_result_message`.
Motivation for replacing this pipeline: see proposal.md. Two constraints shape the design: one
model evaluation is expensive, and the tool's contract (signature, status codes, `content` keys)
is consumed by prompts, the synthesizer and log-parsing eval scripts, so it must not change.

## Goals / Non-Goals

**Goals:**

- Reach targets the grid misses (between nodes, outside ±50%), with far fewer model evaluations.
- Keep `analyze_model_inputs_for_target`'s signature, statuses and `content` keys stable.
- Produce a verifiable number: a script that prints target / achieved / deviation for a random
  target.

**Non-Goals:**

- `analyze_excel_model` (scenario-grid "what-if" tool) — untouched, it has its own combination path.
- Improving English→Russian name resolution — orthogonal accuracy limiter.
- Multi-output targets, hard per-parameter bounds, or exposing search tuning through the LLM.
- `MAX_IFT_INPUTS = 5` in `scripts/new_graph_test.py` (eval-harness case filter) — kept as is;
  only its grid-based rationale in `docs/experiments.md` is rewritten.

## Decisions

1. **Full replacement, not a fallback** (user decision). The grid was the failure source and
   costs `O(max_scenarios)` evaluations before it even starts; running both doubles cost and
   makes the result ambiguous. Alternatives considered: grid-then-search fallback (rejected —
   keeps the slow path), run-both-keep-best (rejected — 2× cost, no clear win).

2. **Algorithm (amended during implementation): probe + scored candidate selection with a fixed
   base step.**
   - Probe: for each input evaluate `f(x0)` and `f(x0 with x_i + 0.1·|x_i|)` → sign
     `d_i ∈ {+1, −1, 0}` and sensitivity `sens_i` (output delta of a one-base-step move)
     (`N+1` evaluations; zero-valued and neutral inputs excluded).
   - `base_step_i = 0.1·|x_i^0|` computed **once** and never recalculated from current values;
     per-input step fraction `f_i ∈ {1, ½, ¼, …}` starts at 1, halves on a miss, never grows.
   - Loop (≤ `IFT_MAX_ITER`): stop checks; build one candidate per eligible input, aimed at the
     target (probe sign × `sign(target − current_out)`), magnitude `f_i·base_step_i`; score each:
     `score_i = ||current_out − target| − sens_i·f_i| / N + IFT_SELECTION_LAMBDA · Σ_j ((cand_j − x_j^0)/base_step_j)²`
     with `N = max(|baseline_out − target|, ε)`; pick the argmin (tie → lowest index) and
     **evaluate it once** for real.
   - Miss (real `|value − target|` grew vs the current point): `f_i /= 2`, reject the point
     (stay put). Otherwise accept.
   - **Why not the literal min-penalty rule from the original draft:** comparing two candidates,
     every term of `Σ|x_j − x_j⁰|/|x_j⁰|` cancels except the corrected input's own change
     `Δrel = ±step_rel`. So full-step candidates always tie, and the first input to miss has the
     strictly smallest change forever → the search degenerates to 1-D on one input and the
     penalty becomes inert. The quadratic form breaks the cancellation (candidate penalty depends
     on accumulated state) and the error term restores the sensitivity-vs-spread trade-off.
   - Alternatives considered: fixed 10% steps (rejected — hundreds of iterations for distant
     targets), per-coordinate bisection brackets (rejected — state-heavy; halving on miss
     converges similarly), scipy (rejected — black box, ignores monotonicity, proven unreliable),
     evaluating all N candidates per iteration (rejected — N×~40 ms/iteration vs 1 for the
     probe-predicted score; nonlinearity is absorbed by miss→halve).

3. **Stop tolerance `ε` is derived from the existing `tolerance` argument**
   (`ε = |target_value| · tolerance / 100`), not a new parameter — `tolerance` already means
   "allowed deviation in percent" (`deviation_percent <= tolerance` is the current match rule),
   so reusing it keeps the signature and the cache semantics consistent. `e` in the request maps
   to this ε. The same ε anchors the score normalization
   `N = max(|baseline_out − target_value|, ε)` (division-by-zero guard, keeps the error term
   O(1) at the start so `lambda` is model-independent).

4. **Search knobs are module constants, not new function parameters**: `IFT_PROBE_STEP = 0.10`,
   `IFT_MAX_ITER = 200` (≈200 × 40 ms ≈ 8 s worst case, typical convergence is far earlier),
   `IFT_SELECTION_LAMBDA = 1.0` (balances hitting the target vs distributing the correction;
   `1.0` = one base-step move on one input weighs as much as a full initial-gap error reduction).
   Signature must stay stable (spec: совместимость результата). All three are folded into the
   cache key (see decision 6).

5. **No hard bounds** (user decision): the ±50% clip is removed; the penalty only ranks
   candidates. `search_config["input_ranges"]` is still emitted, computed as min/max of the
   *visited* points, because `generate_result_message` reads exactly that shape — no changes
   needed there beyond the added search metadata (`probe_step`, `directions`, `max_iter`,
   `lambda`, `epsilon`, `iterations`, `stop_reason`, `excluded`, `penalty`).

6. **Result mapping** (keeps `save_analysis_results`/`generate_result_message` working):
   - `matching_scenarios` — the best point if within `ε`, else `[]` (status `WARNING`).
   - `all_scenarios` — full evaluation trace (baseline + probe + iterations) in the existing dict
     shape (`input_values`, `output_value`, `deviation`, `deviation_percent`); `content` still
     truncates to 50.
   - `optimized_scenario` — final best point with the keys `save_analysis_results` expects
     (`input_values`, `actual_output`, `deviation`, `deviation_percent`), plus
     `"search": "adaptive"`; `None` if no point improved on the baseline.
   - Cache key gains the values of `IFT_PROBE_STEP`, `IFT_MAX_ITER` and `IFT_SELECTION_LAMBDA`
     (spec: кэширование по всем параметрам поиска).

7. **Dead code**: `generate_scenarios`, `test_scenarios`, `optimize_with_regression` are used
   only by IFT and by `tests/test_tools_performance.py::…::test_search_and_optimize`, which
   asserts grid+scipy reference values (378.22 / 0.01 / 1000.0). They are removed together with
   that test, replaced by a test of the new search; scipy stays a dependency only if something
   else imports it (verify during implementation; `optimize_with_regression` is its sole caller).

8. **Verification script**: `scripts/check_ift_target.py` — modeled on `scripts/run_qa.sh`
   (same "load model → run → print numbers" shape) but calling
   `analyze_model_inputs_for_target` **directly** (no HTTP, no LLM), like
   `tests/test_tools_performance.py`: copies `models/model.xlsx`, resolves a couple of inputs,
   picks a random target in a reachable band around the baseline output, runs the tool, prints
   `target / achieved / deviation_percent / iterations / stop_reason` and exits non-zero if the
   deviation exceeds tolerance. Direct call was chosen over HTTP/LLM so the numeric check is
   deterministic (user decision).

## Risks / Trade-offs

- [Probe sign is wrong for non-monotonic outputs] → the loop still terminates (miss → halving →
  step → 0 → no improvement → `MAX_ITER`), returns `WARNING` with the best point; the sign is
  re-derived only at start. If this shows up in practice, a re-probe-on-stagnation rule is the
  follow-up — it is deliberately not specified now.
- [`IFT_SELECTION_LAMBDA` scale is off] → too large: the search crawls between inputs without
  converging (still bounded by `MAX_ITER` → `WARNING`); too small: it concentrates on one input,
  i.e. approaches the degenerate 1-D behaviour the amendment removed. The constant is tunable
  from the check script's iteration counts, exactly like `IFT_MAX_ITER`.
- [Linear probe prediction wrong under strong nonlinearity] → the real evaluation of the chosen
  candidate is authoritative; an overshoot becomes a miss → that input's fraction halves.
- [Penalty divides by `base_step_i = 0`] → inputs with `|x_i⁰| < 1e-9` are excluded from the
  search (no base step, no probe, no penalty contribution) and reported in
  `search_config["excluded"]`; neutral inputs likewise.
- [Target near zero breaks `deviation_percent = deviation / target · 100`] → existing formula
  kept for output compatibility, but the *stop test* uses the absolute ε (and for
  `target_value == 0`, `ε = max(tolerance/100, 1e-9)`-style absolute guard) so division by zero
  cannot hang the loop.
- [Single answer instead of a list] → `matching_scenarios` may contain exactly one entry; LLM
  prompts already phrase it as "найденные значения", and the trace in `all_scenarios` keeps the
  inspection data.
- [Removing scipy refine may regress the one case it solved (sub-grid precision)] → the adaptive
  step halves repeatedly, so it reaches finer-than-grid precision on its own; the new check
  script asserts `deviation_percent <= tolerance` on random targets.

## Migration Plan

Internal algorithm change, no data/API migration. Deploy = normal image rebuild
(`docker compose build app --no-cache`). Rollback = revert the commit; result contract is
unchanged, so downstream consumers need no coordination.

## Open Questions

- Exact `IFT_MAX_ITER` (200) and `IFT_SELECTION_LAMBDA` (1.0) — tune both from the check
  script's iteration counts and hit rate after the first implementation run; both are
  constants, so changing them needs no spec edit.
