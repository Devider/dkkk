# Design

## Context

See `proposal.md` for motivation and scope. `api/v2/agent.py` constructs a fresh GigaChat client and passes it to both `create_deep_agent` and `build_name_resolver`. The router caches the resulting graph once per process. The resolver accepts a `BaseChatModel` and uses `with_structured_output` for extraction and input/output matching; its deterministic validation and interrupt/resume logic are already provider-independent.

`OllamaSettings` already exposes connection, model, temperature, and timeout settings, and `langchain-ollama` is installed. Inspection of the installed adapter confirms provider metadata `ollama`, structured output via `json_schema` by default, and HTTP configuration via `client_kwargs`. Deep Agents currently registers the restricted V2 harness only for provider `giga`. Its durable tool-policy spec explicitly names GigaChat, so this change extends that requirement rather than treating Ollama as already covered.

Cloud.ru embeddings have their own lazy settings and fingerprint. Shared `AppContext` separately initializes GigaChat and checks it at startup; these shared lifecycle paths serve existing functionality and are outside this change. The actual source differs from older project notes: current Compose defines only the app, and current V2 does not use the global `MODEL_TO_USE` selector.

The local `/api/tags` response lists `qwen3.5:9b-mlx` with runner `mlx` and capabilities `completion`, `vision`, `thinking`, and `tools`. This confirms installation and advertised tool support, not successful structured generation or the V2 workflow. Those require the opt-in acceptance checks below.

## Goals / Non-Goals

**Goals:**

- Keep provider selection confined to V2 construction and preserve a single selected generative client throughout the graph.
- Use the installed adapter's native tool and schema APIs, with the existing resolver schemas and validation.
- Make tool visibility independent of the chosen provider and verify it against real adapter metadata.

**Non-Goals:**

- Redesigning resolver prompts, matching algorithms, concurrency, or cloud retrieval.
- Changing shared service startup to remove all GigaChat configuration or network activity.
- Managing Ollama processes, downloading models, or selecting providers per request.

## Decisions

### 1. Use an isolated, validated V2 provider setting

Introduce a small V2 settings object under the config package and expose it through `APP_CONFIG`. Its provider field maps to `V2_MODEL_TO_USE`, accepts the exact values `GIGACHAT` and `OLLAMA`, and defaults to `GIGACHAT`. Keep provider validation separate from model construction. This is a deployment setting read at process initialization, not a request parameter.

The alternative of reusing `MODEL_TO_USE` would make an existing global variable change V2 behavior unexpectedly. A dedicated selector preserves today's V2 default, even where `.env.example` suggests the global selector is Ollama. Existing Ollama fields and defaults remain unchanged; the documented target configuration explicitly sets `OLLAMA_MODEL=qwen3.5:9b-mlx`.

### 2. Add a small typed model factory for V2

Keep `create_v2_agent` responsible for assembling the graph, and extract client construction into a fully typed factory returning `BaseChatModel`. For GigaChat, preserve the current base parameters and configured model. For Ollama, construct `ChatOllama` with the existing URL, model and temperature; map `OLLAMA_TIMEOUT` to `client_kwargs={"timeout": ...}` so it reaches both underlying HTTP clients. Do not forward GigaChat-specific constructor arguments to Ollama.

Pass the returned client to both the main agent and `build_name_resolver`. No generative fallback is added: missing models, connectivity failures, and invalid structured output follow the router's existing HTTP 424 error path. Do not instantiate a second GigaChat client as a fallback for V2 resolution.

Reusing `APP_CTX.llm` was considered, but it is the existing service-specific `Agent` wrapper rather than the plain model V2 currently expects. Changing that wrapper or shared lifecycle would expand the scope into V1.

### 3. Reuse the adapter's structured output path

Keep the resolver's existing `with_structured_output(PydanticSchema)` calls. The installed `ChatOllama` defaults to Ollama's JSON Schema structured output and returns validated Pydantic instances, fitting the current extraction/matching code. GigaChat retains its existing behavior. Do not introduce text scraping, JSON-only prompt parsing, or silent acceptance of malformed responses.

Disable Ollama thinking explicitly with `ChatOllama(reasoning=False)`, which forwards `think=false` to the runtime, as requested by the user after the default-reasoning probe showed excessive latency. The shared selected client applies this to both the main agent and resolver. The live smoke report must record effective model/server versions, the reasoning setting and latency. If the installed MLX runtime cannot satisfy the required schema/tool paths, report the exact failing capability and resolve compatibility before declaring the change verified; advertised capabilities alone are insufficient.

