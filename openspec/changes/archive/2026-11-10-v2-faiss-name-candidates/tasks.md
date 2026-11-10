# Tasks

## 1. Embedding integration and settings

- [x] 1.1 Add compatible langchain-openai, langchain-community and faiss-cpu dependencies and update poetry.lock; verify Poetry resolves them and imports work on the host and deployment Python versions.
- [x] 1.2 Add lazy V2 embedding settings/client for Cloud.ru, API_KEY, raw string input, batch size, timeouts, retries and vector validation; verify mocked requests preserve strings and reject invalid vectors without requiring credentials for V1 startup.
- [x] 1.3 Document model/URL/revision settings and an opt-in minimal Cloud.ru smoke request; verify the documented batch/query shapes against the client and run the smoke request only when credentials are available.

## 2. Persistent catalog generations

- [x] 2.1 Build two normalized exact FAISS indexes from workbook catalogs with LangChain Documents and distinct row IDs; add tests for duplicate names, empty catalogs, named rows without values and category-separated retrieval.
- [x] 2.2 Implement original-file SHA-256, embedding fingerprints, native index plus JSON persistence, manifests and integrity validation; verify save/reload produces the same candidates without document embedding calls and detects corrupt/incompatible artifacts.
- [x] 2.3 Implement per-user generation directories, file locks, atomic active manifests and deferred cleanup; verify simultaneous same-user operations never mix generations, failed publication preserves the old generation and other users remain unaffected.
- [x] 2.4 Add the persistent index Docker volume and host path configuration; document the workbook-volume dependency, reset procedure and current-generation-only reuse, and verify compose configuration resolves the mounts.

## 3. V2 upload and restart lifecycle

- [x] 3.1 Introduce V2 staging/upload orchestration reusing workbook validation/correction helpers while preserving V1 behavior; verify existing V1 upload tests and V2 response fields and hidden route remain valid.
- [x] 3.2 Convert staged .xls to readable .xlsx before catalog extraction while hashing original bytes; verify both accepted extensions reach complete searchable catalogs with the correction backend stubbed where appropriate.
- [x] 3.3 Add hash reuse and build-before-publication, update workbook references only after success and return HTTP 424 on preparation failure; verify identical reupload causes zero document embedding calls, changed upload rebuilds, and embedding failure preserves the old workbook/index pair.
- [x] 3.4 Restore the active workbook reference from disk before V2 invocation and coordinate invocations with replacement; verify a fresh service/store can search and invoke workbook tools without reupload or document embedding calls.
- [x] 3.5 Document first-time reupload, missing/corrupt-artifact recovery and upload failure behavior; verify documentation matches V2 integration test outcomes.

## 4. Resolver candidate retrieval and clarification

- [x] 4.1 Add per-mention batched query embeddings and category-specific top-k retrieval after name extraction, and remove INPUT_CATALOG_EXPERIMENT_FILTER; verify k limits, empty-list handling, full-catalog coverage and independent input/output branches with deterministic embeddings.
- [x] 4.2 Update name-resolution prompts/adapters and clarification candidates to use mention-specific lists and validate IDs against those lists; verify out-of-list model outputs cannot resolve, duplicate row IDs remain distinguishable and unrelated catalog rows are absent from prompts.
- [x] 4.3 Bind checkpointed candidates to generation identity and restart resolution of the original request when stale choices resume; verify old option numbers are never applied to new rows and identical-file reuse preserves valid pending clarification.
- [x] 4.4 Document k=10, cosine ranking, no initial threshold/MMR, and remaining query embedding calls; verify examples and settings agree with resolver behavior.

## 5. Integration checks

- [x] 5.1 Run focused V2 upload/router/resolver and persistence tests with fake embeddings, including replacement during clarification and restart with persistent artifacts; verify no automated test requires Cloud.ru credentials.
- [x] 5.2 Run the required Ruff fix then format sequence and inspect the change diff for unintended edits; verify lint passes for changed code and V1 contracts remain covered.
- [x] 5.3 Evaluate top-k candidate recall on a small representative Russian/English catalog fixture and record results and limitations; verify the correct row appears among candidates independently of the matching LLM's final choice.

## Validation notes

- Focused V2 persistence/upload/router/resolver/policy tests and V1 upload contracts: 56 passed, 3 existing LibreOffice integration tests skipped (backend unavailable).
- Wider unit run: 214 passed, 5 existing failures in `tests/unit/test_correct_workbook.py` because `_FakeXL.__init__` does not accept `logger`. That test and its production correction module are unchanged.
- Ruff fix/format sequence completed; unrelated automatic edits removed. Changed-code lint, `git diff --check`, Poetry check, Compose configuration and strict OpenSpec validation pass. Pylint: 9.51/10.
- New dependency imports verified on host Python 3.14 and deployment Python 3.12.
- Task 5.3 live validation: configured EMBEDDING_API_KEY (API_KEY remains a supported fallback); Cloud.ru batch/query smoke passed with dimension 2048. RU/EN fixture: 24 rows, 8 mentions, recall@10=8/8 (1.0), all expected rows ranked first. Results and small-sample limitations are recorded in docs/v2-catalog-recall.json and docs/v2-catalog-retrieval.md. Fake embeddings separately give 4/8, validating that the harness records misses; automated tests remain offline.
