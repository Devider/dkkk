# `/api/v2` — deep-agent implementation

Architecture notes for the `/api/v2` surface. Read this before changing anything under
`src/aigw_service/api/v2/`. For repo-wide facts (commands, config/bootstrap, LLM backend, Docker,
loguru gotcha) see the root `CLAUDE.md`; this file only covers what is specific to v2.

## TL;DR

- `/api/v2` is backed by **`DeepAgentGraph`** (`main_graph_deep.py`), built on the `deepagents`
  package (`create_deep_agent`), instead of v1's hand-written `orchestrator <-> tool_executor ->
  synthesizer` loop.
- The top-level LLM uses native tool calling and delegates calculations through deepagents' built-in
  **`task`** tool to one of two **compiled subagents**: `analyze_model_inputs_for_target` (IFT) and
  `analyze_excel_model` (EMA). The same LLM call that decides "no more tools" writes the final answer.
  There is no separate synthesizer node.
- Each subagent is a one-node wrapper graph → `OrchestratorTools.dispatch` → `AnalyzerSubAgent`
  (LLM name resolution against the workbook catalog) → LibreOffice calculation in `tools/excel_calc.py`, run in
  a separate **spawned process**.
- External contract (headers, request body, ZIP response) is identical to `/api/v1`.
- **No imports from `api/v1/`**. Every module v2 needs is a v2-local copy. Fixes to shared
  logic (name matching, LO/UNO quirks, stop-event handling) must be applied to both trees by hand.

## Module map

| Path | Role |
|---|---|
| `router.py` | FastAPI routes `POST /upload`, `POST /invoke-agent`; builds `thread_id`, Langfuse config, ZIP response, error mapping |
| `main_graph_deep.py` | `DeepAgentGraph`: outer graph, deep agent construction, subagent wrapper graphs, dedup, recursion limit |
| `prompts/deep_agent.py` | `DEEP_AGENT_SYSTEM_PROMPT` (routing/guardrails/answer style) + `CAPABILITIES_ANSWER` |
| `prompts/ift_analizer.py`, `prompts/ema_analizer.py` | System prompts for the IFT/EMA query-analysis step (`{inputs}`/`{outputs}` catalog placeholders) |
| `prompts/lookup_more.py` | Prompt for the second-pass name lookup (`{lookup}`, `{inputs}`) |
| `subagents/orchestrator_tools.py` | `OrchestratorTools.dispatch`: runs the analyzer, validates required params, submits the LO calc to the process pool, normalizes to `ToolCallResult`; `TOOL_TASK_DESCRIPTIONS` |
| `subagents/analyzer.py` | `AnalyzerSubAgent`: small LangGraph `anlyze_query -> [lookup_more] -> resolve_names` |
| `subagents/dummies.py` | `aretry_structured_llm`: structured-output retry (3 attempts) → dummy fallback |
| `subagents/utils.py` | Catalog building (`_build_catalog_with_ids` → `IN{n}`/`OUT{n}` ids via openpyxl), token/diagnostic helpers |
| `schemas/llm_outputs.py` | Pydantic structured outputs: `QueryAnalysisIFT`, `QueryAnalysisEMA`, `InputItem`, `OutputItem`, `RangeConfig`, `LookupResult` |
| `states/agents.py` | TypedDict states for the IFT/EMA analyzer graphs |
| `tools/storage.py` | `check_uploaded_file`: tool the deep agent calls directly (not via `task`) to check the store + disk for the user's uploaded workbook. New direct tools go in `tools/` and into `tools/__init__.py:DEEP_AGENT_TOOLS` |
| `tools/excel_calc.py` | Real calculations: `analyze_model_inputs_for_target`, `analyze_excel_model`, Jaccard name matching (`find_matching_cell`/`find_matching_outputs`), Inputs/Outputs mappings, result cache |
| `excel_handler.py`, `lo_backend.py` | LibreOffice-via-UNO workbook wrapper (`ExcelWorkbook`, `get_compiled_func`) |
| `russian_stemmer.py` | Pure-Python Snowball Russian stemmer used by `normalize_text` |
| `schemas_file.py`, `utils.py` | Request/response models; `common_headers` header validation |

## Request flow

```
POST /api/v2/upload
  -> saves file to $TMPDIR/user_file_{x-user-id}.{xlsx|xls}
  -> store.aput(("memories", user_id), user_id, {"filename": ...})   # shared with /api/v1

POST /api/v2/invoke-agent   {"message": "..."}
  -> thread_id = f"v2:{user_id}:{session_id or uuid4()}"
  -> DeepAgentGraph.graph.ainvoke({messages:[HumanMessage], thread_id, user_id}, config)
  -> result["messages"][-1].content
  -> ZIP: txt_response.txt + generated/agent_output.xlsx (empty placeholder)
