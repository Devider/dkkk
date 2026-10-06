# Proposal

## Why

The repo currently has two agent implementations — the legacy orchestrator loop
(`main_graph.py:AgentGraph`) and an experimental `deepagents`-based graph
(`main_graph_deep.py:DeepAgentGraph`) — living side by side in `api/v1/` and selected at
deploy time by the `AGENT_GRAPH_IMPL` env var. A client has no way to pick one deliberately,
and nothing distinguishes a "stable" implementation from an "experimental" one in the API
contract itself. `api/__init__.py` already documents the project's versioning convention
("Версия 1.x.y -> /v1, Версия 2.x.y -> /v2") but only `/api/v1` is mounted. This change gives
the deep-agent implementation its own real API version, `/api/v2`, so it can be called
deliberately and evolve independently of the legacy `/api/v1` contract, while `/api/v1`
reverts to being deterministically backed by the legacy implementation only.

## What Changes

- **BREAKING**: `/api/v1/invoke-agent` and `/api/v1/upload` no longer consult
  `AGENT_GRAPH_IMPL` — `/api/v1` is always backed by the legacy orchestrator graph
  (`AgentGraph`). A deployment currently running with `AGENT_GRAPH_IMPL=DEEPAGENTS` will, after
  this change, get the legacy implementation at `/api/v1` and must switch to `/api/v2` for the
  deep-agent behavior.
- Add a new `/api/v2` router (`api/v2/router.py`), mounted in `api/__init__.py`, exposing
  `/api/v2/upload` and `/api/v2/invoke-agent` backed exclusively by `DeepAgentGraph`.
- Give `api/v2/` its own fully independent copy of every module `DeepAgentGraph` needs —
  `main_graph_deep.py`, `prompts/deep_agent.py`, and also `tools.py`,
  `excel_handler.py`/`lo_backend.py`, `subagents/analyzer.py`, `subagents/orchestrator_tools.py`,
  `subagents/dummies.py`, `subagents/utils.py`, `schemas/llm_outputs.py`, `states/agents.py`,
  `prompts/ift_analizer.py` / `ema_analizer.py` / `lookup_more.py`, `russian_stemmer.py`, and
  `utils.py` (header validation) — each duplicated rather than imported from `api/v1/`, and
  trimmed to drop code paths `DeepAgentGraph` doesn't exercise (e.g. the native-tool-calling
  wrappers in `orchestrator_tools.py`, or the dependency-graph/business-summary functions in
  `tools.py` that neither graph actually calls). **`api/v1/` and `api/v2/` SHALL have zero
  cross-imports in either direction** — each is a fully self-contained package.
- Remove the `AGENT_GRAPH_IMPL` setting (`config/base_config.py`) and the `get_agent()`
  branch in `api/v1/router.py` that chose between `AgentGraph`/`DeepAgentGraph` — each router
  now constructs exactly one graph class.
- Update `scripts/run_tool_queries.py` (hardcoded `/api/v1/upload` and
  `/api/v1/invoke-agent` paths) to accept which API version to exercise, so the quality-eval
  harness can target either implementation.
- Move `tests/unit/test_deep_agent_graph.py`'s imports to the new `api/v2` module paths.

## Capabilities

### New Capabilities
- `agent-graph`: baseline-documents the `/api/v1` upload + invoke-agent contract (headers,
  session/thread semantics, bounded orchestrator tool-calling loop, zip response), reflecting
  the state after this change — `/api/v1` deterministically backed by the legacy orchestrator
  graph only (no deployment-time implementation switch). `openspec/specs/` has no capability
  backfilled yet, so this is written as a new capability per the baseline-first rule, not a
  delta against a prior spec.
- `deep-agent-api`: the `/api/v2` upload + invoke-agent request/response contract, backed by
  the `deepagents` task-delegation graph (model decides when to delegate to the
  `analyze_model_inputs_for_target` / `analyze_excel_model` subagents via the `task` tool;
  bounded by a recursion limit with a user-facing fallback message; duplicate subagent calls
  within a turn are deduped).

### Modified Capabilities
- None. `agent-graph` reads as "Modified" in spirit (its backing selection logic is changing)
  but has no prior spec to diff against, so it is created fresh above instead.

## Impact

- **Code**: new `src/aigw_service/api/v2/` package — its own `router.py`, `main_graph_deep.py`,
  and independent copies of ~13 supporting modules (see What Changes), each trimmed of code
  `DeepAgentGraph` doesn't reach; `src/aigw_service/api/v1/router.py` (drop the `DeepAgentGraph`
  branch); `src/aigw_service/api/__init__.py` (mount the new router); `src/aigw_service/config/
  base_config.py` (drop `agent_graph_impl`). This is a substantially larger diff than a two-file
  move — roughly the full agent-support stack gets a second, independently-maintained copy.
- **Ongoing maintenance cost**: a correctness fix to logic both copies share conceptually (e.g.
  the Jaccard/name-resolution matching, a LibreOffice/UNO backend quirk, GigaChat stop-event
  handling) must now be applied to both `api/v1/` and `api/v2/`'s copies by hand — there is no
  shared module or generation step keeping them in sync. This is the deliberate cost of full
  version independence (see design.md - Decisions).
- **Tests**: `tests/unit/test_deep_agent_graph.py` import paths; no behavioral test changes
  expected since `DeepAgentGraph`'s own logic is untouched, only its location.
- **Scripts**: `scripts/run_tool_queries.py` gains an API-version selector;
  `scripts/new_graph_test.py` is unaffected (only ever exercised `AgentGraph` directly).
- **Deployment**: deployments pinning `AGENT_GRAPH_IMPL=DEEPAGENTS` to get the deep agent at
  `/api/v1` must instead call `/api/v2`; the env var is removed, so a leftover setting is simply
  ignored rather than erroring.
- **Docs**: `CLAUDE.md`'s request-flow section describes a single `/api/v1` endpoint pair and
  will need the `/api/v2` pair documented too (tracked as a task, not a spec change).
