# Ollama generation in API v2

V2 supports GigaChat (the default) and Ollama for both the main Deep Agent and `workbook-name-resolver`. `MODEL_TO_USE` does not control V2. To opt in, set these values in `.env` and restart the service:

```dotenv
V2_MODEL_TO_USE=OLLAMA
OLLAMA_MODEL=qwen3.5:9b-mlx
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_TEMPERATURE=0.000001
OLLAMA_TIMEOUT=180
```

For Docker Desktop with Ollama running on the Mac host, use `OLLAMA_BASE_URL=http://host.docker.internal:11434`. `localhost` inside a container refers to that container. The host Ollama endpoint must be reachable from the container; on other Docker platforms configure the appropriate host-reachable URL. Ollama/MLX runs on the host and is not added to Compose. The model must already be installed.

`OLLAMA_TIMEOUT` is the HTTP timeout in seconds for synchronous and asynchronous model calls, not a deadline for the entire multi-step graph. Its existing default remains 60 seconds; 180 is an example for cold starts. V2 disables Ollama thinking explicitly with `reasoning=False` (`think=false` in runtime requests), for both the main agent and resolver.

The selected client is shared by the main agent and structured name resolution. Missing models, connection failures, timeouts and invalid structured responses fail through the existing HTTP 424 path. There is no fallback to a different generative provider.

Embeddings still use Cloud.ru and existing credentials. Changing the generative provider/model does not change the embedding fingerprint or require index rebuild/reupload. Shared application startup still initializes and checks GigaChat for existing functionality, so retain the current GigaChat settings. This feature does not make the whole service offline.

Provider/model selection is fixed per process. Restart after configuration changes and use a fresh session when switching providers; pending conversations are not migrated across providers. To roll back, set `V2_MODEL_TO_USE=GIGACHAT` (or remove it) and restart. Existing catalog/index files remain compatible.

## Tool policy

Both provider profiles hide and reject calls to `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`, `execute`, and `write_todos`. Excel application tools and `task` delegation to `workbook-name-resolver` remain available. The automatic general-purpose subagent is enabled for both providers; workbook-name resolution continues to use the explicit specialized resolver. These exclusions filter model-visible tools and calls; backend permissions remain a separate control.

## Validation

Ordinary pytest runs use mocked inference and require no Ollama or Cloud.ru access. To explicitly run inference against the installed local model:

```sh
PYTHONPATH=src poetry run python scripts/smoke_v2_ollama.py --model qwen3.5:9b-mlx --url http://localhost:11434 --timeout 180
```

The script reports Ollama, model digest/runner and adapter versions, then pass/fail and elapsed time for each native structured extraction/input/output matching stage, a tool-call/result round trip, and a Deep Agent delegating to the actual resolver before a harmless calculation substitute. Each live stage prints a start marker before inference. Delegation checks identify `workbook-name-resolver` explicitly, so a general-purpose task alone cannot satisfy the check. Its single-row catalog uses deterministic fixture embeddings; it makes no Cloud.ru calls and does not mutate workbooks. A separately labelled controlled check validates clarification and invalid/valid numeric resume without inference. The script exits nonzero on failure, does not change provider/model, and does not dump model reasoning. It is not collected by pytest.

### Validation record (2026-11-10)

- Host venv offline regression: **90 passed** (V2 settings/factory, tool policy, router, resolver, and all `tests/api/v2/` tests).
- Ruff check passes on the new/changed implementation, tests and script except three pre-existing findings in `config/__init__.py`: unused `ClassVar`, unused `BaseAppSettings`, and duplicate `aef_tracing`. Formatting check passes for all 11 changed/new Python files.
- Pylint for the changed production modules and probe: **9.68/10**, with the existing `no-else-return` warning in configuration and protected-member warnings for the probe's deliberate reuse of resolver internals.
- Runtime: Ollama **0.40.15**, `langchain-ollama` **1.1.0**, Deep Agents **0.7.16**; model `qwen3.5:9b-mlx`, MLX runner, NVFP4, digest `203e30078279db51132b9e026ceb7bb21330e5b1af67ef190671b375c9770404`.
- A standalone real `ExtractedNames` schema request returned validated input/output names in **64.81 seconds**.
- The full probe against `http://localhost:11434` with `--timeout 180` **failed** in the live schema group with `ReadTimeout` after **265.72 seconds total**. The group includes multiple requests, so this total is not a single-request duration. The probe stopped before live tool round trip and scoped delegation. Full model compatibility is **not verified**; default reasoning behavior was retained.
- Real workbook/API acceptance and rollback/restart acceptance remain pending. Existing Docker volumes contain active catalog generations; the running app was not rebuilt or restarted during these checks.

Next acceptance action requires deciding whether to retry with a larger HTTP timeout or revise the reasoning setting. A timeout does not by itself establish that the MLX runtime lacks schema support.

### Validation record (2026-11-11)

- Kept the general-purpose subagent enabled for both providers by explicit user decision; aligned the proposal, design, tool-policy delta, task wording, documentation and regression assertion. Strict OpenSpec validation passes.
- Host venv offline regression: **90 passed**, covering `tests/config`, all `tests/api/v2`, and the unit tool-policy, router and name-resolution tests.
- Ruff fix/format passes for the changed probe and policy test. Probe Pylint: **9.48/10**; remaining warnings concern deliberate access to resolver internals.
- Controlled clarification, invalid numeric selection and valid resume pass without inference. These checks do not establish live-model compatibility.
- Real workbook/API and rollback/restart acceptance remain pending. The host has no LibreOffice/pyuno runtime, and Docker checks are deferred at the user's request to keep memory available for local generation.