```

Error mapping in `invoke_agent`: `StopEventError` (GigaChat "temporarily unavailable") → **503**;
any other exception → **424** with `FailedDependencyResponse`. Header validation failures → 422.

The `v2:` prefix on `thread_id` is required. v1 and v2 share the same `MemorySaver`
checkpointer singleton but have incompatible state schemas, so a shared thread would corrupt state.

## Graph structure

```
Outer graph (StateGraph[DeepAgentGraphState], checkpointer = APP_CTX.agent_memory.checkpointer)

START -> extract_excel_data -> deep_agent -> END
                                   │
                                   ▼  (self.deep_agent.ainvoke, recursion_limit=16)
              ┌──────────── create_deep_agent(model=APP_CTX.llm) ────────────┐
              │  model call ──tool_calls(task)──► SubAgentMiddleware         │
              │      ▲                                │                      │
              │      └──────── ToolMessage ◄──────────┤                      │
              │                                       ▼                      │
              │     CompiledSubAgent "analyze_model_inputs_for_target"       │
              │     CompiledSubAgent "analyze_excel_model"                   │
              │        (one-node wrapper graph → OrchestratorTools.dispatch) │
              └──────────────────────────────────────────────────────────────┘
```

### `extract_excel_data` (`get_available_inputs_outputs`)

- Reads the filename from the store (`("memories", user_id)`). Raises `ValueError` if no file was
  uploaded, which the router returns as a 424.
- Builds the catalogs with `_build_catalog_with_ids` (openpyxl, `data_only=True`, run via
  `asyncio.to_thread`): `available_inputs = {"IN0": "<name>", ...}` and
  `available_outputs = {"OUT0": ...}`.
- Resets `executed_tool_fingerprints = []` every turn. The checkpointer persists state across turns,
  so without this reset dedup would leak from earlier turns.

### `deep_agent` (`run_deep_agent`)

- Calls the inner deep agent with the full conversation `messages` plus the domain fields
  (`available_inputs`, `available_outputs`, `filename`, `user_id`, `executed_tool_fingerprints`).
- `DeepAgentGraphState` extends `DeepAgentState` with these domain fields. `SubAgentMiddleware`
  forwards the entire parent state to subagents **except** `messages`/`todos`/`structured_response`/
  `skills_metadata`, which is how the subagent wrappers get the catalog and filename without extra
  plumbing. If you add a field the subagents need, add it to both `DeepAgentGraphState` and
  `SubAgentWrapperState`.
- `RECURSION_LIMIT = 16` (each tool round-trip costs about 2 steps). On `GraphRecursionError` it
  returns a canned Russian `RECURSION_LIMIT_MESSAGE` as the final AIMessage instead of failing
  the request.
- Returns all messages from the deep agent (including `task` tool calls/results) into the outer
  `messages`, so later turns see earlier calculation results in history.

### Subagent wrapper graphs (`_build_subagent_wrapper_graph`)

A single node per tool name:

1. `query = messages[-1].content`. This is the `description` the top-level LLM wrote for the `task`
   call. **The subagent sees only this string, not the chat history.** The system prompt tells the
   LLM to make the description self-contained (carry over year, target, ranges from earlier turns).
2. Dedup: fingerprint `f"{tool_name}:{query}"`. A repeat within the turn returns
   `DUPLICATE_CALL_MESSAGE` without recalculating.
3. Logs `TOOL ARGS: {tool_name} | {"reformulated_query": ...}`. This is the marker
   `scripts/run_tool_queries.py` / `analyze_results.py` parse from `server.log`.
4. `OrchestratorTools.dispatch(...)` → returns `AIMessage(result_text)`. That text is what the
   top-level LLM sees as the `task` result.

## Subagent internals

### `OrchestratorTools.dispatch` (`subagents/orchestrator_tools.py`)

```
IFT  (analyze_model_inputs_for_target)          EMA  (analyze_excel_model)
  ift_agent.ainvoke([HumanMessage(query)])         ema_agent.ainvoke([HumanMessage(query)])
  require target_value and output_year             require year and a range (start,end) per input
    └─ missing → WARNING + example query             └─ missing → WARNING + example query
  tools.analyze_model_inputs_for_target(           tools.analyze_excel_model(
     tolerance=0.1, max_scenarios=1000)               ranges, steps (default step 0.5))
```

- Never substitute defaults for values the user did not give (no fake year, no 0 target). If a
  value is missing, return `WARNING` and ask the user. The dummies in `dummies.py` leave
  `target_value`/`year` as `None` for the same reason.
- LO calculations run in a `ProcessPoolExecutor(max_workers=1, mp_context="spawn")` through
  `_run_lo_calc`, so CPU-heavy scenario work doesn't block the event loop or `/health`. Use `spawn`,
  not `fork`: forking from a LangGraph worker thread can deadlock. Return values must be picklable
  and JSON-safe, so `_safe_content` strips things like `result_df` (a DataFrame). Because there is
  one worker, calculations are **serialized across all requests**.
- `_ANALYSIS_CACHE` in `tools/excel_calc.py` (FIFO, max 10, keyed by file path + params + user_id) lives
  **inside the worker process** and survives across calls because the pool is long-lived.
- Result `status` is one of `OK` / `ERROR` / `INFO` / `WARNING`. The system prompt tells the
  top-level LLM how to phrase each one for the user.

### `AnalyzerSubAgent` (`subagents/analyzer.py`)

```
START -> anlyze_query --(any mentioned_input.lookup_more?)--> lookup_more -> resolve_names -> END
                     \--------------- sufficient ----------------------------^
