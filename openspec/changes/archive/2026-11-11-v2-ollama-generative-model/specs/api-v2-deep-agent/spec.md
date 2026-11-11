# Spec Delta

## ADDED Requirements

### Requirement: V2 selects its generative provider independently

The service SHALL select the V2 generative provider using `V2_MODEL_TO_USE`, accepting `GIGACHAT` and `OLLAMA` and defaulting to `GIGACHAT` when unset. This selection SHALL be independent of `MODEL_TO_USE` and SHALL NOT change V1 model selection. Unsupported V2 provider values SHALL be rejected without silently selecting another provider.

#### Scenario: Existing deployment has no V2 provider setting

- **WHEN** `V2_MODEL_TO_USE` is unset
- **THEN** V2 uses the existing configured GigaChat model and connection settings
- **AND** setting `MODEL_TO_USE=OLLAMA` alone does not opt V2 into Ollama

#### Scenario: Deployment opts into local generation

- **WHEN** `V2_MODEL_TO_USE=OLLAMA` and `OLLAMA_MODEL=qwen3.5:9b-mlx`
- **THEN** V2 sends its generative requests to the configured Ollama endpoint using that model
- **AND** V1 continues using its existing configuration and behavior

#### Scenario: Invalid provider is configured

- **WHEN** `V2_MODEL_TO_USE` is neither `GIGACHAT` nor `OLLAMA`
- **THEN** configuration validation or V2 agent creation fails with an error identifying the invalid provider
- **AND** no fallback provider is invoked

### Requirement: V2 generation uses the selected provider throughout resolution

The V2 main agent and workbook name resolver SHALL use the selected generative provider and model. Ollama generation SHALL honor `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_TEMPERATURE`, and `OLLAMA_TIMEOUT`. Structured extraction and matching, tool calls, and numeric clarification/resume SHALL preserve their existing contracts with either provider.

#### Scenario: Ollama resolves workbook names

- **WHEN** a request requiring input and output name resolution runs with Ollama selected
- **THEN** extraction and structured matching use the configured Ollama model
- **AND** Ollama thinking is disabled for both main-agent generation and structured resolution
- **AND** validated resolver results retain the existing fields, canonical-name validation, and unresolved outcomes
- **AND** the main agent delegates through `task` and uses resolved canonical names for subsequent Excel tool calls

#### Scenario: Ollama workflow pauses for a choice

- **WHEN** resolution requires numeric clarification under Ollama
- **AND** the user supplies a valid choice in the same session
- **THEN** V2 resumes the checkpointed workflow using the existing clarification behavior
- **AND** the HTTP message contract and ZIP response format remain unchanged

#### Scenario: Ollama request fails

- **WHEN** Ollama is unavailable, the configured model is missing, generation times out, or structured output cannot be validated during invocation
- **THEN** V2 logs the failure and returns HTTP 424 through its existing invocation failure handling
- **AND** it does not retry the request through GigaChat as a fallback

### Requirement: V2 generative provider selection preserves embeddings

Selecting Ollama for V2 generation SHALL NOT change the embedding provider or configuration, retrieval behavior, embedding fingerprint, or persisted catalog compatibility. Nonempty embedding operations SHALL continue using the existing configured embedding service.

#### Scenario: Existing indexes are used with Ollama

- **WHEN** an existing valid catalog generation is accessed after changing only the V2 generative provider or Ollama model
- **THEN** the stored embedding configuration remains compatible and document embeddings are not rebuilt solely because of that change
- **AND** nonempty semantic queries continue using the configured cloud embeddings

#### Scenario: V2 upload uses local generation configuration

- **WHEN** a workbook is uploaded with `V2_MODEL_TO_USE=OLLAMA`
- **THEN** catalog preparation uses the existing embedding configuration and credentials
- **AND** selecting local generation does not bypass embedding errors or credential requirements
