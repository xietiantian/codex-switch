## Why

Recent Codex installers place an entire standalone runtime below CODEX_HOME
and expose symlink entrypoints. The isolated update helper removes that home
before validating the candidate, leaving a dangling command instead of an
upgrade. Installer prompts can also launch a TUI or uninstall another package
from within a staged update.

## What Changes

- Run child installers noninteractively.
- Validate and preserve recognized standalone runtime assets before removing
  credential-bearing installer scratch; retain legacy direct-file support.
- Represent a complete immutable runtime by a regular, self-validating
  launcher at the existing candidate/bound command path.
- Reuse existing transactional promotion and recovery, with package identity
  bound into launcher bytes and checked before execution.
- Add isolated regression, tamper, companion, and rollback coverage.

## Capabilities

### New Capabilities
- `standalone-runtime-update`: isolated installation and recoverable activation
  of complete standalone Codex runtimes.

### Modified Capabilities
- None; preserve the existing full and CLI-only promotion contracts.

## Impact

Bash installer orchestration, a focused runtime materialization helper,
runtime update tests, and usage documentation. No new production dependency,
profile schema migration, installed workstation update, or release publication.
