# Spec Delta

## Purpose

Lets a client upload an Excel cashflow model and ask natural-language questions about it at a
stable `/api/v1` contract, answered by the legacy bounded tool-calling orchestrator loop.

## ADDED Requirements

### Requirement: Request header validation
Every `/api/v1/upload` and `/api/v1/invoke-agent` request SHALL carry `x-trace-id` (UUID v4,
optional empty), `x-client-id` (pattern `^[A-Z]{2}\d{8}$`, optional empty), `x-session-id` (UUID
v4, optional empty), `x-request-time` (RFC-3339 timestamp, required), and `x-user-id` (≤8
chars). A header that is present but violates its format SHALL cause the request to be
rejected before any business logic runs.

#### Scenario: Malformed header rejected
- **WHEN** a request to `/api/v1/upload` or `/api/v1/invoke-agent` carries an `x-client-id` that
  does not match `^[A-Z]{2}\d{8}$`
- **THEN** the server responds `422` and does not save a file or invoke the agent

### Requirement: Excel upload
`/api/v1/upload` SHALL accept a single `.xlsx` or `.xls` file, store it so a later
`/api/v1/invoke-agent` call from the same `x-user-id` can read it, and record its filename
keyed by `x-user-id`.

#### Scenario: Successful upload
- **WHEN** a client POSTs a `.xlsx` file to `/api/v1/upload` with a valid `x-user-id`
- **THEN** the server responds `200` with the saved filename, and a subsequent
  `/api/v1/invoke-agent` call from the same `x-user-id` can read that workbook

#### Scenario: Non-Excel file rejected
- **WHEN** a client POSTs a file whose extension is not `xlsx` or `xls` to `/api/v1/upload`
- **THEN** the server responds `424` with an error description and does not store the file

### Requirement: Agent invocation requires a prior upload
`/api/v1/invoke-agent` SHALL require that the requesting `x-user-id` has a previously uploaded
workbook; it SHALL NOT fabricate or assume a filename.

#### Scenario: No prior upload
- **WHEN** `/api/v1/invoke-agent` is called for an `x-user-id` that has never called
  `/api/v1/upload`
- **THEN** the server responds `424` with an error description instead of attempting to answer

### Requirement: Conversation threading per session
`/api/v1/invoke-agent` SHALL scope conversation history to the combination of `x-user-id` and
`x-session-id` (when `x-session-id` is absent, the server SHALL generate one for that call), so
that two different users, or the same user across different sessions, never see each other's
prior messages.

#### Scenario: Same user, same session, follow-up question
- **WHEN** a client calls `/api/v1/invoke-agent` twice with the same `x-user-id` and
  `x-session-id`
- **THEN** the second call's answer may reference the first call's question or answer

#### Scenario: Same user, different session
- **WHEN** a client calls `/api/v1/invoke-agent` with the same `x-user-id` but a different
  `x-session-id` than a previous call
- **THEN** the second call's answer SHALL NOT reference the first call's conversation

### Requirement: Deterministic legacy implementation
`/api/v1/invoke-agent` SHALL always be answered by the legacy bounded tool-calling orchestrator
loop. The implementation used SHALL NOT depend on server deployment configuration — there is no
configuration that redirects `/api/v1` traffic to the deep-agent (`/api/v2`) behavior described
in the `deep-agent-api` capability.

#### Scenario: Behavior is independent of deployment config
- **WHEN** `/api/v1/invoke-agent` is called on any deployment of the service
- **THEN** the answer is produced by the same orchestrator tool-calling loop, regardless of any
  environment configuration the deployment sets

### Requirement: Bounded tool-calling loop
The orchestrator SHALL decide, per turn, whether answering requires calling one or more
calculation tools against the uploaded workbook. The number of tool-calling round-trips within
a single `/api/v1/invoke-agent` call SHALL be capped; once the cap is reached, the server SHALL
still return a best-effort answer rather than erroring.

#### Scenario: No calculation needed
- **WHEN** the user's question can be answered from conversation history alone
- **THEN** the server answers without calling any calculation tool

#### Scenario: Iteration cap reached
- **WHEN** the orchestrator keeps requesting tool calls past the configured iteration cap
- **THEN** the server stops requesting further tool calls and returns a synthesized answer based
  on whatever results were gathered so far, rather than an error

### Requirement: Duplicate tool call deduplication
Within a single `/api/v1/invoke-agent` call, if the orchestrator requests the same tool with
identical arguments more than once, the server SHALL NOT execute the calculation a second time;
it SHALL reuse the first result.

#### Scenario: Repeated identical tool call
- **WHEN** the orchestrator requests the same tool with the same arguments twice in one
  `/api/v1/invoke-agent` call
- **THEN** the calculation underlying that tool runs only once, and the orchestrator is told the
  call was already performed

### Requirement: Response envelope
`/api/v1/invoke-agent` SHALL return a ZIP archive containing a text file with the natural-
language answer and a generated Excel file placeholder, on success.

#### Scenario: Successful answer
- **WHEN** `/api/v1/invoke-agent` succeeds
- **THEN** the response is a ZIP archive containing the answer text and a generated Excel
  placeholder file

### Requirement: Upstream LLM unavailability surfaces as service unavailable
If the LLM backend reports it is temporarily unavailable while answering, `/api/v1/invoke-agent`
SHALL respond with a service-unavailable error rather than a generic failure.

#### Scenario: LLM backend temporarily down
- **WHEN** the LLM backend used by the orchestrator reports it is temporarily unavailable
- **THEN** `/api/v1/invoke-agent` responds `503` with a user-facing error description
