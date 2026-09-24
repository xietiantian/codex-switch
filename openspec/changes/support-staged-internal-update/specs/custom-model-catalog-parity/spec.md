## ADDED Requirements

### Requirement: Source provenance survives independent health invalidation
The system SHALL retain a validated original catalog source path, source kind
and digest through managed-overlay capture independently of historical parity
health acceptance. New preparation SHALL validate that source again and obtain
fresh runtime compatibility evidence; retained source provenance SHALL NOT
certify current health.

#### Scenario: Recapture invalidates an old health receipt
- **WHEN** a previously managed custom overlay is recaptured and its old health receipt is invalidated
- **THEN** verified original custom-source path, kind and digest remain available
- **AND** apply revalidates the original source before preparing new parity evidence.

#### Scenario: Overlay origin is missing or inconsistent
- **WHEN** a managed overlay lacks independently verifiable origin or its source/overlay identity disagrees
- **THEN** capture or preparation rejects the ambiguity
- **AND** it does not bless the overlay as a new original custom catalog.

#### Scenario: Default overlay is recaptured
- **WHEN** an overlay originated from runtime cache or bundled default evidence
- **THEN** recapture preserves its default origin
- **AND** preparation collects current cache evidence or the corresponding binary's bundled catalog only when that cache is absent.

#### Scenario: Custom source shares an official slug
- **WHEN** a valid original custom catalog uses a slug also present in the official catalog
- **THEN** custom metadata remains authoritative without official-model comparison
- **AND** binary, source, feature, schema and native behavior validation remain required.

#### Scenario: Overlay changes only the established missing-field rule
- **WHEN** the active model lacks multi_agent_version
- **THEN** the existing projection supplies v2
- **AND** existing values, other model entries and unrelated source fields are preserved without new mutation rules.

#### Scenario: Genuine historical provenance is imported
- **WHEN** an existing managed overlay has a fully validated legacy source record
- **THEN** its original source kind/path/digest can be retained in the independent provenance format
- **AND** its old health result is not used as current parity acceptance.
