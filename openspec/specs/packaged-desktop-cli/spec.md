# packaged-desktop-cli Specification

## Purpose

Resolve and validate the actual Codex Desktop executable across supported bundle layouts throughout preparation, launch and health checks.

## Requirements

### Requirement: Canonical executable for supported Desktop layouts
The system SHALL resolve verified current ChatGPT bundles with either a flat
Resources/codex executable or a packaged codex-cli/CodexCLI.app executable.
Discovery, official binding, parity and status SHALL use the same executable.

#### Scenario: Packaged executable
- **WHEN** a valid packaged layout contains its executable entry and nested CLI
- **THEN** the nested CLI is the canonical executable and passes reference path validation

#### Scenario: Legacy executable
- **WHEN** the packaged directory is absent and the flat CLI is valid
- **THEN** the existing legacy binding remains supported

#### Scenario: Invalid or ambiguous layout
- **WHEN** packaged layout is present but missing a required executable or uses a symlinked bundle member
- **THEN** discovery rejects it with the affected path, even if a legacy CLI also exists

### Requirement: Executable identity across preparation
The system MUST bind reference evidence to the actual CLI executable and
reject stale staged preparation when its binary or packaged entry changes.

#### Scenario: Backend changes without wrapper changes
- **WHEN** the nested executable changes after staging but the wrapper does not
- **THEN** apply rejects the stale stage before runtime/config publication

### Requirement: Installed host failures retain their cause
An invalid installed current host MUST return its discovery diagnostic rather
than representing it as absent. Staging SHALL reject it before download.

#### Scenario: Invalid installed CLI
- **WHEN** the current bundle exists but has no valid supported CLI
- **THEN** stage reports the specific discovery cause and does not invoke its installer

### Requirement: Actual runtime compatibility evidence
Current-version compatibility changes MUST preserve core probes, unknown
difference rejection and explicit unsupported-feature failures.

#### Scenario: Current supported pair
- **WHEN** the current Desktop and internal pair complete preparation and launch
- **THEN** their actual features, schemas, core probe and launcher evidence are recorded

#### Scenario: Unknown difference
- **WHEN** an unreviewed capability or protocol difference appears
- **THEN** existing fail-closed classification remains effective
