# custom-model-catalog-parity Specification

## Purpose

Use explicit custom model catalogs as provider-owned evidence while preserving runtime compatibility, provenance, and source integrity requirements.

## Requirements

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
- **THEN** preparation selects each runtime model source from a valid cache or, when absent, its offline bundled catalog
- **AND** metadata differences pass through the existing comparison policy
- **AND** runtime compatibility and behavior probes remain required.

#### Scenario: Invalid explicit catalog never falls back
- **WHEN** an explicit catalog is empty, non-string, unsafe, malformed or lacks the active model
- **THEN** preparation rejects that configuration without treating it as absent.

#### Scenario: Default model evidence is invalid or changed
- **WHEN** a present runtime cache is unsafe or malformed, an absent cache cannot be replaced by valid bundled evidence, or selected evidence changes during preparation or before promotion
- **THEN** preparation or revalidation fails without changing the bound binary or original configuration.

#### Scenario: Managed default overlay remains comparable
- **WHEN** a default-model candidate is prepared again from its managed overlay
- **THEN** its default-model origin is retained and official comparison still runs, selecting cache or bundled evidence again for each runtime
- **AND** invalid or missing current-policy source-kind provenance is rejected.

#### Scenario: Legacy custom provenance remains usable
- **WHEN** an older custom-only manifest has no source-kind field
- **THEN** preparation resolves its original custom catalog and regenerates current-policy evidence.

#### Scenario: Missing default caches use corresponding bundled catalogs
- **WHEN** either or both default runtime caches are absent and the corresponding binary supports an offline complete model export
- **THEN** preparation compares that binary's exported metadata with the other selected runtime source
- **AND** user cache/configuration files are not created or changed by collection.

#### Scenario: Export failure never bypasses comparison
- **WHEN** offline export is unsupported, fails, times out, exceeds output bounds, is malformed or lacks the active model
- **THEN** preparation fails before promotion without substituting the other runtime's catalog or partial model-list fields.

#### Scenario: Bundled evidence survives transactional publication
- **WHEN** bundled default sources pass all compatibility and behavior checks
- **THEN** their snapshots and binary-bound provenance publish with the existing transaction
- **AND** rollback restores the prior source artifacts together with the bound binary and configuration.

#### Scenario: Repeated default updates collect current evidence
- **WHEN** a generated overlay originated from a cache or bundled default source
- **THEN** a later update validates its origin and selects fresh evidence from current caches or corresponding binaries
- **AND** the overlay is never reclassified as an explicit custom catalog.
