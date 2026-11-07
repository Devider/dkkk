# Design

## Context

V2 is implemented as a separate FastAPI router and uses `create_deep_agent` with a GigaChat chat model. Its agent is cached per process. The request router gets validated common headers, uses `x-user-id` and `x-session-id` to form `thread_id = "{user_id}:{session_id}"`, and calls the agent with both identifiers under `configurable`. The model-facing tool schemas must contain business inputs only; user and thread identifiers are runtime context.

The existing tools are shared with V1. V1's manual tool executor previously inserted IDs into the tool argument dictionary. It now supplies them in the `config` argument to `tool.invoke`, matching V2's runtime mechanism.

## Decisions

### D1: Separate V2 router and agent factory

Mount a dedicated router at `/api/v2`. Keep the V2 agent factory in `api/v2/agent.py` and cache its result in `api/v2/router.py` with a one-entry LRU cache. Reuse the existing Excel tool collection and the existing upload handler.

### D2: Runtime config is the source of tool identity

The HTTP handler derives `user_id`, `session_id`, and the composite `thread_id`. It supplies `thread_id` and `user_id` to `agent.ainvoke` in `configurable`. Identity-dependent tools accept LangChain's injected `RunnableConfig` and read those values from `config["configurable"]`.

The IDs are removed from the corresponding Pydantic tool argument schemas. This prevents the model from being responsible for selecting a user or thread. The V1 tool runner uses the same config propagation so the shared tool change does not break V1.

### D3: Provider-wide GigaChat profile

Register a `HarnessProfile` under the provider key `giga`. The installed `langchain-gigachat` integration reports `ls_provider="giga"`; registering under `gigachat` does not match and leaves default tools visible. The profile excludes the standard filesystem, execution, planning, and subagent tool names. The profile is provider-wide, so it applies to Deep Agents using the `giga` provider in this process.

Exclusion is model-facing tool visibility and invocation filtering, not a security boundary. The underlying filesystem middleware remains part of Deep Agents' required harness structure.

### D4: Response shape and errors

On success, V2 returns a ZIP with `txt_response.txt` and `generated/agent_output.xlsx`. The workbook entry is currently an empty workbook placeholder. Agent invocation errors are logged and mapped to HTTP 424. The response filename includes the session ID.

If the request omits `x-session-id`, the server generates a UUID for that invocation. Clients that need to continue a conversation should send a stable session ID on subsequent requests.

### D5: Dependency floor

Set `deepagents>=0.5.4` because harness profiles are used by V2. Keep the lockfile resolved to the selected Deep Agents version and its transitive dependencies.

## Risks / Trade-offs

- A provider-wide `giga` profile affects any other Deep Agent using that provider in the same process. If the service later hosts multiple GigaChat agents with different tool policies, use model-specific profiles or an explicitly assembled agent stack.
- Tool exclusions depend on exact tool names. If Deep Agents adds or renames built-in tools, update the profile and verify the tool surface after dependency upgrades.
- Runtime config injection must be preserved by every invocation path that calls the shared tools. V1's manual runner is updated; other direct callers must supply config too.
- The generated workbook is only a placeholder in the current endpoint implementation; it is not the modified workbook from Excel tools.
