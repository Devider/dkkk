# Spec Delta

## Purpose

Гарантирует, что каждый tool_call_id из AIMessage оркестратора всегда имеет соответствующий ToolMessage в истории, сохраняемой checkpointer, даже при внутренних ошибках инструментов или досрочном завершении цикла оркестратора.

## ADDED Requirements

### Requirement: tool_executor must produce a ToolMessage for every dispatched call

The system MUST wrap every call to `OrchestratorTools.dispatch()` inside `tool_executor` with exception handling that catches any raised exception and produces a `ToolMessage` with an error description. The `tool_call_id` of the failed call MUST be preserved in the returned `ToolMessage`.

#### Scenario: Tool dispatch raises an exception

- **WHEN** `tool_executor._execute_single_call` calls `OrchestratorTools.dispatch()` and it raises any exception (e.g., HTTP 424, timeout, parsing error)
- **THEN** `_execute_single_call` catches the exception and returns a `ToolMessage(content="<error_description>", tool_call_id=<call["id"]>, name=<call["name"]>)`
- **AND** the exception is logged at ERROR level
- **AND** no exception propagates to the LangGraph node

#### Scenario: Tool dispatch raises asyncio.CancelledError

- **WHEN** `OrchestratorTools.dispatch()` raises `asyncio.CancelledError` during execution
- **THEN** `_execute_single_call` catches the exception and returns a `ToolMessage(content="Запрос был прерван: <reason>", tool_call_id=<call["id"]>, name=<call["name"]>)`
- **AND** the exception is logged at ERROR level

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

#### Scenario: Session continuation after tool failure

- **WHEN** a tool call fails with an exception during invoke N
- **AND** the user sends a new message in the same session (invoke N+1 with the same `thread_id`)
- **THEN** the restored state from the checkpointer contains a `ToolMessage` for the failed call's `tool_call_id`
- **AND** GigaChat does not return 422

#### Scenario: Session continuation after iteration cap

- **WHEN** the orchestrator iteration cap was reached in invoke N, and `force_execute_remaining` closed all remaining `tool_call_id`s
- **AND** the user sends a new message in the same session (invoke N+1)
- **THEN** the restored state contains `ToolMessage` stubs for all capped `tool_call_id`s
- **AND** GigaChat does not return 422

### Requirement: synthesizer must handle error ToolMessage content gracefully

The synthesizer MUST be able to produce a coherent final answer when some `ToolMessage` entries in history contain error descriptions. The synthesizer prompt already includes `tool_results` text, so error entries MUST be distinguishable from successful results.

#### Scenario: Synthesizer receives error tool_results

- **WHEN** `tool_results` in state contains entries with `status="error"` (set by the error-handling `tool_executor`)
- **THEN** `_format_tool_results` includes the error status in the formatted text
- **AND** the synthesizer prompt contains this error information
- **AND** the synthesizer produces a natural-language answer acknowledging the errors or working around them
