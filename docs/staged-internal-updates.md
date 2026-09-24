# Staged internal updates

Use staged updates when configuration must be finalized between downloading a
runtime and publishing it. Staging returns a durable update ID and a complete,
verified runtime. Applying that ID validates the final configuration and commits
the runtime, profile and compatibility artifacts together.

## Stage, configure, apply

```bash
codex-switch update-internal stage --version 1.2.3 --json
```

Keep the returned `update_id` and `runtime_path`. The runtime path can execute
the verified version with its matching assets. Stage does not require an
internal profile or credentials. It leaves the bound command, profile, active
selection and Desktop binding unchanged; a successful stage is not Desktop
compatibility evidence.

Prepare the final home using your own configuration process. It must contain
`config.toml` and, when required by its authentication method, `auth.json`.
Then substitute the returned ID for `UPDATE_ID`:

```bash
codex-switch update-internal apply UPDATE_ID --from-codex-home /path/to/final-home --json
```

Apply privately captures those config/auth inputs and uses them as the final
seed. Saved profile settings do not override the supplied model, provider or
catalog. Omitting `--from-codex-home` uses the saved internal profile and requires
that profile's configuration to exist. Use the same explicit store and home
options before `update-internal` in each command when using nondefault locations.

Full apply requires the actual installed, verified Desktop bundle and its
bundled CLI. Stage reports that reference, and apply revalidates it. PATH
binaries, saved bindings, online release versions and arbitrary reference paths
cannot substitute for the installed bundle. If Desktop is absent, install it
separately before staging work intended for full apply.

Existing explicit compatibility bindings remain supported. In particular,
released `init --codex-bin` or `--app-cli-path` may have saved official profile
paths that differ from the actual Desktop bundled CLI. Stage preserves that
intent and records it separately from the verified Desktop reference; callers
do not need to delete or rewrite their official profile. Apply compares each
with its own staged state and refuses subsequent changes to either. Initial
path differences do not waive any Desktop compatibility checks.

The capture boundary covers config/auth and validated model-catalog evidence.
The caller continues to own auxiliary files and configuration generation.
There is no recipe, file-deployment manifest, hook callback or Desktop-installer
interface. Configured hooks do not run during compatibility probes. An
unsupported separate configuration location fails instead of silently choosing
another file.

Original model-catalog provenance survives invalidation of parity health.
Explicit custom catalogs remain authoritative even for an official-looking
model name. Managed overlays preserve the validated original custom/default
source identity; their mere presence does not turn a default source into a
custom one. Apply still requires fresh runtime compatibility evidence.

## Reuse a runtime or complete first setup

To validate and reference the current complete runtime without reinstalling:

```bash
codex-switch update-internal stage --current --json
```

`--current` and `--version` are mutually exclusive. The current runtime remains
externally owned; cancellation does not move or delete it.

A successful strict CLI-only first installation is also a valid current source
before an internal profile exists. Once Desktop is available, use the selected
bootstrap target and supply the final home:

```bash
codex-switch update-internal stage --current --internal-bin /path/to/internal-bin/codex --json
codex-switch update-internal apply UPDATE_ID --from-codex-home /path/to/final-home --json
```

This adopts the verified bootstrap runtime and transactionally creates the first
profile and binding without a second installer run. An unregistered, altered or
incomplete target, partial profile, or unresolved transaction is rejected.
Failure preserves the bootstrap CLI and rolls back only unchanged artifacts
created by the adoption transaction.

For a first full installation, `stage --version` also accepts an absent profile
and target. Use the existing `--install-dir /path/to/internal-bin` option to
select its target directory. Apply publishes the first command and profile only
after full validation; newly appearing destination state blocks publication.

## JSON and recovery

Successful `--json` commands emit one object on stdout. Diagnostics go to
stderr. Public output excludes configuration, credentials and their private
fingerprints. A staged result includes:

| Field | Meaning |
| --- | --- |
| `schema_version` | Public record schema version, currently `1` |
| `update_id` | Opaque ID used unchanged by apply, status and cancel |
| `state` | Persistent operation state |
| `runtime_path` | Absolute executable path for the retained complete runtime |
| `actual_version` | Version verified from the runtime |
| `runtime_digest` | Digest covering the runtime entry and retained package assets |
| `source` | `staged` for a prepared candidate, or `current` for an existing runtime |
| `desktop_reference` | `present: false`, or the selected bundle's `bundle_path`, `bundled_cli_path`, `version`, `sha256` and `bundle_id` |
| `profile_present` | Whether an internal profile existed when staging began |
| `internal_app_bound` | Whether existing selection/binding requires internal App validation |
| `safe_to_restore` | Whether uncommitted work is settled enough for caller-side restoration; false during active work, after commit, or when recovery is required |

Terminal output can also include a transaction ID and structured error/recovery
information. A failure before ID allocation may omit runtime fields. Keep the
ID from a failed operation when one is returned.

```bash
codex-switch update-internal status UPDATE_ID --json
codex-switch update-internal cancel UPDATE_ID --json
```

| State | Next action |
| --- | --- |
| `staging` | The owner is preparing the runtime; inspect status if it stops |
| `staged` | Apply the final inputs, or cancel |
| `applying` | Preparation/publication is in progress; do not start a competing apply |
| `applied` | The transaction is confirmed; identical apply returns that outcome |
| `failed` | Inspect the error, correct the cause and stage a new update |
| `stale` | A frozen dependency changed; preserve that change and stage again |
| `cancelled` | Uncommitted work is cancelled; stage anew if needed |
| `recovery_required` | Inspect status and use cancel for bounded transaction recovery |

Status is read-only. If publication committed before its response was saved,
status and repeated identical apply recover the confirmed result from the
transaction receipt. They do not reinstall, reprobe or publish again. The first
accepted home and input fingerprint bind the ID; a different home or changed
inputs cannot reuse it.

Apply rejects changes to the runtime, source inputs/catalog, profile, target,
store/homes, selection, bindings or actual Desktop reference. No lock remains
held between separate commands. A stale update does not authorize replacing
the newer state.

Cancel is idempotent. It can recover only unchanged transaction-owned writes,
reports conflicts, and never rolls back a confirmed commit. `safe_to_restore`
does not authorize overwriting unrelated files or manually undoing a committed
update. If it is false, do not restore caller-side state over the operation;
inspect its reported outcome or recovery condition first. Retained candidates
and current runtimes are not garbage-collected
by cancellation; do not remove paths that update records or active bindings
still reference.

## Existing one-shot updates

`codex-switch update-internal` continues to select versions using the existing
ordered update policy. An explicit `--version` selects that version; `--dry-run`
previews without installer or publication writes. Installer failure status is
preserved. Existing-profile updates use the same staged engine, and full
Desktop validation protects an existing internal App binding.

The supported split mode uses its established CLI-only policy. With no Desktop
and no internal App binding, one-shot update can also publish a verified CLI
generation while leaving internal App readiness unverified. Explicit staged
`apply` always requires full Desktop validation. A completely empty one-shot
installation remains the strict CLI-only bootstrap, with no profile capture or
Desktop-ready claim.

Full publication retains rollback evidence until installed-path postconditions
pass. Failure preserves or restores the last-known-good generation and reports
any ownership conflict. Update success does not itself activate a profile,
restart Desktop, or prove acceptance in a live Desktop session.
