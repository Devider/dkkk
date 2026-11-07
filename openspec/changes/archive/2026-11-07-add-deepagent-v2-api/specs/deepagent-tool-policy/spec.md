# Deep Agent Tool Policy Specification

## Purpose

Limit the V2 Deep Agent's model-visible tool set to application tools required for Excel financial-model analysis.

## Requirements

### Requirement: V2 excludes standard Deep Agents tools

The V2 agent SHALL register a GigaChat harness profile under provider key `giga`. The profile SHALL exclude standard Deep Agents tools from the model-visible tool set, including `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`, `execute`, `task`, and `write_todos`. The existing Excel analysis tools SHALL remain available.

#### Scenario: Profile matches the configured GigaChat model

- **WHEN** `create_v2_agent` constructs the configured `langchain_gigachat.GigaChat` model
- **THEN** Deep Agents resolves the profile by the provider identifier reported by the integration (`giga`)
- **AND** the standard tools listed above are excluded from the model's tool set

#### Scenario: Excel tools remain available

- **WHEN** the V2 model receives its available tool definitions
- **THEN** application Excel tools supplied to `create_deep_agent` remain available

#### Scenario: Excluded tool is called by a stale or malformed tool call

- **WHEN** a tool call names a tool excluded by the resolved profile
- **THEN** the exclusion middleware rejects the call as unavailable

### Requirement: Profile exclusions are not treated as a security boundary

The system SHALL treat `excluded_tools` as model-facing tool visibility and invocation filtering only. It SHALL NOT rely on profile exclusions as filesystem or execution authorization controls.

#### Scenario: Tool policy changes

- **WHEN** filesystem or execution access must be prevented for security reasons
- **THEN** the service SHALL enforce the restriction through backend permissions or by not installing the relevant capability
- **AND** it SHALL NOT rely solely on the profile's excluded tool names
