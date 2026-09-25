# Redesign `main_graph.py`: Orchestrator + native tool-calling + multi-turn memory

## Context

Today's graph is a rigid binary fork: `classifier` forces every query down exactly one of two
paths (`analyze_model_inputs_for_target` / `analyze_excel_model`), even when the question needs no
calculation at all (a greeting, a follow-up about a number already given) or, in principle, needs
both tools. `classifier` also has no way to say "no tool needed." Separately, every `/invoke-agent`
call is single-turn — the endpoint sends only the current message, the graph is compiled with no
checkpointer, and nothing but the uploaded filename survives between requests. Users can't ask
follow-ups referencing an earlier answer without restating the whole question.

This redesign:
1. Replaces `classifier` with an **Orchestrator** node that uses real LLM tool-calling
   (`bind_tools`) to decide — per turn, with no hardcoded routing rules — whether to call IFT,
   EMA, both, or neither, looping (ReAct-style) until it's done.
2. Wraps the two existing subagent flows as tools the Orchestrator can call, **without modifying
   `subagents/analyzer.py`, `tools.py`, or `services.py`** — those stay exactly as they are.
3. Adds a **Synthesizer** node (replaces `reviewer`) that always produces the final answer, using
   the user's question, chat history, and whatever tool results (zero, one, or two) came out of
   this turn.
4. Wires up **multi-turn persistence** via the checkpointer that already exists
   (`APP_CTX.agent_memory.checkpointer`, a `MemorySaver`) but is currently only used by the unused
   legacy `services.py` graph — so users can reference earlier Q&A in the same session.

## Coding conventions for this change

- Keep every new/rewritten method small and single-responsibility — e.g. `tool_executor` should
  delegate dispatch-per-call to a small helper rather than growing one large branching method; the
  Orchestrator's prompt-building, LLM call, and result-parsing should stay separable steps.
- Type-annotate with builtin generics (`dict[str, str]`, `list[int]`, `tuple[str, ...]`, `X | None`),
  not `typing.Dict`/`typing.List`/`typing.Optional` — consistent with Python 3.12+ already used
  elsewhere in the codebase.

## Confirmed design decisions

- **Orchestrator decision mechanism: native `bind_tools`**, not a structured-output enum. No
  deterministic if/else routing rules inside the Orchestrator — the LLM alone decides whether/which
  tool(s) to call, based on tool descriptions + conversation context.
- **Multi-turn memory: in scope now.** Wire up the existing `MemorySaver` checkpointer so prior
  turns (including prior tool calls/results) are visible to the Orchestrator and Synthesizer on
  later requests in the same session.
- **Do not touch `services.py` or `tools.py`.** Both are imported from, never edited. `services.py`
  stays legacy/unused as today; `tools.py`'s `analyze_model_inputs_for_target` /
  `analyze_excel_model` calculation functions are called exactly as they are now.
- **`subagents/analyzer.py` (`AnalyzerSubAgent`) is not changed.** IFT/EMA name-resolution logic is
  reused as-is, just invoked from a new call site.
- **Classifier is removed as dead code**: `classify_user_query`, `ClassifierOutput`, the classifier
  prompt, and `dummy_classifier` go away, with `tests/subagents/test_dummies.py` updated to drop the
  classifier-specific assertions (see Files section). This follows the repo's stated preference
  against leaving unused code around.

## New graph shape

```
START -> extract_excel_data -> orchestrator -> (has tool_calls?)
                                    ^                 |-- yes --> tool_executor --\
                                    |                                              |
                                    \----------------------------------------------/
                                    |
                                    |-- no --> synthesizer -> END
```

- `extract_excel_data`: unchanged responsibility (loads filename from the upload store, builds the
  input/output catalog via `_build_catalog_with_ids`) **plus** resets the per-turn tracking fields
  described below, since those are plain (non-reducer) state keys that a checkpointer would
  otherwise carry over stale from the previous turn.
- `orchestrator`: LLM call with `self.llm.bind_tools([...])`, given the full persisted message
  history plus the new turn's `HumanMessage`. Loops back to itself after each `tool_executor` pass
  until it returns an `AIMessage` with no `tool_calls`.
