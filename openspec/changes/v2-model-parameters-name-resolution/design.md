# Design

## Context

V2 builds a Deep Agent from the existing Excel tools and uses the shared agent checkpointer. The uploaded workbook is already stored per user; v1 demonstrates catalog extraction from the workbook's Inputs and Outputs sheets. The V2 endpoint accepts a message and returns a ZIP response, and its request/response contract cannot change in this work.

## Goals / Non-Goals

**Goals:**
- Give the V2 agent a dedicated name-resolution LangGraph subagent invoked through Deep Agents' `task` tool.
- Resolve Inputs and Outputs concurrently against catalogs built from the current user's workbook.
- Pause for user selection when matching is ambiguous and continue the same V2 conversation using its existing message field.
- Return canonical catalog names only after resolution; do not run calculations in the resolver.

**Non-Goals:**
- Changing FastAPI request or response schemas, endpoint paths, or ZIP format.
- Changing V1 name-resolution behavior.
- Calculating or modifying workbook values as part of resolution.

## Decisions

### Catalog content and identifiers

Build two catalogs from the uploaded workbook's Inputs and Outputs sheets. Include every row whose `Наименование` cell contains a non-empty name, regardless of whether year values are populated. Identify each entry with its sheet kind and one-based Excel row number, such as `IN:42` or `OUT:57`. Resolve the selected ID back to the canonical name from that catalog rather than trusting a model-generated name. Catalog IDs are valid for the catalog snapshot built for the current workbook; rebuild catalogs when a new workbook is uploaded.

Reuse the existing workbook mapping conventions where practical, while ensuring catalog construction reads names and row numbers without opening LibreOffice or triggering calculations. Output section headers with empty names are omitted by the non-empty-name rule.

### Parallel resolution workflow

Implement the resolver as a compiled LangGraph workflow exposed to the V2 Deep Agent as a specialized `CompiledSubAgent`. Its graph extracts the requested names and launches independent input and output resolution nodes in parallel. Each node receives only its corresponding catalog and resolves names to catalog entry IDs. A join step validates IDs and converts them to canonical names.

The completed subagent returns a JSON object with `inputs` and `outputs` arrays. Each item explains the mapping so the main agent can connect it to the user's wording and understand whether it can proceed. A resolved item contains the mention as it appeared in the request, the exact canonical catalog name, and a short matching rationale. For example:

```json
{
  "inputs": [
    {
      "mentioned_name": "copper price",
      "resolved_name": "Цена меди (USD/т)",
      "status": "resolved",
      "reason": "Это входной параметр модели для цены меди."
    }
  ],
  "outputs": [
    {
      "mentioned_name": "operating profit",
      "resolved_name": "EBITDA",
      "status": "resolved",
      "reason": "В каталоге модели EBITDA соответствует запрошенному операционному показателю."
    }
  ]
}
```

`status` is `resolved` only after a unique match or user selection is validated against the catalog. For a name explicitly rejected by the user, return an item with `resolved_name: null`, `status: "unresolved"`, and a short reason; do not invent a replacement. An empty category is represented by an empty array. Do not include catalog IDs, years, ranges, or calculated values in the completed result. Years and ranges may be extracted elsewhere by the main agent from conversation context.

When a name has no unique match, the relevant branch raises a LangGraph interrupt containing the original mention and a bounded list of candidates. The user-facing message assigns each candidate a numeric option and includes a numbered "none of these" option. This interrupt payload is an internal control structure, not the completed subagent result. The workflow accepts only the displayed option number or numbers; it does not use an LLM or free-text matching to interpret clarification answers. A deterministic parser validates that every number is syntactically valid, in range, and applicable to the requested clarification, then maps it to the stored catalog candidate ID (or none-of-these). If the answer is invalid, incomplete, or ambiguous, the workflow remains interrupted and asks the user to provide valid numbers for the unresolved choices. Input and output branches may both need clarification; the workflow must preserve all pending interrupts and associate each answer with the correct mention and branch. Number options are scoped to a single clarification message so they cannot be confused across turns.

### Deep Agents integration and checkpointing

Enable the Deep Agents `task` tool in the V2 harness profile and register only the dedicated name-resolver subagent for this purpose. Disable the automatically provided general-purpose subagent so delegation stays scoped to name resolution. The compiled resolver must use a compatible `messages` state channel and inherited parent checkpointer behavior so interrupts propagate through the parent graph. Keep the parent graph's existing per-user/per-session `thread_id` as the resume key.

### Resume without changing the FastAPI contract

The existing endpoint always accepts the user's text in `message` and returns the existing ZIP response. On a new invocation with a thread containing a pending interrupt, treat `message` as the user's clarification and invoke the parent graph with `Command(resume=message)` instead of starting a fresh user-message turn. On an ordinary turn, continue passing the message as a `HumanMessage`. Use the existing checkpoint to distinguish the pending-resume path. The API models, route, and response body format remain unchanged.

The agent must state the clarification question, numbered candidate labels, and the expected numeric reply format in the normal response content so the existing ZIP text entry communicates what the user should send in the next `message`. A clarification response remains wrapped in the same ZIP contract.

### Tool exposure

The resolver does not need calculation tools. Keep the subagent's available tools empty unless a narrowly scoped catalog-reading tool is needed. Catalog retrieval should pass the current workbook catalog from trusted request state/runtime context rather than ask the model to supply a path or user identifier. It must not expose general file-system or execution capabilities.

## Risks / Trade-offs

- [Parallel branches can request multiple clarifications at once] -> Include mention and candidate IDs in each interrupt, preserve interrupt identifiers, and present all pending choices clearly in the ZIP text response.
- [Numeric choices may be confused when multiple names need clarification] -> Number choices within a single clarification message, include the mentioned name beside each choice, and require a numeric selection for each unresolved question.
- [A workbook can change while a clarification is pending] -> Bind candidate IDs and names to the checkpointed catalog snapshot; if the workbook identity/version no longer matches, discard the stale selection and rebuild the catalog before continuing.
- [A message might be mistaken for a resume answer] -> Resume only when the parent thread has pending interrupts; otherwise treat `message` as a normal user turn.
- [Model selection may not be semantically unique] -> Require catalog membership validation and interrupt when the resolver cannot identify a single sufficiently supported match; never fabricate a name.

## Migration Plan

No client migration is required. Deploy the new resolver and workflow handling while preserving the existing request and ZIP response formats. Roll back by restoring the previous V2 agent construction and invocation logic; existing API clients continue using the same contract.
