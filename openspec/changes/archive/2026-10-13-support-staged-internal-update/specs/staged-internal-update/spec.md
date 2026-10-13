## Purpose

Let callers prepare a complete internal runtime, supply final configuration in
a separate command, and inspect or cancel durable update work without exposing
partially validated profile state.

## ADDED Requirements

### Requirement: Stage a complete runtime without activation
`update-internal stage --version V --json` SHALL prepare a private complete
candidate and return a stable opaque update ID, absolute runtime path, actual
version and complete runtime digest. Staging SHALL NOT require an internal
profile or change active profiles, bound commands, runtime configuration,
credentials, shell setup or Desktop selection.

#### Scenario: Stage before a profile exists
- **WHEN** a valid version is staged without an internal profile
- **THEN** the complete executable candidate and durable update record are created
- **AND** the existing or absent live state remains unchanged.

#### Scenario: Stage reports verified runtime identity
- **WHEN** standalone or direct-file candidate preparation succeeds
- **THEN** the returned path executes the verified actual version with its matching assets
- **AND** the digest covers the complete runtime, not merely a launcher file.

#### Scenario: Installer or candidate validation fails
- **WHEN** installation fails, is interrupted, returns a different version or leaves unsafe or incomplete assets
- **THEN** no staged success or active runtime publication is reported
- **AND** any allocated update record exposes its failure without leaking configuration or credentials.

### Requirement: Reference the current runtime without reinstalling
`stage --current --json` SHALL validate and reference the selected existing
complete runtime without executing an installer or transferring its ownership.
It SHALL retain the same complete identity and drift protections as versioned staging.
Successful strict CLI-only bootstrap SHALL be a valid current-runtime source
even while the internal profile is absent; that adoption SHALL NOT be routed
through empty-target installation checks.

#### Scenario: Current runtime is complete
- **WHEN** a caller stages the selected current runtime
- **THEN** the result identifies its existing executable, assets, actual version and digest
- **AND** no reinstall, profile recapture or runtime publication occurs.

#### Scenario: Current runtime is absent or changes
- **WHEN** the selected runtime is missing, incomplete, unsafe or differs before apply
- **THEN** staging or apply fails with an actionable identity diagnostic
- **AND** cancellation never deletes the caller-owned runtime.

#### Scenario: Current runtime comes from CLI-only bootstrap
- **WHEN** strict CLI-only bootstrap completed successfully and its verified runtime exists without an internal profile
- **THEN** stage --current accepts that complete runtime and freezes its identity without running an installer
- **AND** target presence is recorded as verified existing state, not mistaken for a failed empty installation.

#### Scenario: Profileless target has no valid adoption evidence
- **WHEN** a profileless target is unowned, altered or incomplete, or a partial profile or unresolved transaction residue exists
- **THEN** staging or apply refuses adoption with an actionable state diagnostic
- **AND** neither empty-state bootstrap nor current-runtime adoption overwrites or blesses that state.

### Requirement: Apply privately captured final home inputs
`apply ID --from-codex-home DIR --json` SHALL capture `DIR/config.toml` and
`DIR/auth.json` into private update-owned preparation state, retaining validated
absence when the existing authentication contract permits no auth file. It SHALL
prepare against explicit candidate profile context and final destinations without
temporarily replacing a real profile, runtime configuration or saved binding.

#### Scenario: Final configuration differs from the saved profile
- **WHEN** the caller supplies a valid final home with a changed model, provider or catalog
- **THEN** parity and projections use those captured inputs
- **AND** observers continue to see the old complete profile until transactional publication.

#### Scenario: Input layout cannot express the full apply
- **WHEN** final configuration is missing from the supplied home, unsafe, malformed, or requires an unsupported separate configuration location
- **THEN** apply rejects it before profile, runtime, binding or input-capture publication
- **AND** it does not silently choose another configuration file or rewrite caller paths.

#### Scenario: Caller owns auxiliary configuration
- **WHEN** the supplied home includes hooks, recipes, auxiliary files or other orchestration data
- **THEN** the switcher captures only its supported config/auth inputs and validated catalog evidence
- **AND** it neither executes caller hooks during probes nor generates, deletes or deploys auxiliary files.

### Requirement: Freeze and revalidate the complete publication context
Apply SHALL bind the update to candidate identity, captured config/auth and
original catalog evidence, selected store and homes, actual verified Desktop
reference, prior profile, active selection, bindings and target identity or
absence. It SHALL reject drift before publication and use the existing
recoverable transaction for all published runtime/profile/parity artifacts.
Saved profile bindings and the resolved actual Desktop reference SHALL be
frozen independently and compared with their respective prior snapshots.
Supported initial explicit-compatibility path differences SHALL NOT be treated
as concurrent drift or require caller-side profile repair.

