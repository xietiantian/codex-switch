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

#### Scenario: Missing catalog uses default model comparison
- **WHEN** the profile has no model_catalog_json key
- **THEN** preparation uses the internal runtime model cache and official reference cache
- **AND** metadata differences pass through the existing comparison policy
- **AND** runtime compatibility and behavior probes remain required.

#### Scenario: Invalid explicit catalog never falls back
- **WHEN** an explicit catalog is empty, non-string, unsafe, malformed or lacks the active model
- **THEN** preparation rejects that configuration without treating it as absent.

#### Scenario: Default model evidence is missing or changed
- **WHEN** either required runtime cache is missing, unsafe, malformed or changes during preparation or before promotion
- **THEN** preparation or revalidation fails without changing the bound binary or original configuration.

#### Scenario: Managed default overlay remains comparable
- **WHEN** a default-cache candidate is prepared again from its managed overlay
- **THEN** its recorded runtime-cache origin is retained and official comparison still runs
- **AND** invalid or missing current-policy source-kind provenance is rejected.

#### Scenario: Legacy custom provenance remains usable
- **WHEN** an older custom-only manifest has no source-kind field
- **THEN** preparation resolves its original custom catalog and regenerates current-policy evidence.
