# API v2 Deep Agent Specification

## Purpose

Define the HTTP and runtime behavior of the Deep Agents-backed V2 endpoint.

## Requirements

### Requirement: V2 agent endpoint accepts a bounded message

The service SHALL expose `POST /api/v2/invoke-agent`. The request body SHALL contain a non-empty `message` of at most 2000 characters. The endpoint SHALL use the shared common-header dependency and derive the user and session context from request headers.

#### Scenario: Valid request

- **WHEN** a client submits a non-empty message no longer than 2000 characters with valid common headers
- **THEN** the V2 Deep Agent is invoked with that message

#### Scenario: Invalid message

- **WHEN** the message is empty or longer than 2000 characters
- **THEN** request validation rejects the request before agent invocation

### Requirement: V2 preserves a client's conversation thread

The service SHALL use the supplied `x-session-id` when present, otherwise generate a UUID for the invocation. It SHALL form the LangGraph `thread_id` as `"{user_id}:{session_id}"` and pass `thread_id` and `user_id` in the invocation's `configurable` config. It SHALL include user, client, session, and endpoint metadata for tracing when available.

#### Scenario: Client continues a session

- **WHEN** requests use the same `x-user-id` and `x-session-id`
- **THEN** both invocations use the same composite `thread_id` and continue the same checkpointed conversation

#### Scenario: Client omits session ID

- **WHEN** a request omits `x-session-id`
- **THEN** the service generates a UUID and uses it for that invocation's thread and response filename

### Requirement: V2 returns a ZIP response

On successful invocation, the service SHALL return an `application/zip` response containing `txt_response.txt` with the final agent message and `generated/agent_output.xlsx` with an Excel workbook. The attachment filename SHALL include the session ID.

#### Scenario: Agent completes successfully

- **WHEN** the Deep Agent returns a final message
- **THEN** the response is a ZIP containing the text message and workbook entry

### Requirement: V2 reports invocation failures

The service SHALL log agent invocation failures and return HTTP 424 with the exception detail.

#### Scenario: Agent invocation raises

- **WHEN** agent creation or invocation raises an exception
- **THEN** the failure is logged and the endpoint returns HTTP 424

### Requirement: V2 reuses the existing upload handler
The service SHALL expose upload at `/api/v2/upload`, reuse workbook validation and correction behavior, and exclude that route from the V2 OpenAPI schema. V2 upload SHALL prepare persistent searchable catalogs before reporting success, preserving the existing success response fields. V1 upload behavior SHALL remain unchanged.

#### Scenario: V2 upload
- **WHEN** a client uploads a workbook through `/api/v2/upload`
- **THEN** the existing workbook validation and correction behavior is used
- **AND** the route is omitted from the generated API schema
- **AND** the uploaded workbook's searchable catalogs are ready before success is returned

#### Scenario: Index preparation failure
- **WHEN** catalog preparation or document embedding fails
- **THEN** V2 upload returns HTTP 424 using the existing failure response shape
- **AND** the previous active workbook and indexes remain available
