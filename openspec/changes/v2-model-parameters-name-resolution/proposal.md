# Proposal

## Why

API v2 currently exposes Excel tools but has no dedicated name-resolution workflow that maps user wording to the workbook's canonical input and output names. Cross-language matching can select the wrong metric, so the agent needs catalog-based candidate selection and a way to ask the user when a match is ambiguous.

## What Changes

- Add a v2 name-resolution subagent that extracts input parameters and output metrics from the request and resolves them against workbook catalogs.
- Resolve inputs and outputs in parallel workflow nodes.
- Ask the user to choose among catalog candidates when a name cannot be resolved unambiguously, then resume the existing conversation.
- Preserve the existing FastAPI request and response contract.

## Capabilities

### New Capabilities
- `v2-name-resolution`: Resolve user-mentioned input and output names against the uploaded workbook catalog, including clarification for ambiguous matches.

### Modified Capabilities
- `api-v2-deep-agent`: V2 agent workflow supports name-resolution clarification while preserving the HTTP contract.

## Impact

- V2 agent construction and prompt, catalog construction from the uploaded workbook, LangGraph workflow and checkpoint resume handling.
- Existing FastAPI endpoint schema and response format remain unchanged.