### 4. Register equivalent harness profiles for both providers

Reuse one exclusion set and enabled general-purpose-subagent configuration for keys `giga` and `ollama`, registering before graph creation. Preserve `task` and the explicit workbook resolver; workbook-name resolution continues to use that specialized resolver even though general-purpose delegation is also available. Test provider discovery with actual `GigaChat`/`ChatOllama` adapter metadata or equivalent contract fixtures, not an assumed class-name mapping. Existing harness APIs support this extension; an additional middleware stack would duplicate the current mechanism.

### 5. Preserve the embedding boundary and API

Do not add generative model/provider identity to embedding fingerprints or index manifests. No catalog rebuild or reupload is required solely for this change. Nonempty retrieval still calls Cloud.ru with the existing credentials. Preserve memory/store injection, session keys, ZIP responses and clarification/resume behavior.

The existing cached graph means provider or model changes require a process restart. Support one configured provider per process. Do not promise interchangeability of pending checkpoints across provider changes; use a fresh session when switching providers for acceptance checks.

### 6. Separate offline regression tests from opt-in local inference

Offline tests cover settings/defaults, model factory wiring and HTTP timeout forwarding, equivalent tool policy, error propagation, unchanged embedding fingerprint, and existing resolver/router regressions. Mock generation rather than requiring Ollama in ordinary pytest runs.

Add an explicit smoke script following the existing `scripts/smoke_v2_embeddings.py` pattern, but targeting Ollama generation with deterministic in-memory catalog/embedding fixtures. It must make no Cloud.ru calls. Check the three real resolver schemas, a native tool-call/result round trip, and a graph flow with scoped resolver delegation followed by a harmless application-tool substitute. Check clarification/resume with a controlled ambiguous fixture; deterministic orchestration fixtures may be used to guarantee the interrupt branch, clearly distinguished from live model assertions. No workbook mutation is needed for the generation compatibility probe. Print clear pass/fail results without dumping reasoning text.

Run existing router ZIP/error tests and resolver clarification tests alongside this smoke probe; these establish API/orchestration behavior separately from real-model capability. Record the runtime versions and outcomes, including failures. There is no blanket model-quality claim from a small smoke sample.

## Risks / Trade-offs

- [MLX may advertise tools but fail schema or tool parsing] -> Require successful live schema and tool checks on `qwen3.5:9b-mlx`; retain validation errors and do not silently switch models/providers.
- [Reasoning or cold model loading may exceed the existing 60-second timeout] -> Honor `OLLAMA_TIMEOUT`, document a higher deployment value such as 180 seconds for initial testing, and record timings before tuning behavior.
- [Wrong provider key exposes default tools] -> Assert that the Ollama adapter resolves the `ollama` harness and rejects excluded calls while retaining `task` and Excel tools.
- [Container localhost points to the container] -> Document `http://localhost:11434` for a host process and `http://host.docker.internal:11434` for Docker Desktop connecting to host Ollama. Verify host reachability; MLX stays on the host. Other Docker platforms require a host-reachable URL configured by the operator.
- [Local generation is mistaken for offline service operation] -> Document that embeddings and shared GigaChat initialization/startup checks remain, and retain their existing configuration.
- [Provider switches affect checkpointed conversations] -> Restart after configuration changes and use fresh sessions during provider-switch acceptance; no cross-provider checkpoint migration is promised.

## Migration Plan

1. Deploy with `V2_MODEL_TO_USE` unset and verify the existing GigaChat path remains the default.
2. Configure `V2_MODEL_TO_USE=OLLAMA`, `OLLAMA_MODEL=qwen3.5:9b-mlx`, a process-reachable `OLLAMA_BASE_URL`, and a suitable timeout. Preserve existing embedding and shared service settings.
3. Run offline regressions and the opt-in local generation probe; retain the probe report. Restart the service and verify V2 with a fresh session and an existing uploaded workbook using existing cloud retrieval credentials.
4. Verify canonical-name delegation, Excel tool use, ZIP output, and same-session continuation. Use a known ambiguous request for manual clarification/resume acceptance when available; deterministic regression coverage remains required regardless of model choices.
5. Roll back by setting `V2_MODEL_TO_USE=GIGACHAT` (or removing it) and restarting. Keep catalog/index data intact; no embedding migration is involved.