- `tool_executor`: dispatches each `tool_call` in the last `AIMessage` to the matching wrapper (see
  below), appends one `ToolMessage` per call, accumulates results for the Synthesizer, then routes
  back to `orchestrator`.
- `synthesizer`: always runs last. Builds the final Russian-language answer from the user's
  question, full chat history (already in `messages` via the checkpointer), and this turn's
  `tool_results` (0, 1, or 2 entries) — generalizes today's `reviewer` node, which assumed exactly
  one calculation happened.

A hard safety cap (propose **4 orchestrator iterations per turn**) forces a route to `synthesizer`
regardless of the LLM's decision, to bound worst-case latency/cost — this is a loop-safety guard,
not a routing rule, so it doesn't conflict with "no deterministic rules in the Orchestrator."

## Tool wrapping (new file: `subagents/orchestrator_tools.py`)

- Two `@tool`-decorated functions named exactly `analyze_model_inputs_for_target` and
  `analyze_excel_model` (matching the existing calculation function names, which
  `scripts/run_tool_queries.py` and `tests/data/Methanex_tool_test_queries.xlsx` key off of), each
  taking a **single argument**:
  ```python
  class ToolQueryArgs(BaseModel):
      reformulated_query: str = Field(
          description="Self-contained restatement of exactly what to compute, folding in any "
                      "still-relevant details from earlier in the conversation (target values, "
                      "output names, parameter names/ranges, years) so it is fully understandable "
                      "on its own, without the rest of the chat history."
      )
  ```
  The Orchestrator LLM fills `reformulated_query` itself as part of making the tool call — this is
  standard native function-calling behavior, not an extra deterministic step. `ORCHESTRATOR_PROMPT`
  must instruct it to always produce a complete, standalone request here.
- Alongside them, two **dispatch functions** (not the `@tool` objects themselves, and not visible to
  the LLM) that `tool_executor` calls directly:
  ```
  run_ift(reformulated_query, available_inputs, available_outputs, filename, user_id) -> (ToolMessage, result)
  run_ema(reformulated_query, available_inputs, available_outputs, filename, user_id) -> (ToolMessage, result)
  ```
  Only `reformulated_query` comes from the LLM's tool call (via `ToolQueryArgs`); `available_inputs`,
  `available_outputs`, `filename`, and `user_id` are pulled straight from graph state by
  `tool_executor` and passed in directly — none of them are part of the tool schema the LLM sees.
  `user_id` still has to reach `tools.py`'s calc functions exactly as it does today (it's a cache-key
  component and a fallback store lookup via `get_store_file(user_id)` inside `tools.py`, which is
  untouched), so the dispatch functions forward it the same way `main_graph.py` currently does.
  Each builds `messages=[HumanMessage(content=reformulated_query)]` — a single synthetic message,
  **not** the full chat history — and calls `AnalyzerSubAgent.invoke(messages, available_inputs,
  available_outputs, user_id)` exactly as today, then the real calc function from `tools.py`
  (`analyze_model_inputs_for_target`/`analyze_excel_model`, imported unchanged), and builds a
  `ToolMessage(content=<summary>, tool_call_id=..., name=...)`. This is what keeps `analyzer.py`
  completely untouched while ensuring IFT/EMA's own LLM calls (`anlyze_query`, `lookup_more`) only
  ever see one comprehensive request instead of the whole — potentially long, multi-turn — history.
- `tool_executor` logs `"TOOL ARGS: {tool_name} | {args}"` for each dispatched call — this format is
  expected by `scripts/run_tool_queries.py` but is **currently only emitted by the unused
  `services.py` path**, so the live graph's quality-eval tooling is effectively broken today; this
  redesign is a natural point to fix that.
- Include a lightweight dedup guard (skip a tool call whose `(name, args)` fingerprint was already
  executed this turn, append an explanatory `ToolMessage` instead) as a safety net against the LLM
  repeating an identical call — mirroring the idea already proven in `services.py`'s
  `execute_tool`, reimplemented fresh here (not by importing from `services.py`).

