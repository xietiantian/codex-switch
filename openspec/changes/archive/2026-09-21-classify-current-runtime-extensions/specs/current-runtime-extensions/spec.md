## ADDED Requirements

### Requirement: Current image extensions have exact common-contract evidence
The system SHALL classify a reviewed image file-reference extension as optional
only for its exact direction, method and normalized schema pair, and SHALL
prove the remaining URL/common request contract is compatible.

#### Scenario: Known file-reference branches outside core acceptance
- **WHEN** the current schema pair differs only by reviewed image references
- **AND** the supported core acceptance contract does not depend on that extension
- **THEN** coverage identifies image_file_reference and the optional queue records it
- **AND** common request semantics remain required and payloads are not modified.

#### Scenario: Extension becomes required or evidence changes
- **WHEN** the extension is observed, the schema changes, coverage is missing,
  or the common request contract is incompatible
- **THEN** parity remains unhealthy and candidate promotion is blocked.

### Requirement: Feature and additive-request classifications are bounded
The system SHALL accept only explicitly reviewed feature-state pairs and the
exact candidate-only client request, retaining their optional warnings.

#### Scenario: Reviewed feature state without a core dependency
- **WHEN** a feature matches its reviewed stage, default and effective-state pair
- **AND** the core acceptance contract does not require that feature
- **THEN** the difference is optional and remains visible in the queue.

#### Scenario: Feature changes or becomes required
- **WHEN** a reviewed feature changes stage, default, effective state or presence,
  or becomes an observed core dependency
- **THEN** it is not accepted by the optional classification.

#### Scenario: Exact additional backend request
- **WHEN** thread/rollback exists only as the reviewed candidate client request
- **AND** the core contract does not invoke it
- **THEN** it is classified as an optional backend extension
- **AND** changed schemas, other methods or other message directions remain blocked.

### Requirement: Optional classification preserves final core verification
The system SHALL version changed acceptance policy and retain required native
core and typed-v2 probe evidence, fingerprint revalidation and atomic promotion.

#### Scenario: Final evidence is complete
- **WHEN** exact classifications, both required probes and current identities pass
- **THEN** the final receipt binds the same acceptance contract and optional queue.

#### Scenario: Stale policy or failed core evidence
- **WHEN** a receipt uses the previous policy, a required probe fails or an input drifts
- **THEN** reuse or promotion fails and the previous generation is preserved.

#### Scenario: Asynchronous native core conversation
- **WHEN** the app-server handles probe requests asynchronously
- **THEN** the probe waits for each reply before sending the next request or EOF
- **AND** the existing total timeout, output bounds and process cleanup apply.

#### Scenario: Typed evidence binds the actual child
- **WHEN** the typed probe completes
- **THEN** native app-server events and thread/read bind exactly one v2 explorer
  child to the parent, task path and completed turns
- **AND** exact child and parent markers occur in that order
- **AND** missing, duplicate, mismatched or failed evidence remains unhealthy.
