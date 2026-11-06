# Tasks

## 1. Add exception handling to `_execute_single_call` in `main_graph.py`

- [x] 1.1 Wrap `self.orchestrator_tools.dispatch()` call in `_execute_single_call` with `try/except (Exception | asyncio.CancelledError)` and verify the modified method compiles (`ruff check src/aigw_service/api/v1/main_graph.py`)
- [x] 1.2 In the `except` block, log the exception at ERROR level via `logger.exception` and return `ToolMessage(content=f"Ошибка выполнения: {reason}", tool_call_id=call["id"], name=call["name"])` and verify by reading the method source
- [x] 1.3 Write a unit test that patches `OrchestratorTools.dispatch` to raise `Exception("test error")`, calls `_execute_single_call`, and asserts it returns a `ToolMessage` with the expected error content and `tool_call_id` (verify test passes: `pytest tests/unit/test_tool_executor_error_handling.py -v`)
- [x] 1.4 Write a unit test that patches `OrchestratorTools.dispatch` to raise `asyncio.CancelledError("cancelled")` and asserts the same `ToolMessage` behavior with a `CancelledError`-specific message (verify test passes)

## 2. Add `force_execute_remaining` node to the graph

- [x] 2.1 Add a new method `force_execute_remaining(self, state: AgentState) -> AgentState` to `AgentGraph` in `main_graph.py` that iterates over unexecuted `tool_calls` from the last `AIMessage` and returns `{"messages": [ToolMessage(...)]}` for each one (verify the method exists and has correct signature by reading the source)
- [x] 2.2 A `ToolMessage` stub content should state the iteration cap was reached and name the tool that was not executed (e.g., `"Достигнут лимит итераций оркестратора. Инструмент 'analyze_model_inputs_for_target' не выполнен."`) — verify by reading the method source
- [x] 2.3 Add the new node to `_build_graph` via `workflow.add_node("force_execute_remaining", self.force_execute_remaining)` (verify node registration by reading `_build_graph`)
- [x] 2.4 Add `workflow.add_edge("force_execute_remaining", "synthesizer")` so the cap path always ends at synthesizer (verify edge exists by reading `_build_graph`)

## 3. Update `route_after_orchestrator` to route cap cases to the new node

- [x] 3.1 Modify `route_after_orchestrator` to return `"force_execute_remaining"` instead of `"synthesizer"` when `tool_call_count >= MAX_ORCHESTRATOR_ITERATIONS` (verify the condition and return value by reading the method source)
- [x] 3.2 Add `"force_execute_remaining"` to the conditional edges dict in `_build_graph` — the call to `add_conditional_edges("orchestrator", ..., {"tool_executor": ..., "synthesizer": ...})` needs the third argument to include `"force_execute_remaining": "force_execute_remaining"` (verify the dict contains the new key by reading `_build_graph`)

## 4. Update `_format_tool_results` to distinguish error results

- [x] 4.1 Verify that `_format_tool_results` already uses `r["status"]` in the formatted output (`"СТАТУС: {r['status']}"`) and that when `tool_executor` sets `status="error"` on failed calls, the synthesizer receives the error information (verify by reading `_format_tool_results` source)
- [x] 4.2 If `_execute_single_call` exception handler does NOT add to `tool_results` (since it returns a `ToolMessage` but `tool_results.append(result)` is in the try block), add `tool_results.append({"task_description": ..., "status": "error", "result_text": error_content})` in the except block (verify by reading the full `tool_executor` method — check whether `tool_results` is appended before or after `dispatch()`)

**Note on 4.2:** In the current code, `tool_results.append(result)` is called AFTER `dispatch()` returns successfully. In the exception path, we need to also append an error entry. Add this in the except block.

- [x] 4.3 Verify the error entry appended in the except block has the same dict shape as the success entry: `{"task_description": ..., "status": "error", "result_text": error_content}` (verify by reading the full `tool_executor` method)

## 5. Unit tests for `force_execute_remaining` node

- [x] 5.1 Write a unit test that creates a mock `AgentState` with `tool_call_count=4`, last `AIMessage` containing 2 `tool_calls` with different `tool_call_id`, calls `agent_graph.force_execute_remaining(state)`, and asserts the returned `messages` list contains exactly 2 `ToolMessage` objects with correct `tool_call_id` and `name` matching the unexecuted calls (verify test passes: `pytest tests/unit/test_force_execute_remaining.py -v`)
- [x] 5.2 Write a unit test with state where last `AIMessage` has `tool_calls=None` (empty list), calls `force_execute_remaining`, and asserts it returns `{"messages": []}` (no-op, verify test passes)
- [x] 5.3 Write a unit test with state where last `AIMessage` has 1 pending `tool_call`, calls `force_execute_remaining`, and asserts the `ToolMessage.content` includes the iteration cap message and the tool name (verify test passes)

