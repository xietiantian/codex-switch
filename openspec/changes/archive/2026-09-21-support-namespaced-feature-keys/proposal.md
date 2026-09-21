## Why

Codex 0.155.0 emits `guardianv2.thread_context` in `features list`. The
inventory parser and feature identity validators accept only flat names, so
valid output fails before runtime compatibility can be evaluated.

## What Changes

- Accept dot-separated feature names with nonempty, existing-format segments.
- Share that grammar across output parsing, inventories, policy traces and
  serialized feature identifiers; preserve names verbatim.
- Retain strict row, duplicate, stage, state and compatibility checks.

## Capabilities

### New Capabilities
- `namespaced-feature-keys`: consistent namespaced runtime feature identities.

### Modified Capabilities
- None.

## Impact

Parity feature grammar, regression tests and public documentation. No feature
classification, protocol adapter, installer, configuration, dependency or
receipt schema change. Verification uses temporary homes; no live installation
or provider request is authorized by this repair.
