# Proposal

## Why

API v2 currently constructs GigaChat directly, so its main agent and workbook name resolver cannot use the locally installed `qwen3.5:9b-mlx` model. Allow selecting Ollama for V2 generation while retaining the existing cloud embeddings and GigaChat behavior by default.

## What Changes

- Add a V2-only generative provider setting, `V2_MODEL_TO_USE=GIGACHAT|OLLAMA`, defaulting to `GIGACHAT` independently of `MODEL_TO_USE`.
- Construct `ChatOllama` from existing `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_TEMPERATURE`, and `OLLAMA_TIMEOUT` settings when selected, with thinking disabled; use the same selected client in the main agent and resolver.
- Apply the existing Deep Agents tool exclusions and workbook resolver delegation to both providers, keeping the general-purpose subagent enabled for both.
- Validate structured extraction/matching and tool execution with the target `qwen3.5:9b-mlx` model; preserve clarification/resume and HTTP contracts.
- Document host and Docker-to-host Ollama configuration and an opt-in compatibility smoke check.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `api-v2-deep-agent`: Select the generative provider for the main agent and resolver without changing embeddings, V1, or request/response schemas.
- `deepagent-tool-policy`: Enforce the existing model-visible tool policy for both GigaChat and Ollama.

## Impact

- Affects V2 agent construction, harness profiles, V2 configuration, `.env.example`, focused tests, and usage documentation.
- Reuses the existing `langchain-ollama` dependency and existing Ollama settings; no new model-serving service or model download is required.
- Cloud.ru embeddings, index identity, retrieval, catalog persistence, and workbook calculations remain unchanged.
- The shared application context still initializes/checks GigaChat for existing service functionality; this change does not promise GigaChat-free service startup.
- Deployment opts in explicitly and restarts the service to change providers. No breaking API change or data migration is intended.

## Non-goals

- Local embeddings, offline operation of the whole service, or changes to V1 model selection.
- Refactoring shared GigaChat lifecycle/authentication, adding an MLX server, or bundling Ollama inside Docker.
- Per-request provider choice, automatic provider fallback, runtime model switching, or retrieval/name-resolution algorithm changes.
