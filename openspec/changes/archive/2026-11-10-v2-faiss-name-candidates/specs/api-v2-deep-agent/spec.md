# Spec Delta

## MODIFIED Requirements

### Requirement: V2 reuses the existing upload handler
The service SHALL expose upload at `/api/v2/upload`, reuse workbook validation and correction behavior, and exclude that route from the V2 OpenAPI schema. V2 upload SHALL prepare persistent searchable catalogs before reporting success, preserving the existing success response fields. V1 upload behavior SHALL remain unchanged.

#### Scenario: V2 upload
- **WHEN** a client uploads a workbook through `/api/v2/upload`
- **THEN** the existing workbook validation and correction behavior is used
- **AND** the route is omitted from the generated API schema
- **AND** the uploaded workbook's searchable catalogs are ready before success is returned

#### Scenario: Index preparation failure
- **WHEN** catalog preparation or document embedding fails
- **THEN** V2 upload returns HTTP 424 using the existing failure response shape
- **AND** the previous active workbook and indexes remain available
