## Why

The staged update introduced in 8e8e21b requires an existing internal profile
and executable. Earlier update-internal implementations could also install
into an empty target. A new workstation now fails before reaching the installer.

## What Changes

- Recognize an absent internal profile and absent target as a first install.
- Reuse isolated, noninteractive candidate preparation and complete runtime
  preservation, allowing the expected bound state to be absent.
- Publish a validated first command atomically without replacing an existing
  path. Leave profile capture and Desktop activation to their existing commands.
- Preserve all existing-profile update and Desktop parity requirements.

## Capabilities

### New Capabilities
- `first-internal-install`: safe installation before a profile exists.

### Modified Capabilities
- None; existing-profile upgrade promotion remains unchanged.

## Impact

Public update-internal routing, private installer preparation, a first-install
publisher, package inclusion, CLI tests, and documentation. No live workstation
changes, provider requests, new dependency, persistence schema, or release.
