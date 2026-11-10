# v2-catalog-retrieval Specification

## Purpose

Provide persistent, user-scoped semantic retrieval of workbook parameter names to bound the candidates supplied to V2 name resolution and reuse document embeddings.

## Requirements

### Requirement: Prepare user-scoped searchable catalogs on upload
The system SHALL prepare separate searchable Inputs and Outputs catalogs during V2 upload, preserving canonical names and sheet-row identities, including duplicate names and named rows without year values. Catalogs SHALL be scoped to the authenticated user.

#### Scenario: Prepare a new workbook
- **WHEN** a user uploads a supported workbook through V2
- **THEN** every named row in Inputs and Outputs is represented in its corresponding catalog before upload succeeds
- **AND** entries retain identities such as IN:42 and OUT:57

#### Scenario: Isolate users
- **WHEN** two users upload different workbooks
- **THEN** replacing or searching one user's catalogs does not access or alter the other user's catalogs

### Requirement: Reuse persisted catalogs by content identity
The system SHALL persist indexes and catalog metadata across service restarts and reuse valid indexes when the uploaded file's SHA-256 and embedding configuration match. Reuse SHALL avoid document embedding requests. Changed embedding configuration or invalid artifacts SHALL prevent reuse.

#### Scenario: Identical upload
- **WHEN** the current user reuploads identical bytes with unchanged embedding configuration
- **THEN** the existing valid indexes are reused without embedding the catalog names again

#### Scenario: Restart
- **WHEN** the service restarts with the persistent data directory intact
- **THEN** it can locate the user's active workbook and search its persisted catalogs without reupload or document embedding requests

#### Scenario: Incompatible or corrupt index
- **WHEN** a new upload finds incompatible, missing, or corrupt index artifacts
- **THEN** it rebuilds them before reporting success instead of reusing invalid vectors

### Requirement: Replace workbook and indexes consistently
The system SHALL publish a new workbook and its matching indexes together only after preparation succeeds. Failed preparation SHALL preserve the previous active generation. After replacement, the previous index SHALL cease serving new searches and its obsolete files SHALL be removed once active readers finish.

#### Scenario: Successful replacement
- **WHEN** a changed workbook's catalogs and embeddings are prepared successfully
- **THEN** new searches use only the new generation and superseded index files are retired

#### Scenario: Embedding failure
- **WHEN** cloud embedding generation fails during upload
- **THEN** upload reports failure and the previous active workbook and catalogs remain usable

### Requirement: Retrieve bounded candidates for each mention
The V2 resolver SHALL retrieve up to a configurable positive top-k candidates for each extracted name from the corresponding current workbook catalog. The matching LLM SHALL receive these candidates associated with their mention. Proposed names and IDs SHALL be validated against that mention's candidate list before a catalog entry is accepted. Duplicate names SHALL remain distinct by ID.

#### Scenario: Multiple mentions
- **WHEN** a request contains several input and output mentions
- **THEN** each mention has its own ranked candidate list from the correct catalog
- **AND** the matching prompt does not contain unrelated full catalogs

#### Scenario: Proposal belongs to another mention
- **WHEN** the matching LLM proposes a name and ID absent from the current mention's candidate list
- **THEN** that proposal does not become an accepted match for the current mention
- **AND** membership in another mention's list does not authorize its use

#### Scenario: Canonical name with an absent or invalid ID
- **WHEN** a proposed canonical name belongs to the current mention's candidate list but its proposed ID is absent or invalid
- **THEN** the resolver retains the name and obtains its candidate IDs from that list
- **AND** it does not discard a valid name solely because the proposed ID cannot be used
- **AND** multiple rows with that name remain separate candidates requiring clarification

#### Scenario: Duplicate names distinguished by a valid ID
- **WHEN** multiple retrieved rows have the same canonical name
- **AND** the LLM returns an unambiguous match with a valid ID and its matching canonical name
- **THEN** the resolver accepts that specific candidate row instead of expanding the match to all equal names

#### Scenario: Empty catalog or mentions
- **WHEN** a catalog or mention list is empty
- **THEN** no embedding request is made for that empty list and the existing unresolved or empty-result behavior is preserved

### Requirement: Preserve clarification safety across replacement
The resolver SHALL retain deterministic numeric clarification and validate selections against their catalog generation. It SHALL reject selections from a superseded generation and request resolution against the current workbook before returning a canonical name.

#### Scenario: Workbook changes during clarification
- **WHEN** a user uploads a changed workbook while a numeric clarification is pending
- **AND** subsequently submits an option number from the old question
- **THEN** the old option is not applied to a row in the new workbook
- **AND** resolution is restarted against the current generation
