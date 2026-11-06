# Robust Tool Execution Specification

## Purpose

Guarantees that every `tool_call_id` from the orchestrator's `AIMessage` always has a corresponding `ToolMessage` in the history stored by the checkpointer, even when internal tool errors occur or the orchestrator loop terminates early.

**Message order and completeness are critical:** GigaChat enforces a strict constraint on message order and completeness — every `tool_call_id` listed in an `AIMessage` **must** have exactly one corresponding `ToolMessage` with a matching `tool_call_id` in the history persisted by the checkpointer. This constraint is enforced on every `ainvoke` with the same `thread_id`: when the checkpointer restores a session's full message history, if any `tool_call_id` is left unclosed by a `ToolMessage`, GigaChat returns error 422 "every assistant function call must have a result in history". The **order** of `ToolMessage` entries in the history must exactly match the order of `tool_call_id`s in the original `AIMessage.tool_calls` — order violations also trigger a 422 error.

## Requirements

### Requirement: tool_executor must produce a ToolMessage for every dispatched call

The system MUST wrap every call to `OrchestratorTools.dispatch()` inside `tool_executor` with exception handling that catches any raised `Exception` and produces a `ToolMessage` with an error description. The `tool_call_id` of the failed call MUST be preserved in the returned `ToolMessage`.

`asyncio.CancelledError` MUST NOT be converted into a `ToolMessage`: it signals that the whole run is being aborted (a newer request superseded this one, or the client went away), and it MUST propagate out of the node so the run stops instead of continuing towards the synthesizer and answering a client that is no longer waiting.

#### Scenario: Tool dispatch raises an exception

- **WHEN** `tool_executor._execute_single_call` calls `OrchestratorTools.dispatch()` and it raises an exception (e.g. HTTP 424, timeout, parsing error)
- **THEN** `_execute_single_call` catches the exception and returns a `ToolMessage(content="<error_description>", tool_call_id=<call["id"]>, name=<call["name"]>)`
- **AND** the exception is logged at ERROR level
- **AND** no exception propagates to the LangGraph node

#### Scenario: Tool dispatch raises asyncio.CancelledError

- **WHEN** `OrchestratorTools.dispatch()` raises `asyncio.CancelledError` during execution
- **THEN** `_execute_single_call` re-raises the cancellation instead of returning a `ToolMessage`
- **AND** the cancelled call is not recorded as a `ToolMessage` or as an error entry in `tool_results`
- **AND** the cancellation is logged
- **AND** the run aborts, so no response is produced for a superseded request

#### Scenario: Tool dispatch returns normally

- **WHEN** `OrchestratorTools.dispatch()` completes without raising an exception
- **THEN** `_execute_single_call` returns `ToolMessage` with the normal `result["result_text"]` as content (existing behavior unchanged)

### Requirement: force_execute_remaining must close unexecuted tool_calls at iteration cap

When the orchestrator iteration cap (`MAX_ORCHESTRATOR_ITERATIONS`) is reached, a new `force_execute_remaining` node MUST be invoked instead of `synthesizer`. This node MUST produce a `ToolMessage`-stub for every unexecuted `tool_call_id` present in the last `AIMessage` from the orchestrator.

#### Scenario: Iteration cap reached with pending tool_calls

- **WHEN** `route_after_orchestrator` returns `"force_execute_remaining"` because `tool_call_count >= MAX_ORCHESTRATOR_ITERATIONS`
- **AND** the last `AIMessage` contains `tool_calls` that have not yet been executed
- **THEN** `force_execute_remaining` node produces one `ToolMessage` per unexecuted `tool_call_id` with content describing the cap was reached
- **AND** routing proceeds to `synthesizer` on the next step

#### Scenario: Iteration cap reached with no pending tool_calls

- **WHEN** `route_after_orchestrator` returns `"force_execute_remaining"` because `tool_call_count >= MAX_ORCHESTRATOR_ITERATIONS`
- **AND** the last `AIMessage` has no `tool_calls` or all were already executed
- **THEN** `force_execute_remaining` returns empty `messages` (no-op) and routing proceeds to `synthesizer`

#### Scenario: Iteration cap not reached

- **WHEN** `tool_call_count < MAX_ORCHESTRATOR_ITERATIONS` and the orchestrator produced `tool_calls`
- **THEN** routing proceeds to `tool_executor` as before (existing behavior unchanged)

### Requirement: restored session history must satisfy GigaChat tool_call_id constraint

After applying this change, the message history stored in the checkpointer MUST satisfy the GigaChat constraint that every `tool_call_id` referenced in any `AIMessage` has exactly one corresponding `ToolMessage` with matching `tool_call_id`. This MUST hold across session continuation (subsequent `ainvoke` calls with the same `thread_id`).

**Message order is critical:** GigaChat verifies that every `tool_call_id` in history is not merely present, but in the correct position relative to the originating `AIMessage`. `ToolMessage` entries must be appended to the history in the same order that `tool_call_id`s appeared in `AIMessage.tool_calls`. Order violations — such as mixing `ToolMessage` entries from different `ainvoke` calls, or inserting a `ToolMessage` before its corresponding `AIMessage` — also trigger a 422 error. The LangGraph `add_messages` reducer automatically preserves message order, and no implementation element must violate this invariant.

#### Scenario: Session continuation after tool failure

- **WHEN** a tool call fails with an exception during invoke N
- **AND** the user sends a new message in the same session (invoke N+1 with the same `thread_id`)
- **THEN** the restored state from the checkpointer contains a `ToolMessage` for the failed call's `tool_call_id`
- **AND** the `ToolMessage` remains at the same position in the history as when it was originally created
- **AND** GigaChat does not return 422

#### Scenario: Session continuation after iteration cap

- **WHEN** the orchestrator iteration cap was reached in invoke N, and `force_execute_remaining` closed all remaining `tool_call_id`s
- **AND** the user sends a new message in the same session (invoke N+1)
- **THEN** the restored state contains `ToolMessage` stubs for all capped `tool_call_id`s
- **AND** all `ToolMessage` stubs are in the correct order relative to the originating `AIMessage`
- **AND** GigaChat does not return 422

### Requirement: synthesizer must handle error ToolMessage content gracefully

The synthesizer MUST be able to produce a coherent final answer when some `ToolMessage` entries in history contain error descriptions. The synthesizer prompt already includes `tool_results` text, so error entries MUST be distinguishable from successful results.

#### Scenario: Synthesizer receives error tool_results

- **WHEN** `tool_results` in state contains entries with `status="error"` (set by the error-handling `tool_executor`)
- **THEN** `_format_tool_results` includes the error status in the formatted text
- **AND** the synthesizer prompt contains this error information
- **AND** the synthesizer produces a natural-language answer acknowledging the errors or working around them
