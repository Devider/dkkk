# Spec Delta

## MODIFIED Requirements

### Requirement: V2 excludes standard Deep Agents tools except scoped delegation

The V2 agent SHALL apply the same tool policy for GigaChat and Ollama. It SHALL exclude `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`, `execute`, and `write_todos` from the model-visible tool set. The `task` tool SHALL remain available for delegation to `workbook-name-resolver`. The automatically provided general-purpose subagent SHALL be enabled for both providers. The existing Excel analysis tools SHALL remain available.

#### Scenario: Profile matches the configured GigaChat model

- **WHEN** V2 uses the configured GigaChat model
- **THEN** its provider-specific tool policy excludes the standard tools listed above from the model's tool set

#### Scenario: Profile matches the configured Ollama model

- **WHEN** V2 uses the configured Ollama model, including `qwen3.5:9b-mlx`
- **THEN** its provider-specific tool policy excludes the same standard tools from the model's tool set
- **AND** provider selection does not change the excluded built-in tools or availability of general-purpose delegation

#### Scenario: Workbook name resolution is available

- **WHEN** the V2 model receives its available delegation tools and subagents with either provider
- **THEN** it can call `workbook-name-resolver` through `task`
- **AND** the automatically provided general-purpose subagent is also available

#### Scenario: Excel tools remain available

- **WHEN** the V2 model receives its available tool definitions with either provider
- **THEN** application Excel tools supplied to the agent remain available

#### Scenario: Excluded tool is called by a stale or malformed tool call

- **WHEN** a tool call names a tool excluded by the resolved profile with either provider
- **THEN** the exclusion middleware rejects the call as unavailable
