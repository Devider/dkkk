# v2-name-resolution Specification

## Purpose

Resolve user-mentioned workbook input parameters and output metrics to exact names from the uploaded model's catalogs. When a name cannot be matched unambiguously, obtain the user's selection before returning resolved names.

## Requirements

### Requirement: Resolve input and output names from workbook catalogs

The system SHALL extract names of requested input parameters and output metrics and resolve each against the corresponding catalog from the user's uploaded workbook. It SHALL obtain mention-specific semantic candidates before requesting structured matching results from the LLM, as specified by `v2-catalog-retrieval`.

#### Scenario: Names resolve unambiguously

- **WHEN** the request mentions input parameters and output metrics that each have one validated unambiguous catalog match
- **THEN** the resolver returns a JSON object with `inputs` and `outputs` arrays
- **AND** each item identifies the original mention, canonical name, status, rationale, and resolution source
- **AND** it performs input and output resolution concurrently
- **AND** it does not execute workbook calculations

#### Scenario: Catalog entry identity

- **WHEN** the system builds an input or output catalog
- **THEN** it includes every row with a non-empty name in the relevant sheet
- **AND** it identifies the entry using the sheet kind and its one-based Excel row number

#### Scenario: Empty category

- **WHEN** no input parameters or no output metrics are mentioned
- **THEN** the corresponding result array is empty

### Requirement: Preserve structured matching data

The resolver SHALL preserve the structured LLM matching response for each mention through conversion and clarification. Its completed result SHALL retain validated proposed equivalents as `matched_names`, validated candidates as `candidate_names`, and the original matching explanation as `explanation`. A user-facing decision reason SHALL NOT replace that explanation.

#### Scenario: Convert an LLM match

- **WHEN** a structured matching response is converted to resolver state
- **THEN** the response's proposed equivalent name, ID, candidate names, ambiguity flag, and explanation remain associated with that mention
- **AND** accepted canonical names are validated against that mention's retrieved candidates

#### Scenario: Return the completed result

- **WHEN** the resolver sends its completed result to the main agent
- **THEN** each item includes `mentioned_name`, `resolved_name`, `status`, `reason`, `matched_names`, `candidate_names`, `explanation`, and `resolution_source`
- **AND** the result contains canonical names rather than catalog IDs, calculated values, years, or ranges
- **AND** each `matched_names` entry is a validated proposed equivalent from the LLM response, which can differ from the user's final selection

### Requirement: Keep repeated mentions separate

The resolver SHALL associate each extracted mention occurrence with its own structured matching result and clarification choice. Repeated mentions SHALL NOT merge separate LLM matches or consume the same response item more than once.

#### Scenario: Repeated wording with distinct matches

- **WHEN** two extracted mentions have identical wording but their LLM response items identify different equivalents
- **THEN** the resolver preserves two independent matches in mention order
- **AND** it does not turn them into two copies of a combined ambiguous candidate list

### Requirement: Return unambiguous matches without clarification

The resolver SHALL return a match automatically only when the LLM marks it unambiguous and validation identifies exactly one candidate row. Such an item SHALL have `status=resolved`, the exact canonical `resolved_name`, and `resolution_source=llm`.

#### Scenario: Unique validated equivalent

- **WHEN** the LLM returns `is_unambiguous=true` and exactly one candidate row is validated
- **THEN** the resolver returns the canonical equivalent to the main agent without asking the user to select it
- **AND** the original mention and matching explanation remain available

### Requirement: Clarify ambiguous catalog matches

The resolver SHALL interrupt for numeric user selection when validated candidates exist but the match is not unambiguous. It SHALL offer only the LLM's validated `candidate_names` in their proposed order, plus a numbered none-of-these option. It SHALL validate replies deterministically without LLM or free-text interpretation.

#### Scenario: Candidate order differs from retrieval order

- **WHEN** the LLM proposes ambiguous candidates in an order different from semantic retrieval
- **THEN** the displayed numeric choices follow the LLM's candidate order
- **AND** a preferred equivalent or retrieved row absent from those candidates is not added as an extra choice

#### Scenario: One candidate still needs confirmation

- **WHEN** the LLM returns `is_unambiguous=false` and one valid candidate
- **THEN** the resolver interrupts for the user's selection instead of automatically resolving the name

#### Scenario: User selects a candidate

- **WHEN** name resolution is interrupted with candidate catalog entries
- **AND** the user replies with a valid candidate selection in the same conversation thread
- **THEN** the workflow resumes and records the selected canonical name for the corresponding mention
- **AND** the result has `status=resolved` and `resolution_source=user`
- **AND** its `reason` explicitly states that the user selected and confirmed that equivalent
- **AND** `matched_names`, `candidate_names`, and the original `explanation` remain available

#### Scenario: Multiple pending choices

- **WHEN** several input or output mentions require clarification
- **THEN** the resolver presents their numbered candidate lists together and requests one selection per mention separated by commas
- **AND** each selection is applied only to its corresponding mention and catalog category

#### Scenario: Invalid or incomplete numeric reply

- **WHEN** the user reply contains a non-numeric, out-of-range, ambiguous, or incomplete selection
- **THEN** the workflow remains interrupted and asks for numeric selections for the unresolved choices
- **AND** it does not infer a selection using an LLM

### Requirement: Preserve unresolved outcomes without inventing equivalents

The resolver SHALL return an unresolved item when no valid equivalent exists or the user rejects all offered candidates. The item SHALL retain its original mention and explanation, use `resolved_name=null`, `status=unresolved`, and `resolution_source=null`, and SHALL NOT invent a replacement.

#### Scenario: No suitable candidate found

- **WHEN** matching produces no valid candidate for a mention
- **THEN** the resolver returns that mention as unresolved without interrupting solely to offer unrelated retrieved rows
- **AND** it preserves the matching explanation instead of replacing it with a generic no-match message

#### Scenario: User rejects the candidates

- **WHEN** the user selects the none-of-these option
- **THEN** the result leaves that mention unresolved and records the rejection in `reason`
- **AND** the originally proposed names, candidate names, and matching explanation remain available
