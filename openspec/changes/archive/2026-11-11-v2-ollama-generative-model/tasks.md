# Tasks

## 1. V2 provider configuration and client construction

- [x] 1.1 Add typed V2 settings for `V2_MODEL_TO_USE=GIGACHAT|OLLAMA`, defaulting to `GIGACHAT`, and expose them through `APP_CONFIG`; add offline tests under `tests/config/` verifying default, both accepted values, invalid-value rejection, and independence from `MODEL_TO_USE`.
- [x] 1.2 Extract a typed V2 model factory returning `BaseChatModel` and wire its selected client into both the main agent and resolver; add offline tests under `tests/api/v2/` verifying unchanged GigaChat parameters, Ollama URL/model/temperature, HTTP timeout forwarding to both clients, and shared selected-client wiring without live inference.
- [x] 1.3 Add a V2 Ollama example to `.env.example` and `docs/v2-ollama.md`, preserving current provider/model defaults; verify the examples explicitly configure `qwen3.5:9b-mlx`, distinguish host and Docker Desktop URLs, and explain restart requirements, existing cloud embeddings, and shared GigaChat startup/configuration.

## 2. Provider-independent V2 tool policy

- [x] 2.1 Extend harness registration to apply the existing exclusions and enabled general-purpose subagent under both `giga` and `ollama`; verify provider resolution for a real `ChatOllama` instance without network calls and retain `task`, the explicit resolver, and Excel tools.
- [x] 2.2 Extend `tests/unit/test_v2_tool_policy.py` for both providers and update its factory-wiring assertions; verify the same excluded tool calls are rejected, application tools remain visible, and both the configured specialized resolver and general-purpose subagent are available with either provider.
- [x] 2.3 Document the equivalent tool policy in `docs/v2-ollama.md`; verify its excluded-tool list matches `V2_EXCLUDED_BUILTIN_TOOLS` and does not describe exclusions as backend permissions.

## 3. Structured resolution and failure compatibility

- [x] 3.1 Add focused offline tests under `tests/api/v2/` for Ollama-backed structured extraction/input/output matching using mocked adapter responses; verify the existing Pydantic schemas, candidate validation, unresolved items, and deterministic numeric clarification/resume still work without changing resolver algorithms.
- [x] 3.2 Extend V2 router regression tests for model-not-found/connectivity/timeout and invalid structured-output failures; verify HTTP 424 and no generative fallback to GigaChat, while successful and interrupted responses preserve the existing ZIP and session contracts.
- [x] 3.3 Add an offline compatibility regression proving that changing only V2 generative provider/model preserves retrieval settings and embedding fingerprint; verify existing valid catalogs remain reusable without document re-embedding and existing embedding tests pass.

## 4. Opt-in model compatibility probe

- [ ] 4.1 Add `scripts/smoke_v2_ollama.py` with deterministic in-memory catalog/embedding fixtures and no Cloud.ru requests; verify live `qwen3.5:9b-mlx` returns validated results for extraction and both matching schemas, completes a native tool-call/result round trip, and delegates to the resolver before a harmless application-tool substitute. Clearly distinguish controlled clarification/resume checks from assertions against live generation.
- [x] 4.2 Document the smoke command, pass/fail expectations, timeout override, and runtime-version reporting in `docs/v2-ollama.md`; verify the script is opt-in, excluded from ordinary pytest inference, and reports failures without dumping reasoning text or silently changing models/providers.

## 5. Integration acceptance

- [x] 5.1 Run the focused settings/factory tests, `tests/unit/test_v2_tool_policy.py`, `tests/unit/test_v2_router.py`, `tests/unit/test_v2_name_resolution.py`, and relevant `tests/api/v2/` regressions from the host venv; run required Ruff fix/format checks and Pylint (>7) for the changed Python scope, recording results and any unrelated pre-existing failures.
- [ ] 5.2 Run the opt-in compatibility probe against the installed `qwen3.5:9b-mlx`; record Ollama/model/adapter versions, timings, schema and tool-call results in the V2 Ollama documentation. Treat a failed required capability as an unresolved acceptance failure, not a passed smoke check.
- [ ] 5.3 Verify a real V2 request with Ollama, a fresh session, an existing workbook, and current cloud retrieval credentials; record canonical-name delegation, Excel tool execution, ZIP response, session continuation, and clarification/resume where a known ambiguous case is available. Verify rollback to GigaChat by configuration/restart requires no index rebuild.