## State changes (in `main_graph.py`)

- Drop `classification_result`, `ift_*`/`ema_*` singular result fields.
- Add, reset every turn by `extract_excel_data`:
  - `tool_call_count: int`
  - `executed_tool_fingerprints: list[str]`
  - `tool_results: list[...]` — one entry per tool call this turn, each carrying the tool name and
    its `ModelInputAnalysisToolResult`/`ExcelAnalysisToolResult` (both from `tools.py`, unchanged),
    consumed by `synthesizer`.
- `messages` keeps its `add_messages` reducer — this is what makes multi-turn history work once a
  checkpointer is attached: each new request only needs to add the new `HumanMessage`, and the
  checkpointer restores everything else for that `thread_id`.

## Multi-turn persistence

- Attach the existing checkpointer at compile time in `AgentGraph._build_graph()`:
  `workflow.compile(checkpointer=APP_CTX.agent_memory.checkpointer)` — same object
  `services.py` already uses (`APP_CTX.agent_memory.checkpointer`, a `MemorySaver`), just wired into
  the graph that's actually live. `AgentGraph` is already a process-lifetime singleton
  (`@lru_cache(maxsize=1)` in `router.py:get_agent`), so this is safe.
- **Bug fix required in `router.py`, and it must account for multi-user isolation**:
  `thread_id = headers.get("x-session-id")` currently falls back to the literal empty string `""`
  when the header is absent, so every session-less request would share one giant thread once a
  checkpointer is attached. But `MemorySaver` partitions purely by `thread_id` — it has no notion of
  `user_id` — so fixing the empty-string fallback alone isn't enough: two *different* users who
  happen to send the same `x-session-id` would still have their conversations merged. Since
  `x-user-id` is a required, validated header on every request, compose the LangGraph `thread_id` as
  `f"{user_id}:{session_id or uuid4()}"` in `router.py`, so isolation holds regardless of what
  session id (or lack of one) any given client sends.
- No `CopilotAgentRequest`/`schemas_file.py` changes needed — the checkpointer, not the request
  body, is what carries history; the endpoint keeps sending just the current message.