#### Scenario: Released init produced an explicit compatibility binding
- **WHEN** a saved official profile created by released init has explicit CLI paths different from the verified actual Desktop bundled CLI
- **THEN** stage preserves that manifest and accepts the supported initial difference
- **AND** full apply uses the actual verified Desktop reference, preserves the official binding intent, and supports subsequent switch and verification under existing rules.

#### Scenario: Frozen inputs or destination identities change
- **WHEN** the runtime, config, auth, original catalog, store/home, target, profile or binding changes after capture or staging
- **THEN** apply fails before overwriting the changed state
- **AND** changed, missing and newly appearing sources are distinguished.

#### Scenario: Actual Desktop reference differs
- **WHEN** the actual Desktop bundle or bundled CLI changes from the frozen Desktop reference, or a saved profile binding changes from its own frozen state
- **THEN** apply refuses stale or inconsistent evidence
- **AND** a saved App binding cannot bypass actual bundle identity, schemas, features or required native probes.

#### Scenario: Publication or its postcondition fails
- **WHEN** an apply fails during publication or installed-path verification
- **THEN** transaction recovery restores its unchanged prior runtime, profile, config, auth, bindings and parity artifacts
- **AND** unrelated concurrent changes are preserved and reported as requiring recovery.

### Requirement: Persist update identity and terminal outcomes
Update records SHALL survive process exit, retain their candidate references,
and bind the first accepted apply input fingerprint to that update ID. No lock
SHALL remain held between separate commands. Each mutation SHALL serialize with
competing store/update mutations and revalidate previously observed state.

#### Scenario: Same-input apply is repeated after commit
- **WHEN** the same update ID and same inputs are submitted again and the committed transaction is confirmed
- **THEN** the existing terminal result is returned
- **AND** no installer, probe, profile capture or publication is repeated.

#### Scenario: Update ID is reused for different inputs
- **WHEN** apply receives a previously bound ID with a different home or input fingerprint
- **THEN** it rejects the mismatch without changing the saved result or publishing again.

#### Scenario: Process exits between transaction commit and result persistence
- **WHEN** the publication transaction has a durable committed result but the update result was not yet copied
- **THEN** status and repeated identical apply derive the confirmed terminal result from that transaction
- **AND** they never repeat publication.

#### Scenario: Competing operations occur between commands
- **WHEN** another update or profile mutation changes a staged dependency
- **THEN** the older update rejects the stale context
- **AND** staging does not prevent unrelated commands by retaining a long-lived lock.

### Requirement: Query and cancel provide bounded recovery
`status ID --json` SHALL report persistent state, verified identity, applicable
terminal outcome and an actionable recovery condition without mutating active
state. `cancel ID --json` SHALL be idempotent and cancel only uncommitted owned
work, using transaction recovery when necessary. These are the recovery
commands; apply SHALL NOT silently resume interrupted publication or accept
replacement inputs.

#### Scenario: Apply is interrupted before or during publication
- **WHEN** the owning process exits before a confirmed terminal transaction
- **THEN** status distinguishes unpublished work from recoverable or ambiguous publication
- **AND** cancel restores only unchanged transaction-owned writes or reports the exact conflicting state.

#### Scenario: Cancel is repeated or follows commit
- **WHEN** cancellation is repeated or the update already committed
- **THEN** it returns the confirmed terminal state
- **AND** a committed update is not rolled back and caller files are not deleted.

#### Scenario: Candidate is still referenced
- **WHEN** an update record or selected runtime still references candidate assets
- **THEN** those assets remain valid and are excluded from scratch cleanup
- **AND** cancellation cannot reclaim an externally owned current runtime.

#### Scenario: Malformed or foreign update record
- **WHEN** a record has an unsupported schema, unsafe path, symlink, ownership mismatch or altered referenced digest
- **THEN** query or mutation reports invalid state without following unsafe paths or executing candidate code.

### Requirement: Preserve compatible one-shot update behavior
Existing one-shot `update-internal` SHALL use the same candidate preparation and
transactional apply engine with existing profile context, while retaining
ordered version selection, preview, exit-status and explicit CLI-only rules.
No interface SHALL accept deployment recipes, arbitrary executable hooks or
a Desktop installer.

#### Scenario: Existing profile uses one-shot update
- **WHEN** a caller uses the existing one-shot command with a valid profile
- **THEN** version selection, complete staging, parity validation and publication follow the shared engine
- **AND** errors preserve the existing update safety guarantees.

#### Scenario: Dry run and unsupported options
- **WHEN** preview is requested or an unsupported recipe/hook/manifest option is supplied
- **THEN** preview performs no installer or publication writes and unsupported options fail before mutation.

#### Scenario: Existing App-bound installation requests reduced validation
- **WHEN** a current App binding requires full compatibility proof
- **THEN** the existing binding and validation policy remain enforced
- **AND** the staged interface cannot reinterpret partial state as a fresh CLI-only install.
