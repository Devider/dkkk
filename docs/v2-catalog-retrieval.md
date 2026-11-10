# V2 semantic catalog retrieval

V2 uses Cloud.ru via LangChain OpenAIEmbeddings. Initialization is lazy: V1 startup needs no cloud credentials. Set `EMBEDDING_API_KEY` (or the legacy `API_KEY`) in the deployment environment; it is never stored in index manifests.

| Variable | Default |
| --- | --- |
| V2_EMBEDDING_URL | https://foundation-models.api.cloud.ru/v1 |
| V2_EMBEDDING_MODEL | ai-sage/Giga-Embeddings-instruct-3B |
| V2_EMBEDDING_REVISION | 1 |
| V2_EMBEDDING_BATCH_SIZE | 32 |
| V2_EMBEDDING_TIMEOUT | 60 seconds |
| V2_EMBEDDING_RETRIES | 2 |
| V2_INDEX_DIR | data/faiss (host), /app/data/faiss (Docker) |
| V2_RETRIEVAL_K | 10 |

The client sends raw strings with `check_embedding_ctx_length=False` and `encoding_format=float`. Document batches have `input: ["Цена меди", "Выручка"]`; a query uses `input: ["copper price"]`. Returned vectors must have the expected count, consistent dimension, finite values and nonzero norm. Both paths normalize vectors. Bump the revision whenever the provider changes vector semantics behind a stable model name or model instructions change. URL/model/revision/preprocessing changes invalidate reuse; k changes do not.

Opt-in compatibility probe (performs paid/network requests only with EMBEDDING_API_KEY set):

```sh
PYTHONPATH=src poetry run python scripts/smoke_v2_embeddings.py
```

Automated tests use deterministic embeddings or mocked HTTP; they need no Cloud.ru credentials.

## Persistent deployment and migration

Docker Compose mounts `app_faiss` at `/app/data/faiss` and keeps corrected generation workbooks in `app_tmp:/tmp`. Both volumes must survive a restart. On the host, configure `V2_INDEX_DIR` to a persistent writable directory and preserve the system temporary directory. The active manifest restores the tool store's workbook reference before V2 invocation. A missing workbook or missing/corrupt/incompatible index requires reupload; searching never rebuilds document embeddings implicitly.

Existing users must upload once via `/api/v2/upload` after deployment. Identical original bytes reuse only the current valid generation (SHA-256 before correction); changed bytes or URL/model/revision rebuild before success. Reverting to an older file rebuilds it because retired indexes are deleted. Failed preparation returns HTTP 424 and leaves the old active pair intact. Changing k alone preserves reuse and pending clarification.

To reset one user's catalogs, stop the service and remove that user's SHA-256-named directory under the index root and its referenced `v2_<generation>.xlsx` from the workbook volume, then restart and reupload. For a full reset remove the contents of both dedicated volumes while stopped; this discards uploaded workbooks and indexes. Do not remove volumes during active requests.

## Candidate resolution and clarification

After extraction, V2 embeds nonempty mentions in category batches and retrieves up to `V2_RETRIEVAL_K=10` candidates separately from Inputs and Outputs. All named rows participate, including duplicate names and rows without year values. Exact normalized inner product ranks by cosine similarity; there is initially no threshold or MMR. Scores are similarities, not confidence probabilities. Each mention's prompt and ID validation use only that mention's candidates. The matching LLM may reject every retrieved candidate; numeric clarification uses only validated candidates from that list.

Checkpointed candidate snapshots carry generation identity, not FAISS objects. Replacing a workbook while clarification is pending discards the old submitted number and resolves the original request against the current generation, producing new clarification if needed. Reuploading identical bytes preserves the generation and pending choices. V2 invocations pin the generation until tools finish; replacement waits for active readers before retiring old files. File locks cover workers on a single shared host/filesystem; independent hosts and concurrent V1/V2 mutations are outside this coordination scope.

Persisted document vectors avoid document embedding calls on restart and identical uploads. Nonempty queries still require Cloud.ru calls, including after restart. Retrieval quality and top-k recall depend on the provider/model and require representative evaluation before tuning.

## Recall evaluation

Run the opt-in RU/EN evaluation against 24 canonical rows and 8 mentions (four queries per category) without a matching LLM:

```sh
PYTHONPATH=src poetry run python scripts/evaluate_v2_recall.py --output /tmp/v2-recall.json
```

The report records candidate row IDs and recall@k independently of final LLM decisions. Default k is 10, below the 12 rows in each category. The fixture includes metal prices, FX, financial outputs and similar distractors. It is a small diagnostic sample, not a quality target for the full workbook catalog.

Offline deterministic embeddings yield 4/8 (recall@10=0.5), validating category filtering, batching, normalization, bounded retrieval and scoring/reporting mechanics. The fake model recognizes copper/revenue only; tied distractors exclude gold/EBITDA and demonstrate recorded misses. Those vectors encode a tiny alias oracle; this result is **not an estimate of Cloud.ru semantic quality**. Live Cloud.ru evaluation on 2026-11-10 returned **8/8 recall@10=1.0**, with every expected row ranked first. The batch/query smoke request also passed with 2048-dimensional vectors. Full candidate lists are recorded in [v2-catalog-recall.json](v2-catalog-recall.json). This small sample checks RU/EN aliases for copper, gold, revenue and EBITDA; it does not establish recall for the full production catalog, duplicate-name disambiguation or domain-specific abbreviations. Evaluate representative production queries before tuning k.
