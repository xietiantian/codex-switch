## MODIFIED Requirements

### Requirement: Detect an empty installation safely
The one-shot update-internal command SHALL allow first CLI-only installation when
the internal profile and selected target are absent, without requiring Desktop
reference data or profile configuration. An explicit staged full apply SHALL
also support that absent state when final home inputs and a verified actual
Desktop reference are available, without weakening absent-state checks.
When strict CLI-only bootstrap has already completed, `stage --current` and
full apply SHALL instead support adoption of that verified existing runtime
with an absent profile and a verified actual Desktop reference.

#### Scenario: New workstation
- **WHEN** the profile and target do not exist and the caller requests one-shot bootstrap
- **THEN** a validated CLI is installed at the requested target
- **AND** no profile, runtime config, or Desktop binding is created.

#### Scenario: Existing or partial installation
- **WHEN** a target, dangling link, partial profile, or backup already exists
- **THEN** it is not treated as an empty installation
- **AND** existing upgrade checks or an actionable error preserve that state.

#### Scenario: First complete apply with an installed Desktop
- **WHEN** an explicit staged apply has no prior CLI or internal profile, valid final home inputs and a verified actual Desktop reference
- **THEN** full parity prepares using private candidate context
- **AND** the first runtime, profile, auth/config projection and binding artifacts publish together only after validation.

#### Scenario: Desktop is unavailable for full apply
- **WHEN** an explicit staged full apply lacks a verified actual Desktop reference
- **THEN** it fails without synthesizing parity proof or publishing partial full-mode state
- **AND** the separately selected strict empty CLI-only bootstrap remains available.

#### Scenario: State appears during first full apply
- **WHEN** a profile, target, backup or competing transaction appears after absent-state capture
- **THEN** first publication fails without overwriting it
- **AND** rollback removes only unchanged artifacts created by this transaction.

#### Scenario: CLI-only bootstrap is followed by full current-runtime adoption
- **WHEN** strict CLI-only bootstrap has succeeded, the internal profile is still absent, and the caller stages the verified current runtime after actual Desktop becomes available
- **THEN** full apply validates final home inputs and actual Desktop parity and transactionally creates the first profile, config/auth projection and binding
- **AND** the current runtime is preserved without reinstalling or rerunning empty-target bootstrap.

#### Scenario: First adoption fails or its existing runtime changes
- **WHEN** first full current-runtime adoption fails validation, is cancelled, or observes runtime/profile/transaction-state drift
- **THEN** the previously installed CLI remains intact and only unchanged newly published adoption artifacts can be rolled back
- **AND** a partial profile, tampered runtime or unresolved residue is rejected rather than treated as successful CLI-only bootstrap.
