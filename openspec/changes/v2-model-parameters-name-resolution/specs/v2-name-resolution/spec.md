# Spec Delta

## Purpose

Resolve user-mentioned workbook input parameters and output metrics to exact names from the uploaded model's catalogs. When a name cannot be matched unambiguously, obtain the user's selection before returning resolved names.

## ADDED Requirements

### Requirement: Resolve input and output names from workbook catalogs
The system SHALL extract names of requested input parameters and output metrics and resolve each against the corresponding catalog from the user's uploaded workbook.

#### Scenario: Names resolve unambiguously
- **WHEN** the request mentions input parameters and output metrics that each have one unambiguous catalog match
- **THEN** the resolver returns a JSON object with `inputs` and `outputs` arrays
- **AND** each array item identifies the user-mentioned name, its canonical catalog name, a resolution status, and a brief rationale
- **AND** it performs input and output resolution concurrently
- **AND** it performs input and output resolution concurrently
- **AND** it does not execute workbook calculations

#### Scenario: Catalog entry identity
- **WHEN** the system builds an input or output catalog
- **THEN** it includes every row with a non-empty name in the relevant sheet
- **AND** it identifies the entry using the sheet kind and its one-based Excel row number

### Requirement: Clarify ambiguous catalog matches
The system SHALL ask the user to select from catalog candidates by numeric option when a mentioned name has no single unambiguous match. It SHALL validate clarification replies deterministically and SHALL NOT use an LLM or free-text matching to interpret them.

#### Scenario: User selects a candidate
- **WHEN** name resolution is interrupted with candidate catalog entries
- **AND** the user replies with a candidate selection in the same conversation thread
- **THEN** the workflow resumes and returns the selected canonical catalog name

#### Scenario: Invalid or incomplete numeric reply
- **WHEN** the user reply contains a non-numeric, out-of-range, ambiguous, or incomplete selection
- **THEN** the workflow remains interrupted and asks for numeric selections for the unresolved choices
- **AND** it does not infer a selection using an LLM

#### Scenario: No suitable candidate
- **WHEN** the user indicates that none of the offered candidates is suitable
- **THEN** the workflow leaves that name unresolved and does not invent a canonical name
- **AND** the result identifies the original mention, uses a null canonical name, and marks the item unresolved with a brief reason
