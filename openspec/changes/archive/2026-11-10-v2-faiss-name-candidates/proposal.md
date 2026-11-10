# Proposal

## Why

V2 name resolution currently sends workbook catalogs to the matching LLM, and Inputs additionally has a temporary gold-only filter. Semantic candidate retrieval will reduce prompt size while covering the full catalog; persisting document embeddings will avoid paying for them again after restarts or identical uploads.

## What Changes

- Build separate Inputs and Outputs FAISS indexes during V2 workbook upload using Cloud.ru embeddings through LangChain.
- Persist user-scoped indexes, canonical row identities, catalog metadata, and workbook identity in a configurable Docker volume.
- Reuse indexes for identical uploaded bytes by SHA-256 and embedding configuration; atomically replace the active workbook/index generation for changed files and remove superseded indexes.
- Retrieve configurable top-k candidates per extracted mention before the existing name-resolution LLM and numeric clarification flow; remove the temporary Inputs filter.
- Restore persisted active workbook references after restart and reject stale clarification selections after replacement.

## Capabilities

### New Capabilities
- `v2-catalog-retrieval`: Durable user-scoped workbook indexing, hash reuse, and semantic candidate retrieval for the V2 resolver.

### Modified Capabilities
- `api-v2-deep-agent`: V2 upload prepares the searchable catalog before reporting success while retaining its route and response shape.

## Impact

Touches V2 upload routing, name-resolution graph/prompts, configuration, Poetry dependencies/lock, Docker Compose, documentation, and focused tests. Adds `langchain-openai`, `langchain-community`, and `faiss-cpu`; calls Cloud.ru's embeddings endpoint. The existing completed name-resolution change remains a dependency and is not rewritten.

## Non-goals

V1 behavior, calculation tools, API schema changes, distributed deployments across hosts, incremental row indexing, and retaining historical indexes are outside scope. Query embeddings still require cloud calls; disk reuse avoids document embedding calls only.