## 6. Unit tests for `route_after_orchestrator` routing logic

- [x] 6.1 Write a unit test that creates state with `messages=[AIMessage(tool_calls=[...])]` and `tool_call_count=3`, calls `route_after_orchestrator`, and asserts it returns `"tool_executor"` (below cap, verify test passes)
- [x] 6.2 Write a unit test with `tool_call_count=4` (at cap) and `tool_calls` present, calls `route_after_orchestrator`, and asserts it returns `"force_execute_remaining"` (verify test passes)
- [x] 6.3 Write a unit test with `messages=[AIMessage(tool_calls=None, content="answer")]`, calls `route_after_orchestrator`, and asserts it returns `"synthesizer"` (no tool calls, verify test passes)
- [x] 6.4 Write a unit test with `tool_call_count=4` and `tool_calls=None`, calls `route_after_orchestrator`, and asserts it returns `"force_execute_remaining"` (cap reached but no pending tool_calls — node handles no-op, verify test passes)

## 7. Unit tests for `tool_executor` error path integration

- [x] 7.1 Write a unit test that calls `tool_executor` with a state where `_execute_single_call` encounters an error (patch `dispatch` to raise), and asserts the returned `messages` contains `ToolMessage` for ALL calls (not just successful ones), and `tool_results` contains an error entry (verify test passes)
- [x] 7.2 Write a unit test that calls `tool_executor` with a state where one of two `tool_calls` succeeds and the other fails, and asserts the returned `messages` contains 2 `ToolMessage`s (one with success content, one with error content), and `tool_results` has both success and error entries (verify test passes)
- [x] 7.3 Write a unit test that calls `tool_executor` with deduplicated fingerprints (existing behavior), and asserts the returned `ToolMessage` still works correctly — regression test for dedup (verify test passes)
- [x] 7.4 Write a unit test for `_execute_single_call` where `dispatch` raises `RuntimeError("subagent timeout")`, and asserts `logger.exception` is called (use `unittest.mock.patch.object(logger, "exception")` and verify the exception was logged, verify test passes)

## 8. End-to-end tests for session continuity (GigaChat 422 constraint)

- [x] 8.1 Write an e2e test: mock `OrchestratorTools.dispatch` to raise `Exception("HTTP 424")` on the second tool call, invoke the compiled graph once with a `thread_id`, then invoke again with the same `thread_id` (simulating session continuation), and assert that the second invocation does NOT raise an exception and returns a valid response (this proves the history constraint is satisfied, verify test passes)
- [x] 8.2 Write an e2e test: manually set up checkpoint state with `tool_call_count=4`, last `AIMessage` containing unexecuted `tool_calls`, invoke the graph, and assert it routes through `force_execute_remaining → synthesizer` and returns a final answer without 422 (verify test passes)
- [x] 8.3 Write an e2e test: invoke the graph twice with the same `thread_id`, first call has a tool failure (dispatch raises), second call is a normal user question — verify the second call's response is a coherent text answer (not a 422), and the `AIMessage` from the first call has a matching `ToolMessage` in the restored state (verify test passes)

## 9. Integration-level verification and regression testing

- [x] 9.1 Run the existing integration test `tests/test_agent.py` and verify it passes (command: `pytest tests/test_agent.py -v --timeout=60`), confirming normal orchestrator→tool_executor→synthesizer flow is unchanged
- [x] 9.2 Run `ruff check src/ && ruff format --check src/` and verify no lint/format issues (command: `ruff check src/aigw_service/api/v1/main_graph.py && ruff format --check src/aigw_service/api/v1/main_graph.py`)
- [x] 9.3 Run all existing unit tests (`pytest tests/unit/ -v`) and verify they pass (command: `pytest tests/unit/ -v`)
- [x] 9.4 Verify the compiled graph loads without errors by importing `AgentGraph` and accessing `agent_graph.graph` (command: `python -c "from aigw_service.api.v1.main_graph import AgentGraph; g = AgentGraph(); assert g.graph is not None"`), confirming the new node and edges compile correctly
