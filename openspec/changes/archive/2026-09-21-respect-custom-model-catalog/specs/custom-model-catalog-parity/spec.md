## ADDED Requirements

### Requirement: Custom model evidence is provider-owned
The system SHALL use the validated explicit original model catalog as the
authority for a custom model and SHALL NOT equate it with an official model by slug.

#### Scenario: Custom catalog without an official model cache
- **WHEN** a candidate has a valid explicit provider and original model catalog
- **THEN** parity preparation does not require or read official models_cache.json
- **AND** it continues runtime compatibility validation.

#### Scenario: Matching or unknown official slug
- **WHEN** the custom model shares an official slug or has no official counterpart
- **THEN** official model metadata does not constrain custom metadata
- **AND** absent, malformed, symlinked or changed official caches are not inputs.

#### Scenario: Managed overlay preserves custom-source identity
- **WHEN** the effective catalog is a previously managed overlay
- **THEN** preparation resolves and validates its recorded original source
- **AND** missing or inconsistent source provenance is rejected.

### Requirement: Skipped comparison is explicit and bounded
The system SHALL record official-model comparison as not applicable for custom
catalogs and SHALL preserve runtime compatibility and source integrity checks.

#### Scenario: Durable applicability evidence
- **WHEN** custom-catalog preparation succeeds
- **THEN** the receipt contains a stable informational custom-catalog finding
- **AND** serialization and revalidation preserve that finding
- **AND** the versioned policy rejects receipts made under the previous policy.

#### Scenario: Runtime and custom-source failures remain blocking
- **WHEN** catalog shape, active entry, source provenance, source identity,
  binary/config identity, required protocol coverage or bounded behavior is invalid
- **THEN** preparation or revalidation fails before promotion
- **AND** the existing bound binary and original configuration are preserved.

#### Scenario: Ordinary model-policy callers retain comparison
- **WHEN** policy evaluation has no explicit custom-catalog context
- **THEN** existing official/internal model metadata differences remain classified.