```

- `anlyze_query`: structured output (`QueryAnalysisIFT` / `QueryAnalysisEMA`) with the analyzer
  prompt formatted with the stringified catalogs (`ID: IN3 | Название: ...`). The LLM picks
  catalog **ids**.
- `lookup_more`: second structured call (`LookupResult`) only for items flagged `lookup_more=True`.
- `resolve_names`: maps the chosen `equivalent_input_id` → canonical name from `available_inputs`.
  A hallucinated id raises a `KeyError`.
- All structured calls go through `aretry_structured_llm` (async, up to 3 attempts, then dummy).
- Note the node is spelled `anlyze_query` (sic). Keep it that way if logs/tests refer to it.

### Calculation layer (`tools/excel_calc.py`)

- Opens the workbook with `ExcelWorkbook` (a fresh headless `soffice` per call, ReadOnly,
  in-memory edits only), rebuilds Inputs/Outputs mappings, and resolves names **a second time**
  with Jaccard matching (`find_matching_outputs`, `find_matching_cell`). LLM-chosen canonical names
  normally match exactly here. Mismatches show up as `INPUT MATCHING` / `OUTPUT MATCHING` log lines.
- IFT: grid of scenarios (`generate_scenarios`) → `get_compiled_func` → `test_scenarios` →
  `optimize_with_regression`.
- EMA: sweep over the given ranges/steps for each output and year.
- The file path comes from the store (`get_store_file(user_id)`), with `file_name` as a fallback.

## Top-level prompt behavior (`prompts/deep_agent.py`)

These rules are enforced by the prompt, not by code:

- Never invent numbers. Any numeric answer about the model requires a `task` call.
- No `task` for: greetings/clarifications, capability questions (answer with `CAPABILITIES_ANSWER`
  almost verbatim), answers already in history, off-topic requests (canned refusal), or languages
  other than Russian/English (canned refusal).
- Never expose internals to the user (tool names, `deepagents`/LangGraph, cell refs, ids, placeholder
  values like "Year 0"). `CAPABILITIES_ANSWER` is a separate constant so a test can check it for leaked
  identifiers.
- Answers in Russian, formal tone. On `ERROR`/`INFO`/`WARNING`, ask only for what the tool result
  says is missing.

## Differences from `/api/v1` at a glance

| | v1 `AgentGraph` | v2 `DeepAgentGraph` |
|---|---|---|
| Loop | custom `orchestrator <-> tool_executor` | `create_deep_agent` + `task` tool |
| Iteration cap | `MAX_ORCHESTRATOR_ITERATIONS=4` → forced synthesis | `recursion_limit=16` → canned message |
| Final answer | separate `synthesizer` LLM call | the deep agent's last model turn |
| Tool arg | `reformulated_query` on bound tools | `task` `description` |
| Dedup | `_fingerprint_call` in `tool_executor` | fingerprint in subagent wrapper node |
| LO calc execution | in-process | spawned `ProcessPoolExecutor` (1 worker) |
| `thread_id` | `{user}:{session}` | `v2:{user}:{session}` |
| Store namespace | `("memories", user_id)` | same (uploads are shared) |

## Tests and specs

- `tests/unit/test_deep_agent_graph.py`: `extract_excel_data`, subagent wrapper dispatch + dedup,
  full deep-agent loop with a mocked LLM, capabilities-prompt leak check.
- `tests/unit/test_v2_router.py`: header validation, upload, "invoke requires prior upload",
  success ZIP, store shared with v1, `thread_id` isolation from v1.
- `tests/unit/test_stop_event.py`: GigaChat stop-event wrapping in `context.py` (shared by both
  versions). (`test_tool_dedup.py` targets v1 only.)
- Behavior spec / design rationale: `openspec/changes/add-deep-agent-v2-api/` (or
  `openspec/specs/deep-agent-api/` once archived).

## Rules of thumb when changing v2

- Don't import from `aigw_service.api.v1`, and don't make v1 import from v2.
- If you change logic that has a twin in v1 (`tools/excel_calc.py` matching, `lo_backend.py`, `russian_stemmer.py`,
  analyzer, dummies), decide explicitly whether v1 needs the same fix.
- New subagent: add a `TOOL_TASK_DESCRIPTIONS` entry, a branch in `OrchestratorTools.dispatch`, and a
  `_build_compiled_subagent(...)` in `DeepAgentGraph.__init__`; mention it in the system prompt and
  check the prompt's "don't leak internals" list.
- Anything that crosses into `_run_lo_calc` must be picklable and importable under `spawn`.
- Use `logger.opt(exception=True).error("...: {}", str(e))` in `except` blocks (loguru brace issue).
- Behavior changes go through OpenSpec (`opsx:propose`). See the root `CLAUDE.md`.
