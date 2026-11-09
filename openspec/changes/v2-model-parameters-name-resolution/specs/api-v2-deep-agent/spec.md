# Spec Delta

## MODIFIED Requirements

### Requirement: V2 preserves a client's conversation thread
The service SHALL use the supplied `x-session-id` when present, otherwise generate a UUID for the invocation. It SHALL form the LangGraph `thread_id` as `"{user_id}:{session_id}"` and pass `thread_id` and `user_id` in the invocation's `configurable` config. It SHALL include user, client, session, and endpoint metadata for tracing when available. The V2 workflow SHALL resume pending user clarifications on that same thread through the existing message field without changing the FastAPI request or response contract.

#### Scenario: Client continues a session
- **WHEN** requests use the same `x-user-id` and `x-session-id`
- **THEN** both invocations use the same composite `thread_id` and continue the same checkpointed conversation

#### Scenario: Client omits session ID
- **WHEN** a request omits `x-session-id`
- **THEN** the service generates a UUID and uses it for that invocation's thread and response filename

#### Scenario: User answers a name-resolution clarification
- **WHEN** a V2 invocation pauses to ask the user to choose a catalog candidate
- **AND** the client sends the choice as a follow-up message using the same session
- **THEN** the service resumes the pending workflow on the same thread
- **AND** the request and response schemas remain unchanged
