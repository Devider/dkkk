# Spec Delta

## Purpose

Lets a client upload an Excel cashflow model and ask natural-language questions about it at a
`/api/v2` contract, answered by a model that autonomously delegates calculations to subagents
rather than following a fixed tool-calling loop.

## ADDED Requirements

### Requirement: Request header validation
Every `/api/v2/upload` and `/api/v2/invoke-agent` request SHALL be validated against the same
header contract as `/api/v1`: `x-trace-id`, `x-client-id`, `x-session-id`, `x-request-time`, and
`x-user-id`, with the same formats and optionality. A header that is present but violates its
format SHALL cause the request to be rejected before any business logic runs.

#### Scenario: Malformed header rejected
- **WHEN** a request to `/api/v2/upload` or `/api/v2/invoke-agent` carries an `x-request-time`
  that is not a valid RFC-3339 timestamp
- **THEN** the server responds `422` and does not save a file or invoke the agent

### Requirement: Excel upload
`/api/v2/upload` SHALL accept a single `.xlsx` or `.xls` file and store it so a later
`/api/v2/invoke-agent` call from the same `x-user-id` can read it. A workbook uploaded via
`/api/v1/upload` for a given `x-user-id` SHALL also be usable by `/api/v2/invoke-agent` for that
same `x-user-id`, and vice versa — the stored workbook is keyed by user, not by API version.

#### Scenario: Successful upload
- **WHEN** a client POSTs a `.xlsx` file to `/api/v2/upload` with a valid `x-user-id`
- **THEN** the server responds `200` with the saved filename, and a subsequent
  `/api/v2/invoke-agent` call from the same `x-user-id` can read that workbook

#### Scenario: Upload via the other version is still usable
- **WHEN** a client uploads a workbook via `/api/v1/upload` for a given `x-user-id`, then calls
  `/api/v2/invoke-agent` for that same `x-user-id`
- **THEN** the deep-agent implementation can read that workbook without a separate
  `/api/v2/upload` call

### Requirement: Agent invocation requires a prior upload
`/api/v2/invoke-agent` SHALL require that the requesting `x-user-id` has a previously uploaded
workbook (via either `/api/v1/upload` or `/api/v2/upload`); it SHALL NOT fabricate or assume a
filename.

#### Scenario: No prior upload
- **WHEN** `/api/v2/invoke-agent` is called for an `x-user-id` that has never uploaded a
  workbook
- **THEN** the server responds `424` with an error description instead of attempting to answer

### Requirement: Conversation threading per session
`/api/v2/invoke-agent` SHALL scope conversation history to the combination of `x-user-id` and
`x-session-id` (when `x-session-id` is absent, the server SHALL generate one for that call), so
that two different users, or the same user across different sessions, never see each other's
prior messages. A `/api/v1` session and a `/api/v2` session for the same `x-user-id`/
`x-session-id` pair are independent conversation histories.

#### Scenario: Same user, same session, follow-up question
- **WHEN** a client calls `/api/v2/invoke-agent` twice with the same `x-user-id` and
  `x-session-id`
- **THEN** the second call's answer may reference the first call's question or answer

#### Scenario: v1 and v2 histories do not mix
- **WHEN** a client calls `/api/v1/invoke-agent` and `/api/v2/invoke-agent` with the same
  `x-user-id` and `x-session-id`
- **THEN** neither call's answer references the other version's conversation history

### Requirement: Autonomous calculation delegation
Rather than a fixed orchestrator tool-calling loop, the model behind `/api/v2/invoke-agent`
SHALL itself decide, from the conversation alone, whether answering requires a calculation
against the uploaded workbook, and if so, delegate that calculation to the appropriate
subagent. The model SHALL NOT fabricate numeric results it has not obtained from a delegated
calculation.

#### Scenario: No calculation needed
- **WHEN** the user's question can be answered from conversation history alone
- **THEN** the server answers without delegating any calculation

#### Scenario: Calculation required
- **WHEN** the user's question requires specific numeric values, scenarios, or target-seeking
  results from the workbook
- **THEN** the server delegates the calculation to a subagent and bases its answer on the
  returned result, rather than inventing a figure

### Requirement: Bounded delegation depth
A single `/api/v2/invoke-agent` call SHALL cap how many delegation round-trips it allows; if
the cap is reached before an answer is produced, the server SHALL return a user-facing message
asking the user to simplify or split their request, rather than an error or an infinite loop.

#### Scenario: Delegation depth cap reached
- **WHEN** the model keeps delegating calculations past the configured depth cap without
  reaching a final answer
- **THEN** `/api/v2/invoke-agent` responds with a message asking the user to simplify or split
  the request, not a `5xx` error

### Requirement: Duplicate delegation deduplication
Within a single `/api/v2/invoke-agent` call, if the model delegates the same calculation with
identical inputs more than once, the server SHALL NOT execute it a second time; it SHALL report
that the calculation was already performed.

#### Scenario: Repeated identical delegation
- **WHEN** the model delegates the same calculation with the same inputs twice in one
  `/api/v2/invoke-agent` call
- **THEN** the calculation runs only once, and the model is told the calculation was already
  performed

### Requirement: Response envelope
`/api/v2/invoke-agent` SHALL return a ZIP archive containing a text file with the natural-
language answer and a generated Excel file placeholder, on success — the same envelope shape as
`/api/v1/invoke-agent`.

#### Scenario: Successful answer
- **WHEN** `/api/v2/invoke-agent` succeeds
- **THEN** the response is a ZIP archive containing the answer text and a generated Excel
  placeholder file

### Requirement: Upstream LLM unavailability surfaces as service unavailable
If the LLM backend reports it is temporarily unavailable while answering, `/api/v2/invoke-agent`
SHALL respond with a service-unavailable error rather than a generic failure.

#### Scenario: LLM backend temporarily down
- **WHEN** the LLM backend used by the deep agent reports it is temporarily unavailable
- **THEN** `/api/v2/invoke-agent` responds `503` with a user-facing error description
