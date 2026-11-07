# Proposal: Add a Deep Agent API v2

## Why

The service needs a separate API version backed by LangChain Deep Agents while reusing its existing Excel analysis tools. The agent needs client and thread identity for file lookup and checkpointing, but those values must come from trusted request context rather than model-generated tool arguments. The API also does not need Deep Agents' general filesystem and planning tools for this Excel-only workflow.

## What Changes

- Add `/api/v2/invoke-agent`, accepting a bounded text message and returning a ZIP containing a text response and an Excel workbook entry.
- Reuse the existing upload endpoint under `/api/v2/upload` without publishing it in the V2 OpenAPI schema.
- Construct and cache one Deep Agent per process, backed by GigaChat, the shared checkpointer, store, and the existing Excel tools.
- Build `thread_id` from the request's user and session IDs; pass both IDs to the agent in LangGraph's runtime config and attach tracing metadata/callbacks.
- Refactor identity-dependent Excel tools to obtain identity from `RunnableConfig`; keep identity out of the model-facing tool schemas. Update the V1 manual tool runner to pass the same runtime config.
- Register a GigaChat harness profile that excludes Deep Agents' standard tools from the model-visible tool set.
- Require `deepagents>=0.5.4`, which provides harness profiles, and lock the dependency graph.

## Capabilities

### New Capabilities

- `api-v2-deep-agent`: V2 request, session, invocation, tracing, and ZIP response contract.
- `tool-runtime-context`: trusted request identity propagation into tools through runtime config.
- `deepagent-tool-policy`: V2 GigaChat profile excluding built-in Deep Agents tools.

## Impact

- **API registration:** `src/aigw_service/api/__init__.py` mounts the V2 router at `/api/v2`.
- **New V2 implementation:** `src/aigw_service/api/v2/` contains the router, agent factory, prompt, and harness profile.
- **Shared tools:** `src/aigw_service/api/v1/tools.py` and its V1 runner in `services.py` change to use runtime config for identity.
- **Dependencies:** `pyproject.toml` and `poetry.lock` add Deep Agents and its resolved dependencies.
- **Compatibility:** V1 tool calls continue to receive their identity values, now through LangChain invocation config rather than tool arguments.
