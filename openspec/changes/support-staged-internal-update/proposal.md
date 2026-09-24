## Why

Callers need a complete runtime before generating their final configuration,
but the current one-shot update requires an existing profile and prepares parity
from its saved configuration. A durable stage/apply contract lets callers supply
the final home without exposing a partially updated profile or losing recovery
evidence when a process exits.

## What Changes

- Add `update-internal stage --version V --json` and `stage --current --json`,
  returning an opaque update ID, complete runtime path, actual version and
  digest without changing active profiles, configuration or runtime selection.
- Add `apply ID --from-codex-home DIR --json`, `status ID --json`, and
  `cancel ID --json`. Apply privately captures final config/auth, prepares parity
  with explicit candidate context, and publishes through the existing
  transaction only after revalidating frozen inputs and live identities.
- Persist update ownership, candidate references, input fingerprints and
  transaction outcomes for interruption, stale-state rejection, same-input
  idempotence and safe cancellation without long-lived inter-command locks.
- Keep the one-shot interface as an adapter over the same engine. Preserve
  strict empty CLI-only bootstrap; add full first publication from explicit
  final config when a verified Desktop bundle is available.
- Preserve original catalog source kind/path/digest separately from health
  acceptance across managed-overlay recapture. Keep custom-source authority,
  default cache/bundled comparison and the existing bounded overlay rule.
- Cover public commands, persistence, rollback, native parity and packaged
  upgrades with isolated test-first validation and neutral documentation.

## Capabilities

### New Capabilities

- `staged-internal-update`: persistent runtime staging, private candidate apply,
  query/cancel recovery and compatible one-shot routing.

### Modified Capabilities

- `first-internal-install`: distinguish strict CLI-only bootstrap from an
  explicit staged full apply with an existing verified Desktop reference.
- `custom-model-catalog-parity`: preserve independently validated source
  provenance through recapture without reusing historical health acceptance.

## Impact

The CLI wrapper, update adapter, parity/config projection, capture, runtime
binding transaction and package runtime inventory are affected. A bounded new
update-session module stores private versioned records; there is no new
production dependency. Existing archived changes remain historical records.

Callers retain configuration generation, auxiliary files and orchestration.
The switcher accepts no recipes, arbitrary hooks or deployment manifests,
does not install Desktop, and uses the actual installed verified Desktop bundle
as the parity reference. Scope covers source, isolated verification and the
already authorized existing PR update; live workstation changes and release
publication are excluded.
