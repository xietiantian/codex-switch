## ADDED Requirements

### Requirement: Reuse compatibility evidence for non-runtime edits
The launcher SHALL retain immutable generation validation and SHALL reuse compatibility evidence when parsed compatibility inputs remain unchanged.

#### Scenario: Desktop records trust or plugin state
- **WHEN** a valid runtime receives reviewed trust, integration or UI bookkeeping edits
- **THEN** launch reaches the backend without requiring identical config bytes or a provider probe

#### Scenario: Runtime settings change
- **WHEN** model, provider, features or another compatibility input changes
- **THEN** launch uses the initial preparation policy on current inputs and publishes only a healthy generation

#### Scenario: Integrity or preparation fails
- **WHEN** an immutable artifact is damaged or current input fails preparation
- **THEN** launch fails with a specific reason and does not overwrite user input or publish unhealthy evidence

#### Scenario: Old receipt changes
- **WHEN** a legacy byte-bound receipt no longer matches current runtime config
- **THEN** the launcher requires fresh preparation instead of inventing reusable evidence

### Requirement: Preserve the materialized runtime at launch
The managed launcher SHALL avoid restoring legacy fallback settings into an already materialized generation.

#### Scenario: A removed marketplace remains in a snapshot
- **WHEN** the runtime omits a marketplace still recorded in an old support snapshot
- **THEN** launching does not restore that marketplace

### Requirement: Verify the actual launch path explicitly
The public verify command SHALL offer an explicit actual-launch smoke check.

#### Scenario: Wrapper fails before initialize
- **WHEN** actual-launch verification encounters a failed generation check or startup
- **THEN** it reports failure rather than passing through the isolated backend smoke bypass
