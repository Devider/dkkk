# Orchestrator redesign — implementation report

Companion to [`orchestrator_redesign_plan.md`](./orchestrator_redesign_plan.md) (the approved plan).
This document records what was actually built, the bugs found and fixed along the way, and what
was verified before calling it done.

## Summary

Replaced the rigid binary `classifier -> (IFT | EMA) -> reviewer` graph in
`src/aigw_service/api/v1/main_graph.py` with an LLM-driven ReAct loop:

```
START -> extract_excel_data -> orchestrator -> (has tool_calls?)
                                    ^                 |-- yes --> tool_executor --\
                                    |                                             |
                                    \---------------------------------------------/
                                    |
                                    |-- no --> synthesizer -> END
```

- **Orchestrator** uses native LLM tool-calling (`llm.bind_tools(...)`) to decide — per turn, with
  no hardcoded routing rules — whether to call `analyze_model_inputs_for_target`,
  `analyze_excel_model`, both, or neither.
- **`tool_executor`** dispatches each tool call to `OrchestratorTools`, which wraps the existing
  `AnalyzerSubAgent` flows and the calculation functions in `tools.py` — neither was modified.
- **Synthesizer** always produces the final answer, from the user's question, chat history, and
  this turn's tool results (0, 1, or 2 of them).
- **Multi-turn memory**: the graph is now compiled with the checkpointer that already existed
  (`APP_CTX.agent_memory.checkpointer`, a `MemorySaver`) but was previously only wired into the
  unused legacy `services.py` graph. Conversations persist across `/invoke-agent` calls that share
  a session, so follow-up questions can reference earlier answers without repeating context.
- The classifier and its dead code were removed once the Orchestrator took over routing.

## Files changed

