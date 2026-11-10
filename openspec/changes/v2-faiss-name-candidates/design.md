# Design

## Context

See proposal.md for motivation. V2 currently registers the V1 upload function directly. That function overwrites a per-user temporary workbook, corrects it using LibreOffice, and publishes its filename in the agent store. The default store is in memory. The V2 resolver rebuilds catalogs with openpyxl on invocation, sends entire catalogs to matching prompts, and filters Inputs to names containing `золото`. Existing tests preserve duplicate row names and deterministic clarification. The completed `v2-model-parameters-name-resolution` change has not yet been synced into main specs; this change builds on its implementation and clarification contract without duplicating its entire capability.

## Goals / Non-Goals

**Goals:** Keep document embeddings reusable across restarts; publish matching workbook/catalog generations; make each mention's candidates explicit and bounded.

**Non-Goals:** Historical index retention, cross-user embedding deduplication, distributed coordination across hosts, automatic cloud-independent semantic search, and incremental indexing.

## Decisions

### Cloud embeddings through LangChain

Add compatible `langchain-openai`, `langchain-community`, and `faiss-cpu` dependencies through Poetry. Use `OpenAIEmbeddings` with explicit `base_url=https://foundation-models.api.cloud.ru/v1`, `model=ai-sage/Giga-Embeddings-instruct-3B`, and secret `API_KEY`. Disable OpenAI-specific token preprocessing (`check_embedding_ctx_length=False`) to send strings; use float encoding if required by the endpoint. Validate endpoint compatibility with a small opt-in smoke request during implementation, including document batches and query embedding. No live-cloud request is required in automated tests.

Expose dedicated settings for URL, model, secret, batch size (initially 32), timeout (60 seconds), bounded retries (2), index directory, and positive retrieval k (initially 10). These defaults are proposed tuning choices, not established quality targets. Keep client initialization lazy so V1 does not require the cloud key. Validate returned vectors for count, consistent dimension, finite values, and nonzero norm before publication. Query and document embeddings use the same model and normalization. Model instructions, if required by the provider, must be represented in the embedding fingerprint.

Alternatives: direct OpenAI SDK would work but bypass the requested LangChain interface; local embedding models add runtime and model distribution requirements.

### Two exact indexes and explicit per-mention candidates

Use separate normalized `IndexFlatIP` indexes for Inputs and Outputs through the LangChain FAISS wrapper. Normalized dot product implements cosine ranking; exact search suits approximately 1,000 names without approximate-index tuning. Embed canonical names, preserve each row as a distinct Document with its existing IN/OUT ID and original name. Empty catalogs use an explicit empty marker without cloud requests.

After extraction, batch embeddings for nonempty mentions and search each mention against its corresponding index with k capped by catalog size. Supply ranked candidates grouped by mention to the matching LLM. Update prompts/adapters to enforce membership in that mention's candidate list, preserve duplicate identities, and retain numeric clarification. Remove the gold-only experiment filter. The clarification fallback must also use the mention's retrieved list instead of expanding to the full catalog. No score cutoff or MMR initially: thresholds require evaluation and would otherwise discard useful candidates. Scores are similarity values, not confidence probabilities.

Alternative: a union of all candidates in the current prompt is simpler but allows a mention to select candidates retrieved only for a different mention.

### Durable user-scoped generations

Use a configurable root, default `data/faiss` on host and `/app/data/faiss` in Docker with a dedicated named volume. Derive safe user directory keys from trusted user identity. Store immutable generation directories containing the two FAISS files, JSON catalog/ID mappings, and a manifest. Use native FAISS file persistence plus JSON to reconstruct LangChain's docstore; avoid pickle deserialization.

The manifest records SHA-256 of original uploaded bytes, hash of the corrected indexed workbook, filename/reference, embedding URL/model/preprocessing fingerprint, vector dimension, normalization, format version, and generation ID. Hash original bytes before correction, since correction can rewrite ZIP metadata. Reuse requires the current user's original hash and complete compatible artifacts. A changed provider/model/preprocessing or format version forces a rebuild on upload. Changing only k does not. Provider changes behind an unchanged model name require an explicit configured embedding revision bump.