- Note the limitation (not a regression — matches today's upload-store behavior): `MemorySaver` is
  in-process only, so conversation memory is lost on `docker compose restart`. No durable/Pangolin
  checkpointer exists in the codebase today; adding one is out of scope here.
- Orchestrator/Synthesizer prompts should explicitly instruct: if the current question can already
  be answered from earlier tool results visible in chat history, don't re-call the tool — answer
  from that context instead.
- **Multi-user isolation, unaffected/confirmed by this redesign**: uploaded files are already
  isolated per `user_id` (`/tmp/user_file_{x-user-id}.{ext}` + store namespace `("memories",
  user_id)`), untouched here. `AgentGraph` is a single shared singleton, but holds no per-request
  mutable state, so concurrent requests from different users don't interfere structurally; each
  IFT/EMA tool call also spawns its own throwaway LibreOffice process per `lo_backend.py`, so
  concurrent calculations don't contend either. The only new cross-user surface this redesign
  introduces is the checkpointer key, addressed by the composite `thread_id` above.

## Prompts (new files)

- `prompts/orchestrator.py` (`ORCHESTRATOR_PROMPT`): domain framing + the two tools' selection
  criteria (repurposing the distinguishing examples from today's classifier prompt: target-seeking
  language → IFT, range/scenario-sweep language → EMA), explicit "no tool needed" guidance
  (greetings, off-topic, already-answered-in-history), explicit reuse-prior-answers instruction, and
  an explicit instruction on how to fill `reformulated_query` when it does call a tool: restate the
  request so it stands alone (target/output names, parameters, ranges, years), since the tool will
  never see the rest of the conversation — only this one string.
- `prompts/synthesizer.py` (`SYNTHESIZER_PROMPT`): generalizes `prompts/reviewer.py` to handle 0–2
  `tool_results` entries instead of exactly 1, keeps the existing OK/ERROR/INFO status branching per
  result, adds continuity instructions for when it must answer purely from chat history.

## Files touched

**New:**
- `src/aigw_service/api/v1/subagents/orchestrator_tools.py`
- `src/aigw_service/api/v1/prompts/orchestrator.py`
- `src/aigw_service/api/v1/prompts/synthesizer.py`

**Modified:**
- `src/aigw_service/api/v1/main_graph.py` — new `AgentState`/`AgentInput`, new nodes
  (`orchestrator`, `tool_executor`, `synthesizer`), new edges/conditional routing, checkpointer at
  compile time. `classifier`/`analyze_model_inputs_for_target`/`analyze_excel_model`/`reviewer`
  node methods removed (logic moves into `orchestrator_tools.py` + `synthesizer`).
- `src/aigw_service/api/v1/router.py` — `thread_id` fallback fix (`uuid4()` instead of `""`).

**Removed (dead code cleanup):**
- `src/aigw_service/api/v1/subagents/classifier.py`
- `src/aigw_service/api/v1/prompts/classifier.py`
- `src/aigw_service/api/v1/prompts/reviewer.py` (superseded by `synthesizer.py`)
- `ClassifierOutput` + `dummy_classifier` from `schemas/llm_outputs.py` /
  `subagents/dummies.py`, and their assertions in `tests/subagents/test_dummies.py`.

**Unchanged (explicitly, per constraints):**
- `src/aigw_service/api/v1/subagents/analyzer.py`, `subagents/utils.py`
- `src/aigw_service/api/v1/tools.py`
- `src/aigw_service/api/v1/services.py`
- `src/aigw_service/api/v1/prompts/ema_analizer.py`, `prompts/ift_analizer.py`, `prompts/lookup_more.py`
- `src/aigw_service/api/v1/states/agents.py`
- `src/aigw_modules/ai_agents/memory.py`

## Verification

1. `ruff check --fix src && ruff format src`, `pylint src` (>7) per CLAUDE.md.
2. `pytest tests/unit/` — confirm nothing else regresses; update `test_dummies.py` for the removed
   classifier pieces.
3. `python scripts/new_graph_test.py` (per `scripts/TESTING.md`) to exercise `AgentGraph` directly:
   - a query needing only IFT, one needing only EMA, one needing neither (e.g. a greeting), and one
     crafted to plausibly need both — confirm the Orchestrator loop terminates correctly in each
     case and `tool_results` has the right count.
   - a two-turn exchange where turn 1 establishes a parameter/filename/target and turn 2 refers back
     to it elliptically ("а если на 10% больше?") — inspect the logged `reformulated_query` to
     confirm the Orchestrator folded turn 1's context in, and that `analyzer.py` still only ever saw
     that one synthetic message.
4. Start the app (`python3 src/aigw_service/__main__.py 2>&1 | tee server.log` or
   `docker compose up -d`) and:
   - `POST /upload` then two sequential `POST /invoke-agent` calls **with the same `x-session-id`**,
     second one referencing "the previous answer" — confirm the Synthesizer answers from history
     without recomputation.
   - Two `/invoke-agent` calls **without `x-session-id`** — confirm they do *not* see each other's
     history (thread_id fallback fix working).
   - Two `/invoke-agent` calls from **different `x-user-id` values but the same `x-session-id`** —
     confirm they do *not* see each other's history either (composite thread_id key working).
   - `python scripts/run_tool_queries.py --subset 5 --url http://localhost:8080 --log server.log`
     then `python scripts/analyze_results.py test_output/tool_query_results.json` — confirm
     `TOOL ARGS` lines are now present and parse correctly for the live graph.
5. Manually sanity-check that `bind_tools()`-wrapped calls still route through
   `context.py`'s `_wrap_llm_with_stop_event` monkey-patch (GigaChat `ForbiddenError` → `StopEventError`
   still triggers) — this patches the instance's `invoke`/`ainvoke`, which `RunnableBinding` from
   `bind_tools()` should still delegate to, but worth a direct check given it's untested with tool
   calls in this codebase.
