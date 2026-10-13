## Why

Current ChatGPT Desktop packages place Codex inside `codex-cli/CodexCLI.app`.
Discovery rejects this installed host and staged updates hide the cause as an
absent reference. Parity independently assumes the old single-file path.

## What Changes

- Resolve both supported bundle layouts through one canonical executable path.
- Bind version, schema, feature and receipt identity to the actual executable.
- Preserve discovery diagnostics and reject invalid installed hosts before download.
- Review current Desktop/internal feature and protocol differences using actual
  runtime evidence; preserve core probes and unknown-difference rejection.
- Verify staged updates, switching and launch against current native runtimes.

## Capabilities

### New Capabilities

- `packaged-desktop-cli`: consistent Desktop layout discovery, reference identity,
  diagnostics and current-runtime compatibility verification.

### Modified Capabilities

None.

## Impact

Generic Desktop discovery, parity, update snapshots, status/check-official,
focused/native tests and release manifest. No caller-specific business logic,
project setup, credentials, new dependency or release publication.
