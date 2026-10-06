# Design

## Context

Today `src/aigw_service/api/v1/` holds both graph implementations. `main_graph.py:AgentGraph`
(legacy orchestrator loop) and `main_graph_deep.py:DeepAgentGraph` (`deepagents` task-
delegation) are both constructed only from `api/v1/router.py:get_agent()`, which branches on
`APP_CONFIG.app.agent_graph_impl` (`AGENT_GRAPH_IMPL` env var, default `LEGACY`). Both graphs
are mounted at the same `/api/v1/upload` + `/api/v1/invoke-agent` pair — the implementation is a
deployment-time choice, invisible in the URL. See proposal.md - Why for the motivation to split
these into separate, independently callable API versions.

`DeepAgentGraph` already depends only on: `main_graph_deep.py` itself, `prompts/deep_agent.py`
(`DEEP_AGENT_SYSTEM_PROMPT`, `CAPABILITIES_ANSWER`), and shared infra also used by `AgentGraph`
— `subagents/analyzer.py:AnalyzerSubAgent`, `subagents/orchestrator_tools.py:OrchestratorTools`
(+ `TOOL_TASK_DESCRIPTIONS`), `subagents/utils.py:_build_catalog_with_ids`, `tools.py`,
`excel_handler.py`/`lo_backend.py`, `schemas/llm_outputs.py`, `states/agents.py`,
`prompts/ift_analizer.py` / `ema_analizer.py` / `lookup_more.py`. No file is exclusively used by
`DeepAgentGraph` except `main_graph_deep.py` and `prompts/deep_agent.py`.

`api/__init__.py` already states the intended convention: path version should track service
major version (`/v1` for `1.x.y`, `/v2` for `2.x.y`), but only `/api/v1` is mounted today.

## Goals / Non-Goals

**Goals:**
- Give the deep-agent implementation a real, independently callable API surface at `/api/v2`,
  mirroring `/api/v1`'s structure (`router.py`, its own `upload` + `invoke-agent`).
- Make `/api/v1` deterministic: always the legacy orchestrator graph, no env-driven branch.
- Give `api/v2/` a fully independent copy of every module `DeepAgentGraph` needs, trimmed to
  drop code paths it doesn't exercise. **Zero cross-imports between `api/v1/` and `api/v2/` in
  either direction** — either version's internals can change, be deprecated, or be deleted
  without touching the other.

**Non-Goals:**
- Not changing `AgentGraph`'s (legacy) or `DeepAgentGraph`'s *behavior* for any code path it
  actually exercises — this is a structural/packaging change, not a logic change. Code paths
  dropped during trimming are, by definition, ones neither graph's live behavior depends on.
- Not keeping the two copies byte-identical over time, and not building any code-generation or
  sync tooling to keep them that way — once duplicated, `api/v1/`'s and `api/v2/`'s copies are
  independent and may drift; see Risks below for the accepted cost.
- Not changing the ZIP response envelope, the memory-store backend, or LibreOffice/UNO
  integration.
- Not removing `AGENT_GRAPH_IMPL` from `.env.example`/deployment docs beyond what's needed so a
  leftover setting is harmlessly ignored (tracked as a task, not a design concern).

## Decisions

**Full duplication, zero cross-imports — not shared modules, not a hoisted common location.**
Every module `DeepAgentGraph` needs gets a literal, independent copy under `api/v2/`:
`main_graph_deep.py`, `prompts/deep_agent.py`, `prompts/ift_analizer.py`/`ema_analizer.py`/
`lookup_more.py`, `schemas/llm_outputs.py`, `states/agents.py`, `subagents/analyzer.py`,
`subagents/orchestrator_tools.py`, `subagents/dummies.py`, `subagents/utils.py`, `tools.py`,
`excel_handler.py`, `lo_backend.py`, `russian_stemmer.py`, `utils.py` (header validation), and
`schemas_file.py`. `api/v1/` keeps its own copies untouched. Neither package imports from the
other, in either direction. Two alternatives considered and rejected:
- *Cross-import from `api/v2/` into `api/v1/`* (the previous version of this design) — rejected
  per explicit requirement: the two versions must be independently changeable/removable, and a
  cross-import makes `api/v1/` a load-bearing dependency of `api/v2/` forever.
