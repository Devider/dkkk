# Spec Delta

## Purpose

Isolates parallel requests to the same agent `thread_id`: while one run is in flight, a new request for that thread supersedes it and starts from the last successfully completed state, keeping the LLM conversation history consistent.

## ADDED Requirements

### Requirement: The latest request for a thread always wins (run_latest_wins)

When a new request arrives for a `thread_id` that already has an active run in flight, the system SHALL cancel the active run and run the new one in its place. The cancellation MUST NOT surface a failure to the submitting client: a superseded run is only logged and MUST NOT send any response to its client — not a success payload, not an HTTP error.

#### Scenario: A new request arrives while an active run is in flight

- **WHEN** a request A for a `thread_id` is in flight and the user sends request B for the same `thread_id`
- **THEN** request B cancels request A
- **AND** the client that initiated request B receives the result of request B
- **AND** the cancellation of request A is logged at WARNING level

#### Scenario: A superseded run sends nothing to its client

- **WHEN** run A is cancelled because a newer request superseded it
- **THEN** the request of run A is aborted without producing any HTTP response (no success payload, no HTTP 409, no HTTP 424)
- **AND** the only observable effect of A's cancellation on the server side is the log entry
- **AND** the client receives a response only from the winning run B

### Requirement: A new run starts from the last successfully completed state

Each run for a `thread_id` SHALL execute in isolation from concurrent runs: it MUST be based on the checkpoint of the last successfully completed run of that thread (HEAD), not on shared mutable state that other runs could write concurrently. While a run is in flight, its intermediate checkpoints MUST NOT become the base for another run of the same thread.

#### Scenario: Intermediate checkpoints of an active run are isolated

- **WHEN** request A for a `thread_id` creates intermediate checkpoints c1, c2, c3 (the run is still in flight)
- **AND** request B arrives for the same `thread_id`, cancelling A
- **THEN** the base state for request B MUST NOT include c1/c2/c3
- **AND** HEAD stays at the last successfully completed run's value (not c1/c2/c3 from A)

#### Scenario: A cancelled run's mid-turn checkpoints never become HEAD

- **WHEN** request A is cancelled while its turn is in progress (for example during a tool call)
- **AND** LangGraph has already persisted checkpoints for A's in-progress turn
- **THEN** none of those checkpoints is recorded as HEAD: a cancelled run stops in a checkpoint whose pending nodes are never executed, so the run never reaches a stopping checkpoint of its own
- **AND** HEAD stays at the checkpoint the last successfully completed run ended on
- **AND** the next run for the thread forks from that HEAD, so it does not resume A's unfinished turn

#### Scenario: A run after cancellation starts from the previous success

- **WHEN** request A was cancelled by request B, and then request C arrives for the same `thread_id`
- **THEN** request C starts from HEAD = the last successfully completed run
- **AND** request C does not see or inherit the intermediate checkpoints of the cancelled run A in its input state

### Requirement: HEAD is only updated on successful run completion

The system SHALL keep a HEAD pointer, per `thread_id`, to the checkpoint the last successfully completed run ended on. HEAD MUST NOT be updated to a checkpoint of a non-completed or cancelled run.

#### Scenario: Successful completion updates HEAD

- **WHEN** a run for a `thread_id` completes successfully to the end
- **THEN** the system records the checkpoint that run ended on as the new HEAD for that `thread_id`
- **AND** subsequent runs start from that HEAD

#### Scenario: Cancellation does not change HEAD

- **WHEN** run A for a `thread_id` is cancelled (CancelledError) before completion
- **THEN** HEAD for the `thread_id` is NOT updated and stays at the last successfully completed checkpoint

### Requirement: Multi-turn history is preserved between successful runs

The LLM message-history consistency SHALL hold within a multi-turn dialogue on a single `thread_id`. Each successful run SHALL continue the history from the last successfully completed run, not from an interrupted or concurrent state.

#### Scenario: Continuation after cancellation

- **WHEN** run A for a `thread_id` was cancelled and then run C completes successfully
- **THEN** the message history C continues is the one the last successfully completed run before A ended with, plus C's own turn
- **AND** the cancelled run A is not recorded in the history as a completed exchange: no error message of the cancelled tool call is persisted as A's answer
- **AND** the checkpoint C commits does not introduce a new assistant turn with tool calls left without results

## MODIFIED Requirements

### Requirement: tool_executor must produce a ToolMessage for every dispatched call

<!-- capability: robust-tool-execution -->
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
