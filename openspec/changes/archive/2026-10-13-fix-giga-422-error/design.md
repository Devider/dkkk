# Design

## Context

See proposal.md - Why.

The main graph (`main_graph.py`) implements a LangGraph state machine:
`extract_excel_data → orchestrator ↔ tool_executor → synthesizer → END`.

The orchestrator uses LLM native tool-calling to decide which of two tools (`analyze_model_inputs_for_target` / `analyze_excel_model`) to invoke. Each tool execution goes through `OrchestratorTools.dispatch()`, which delegates to `AnalyzerSubAgent` + `ProcessPoolExecutor`.

The graph is compiled with `MemorySaver` checkpointer. On session continuation (same `thread_id`), `ainvoke` restores the full message history. GigaChat rejects any history where an `AIMessage` references a `tool_call_id` without a matching `ToolMessage`.

Two failure paths produce this inconsistency:
1. **Uncaught exceptions in `tool_executor`**: `_execute_single_call` calls `dispatch()` without try/except. If dispatch fails, the `tool_executor` node itself raises, no `ToolMessage` is added, and the history is corrupted.
2. **Iteration cap**: `route_after_orchestrator` returns `"synthesizer"` when `tool_call_count >= MAX_ORCHESTRATOR_ITERATIONS`, but the last `AIMessage` may still contain unexecuted `tool_calls`.

## Goals / Non-Goals

**Goals:**
- Guarantee that every `tool_call_id` in any `AIMessage` has a corresponding `ToolMessage` in the checkpointer history
- Restore session continuity after any internal tool failure
- Close unexecuted tool calls at iteration cap without GigaChat 422

**Non-Goals:**
- Fixing the root cause of tool failures (e.g., IFT 424 errors) — only the symptom (broken session history)
- Changing GigaChat API usage or the `/invoke-agent` contract
- Modifying the synthesizer prompt or the orchestrator prompt
- Adding retries for tool failures (future improvement)
- Changing the checkpointer mechanism

## Decisions

### Decision 1: Catch exceptions at `_execute_single_call`, not in `dispatch()`

**Choice:** Wrap `dispatch()` in `try/except` inside `_execute_single_call` method.

**Rationale:**
- `dispatch()` is a method on `OrchestratorTools` and lives in `subagents/orchestrator_tools.py`. Catching at `_execute_single_call` keeps the error handling close to the `ToolMessage` creation point, minimizing the chance of a future refactor accidentally re-throwing.
- Raising exceptions from `dispatch()` to `tool_executor` is the expected LangGraph pattern — the graph node should not propagate unhandled exceptions as that breaks the checkpoint state.

**Alternatives considered:**
- Catch inside `dispatch()`: Would require wrapping each dispatch branch (`_run_ift`, `_run_ema`) and adding logic to return error dicts. More places to touch.
- Add a retry wrapper around `dispatch()`: Out of scope — the intent focuses on guaranteed `ToolMessage` production, not reliability of tool execution itself.

### Decision 2: New `force_execute_remaining` node instead of closing tool_calls inline

**Choice:** Add a dedicated LangGraph node `force_execute_remaining` that routes from `orchestrator` when iteration cap is reached.

**Rationale:**
- Keeps the routing logic clean — `route_after_orchestrator` returns a single string, and the cap-case doesn't mix with the "no tool calls" case.
- Makes the cap behavior explicit and testable as a separate node.
- Avoids modifying `route_after_orchestrator` to produce side effects (it should only return a string).
- Follows LangGraph best practices: each decision point has a clear node boundary.

**Alternatives considered:**
- Close tool_calls inline in `route_after_orchestrator`: Impossible — it's a synchronous method that has no access to the `AgentState` update mechanism (returns only a route string).
- Close tool_calls in `synthesizer`: Would require synthesizer to patch `messages` state before producing the answer, mixing concerns and risking double-persistence of messages.
- Add `tool_calls` field to `AgentState` and close in a reducer: Over-engineered for a rare code path; requires modifying `add_messages` behavior.

### Decision 3: `force_execute_remaining` returns `ToolMessage` stubs, not actual tool results

**Choice:** The stub `ToolMessage` content is a short description: "Достигнут лимит итераций оркестратора. Инструмент '<name>' не выполнен."

**Rationale:**
- The purpose is solely to satisfy the GigaChat `tool_call_id` constraint. No actual computation should happen at this point (the cap exists to prevent infinite loops).
- The synthesizer will see these as error entries and can acknowledge the limitation in its final answer.

**Alternatives considered:**
- Attempt to run the remaining tools: Defeats the purpose of the cap and risks hanging or further errors.

### Decision 4: Exception types to catch

**Choice:** Catch `Exception` (base class) to handle all unexpected errors, including `asyncio.CancelledError`.

**Rationale:**
- `asyncio.CancelledError` inherits from `BaseException`, not `Exception`, in Python 3.8+. A plain `except Exception` would miss it. The intent explicitly mentions `CancelledError`.
- Catching `BaseException` directly would also catch `SystemExit`/`KeyboardInterrupt`, which we do NOT want to silently suppress.

**Refinement:** We catch `Exception` plus explicitly `asyncio.CancelledError`. This is the recommended pattern for asyncio code.

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| Error `ToolMessage` content may confuse GigaChat on next invoke | The synthesizer prompt already handles error `tool_results` via `_format_tool_results`. The synthesizer is designed to work with mixed success/failure results. |
| Adding `force_execute_remaining` node changes graph topology, may affect existing tests | The new node only changes routing behavior at iteration cap (rare). Normal flow is unchanged. Add explicit unit tests for the cap path. |
| Silent error suppression — tools failing but appearing "resolved" in history | The `ToolMessage` content clearly states the error reason, and `tool_results` in state preserves the error status. The synthesizer can acknowledge failures. |
| `CancelledError` handling may mask legitimate cancellation bugs | Logged at ERROR level. The error is still observable in logs. The session is preserved at the cost of the failed tool call. |
| Memory growth in checkpointer — more messages per turn | Each failed/capped call adds one extra `ToolMessage`. At max 2 tools per turn and 4 iterations, this is at most 8 extra messages per turn. Negligible. |

## Migration Plan

No migration is needed. The change is entirely internal to the graph execution flow. Deployment:

1. Deploy new code (no config changes required)
2. Existing sessions in the checkpointer remain unchanged — the fix only affects new tool call executions
3. No database or state migration required

Rollback: revert the `main_graph.py` change. The graph topology reverts to the original 4-node graph.

## Open Questions

- **Should we add a `tool_error_count` field to `AgentState` for observability?** Not required for the spec. Could be added in a follow-up change if error rate monitoring proves useful. The intent does not require this, and logging at ERROR level already provides observability.
