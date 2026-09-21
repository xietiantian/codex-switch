## ADDED Requirements

### Requirement: Noninteractive staged installation
The update helper SHALL run the child installer with CODEX_NON_INTERACTIVE=1
and SHALL preserve installer failure status without activating a candidate.

#### Scenario: Installer would request interactive actions
- **WHEN** a staged installer supports uninstall and launch prompts
- **THEN** it observes noninteractive mode and neither action is requested.

### Requirement: Complete runtime survives scratch cleanup
The helper SHALL validate and preserve recognized standalone runtime assets
before removing installer scratch, without copying configuration or credentials.

#### Scenario: Standalone entrypoint targets private CODEX_HOME
- **WHEN** the installer creates a symlink to its standalone package
- **THEN** the final candidate remains a regular executable after scratch removal
- **AND** execution can find its matching code-mode host, rg, and resources.

#### Scenario: Existing direct-file installer
- **WHEN** the installer writes a regular codex executable directly
- **THEN** existing signing, validation, and promotion behavior is preserved.

#### Scenario: Unsafe or incomplete output
- **WHEN** runtime output contains a foreign link, unsupported layout, missing
  required companion, or unexpected private-state file
- **THEN** preparation fails before activation and the bound command is unchanged.

### Requirement: Generation identity and stable command
The generated launcher SHALL bind and verify the entire selected package before
execution while retaining the existing bound command path and runtime environment.

#### Scenario: Arguments and runtime environment
- **WHEN** the stable command is invoked after successful preparation
- **THEN** it forwards arguments and preserves caller HOME and CODEX_HOME
- **AND** it selects the exact prepared runtime independently of later installers.

#### Scenario: Runtime was altered
- **WHEN** a package file, mode, type, or file set differs from its manifest
- **THEN** execution fails before running the altered runtime.

### Requirement: Recoverable package activation
Standalone candidates SHALL use existing full or CLI-only transactional
promotion, retaining their respective validation and rollback requirements.

#### Scenario: Post-promotion validation fails
- **WHEN** selection of a standalone candidate fails its final postcondition
- **THEN** the previous command and manifest are restored and remain executable.

#### Scenario: Package already exists
- **WHEN** identical runtime content is materialized again
- **THEN** only an exactly validated existing generation may be reused
- **AND** foreign generation content is neither overwritten nor removed.

#### Scenario: Interrupted package preparation
- **WHEN** a termination signal arrives while preparing or signing the runtime
- **THEN** the helper forwards the signal, waits for child cleanup, and returns
  the signal exit status without reporting a candidate ready for activation.

#### Scenario: Native process ownership observation
- **WHEN** the regular launcher executes its native runtime
- **THEN** argv[0] retains the stable command path used by process attestation
- **AND** native executable discovery still locates the matching package assets.