An atomic per-user active-manifest replacement selects the generation. Restart loads this manifest and restores the uploaded-file reference into the existing agent store before V2 tools run; otherwise the in-memory store would lose access despite valid persisted vectors. Retain the associated corrected workbook on the existing persistent `/tmp` volume using a unique generation filename compatible with existing calculation-tool path lookup. Validate artifact integrity on load; missing/corrupt artifacts require reupload/rebuild and never silently use stale data. Resolver calls do not unexpectedly rebuild full document embeddings.

Alternative: LangChain `save_local/load_local` is convenient but includes pickled metadata; memory-only persistence loses the agreed cost savings after restart.

### Transactional V2 upload lifecycle

Introduce V2 upload orchestration that reuses validation/correction helpers but does not call the current destructive V1 handler and then attempt indexing. V1 keeps its behavior; extracting shared pure helpers is allowed without changing its contract. Stage the uploaded workbook under a unique filename and hash original content, then reuse a valid current generation or correct/read/embed/build into staging. Preserve the existing success response fields; HTTP 424 reports preparation failure.

Keep publication serialized per user and consistent with V2 invocations using that user's workbook. Support file locks for multiple workers on one host/shared filesystem; independent hosts are outside scope. A failed preparation never updates active references or overwrites the old workbook. Publish only after all files validate, then refresh the agent-store reference. The persisted active manifest is authoritative and repairs an interrupted store refresh after restart. Searches pin one immutable generation; defer obsolete generation deletion until readers finish. Remove partial staging directories and obsolete indexes under the same lifecycle control. Reuploads reuse only the current generation; reverting to an older deleted file rebuilds it.

The upload handler accepts `.xls` while the catalog reader uses openpyxl. Preserve accepted extensions by converting staged `.xls` to `.xlsx` with LibreOffice before extraction; hash the original `.xls` for reuse. Both formats must reach a readable catalog before success.

### Bind clarification to the generation

Checkpoint only generation identity and JSON candidate snapshots, not FAISS/client objects. Before resolving or resuming, compare the checkpoint generation to the active manifest. On mismatch discard the stale choices and re-extract/retrieve from the original request against the active generation; do not interpret the submitted option against newly numbered candidates. Same-byte reuse keeps the generation and valid pending choices. V2 detects pending resume as today, with a generation guard before applying choices.

## Risks / Trade-offs

- [Correct candidate outside top-k] -> configurable k, record candidate lists and evaluate recall on representative Russian/English mentions before tuning.
- [Cloud outage or limits] -> bounded batches/retries and timeouts; preserve old active generation on upload failure. Query embeddings still need cloud access.
- [Cold restart loses in-memory workbook lookup] -> restore from the durable active manifest and verify the referenced workbook exists.
- [Concurrent replacement or crash] -> immutable staging, atomic active pointer, per-user locks, generation-pinned reads, and cleanup after readers finish.
- [V1 overwrites its own workbook reference] -> V2 uses its persisted generation reference and restores it for V2 tool execution; incompatible data must not be used. Concurrent V1/V2 access is outside this V2-only feature's coordination guarantee.
- [Spec overlap with the completed unsynced resolver change] -> preserve its ID/clarification semantics; archive/sync that change before archiving this one.

## Migration Plan

Install the new dependencies, provide Cloud.ru credentials, configure the persistent index volume alongside the existing workbook volume, then deploy. Existing users reupload once to establish a generation. Thereafter restart and identical reupload reuse stored document vectors. Rollback restores previous V2 routing/resolution and leaves persistent artifacts unused; no client payload migration is required.

## References

- [LangChain OpenAIEmbeddings implementation](https://github.com/langchain-ai/langchain/blob/master/libs/partners/openai/langchain_openai/embeddings/base.py): custom endpoint and raw-string preprocessing settings.
- [LangChain FAISS integration](https://github.com/langchain-ai/langchain-community/blob/main/libs/community/langchain_community/vectorstores/faiss.py): vector-store wrapper, normalization and persistence behavior.
