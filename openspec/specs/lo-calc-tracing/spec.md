# lo-calc-tracing Specification

## Purpose

Gives the LibreOffice calculation dispatches (the agent graph's slowest, most failure-prone steps)
visibility in Langfuse as discrete, timed tool observations, so their cost and failures can be
inspected per request instead of being invisible to tracing.

## Requirements

### Requirement: Tool dispatch observations
Each LibreOffice calculation dispatch (`analyze_model_inputs_for_target`, `analyze_excel_model`)
SHALL be wrapped in a Langfuse tool-typed observation named after the literal tool name, with the
resolved call arguments as `input` and the calculation result as `output`, when a compatible
Langfuse client is active.

#### Scenario: Successful dispatch produces a tool span
- **WHEN** a request dispatches `analyze_model_inputs_for_target` or `analyze_excel_model` with an
  active Langfuse client
- **THEN** a `tool`-typed Langfuse observation is created with that literal tool name, `input`
  matching the resolved call arguments, and `output` matching the calculation result once it
  completes

#### Scenario: Cache-hit dispatch still produces exactly one flat span
- **WHEN** a dispatch resolves from the in-process calculation cache instead of running a fresh
  LibreOffice recalculation
- **THEN** exactly one tool span is created for the call (no child spans distinguishing cache-hit
  from fresh recalculation); the span's shorter duration is the only observable difference

### Requirement: Graceful degradation without an active Langfuse client
Tracing SHALL add no observation and raise no exception when there is no usable Langfuse client,
so calculation dispatch behavior is unaffected by the tracing backend's state.

#### Scenario: Tracing is disabled or absent
- **WHEN** the application's tracing handle is `None`
- **THEN** the calculation dispatch proceeds normally with no tool span created and no exception
  raised

#### Scenario: Active backend is not the Langfuse client
- **WHEN** the application's tracing handle is the AEF tracing backend rather than the Langfuse
  client (the two backends are mutually exclusive)
- **THEN** the calculation dispatch proceeds normally with no tool span created and no exception
  raised

#### Scenario: Langfuse client is configured but not started
- **WHEN** the application's tracing handle is the Langfuse client but its underlying client handle
  is not yet initialized
- **THEN** the calculation dispatch proceeds normally with no tool span created and no exception
  raised

### Requirement: Dispatch errors are visible on the span
When a calculation dispatch raises inside an active tool span, the span SHALL be marked as an error
with the exception recorded, and the exception SHALL still propagate to the caller unchanged.

#### Scenario: Forced dispatch error marks the span as failed
- **WHEN** a calculation dispatch (e.g. an unreachable output name) raises an exception while a tool
  span is open
- **THEN** the span is marked `ERROR` with the exception recorded, and the same exception continues
  to propagate out of the dispatch call

### Requirement: Correct user attribution on the parent trace
The agent request trace that tool dispatch spans nest under SHALL carry the user identifier under
the metadata key the active Langfuse integration actually recognizes, so requests are attributed to
the correct user.

#### Scenario: Request trace shows the requesting user
- **WHEN** a user issues a request that is traced via the LangChain Langfuse callback handler
- **THEN** the resulting trace's `user_id` field is populated with that user's identifier
