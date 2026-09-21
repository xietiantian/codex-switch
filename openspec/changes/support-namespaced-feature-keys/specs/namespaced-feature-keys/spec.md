## ADDED Requirements

### Requirement: Feature identity supports namespaces consistently
The system SHALL accept dot-separated names whose nonempty segments each match
the existing flat feature-name grammar, and SHALL preserve the full name as
one identifier in collection, comparison, traces and serialized policy evidence.

#### Scenario: Current runtime output contains a dotted feature
- **WHEN** isolated and effective feature output contain guardianv2.thread_context
- **THEN** collection succeeds and retains its stage and both boolean states
- **AND** guardianv2 and guardianv2.thread_context remain distinct features.

#### Scenario: Namespaced features reach compatibility policy
- **WHEN** inventory comparison or an acceptance trace contains a dotted name
- **THEN** the ordinary compatibility policy evaluates that full identifier
- **AND** serializable evidence retains that identifier without normalization.

### Requirement: Syntax support preserves fail-closed validation
The system SHALL retain row, state, stage, uniqueness and compatibility
validation when accepting namespaced feature keys.

#### Scenario: Invalid namespace or malformed row
- **WHEN** a name has empty segments or illegal characters, a row is malformed,
  a boolean/stage is unknown, or a feature is duplicated
- **THEN** collection rejects the output before candidate promotion.

#### Scenario: Valid dotted feature has unclassified drift
- **WHEN** official and internal inventories disagree on an unclassified dotted feature
- **THEN** policy reports that drift as unhealthy rather than ignoring the feature.

#### Scenario: Existing flat names
- **WHEN** an older CLI emits valid flat names
- **THEN** collection, comparison and policy keep their existing behavior.