- *Hoist shared code to a version-agnostic `api/common/`* — this would also satisfy "no
  `v1`↔`v2` cross-imports" (both would import from a third location instead of each other), but
  it does not satisfy what was actually asked: literal per-version duplicates, each fully
  readable and modifiable within its own package without hopping to a third shared location,
  and with no shared location at all whose lifecycle both versions depend on.

**What gets trimmed during duplication, and the principle for deciding.**
A function/class is kept in `api/v2/`'s copy only if it is reachable from `DeepAgentGraph`'s
actual runtime call graph: `main_graph_deep.py` → `AnalyzerSubAgent` (IFT/EMA) →
`OrchestratorTools.dispatch` → the `tools.py` functions invoked by name through
`_run_lo_calc`'s `getattr(_tools, func_name)` → their own internal helpers (name resolution:
`find_matching_cell`, `find_matching_outputs`, `create_input_mapping`, `create_output_mapping`,
`normalize_text`, `jaccard_similarity`, and everything `russian_stemmer.py` provides them) —
plus whatever the FastAPI layer needs (`common_headers`, `CopilotAgentRequest`,
`FileLoaderResponse`, `FailedDependencyResponse`). Concretely, this drops:
- `subagents/orchestrator_tools.py`'s `OrchestratorTools.TOOLS`, `ToolQueryArgs`, and the two
  `@tool`-decorated `analyze_model_inputs_for_target`/`analyze_excel_model` stub functions —
  these exist only so the *legacy* orchestrator can `llm.bind_tools(...)`; `DeepAgentGraph`
  calls `OrchestratorTools.dispatch()` directly and never touches `.TOOLS`.
- `tools.py`'s dependency-graph and reporting functions not on the calculation call graph above
  — `build_dependency_graph`/`visualize_dependency_graph`/`parse_cell_ref`/
  `get_direct_dependencies`/`build_dependency_graph_from_cell`,
  `find_output_cell_by_description`/`get_output_cell_address`, `get_output_info`,
  `greeting_user`/`greet_user`/`GreetingUserSchema`, `get_outputs_business_summary`,
  `describe_outputs_sheet`, `modify_excel_input_value` — and their now-unreferenced arg/result
  classes (`BuildDependencyGraphArgs`/`Result`, `DescribeOutputsSheetToolArgs`/`Result`,
  `GetOutputInfoToolArgs`/`Result`, `ExcelInputModificationToolArgs`/`Result`). None of these are
  called by either graph's live path today, in `api/v1/` or `api/v2/`.
- `states/agents.py`'s `ClassifierInput`/`ClassifierState` — leftover from the already-removed
  classifier node (see `CLAUDE.md`), unused by both graphs.
- `schemas_file.py`'s template schemas not used by `upload`/`invoke-agent` —
  `ExampleGenAIReadyAPIRequest`/`Response`, `MemorizerAgentRequest`/`Response`,
  `GreeterAgentRequest`/`Response`, `GraphAgentRequest`/`Response`, and `CopilotAgentResponse`
  if tracing confirms it unused — keeping only `CopilotAgentRequest`, `FileLoaderResponse`,
  `FailedDependencyResponse`.
`api/v1/`'s copies are left exactly as they are — trimming only ever happens on the new `api/v2/`
copy, never on the original.

**`/api/v2` gets its own, fully independent `upload` endpoint.**
Alternative considered: `/api/v2` exposes only `invoke-agent` and relies on clients having
called `/api/v1/upload` first. Rejected: it would make `/api/v2` a second-class, not
independently usable API surface, contradicting the "real API version" intent — a client that
never touches `/api/v1` should be able to use `/api/v2` end-to-end. Chosen approach: `api/v2`
defines its own `/api/v2/upload` route with its own independent handler code (no call into
`api/v1/router.py` — that would be a cross-import). Both handlers still write to the same
`APP_CTX.agent_memory.store` under the same `("memories", user_id)` namespace convention, so the
*observable* behavior — a workbook uploaded via either version is readable by both — is
preserved (per the `deep-agent-api` spec's "Upload via the other version is still usable"
scenario) even though the two handlers share no code.