**New**
- `src/aigw_service/api/v1/subagents/orchestrator_tools.py` — `OrchestratorTools`: wraps IFT/EMA as
  tools taking a single `reformulated_query` argument (filled by the Orchestrator LLM itself, since
  the subagents never see full chat history — only that one self-contained request), dispatches to
  the unchanged `AnalyzerSubAgent` + `tools.py` functions, and sanitizes results for the
  checkpointer (see bug #5 below).
- `src/aigw_service/api/v1/prompts/orchestrator.py` — `ORCHESTRATOR_PROMPT`.
- `src/aigw_service/api/v1/prompts/synthesizer.py` — `SYNTHESIZER_PROMPT`.

**Modified**
- `src/aigw_service/api/v1/main_graph.py` — full rewrite: new `AgentState`/`AgentInput`, the four
  nodes above, checkpointer attached at compile time.
- `src/aigw_service/api/v1/router.py` — `thread_id` now composed as `f"{user_id}:{session_id or
  uuid4()}"` (see bug #2 below); fixed a logging typo (bug #1).
- `src/aigw_service/api/v1/schemas/llm_outputs.py` — removed `ClassifierOutput`.
- `src/aigw_service/api/v1/subagents/dummies.py` — removed `dummy_classifier`; fixed a latent
  double-tuple bug in the generic dummy fallback (bug #4 below).
- `tests/subagents/test_dummies.py` — dropped classifier-specific tests; generic
  `validate_structured_output`/`retry_structured_llm` coverage now uses a local test-only schema
  instead of the removed `ClassifierOutput`, plus a new test for the fallback path that caught bug
  #4.
- `Dockerfile` — fixed the `docker-entrypoint.sh` path (bug #3 below).

**Removed (dead code)**
- `src/aigw_service/api/v1/subagents/classifier.py`
- `src/aigw_service/api/v1/prompts/classifier.py`
- `src/aigw_service/api/v1/prompts/reviewer.py` (superseded by `synthesizer.py`)

**Explicitly unchanged**, per the plan's constraints: `subagents/analyzer.py`, `subagents/utils.py`,
`tools.py`, `services.py`, `states/agents.py`, `prompts/ema_analizer.py`, `prompts/ift_analizer.py`,
`prompts/lookup_more.py`.

## Bugs found and fixed

Several of these predate this change and were only surfaced by it (new test coverage, or the graph
actually running end-to-end against live GigaChat for the first time in this session). Each is
noted as pre-existing or newly introduced by the redesign.

1. **`router.py` logging typo (pre-existing)** — `logger.opt(exeption=True)` instead of
   `exception=True`, in both `/upload`'s and `/invoke-agent`'s exception handlers. This crashed the
   exception handler itself, turning every real error into an opaque raw 500 instead of the
   intended 424 JSON response — actively blocked diagnosing every other issue below until fixed.

2. **`router.py` thread-id isolation (pre-existing, newly load-bearing)** — `thread_id` fell back to
   the literal empty string `""` when `x-session-id` was absent, and was used as-is with no
   `user_id` component. Harmless while the graph had no checkpointer; once multi-turn persistence
   was wired in, this would have let unrelated users' or unrelated sessions' conversations merge.
   Fixed by composing `thread_id = f"{user_id}:{session_id or uuid4()}"`.

3. **`Dockerfile` broken build (pre-existing)** — `COPY docker-entrypoint.sh ./` referenced a path
   that doesn't exist at the repo root; the file actually lives at `scripts/docker-entrypoint.sh`.
   The Docker build failed before reaching any of this session's changes. One-line path fix.

4. **`dummies.py` nested-tuple bug in the generic dummy fallback (pre-existing)** —
   `retry_structured_llm`'s fallback for any schema not explicitly registered
   (`lambda: (schema(), False)`) returned a 2-tuple, which then got wrapped again by the caller's
   `return dummy_fn(), False, None`, producing a nested `((instance, False), False, None)` instead
   of `(instance, False, None)`. This affected `LookupResult` in production (used by
   `analyzer.py`'s `lookup_more` node, never registered in the dummy map) and was only caught
   because removing the dead `ClassifierOutput` entry required adding fresh test coverage for this
   fallback branch. Fixed by making the fallback `schema` (a bare callable) instead of a lambda
   returning a tuple.

5. **`orchestrator_tools.py` non-serializable checkpoint state (introduced by this change)** —
   `tools.py`'s `analyze_excel_model` embeds a raw `pandas.DataFrame` in its `content` dict
   alongside plain file-path strings. Irrelevant while the graph was stateless; broke the new
   `MemorySaver` checkpointer with `Type is not msgpack serializable: DataFrame`. Fixed by
   sanitizing `content` at the `OrchestratorTools` boundary — dropping any value that isn't
   JSON-serializable before it enters graph state (the DataFrame's data already lives on disk via
   the sibling `scenario_file` path, so nothing is lost).

6. **GigaChat tool-call bypass / hallucination (introduced by this change, model-behavior)** —
   GigaChat's `bind_tools` only supports `tool_choice="auto"` (freely skip), `"none"`, or a single
   hardcoded tool name — there is no "must call one of these, your choice" mode (confirmed by
   reading `langchain_gigachat.GigaChat.bind_tools`'s source). With a first-draft prompt, the
   Orchestrator responded to a query that unambiguously required `analyze_excel_model` by
   fabricating a complete, plausible-looking table of financial figures directly in its text
   response, without calling any tool. Mitigated (per explicit direction, without adding
   deterministic routing rules) by adding a forceful anti-hallucination instruction to
   `ORCHESTRATOR_PROMPT`: it has no access to real model data and must call a tool for any question
   requiring specific numbers, never invent or estimate them. Confirmed effective in live testing
   afterward — this is prompt-level mitigation, not a hard guarantee, and worth re-checking if
   GigaChat's behavior drifts.

7. **GigaChat degenerate completion on assistant-ending prompts (introduced by this change)** — once
   the Orchestrator correctly called a tool and then, on its next turn, decided no more tools were
   needed, its own text reply became the last message in state. The Synthesizer's prompt
   (`[SystemMessage, *messages]`) then ended on that assistant-role message. GigaChat responded to
   that shape with a degenerate completion — `{'content': ''}` with no `role` field at all — which
   crashes the strict `gigachat` SDK's Pydantic response validation
   (`ValidationError: choices.0.message.role Field required`). Every mid-loop call (which ends on a
   `ToolMessage`) worked fine; only prompts ending on an `AIMessage` triggered this. Fixed by having
   the Synthesizer append a synthetic trailing `HumanMessage` ("Сформулируй финальный ответ...")
   before the final LLM call — not persisted to state, only the actual answer is added to
   `messages`.

## Verification performed

- `ruff check --fix` / `ruff format`: clean on every file this change touched. (Ran once
  accidentally un-scoped across the whole `src` tree; it started auto-fixing pre-existing issues in
  `tools.py`/`services.py` and others — reverted immediately, since those two files are explicitly
  off-limits, and re-ran scoped to only the files this change actually touches.)
- `pylint`: 9.84–10.00/10 on every touched file (well above the >7 bar); all remaining warnings are
  on pre-existing lines this change didn't touch.
- `pytest tests/unit/ tests/subagents/`: 64 passed. The only failures are 6 pre-existing ones in
  `test_stop_event.py` (an unrelated `NameError: StopEventError is not defined` bug in `context.py`,
  never touched by this change) — confirmed pre-existing by stashing this session's changes and
  re-running against the original code.
- `AgentGraph()` construction: imports and compiles successfully with the checkpointer attached;
  node list confirmed as `extract_excel_data, orchestrator, tool_executor, synthesizer`.
- **Live end-to-end test** against the real GigaChat cloud API, via Docker
  (`docker compose up -d`, port 8080): uploaded `models/model.xlsx` and ran the first question from
  `scripts/run_qa.sh` (a scenario-analysis query needing `analyze_excel_model` with two input
  ranges and three output metrics). Confirmed via server logs that the Orchestrator correctly
  called the tool with a well-formed `reformulated_query`, the resolver correctly mapped free-text
  aliases to canonical input/output names, the real LibreOffice recalculation ran, and the final
  answer's numbers matched the tool's actual computed best/worst-case scenarios (not fabricated).
  Final response: `HTTP 200` with a complete, correctly-formatted answer.

## Known limitations / not done

- **Only Q1 of `scripts/run_qa.sh` was run live.** Q2–Q5 (including the IFT path,
  `analyze_model_inputs_for_target`) were not exercised end-to-end against the live server in this
  session.
- **`scripts/new_graph_test.py`** still reads a `classification_result` key from the graph's result
  that no longer exists in the new state shape. It's a standalone script, not pytest, so nothing
  broke silently — it just needs a small update to stay useful as a verification harness. Flagged
  earlier, deferred, not yet done.
- **In-memory checkpointer and store**: `APP_CTX.agent_memory.checkpointer` (`MemorySaver`) and
  `.store` (`InMemoryStore`) are both process-local. Conversation memory and the uploaded-file
  record are lost on container restart — a pre-existing limitation for the file store, now also
  true of conversation history. No durable/Pangolin-backed checkpointer exists in the codebase
  today; adding one was explicitly out of scope for this change.
- **Anti-hallucination fix is prompt-level, not enforced in code.** GigaChat's API has no
  "must-call-a-tool" mode, so the mitigation in bug #6 relies on the model following instructions.
  Worth periodic spot-checking, especially after any GigaChat model version change.
- A running Docker container was left up on port 8080 at the end of this session (`docker compose
  down` not run), with the diagnostic monkey-patch used mid-session to inspect raw GigaChat
  responses already gone (a clean rebuild replaced it before the final fixes were verified).
