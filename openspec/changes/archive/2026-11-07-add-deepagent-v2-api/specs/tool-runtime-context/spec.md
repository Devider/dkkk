# Tool Runtime Context Specification

## Purpose

Pass trusted request identity to identity-dependent Excel tools without asking the model to provide user or thread identifiers.

## Requirements

### Requirement: Identity-dependent tools read request identity from runtime config

Tools that need user or thread identity SHALL obtain `user_id` and `thread_id` from LangChain's injected `RunnableConfig` under `configurable`. These identifiers SHALL NOT be required fields in model-facing tool argument schemas.

The requirement applies to `analyze_model_inputs_for_target`, `analyze_excel_model`, `modify_excel_input_value`, `get_output_info`, `greeting_user`, and `describe_outputs_sheet`.

#### Scenario: V2 invokes an identity-dependent tool

- **WHEN** V2 invokes the agent with `configurable.user_id` and `configurable.thread_id`
- **AND** the model calls an identity-dependent tool
- **THEN** the tool receives the invocation config and uses those IDs for user-scoped file lookup or thread-aware behavior
- **AND** the model-facing tool schema does not request either ID

#### Scenario: Runtime identity is absent

- **WHEN** an identity-dependent tool is invoked without one or both configurable IDs
- **THEN** the missing value is treated as `None` and existing tool-level missing-identity behavior applies

### Requirement: V1 passes identity through the same runtime channel

The V1 manual tool runner SHALL pass state `user_id` and `thread_id` to `tool.invoke` through a LangChain config object's `configurable` map. It SHALL NOT append these values to the model-generated tool argument dictionary.

#### Scenario: V1 invokes an identity-dependent tool

- **WHEN** V1 executes a tool call with user and thread values in agent state
- **THEN** the tool receives those values in its invocation config
- **AND** the business-argument payload is unchanged by identity injection