**`AGENT_GRAPH_IMPL` is removed, not deprecated-but-kept.**
Alternative considered: keep the setting but make it a no-op, for deployment safety during
rollout. Rejected: the setting's only purpose was choosing the implementation for `/api/v1`;
once that choice is a URL path instead, a kept-but-ignored env var is pure confusion with no
migration value — `config/base_config.py` fields are read directly off env vars with no
deprecation-warning mechanism in this codebase. A deployment that sets it will simply have the
setting silently ignored (pydantic-settings does not error on unknown/unused env vars), which is
the simplest safe behavior.

**`get_agent()` becomes two separate single-implementation factories.**
`api/v1/router.py:get_agent()` drops its `if/else` and unconditionally returns `AgentGraph()`.
`api/v2/router.py` gets its own `lru_cache`d `get_agent()` returning `DeepAgentGraph()`. No
shared "agent factory" abstraction is introduced — each router's factory is a one-line
`lru_cache(maxsize=1)` function, matching the existing style, and the two are intentionally not
unified since they will keep diverging (different graph classes, likely different config knobs
over time).

## Risks / Trade-offs

- **Breaking change for current `AGENT_GRAPH_IMPL=DEEPAGENTS` deployments** → Mitigation: call
  this out explicitly in the proposal and in deployment docs/`.env.example`; this is an
  experimental flag on a `feature/deepagents` branch, not yet a documented stable contract, so
  the blast radius is expected to be limited to internal testing deployments.
- **`/api/v2/upload` and `/api/v1/upload` sharing one store keyed only by `x-user-id`** means a
  workbook uploaded for deep-agent testing is also visible to `/api/v1/invoke-agent` and vice
  versa → Mitigation: this matches today's behavior already (the store was never
  version-scoped) and is called out explicitly in the `deep-agent-api` spec so it's a documented
  contract, not an accidental coupling.
- **Duplicate-maintenance burden**: a correctness fix to logic both copies conceptually share —
  the Jaccard/cross-lingual name-resolution matching, a LibreOffice/UNO backend quirk, the
  GigaChat stop-event/loguru-brace handling — must now be applied by hand in both `api/v1/`'s
  and `api/v2/`'s copies, and the two can silently drift apart over time → Mitigation: add a
  one-line comment at the top of each duplicated file in `api/v2/` naming its `api/v1/`
  counterpart by path, and note in `CLAUDE.md` that a fix to one of these modules should be
  checked against the other version's copy. Accepted as the deliberate cost of full version
  independence (see Decisions).
- **Trimming could remove something actually needed** if the call-graph trace misses a dynamic
  dispatch path — notably `orchestrator_tools.py`'s `_run_lo_calc` resolves the target function
  by string via `getattr(_tools, func_name)`, which a naive grep-based trace could miss →
  Mitigation: after trimming each module, run the full existing test suite plus a live smoke
  test through `/api/v2/invoke-agent` exercising both `analyze_model_inputs_for_target` and
  `analyze_excel_model` before considering `api/v2/tools.py` complete (see tasks.md).

## Migration Plan

1. Build `api/v2/` alongside the untouched `api/v1/` (additive) before removing anything from
   `api/v1/router.py` or `config/base_config.py`, so the service is import-clean at every
   intermediate commit.
2. Duplicate every module `api/v2/` needs (see Decisions) one at a time, innermost dependencies
   first (prompts/schemas/states have no internal cross-module imports to rewrite; `tools.py`
   next, trimmed per the principle above; then `subagents/` and `excel_handler.py`/
   `lo_backend.py`; `main_graph_deep.py` last), rewriting each copy's internal imports to point
   only within `api/v2/`. Update `tests/unit/test_deep_agent_graph.py`'s imports to match.
   Verify zero cross-imports in either direction before moving on (see tasks.md).
3. Add `api/v2/router.py` (`upload` + `invoke-agent`, mirroring `api/v1/router.py`'s structure,
   using `api/v2/`'s own `utils.py`/`schemas_file.py` copies) and mount it in `api/__init__.py`
   at `/api/v2`.
4. Remove the `AGENT_GRAPH_IMPL`-driven branch from `api/v1/router.py:get_agent()` and drop
   `agent_graph_impl` from `config/base_config.py`.
5. Update `scripts/run_tool_queries.py` to accept an API-version selector instead of a hardcoded
   `/api/v1` path.
6. Rollback: since this is an additive duplication with no logic changes to either graph's
   reachable code, rollback is a plain `git revert` of the commit(s); there is no data migration
   to undo.

## Open Questions

None — the scope, approach, and task breakdown above are final for this change.
