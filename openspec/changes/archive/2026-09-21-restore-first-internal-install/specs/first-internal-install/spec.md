## ADDED Requirements

### Requirement: Detect an empty installation safely
The public update-internal command SHALL allow first installation when the
internal profile and selected target are absent, without requiring Desktop
reference data or profile configuration.

#### Scenario: New workstation
- **WHEN** the profile and target do not exist
- **THEN** a validated CLI is installed at the requested target
- **AND** no profile, runtime config, or Desktop binding is created.

#### Scenario: Existing or partial installation
- **WHEN** a target, dangling link, partial profile, or backup already exists
- **THEN** it is not treated as an empty installation
- **AND** existing upgrade checks or an actionable error preserve that state.

### Requirement: First publication is safe and noninteractive
First installation SHALL use private candidate preparation, preserve complete
runtime packages, and publish without replacing a pre-existing target.

#### Scenario: Preparation fails
- **WHEN** installation, signing, or candidate version verification fails
- **THEN** no target command is published and failure is returned.

#### Scenario: Concurrent target or profile appears
- **WHEN** the target or profile appears after initial classification
- **THEN** publication fails without overwriting the new state.

#### Scenario: Installed-path verification fails
- **WHEN** the published command fails its exact version postcondition
- **THEN** only the unchanged command published by this operation is removed
- **AND** failure is returned without claiming Desktop readiness.

#### Scenario: Preview on an empty home
- **WHEN** update-internal --dry-run is requested
- **THEN** first installation is reported without installer or filesystem writes.

#### Scenario: Automatic version selection
- **WHEN** no explicit version is supplied
- **THEN** only a valid unblocked latest version or validated policy fallback
  is installed; unavailable metadata does not trigger an unpinned install.
