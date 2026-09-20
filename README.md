# codex-switch

Project-agnostic Codex workstation CLI for switching Codex profiles, managing
profile auth snapshots, binding CLI/App binaries, and updating the internal
Codex CLI.

## Install

```bash
curl -fsSL "https://github.com/cYz26/codex-switch/releases/latest/download/install.sh" | bash
```

The installer writes the implementation to
`~/.local/share/codex-switch/current` and creates
`~/.local/bin/codex-switch`.

Useful installer overrides:

```bash
CODEX_SWITCH_INSTALL_DIR="$HOME/.local/bin"
CODEX_SWITCH_LIB_DIR="$HOME/.local/share/codex-switch"
CODEX_SWITCH_VERSION="v0.1.3"
CODEX_SWITCH_TARBALL_URL="https://example.com/codex-switch.tar.gz"
CODEX_SWITCH_SOURCE_TARBALL_URL="https://github.com/cYz26/codex-switch/archive/refs/tags/v0.1.3.tar.gz"
CODEX_SWITCH_SOURCE_DIR="/path/to/local/codex-switch"
CODEX_SWITCH_DRY_RUN=1
```

If the release bundle asset is unavailable, the installer can fall back to a
source archive. The trusted installer copies only the fixed release allowlist
from that archive and does not execute archive-owned scripts while staging it.

## Usage

```bash
codex-switch status
codex-switch internal
codex-switch split
codex-switch split --keep-version
codex-switch internal --app-profile official
codex-switch sync-shared --dry-run
codex-switch official
codex-switch restore <backup-id> --dry-run
codex-switch check-update
codex-switch update-internal --dry-run
codex-switch env check-internal
```

`codex-switch internal` checks the internal profile's bound Codex CLI and
automatically delegates to `codex-switch update-internal` when a newer internal
release is detected. That synchronized internal-App command retains the full
Desktop compatibility promotion described below.

To keep the shell on the internal CLI while ChatGPT Desktop uses the official
bundled CLI, run:

```bash
codex-switch split
```

This is the only supported cross-profile pairing. Without `--app-profile`, App
and CLI remain synchronized exactly as before. The split command prepares and
verifies the internal CLI/home, keeps an already healthy official App binding
unchanged, and commits the explicit owners in one recoverable transaction. If
the App surface is stale or incomplete, the same command instead plans a
recoverable rebind. Use
`status` to see both owners; `doctor` and `verify internal` check each surface
against its own binding. Internal App parity is explicitly reported as not
applicable because the App owner is official. `codex-switch split` is the
concise preset for
`codex-switch internal --app-profile official`; a real apply retains both
codex-switch self-update and internal update detection. Split preview bypasses
self-update and update detection so it remains zero-write and zero-network.
When apply-time detection
selects a newer internal binary, `split` uses an atomic CLI-only promotion: it
validates and records the exact version and SHA-256, leaves Desktop/parity
artifacts untouched, marks internal App readiness unverified, and runs bounded
local CLI runtime verification through the managed shell shim. Executable
SHA-256 validation uses a stable streaming read under an independent 2 GiB
safety bound; the 16 MiB config/receipt artifact limit is not applied to the
backend and the complete executable is never buffered as one payload. It does
not run an internal app-server smoke or require the official App to exit.
Before commit, promotion renders a private copy of the real internal shim and
executes its `--version` path; failure restores the previous binary and manifest.
Final split smoke separately executes the actual post-switch store shim.
Direct `codex-switch update-internal` remains the explicit full Desktop-parity
update path. For a controlled switch
that keeps both currently installed versions, use
`codex-switch split --keep-version`. This option does not skip Plugin repair,
verify, Doctor, status, App-effect derivation, transaction validation, or the
mandatory shared-readiness step described below. Add `--dry-run` to either form
for a preview; the plan reports an App action of `preserve` or `rebind`, states
that shared synchronization will follow a successful apply without running it,
and performs no installed-wrapper self-update or network check. A healthy
canonical official App may remain
running for `preserve`, and the transaction does not rewrite its LaunchAgent,
GUI environment, Home, processes, or Desktop global state. Only `rebind`
requires fully quitting ChatGPT/Codex App and keeping it closed until the
switch completes. The final result omits App restart guidance for `preserve`
and retains it only for a reported `rebind`. A running App/app-server or
unreadable process inventory on that path fails before backup and mutation. The
fixed `split` preset rejects `--app-profile`; use the explicit long form only
when diagnosing its underlying interface. Do not combine `--app-profile` with
`--skip-app-cli`. During apply, counted `current/total/name` lines report each
planned shared-support entry. Both the Python producer and the action-capture
filter run unbuffered, so progress remains visible while synchronization is
still running; a late binding write during a required rebind is still caught by
final CAS and journal rollback.

In this split, Plugin and Skill usage has one generationed desired-state layer,
while the App and CLI keep separate runtime configs and separate plugin caches.
After a real `split` (or `internal --app-profile official`) switch transaction
commits, the wrapper runs Official-to-internal shared synchronization exactly
once before generic Plugin repair, verify, Doctor, or status. Later skip options
do not skip this readiness boundary. If synchronization fails, the wrapper
returns that failure, stops every later step, and prints exact `sync-shared
--dry-run`, `sync-shared`, and Doctor recovery commands. The selected split
identity remains committed, while shared state retains its own last-known-good
generation.

When the official App later adds, updates, disables, or removes a Plugin or
configured Skill, the next functional managed internal CLI invocation repeats
the same reconciliation as a fallback and proves the internal cache before
running the requested Codex command. `codex --help` and `codex --version` remain
read-only.

The official App is the authority for this shared subset. Direct changes to
`marketplaces.*`, `plugins.*`, or `skills.config` in the managed internal home
are treated as target drift and replaced from the current official projection;
unrelated internal model/provider/auth, MCP, feature, and runtime settings stay
profile-local. Preview or apply the same Official-to-internal reconciliation
with:

```bash
codex-switch sync-shared --dry-run
codex-switch sync-shared
```

The apply form writes only the internal target, so the official App may remain
running. Status, Doctor, verify, and `sync-shared` report the same generation,
source/target, secret-safe changed operations, readiness, and remediation. Both
split apply and functional CLI preflight perform repairable synchronization
automatically; unsafe source, cache, or compare-and-swap failures stop before
later wrapper steps or backend execution and print exact preview/apply/Doctor
commands.

Normal `check-update`, `internal`, and `official` flows also compare the
selected profile CLI with the latest stable `openai/codex` release. The output
labels the stable baseline and reports whether the selected CLI is behind,
matching, or ahead. Prereleases are not used as the default baseline, and
lookup or version-parsing failures are advisory only. This comparison never
selects an internal install target: internal updates remain governed by the
internal release source. `--skip-update-check` skips both the profile-specific
check and the upstream stable advisory.

Persistent local commands installed by `install.sh` also check for a
codex-switch implementation self-update before ordinary command execution,
except the two split-preview forms which remain zero-write and zero-network.
A release-installed wrapper syncs `~/.local/share/codex-switch/current` from the
configured release tarball when a newer bundle is available, and re-runs the
original command against the synced wrapper. Source checkout usage such as
`scripts/codex-switch status` does not self-modify.

When a self-update check runs, status is printed to stderr before the command's
normal output. A current install reports `codex-switch self-update: already up
to date <version>`; an updated install reports the synced version transition.
Explicitly skipped checks stay quiet.

Self-update controls:

```bash
codex-switch --skip-self-update status
CODEX_SWITCH_SKIP_SELF_UPDATE=1 codex-switch status
CODEX_SWITCH_TARBALL_URL="https://example.com/codex-switch.tar.gz" codex-switch status
CODEX_SWITCH_SOURCE_TARBALL_URL="https://github.com/cYz26/codex-switch/archive/refs/tags/v0.1.3.tar.gz" codex-switch status
```

Self-update failures are warnings for ordinary commands; the current local
implementation continues to run. If the configured release bundle is missing
and a source archive fallback is available, self-update stages from the source
archive through the fixed release allowlist. The downloaded
`scripts/package-release.sh` is copied as inert data and is not executed to
build the staged implementation.

Run without installing a PATH command:

```bash
curl -fsSL "https://github.com/cYz26/codex-switch/releases/latest/download/run.sh" | bash -s -- status
curl -fsSL "https://github.com/cYz26/codex-switch/releases/latest/download/run.sh" | bash -s -- internal
curl -fsSL "https://github.com/cYz26/codex-switch/releases/latest/download/run.sh" | bash -s -- official --dry-run
```

The remote runner downloads the release bundle to
`~/.local/share/codex-switch/current` and executes the bundled
`scripts/codex-switch`. It does not create `~/.local/bin/codex-switch`; use the
installer when you want a persistent PATH command. Like the installer, it can
fall back to a source archive when the release bundle asset is unavailable,
using the same trusted allowlist copy without executing archive-owned staging
helpers. It then runs the copied `scripts/codex-switch` as the requested
command.

Release assets are published by GitHub Actions. When release-relevant changes
land on `main`, the automatic release workflow verifies the repository, bumps
`VERSION` to the next patch version, creates the matching `v*` tag, runs
`scripts/package-release.sh`, and uploads `install.sh`, `run.sh`, and
`codex-switch.tar.gz` to the matching GitHub release. Planning, OpenSpec,
verification, and docs-only changes do not create a release. A manually pushed
`v*` tag still runs the tag release workflow for explicit reruns.

## Config Model

Profile switching uses independent Codex homes.

`codex-switch official` keeps official mode on the official Codex home,
defaulting to `~/.codex`, and uses the current verified ChatGPT Desktop bundled
CLI when it is available. `codex-switch internal` prepares and activates a
managed internal home at `~/.codex-switch/homes/internal` by default. The shell
shim and Codex Desktop binding are switched to the target profile's effective
home unless the explicit internal-CLI/official-App split above is selected.

Generic cross-home support sync uses one exact V1 allowlist: global
`AGENTS.md`, `prompts/`, `rules/`, and personal standalone `skills/`. Unknown
top-level entries, including `pets/`, are ignored; an existing same-named
target is preserved rather than deleted. Auth files, sessions, history, logs,
sqlite state, temporary/browser/process state, profile-specific model/provider
layers, `agent-kb`, `plugins`, `computer-use`, caches, credentials, global-state
temporary/backup variants, installation/version markers, and vendor/update
caches are never admitted by generic Home selection. Desktop settings stored
in the canonical `.codex-global-state.json` are handled separately as a
sanitized settings subset; prompt history, thread permissions, queued
follow-ups, remote thread summaries, credentials, and remote routing
identifiers stay profile-local.
Shared support sync also refuses to copy self-referential symlinks or symlinks
that point back into the target home, so profile switches do not create symlink
loops.

Internal Codex binary upgrades are compatibility checkpoints. When
`codex-switch update-internal` or a manual profile edit changes the internal
profile's configured `codex_bin`, re-check internal Desktop compatibility
instead of assuming the existing shim still applies.

The exception is the CLI-only generation published automatically by
`codex-switch split`: it is accepted only by the managed internal shell path.
Selecting internal as the App owner fails before backup or App mutation until a
successful full `update-internal`/`set-bin internal` rebind publishes fresh
Desktop evidence and clears the CLI-only marker.

```bash
codex-switch --skip-self-update status
codex-switch internal --skip-update-check
codex-switch --skip-self-update verify internal --responses-tool-smoke --report
```

### Internal parity and staged updates

This section applies when the App owner is `internal`. In the supported
internal-CLI/official-App split, status, Doctor, and verify continue checking
the internal CLI and official App bindings but print
`Internal App parity: not applicable (App profile: openai-official)` instead of
collecting or repairing an internal Desktop parity receipt.

Internal parity uses the current verified ChatGPT Desktop bundled CLI resolved
by the canonical Runtime Binding as its official reference. A Codex binary on
PATH, the latest network release, cached release metadata, and the stable
release advisory are observations only; none can replace that bundled
reference. The internal binary, model, endpoint, provider, and auth remain the
only allowed identity differences.

`status`, `doctor`, and `verify internal --repair=none` consume the same
profile-local parity receipt without repairing it. Missing, stale, malformed,
core-drift, probe-failed, or unclassified evidence reports
`Parity health: unhealthy` with stable finding codes. Known optional drift stays
healthy unless its policy escalates it and appears separately as a
deterministically ordered `Parity sync:` queue.

Receipt schema v2 binds sorted direction/method coverage, the exact adapter
rule used for transformed paths, deterministic optional-extension identifiers,
and the versioned official Desktop acceptance trace. Schema-v1 receipts cannot
imply that coverage and are regenerated through staged repair rather than
patched. Preparation first determines probe eligibility, then requires both
`core_protocol` and `typed_subagent_v2` to pass, revalidates every mutable
fingerprint, and only then constructs the final receipt. Unknown or uncovered
drift stops before probes; missing or failed required probe evidence cannot
produce a healthy receipt.

Internal multi-agent v2 has no silent v1 fallback. Failed or unknown v2 evidence
leaves the previous runtime generation effective and reports unhealthy. After
reviewing the findings, an explicit
`codex-switch --skip-self-update verify internal --repair=safe` routes repair
through the same staged `set-bin internal <current-backend>` rebind; it does not
patch the receipt, overlay, config, launcher, or manifest in place.

With no internal profile and no command at the selected install target,
`update-internal` performs a first installation. It validates a private candidate
and publishes the command without overwriting another installation. Existing
commands, dangling links, partial profiles, and leftover backups are not treated
as fresh installs. Configure Codex and run `init --capture-current internal`
after installation; this path does not create a profile, change configuration,
activate Desktop, or claim Desktop compatibility. `--dry-run` previews this
path without filesystem changes. An explicit `--version` works without release
discovery; otherwise a valid latest release or policy fallback is required.

For an existing profile, `update-internal` installs and probes a candidate in a private sibling directory
while the bound binary remains available. It validates parity before replacing
the bound path, promotes the binary and runtime bundle through one recoverable
journal, and retains the old binary backup until version, binding, app-server,
capability-receipt, and parity-receipt postconditions all pass. Failure restores
or preserves the last-known-good generation and prints neither verified success
nor `Restart required`. Durable success retires the backup first and then prints
one restart notice. `update-internal --dry-run` performs none of these
mutations.

Staged installers run with `CODEX_NON_INTERACTIVE=1`, so supported installers
skip uninstall and launch prompts. Standalone packages are preserved under
`.codex-internal-runtimes/<digest>` beside the bound command before the private
installer home is removed. Only runtime assets are retained; installer config,
credentials, and shell-profile changes remain disposable.

The existing `codex` path becomes a regular launcher that verifies the complete
package and executes its native command with the caller's arguments, `HOME`,
and `CODEX_HOME`. It uses the Python interpreter selected during preparation
and reads package files for integrity verification on each invocation. Native
executables are signed before their hashes are recorded. Older direct-file
installers retain their existing behavior. Generations are retained for safe
rollback; automatic runtime garbage collection is not part of this update.

Source tests, a successful update, or a successful rebind do not satisfy the
live Desktop acceptance contract. Fully quitting and reopening ChatGPT,
creating a real provider-backed typed `explorer` Subagent task, and attesting
launcher, proxy, backend, receipt, overlay, and config ownership require
explicit authorization at the Human Gate before they run.

For each internal binary upgrade intended to serve the internal App, verify the actual Desktop App bundle binary,
the internal `codex_bin`, the generated app wrapper, and the running
app-server path. Re-compare Desktop bundle and internal app-server schemas when
request compatibility may have changed, then update or remove proxy
conversions such as namespace dynamic tool flattening, unsupported marketplace
kind filtering, model alias handling, and app-server flag routing as needed.
Finish with a real internal Desktop switch test and the focused regression
tests that cover the affected compatibility path.

If internal mode fails after a tool call with either an Azure resource mismatch
or `Item with id 'rs_…' not found`, use the Responses tool-follow-up smoke and
the troubleshooting note in
`docs/troubleshooting/internal-azure-responses-resource-stickiness.md`. That
scenario means AIDP either routed one Responses context across different Azure
resources or did not return portable encrypted reasoning content for
`store=false` continuation. Codex-switch records sanitized evidence and can
degrade old Desktop history safely; full-fidelity repair belongs upstream.

As of 2026-07-03, internal release `0.142.5` is treated as a known-bad default
upgrade target for this workstation flow. `codex-switch internal` keeps or
installs pinned fallback `0.142.4` while `internal-rust-v0.142.5` remains the
latest release, then resumes ordinary latest auto-update when a later internal
release appears. Override only for explicit testing with:

```bash
CODEX_SWITCH_INTERNAL_BLOCKED_VERSIONS= codex-switch internal
codex-switch update-internal --version 0.142.5
```

Profile switches write a `codex` shim under `~/.codex-switch/bin` and install
an idempotent managed block in the shell startup file so newly opened shells
prefer that shim over older binaries earlier on PATH. To align an already-open
shell immediately, run:

```bash
eval "$(codex-switch shim-env)"
codex-switch --skip-self-update status
```

When switching, the target home `config.toml` is generated by merging shared
settings from the source home with profile-specific settings from the target
profile's last valid runtime `config.toml`. If the target runtime config is
missing or invalid, codex-switch falls back to
`~/.codex-switch/profiles/<profile>/config.toml`. After a successful validated
switch, that canonical profile config is refreshed from the runtime config so
future fallback data stays current. The internal Codex Desktop wrapper may fold
shared app-home settings back into the official home, but it preserves official
profile-specific runtime settings such as `model` and `personality`. When a
profile-specific runtime setting is removed, refresh preserves that removal
instead of resurrecting an older fallback value; auth storage metadata is kept
as structural profile metadata.

Generated TOML keeps managed `# codex-switch:` comments. These comments mark
which settings came from the shared layer and which settings are
profile-specific; they are only annotations, and the file remains normal TOML.

Plugin enablement and plugin installation are separate layers. Profile
switching syncs shared plugin configuration such as marketplaces, enabled
plugins, skill config, and hook trust into the target `config.toml`, but it
does not copy or symlink another profile's `plugins/` cache. One-key switches
(`codex-switch internal` and `codex-switch official`) run plugin repair after a
successful switch, before doctor, by refreshing the target profile's
marketplace/catalog view, installing missing enabled plugins, and refreshing
only local plugin caches that are provably stale. Use `--skip-plugin-repair` to
skip that repair step.

The explicit repair command is:

```bash
codex-switch repair-plugins <profile>
```

For `internal` and `openai-official`, that command resolves the target profile's
canonical backend CLI independently of the currently running App, sets the
target `CODEX_HOME` explicitly, and verifies the selected CLI with `--version`.
It then refreshes configured plugin marketplaces with
`codex plugin marketplace upgrade --json`, primes the available plugin catalog
with `codex plugin list --available --json`, and invokes `codex plugin add` only
for missing available plugins or installed local plugins whose source tree
provably differs from the matching versioned cache. Matching caches are no-ops;
remote, incomplete, or otherwise uninspectable catalog sources are reported and
left unchanged.

If a confirmed stale cache belongs to a target profile whose app-server is
already running, repair fails closed instead of replacing files underneath the
active session. Quit ChatGPT completely, rerun
`codex-switch repair-plugins <profile>`, and then reopen ChatGPT. Enabled
plugins that are no longer available from configured marketplaces are skipped
by repair and remain visible to `codex-switch doctor` as active-profile
materialization issues. This keeps uninstalled official plugins visible in the
target profile without copying another profile's `plugins/` directory.
`codex-switch doctor` still checks the active profile's plugin materialization
state and reports this command if enabled plugins are missing, including after
low-level
`codex-switch switch <profile>` invocations that bypass the one-key
post-switch flow. `codex-switch internal --help`,
`codex-switch split --help`, and `codex-switch official --help` are pure help
paths and do not run update, switch, Plugin repair, Doctor, or status steps.

For the supported internal-CLI/official-App split, the continuous shared layer
is intentionally narrower than the legacy switch-time merge. It owns only
secret-screened `marketplaces.*`, `plugins.*`, and `skills.config` semantics.
The official App is always the source of truth for those semantics and the
internal CLI is a derived, independently materialized target; there is no
reverse automatic sync, source-choice prompt, or background watcher.
The official personal `skills/` directory is the canonical personal-Skill root;
the internal home may use one validated link to it. Plugin-contributed Skills
stay inside each profile's independently materialized plugin cache, and their
absolute paths are rendered for that cache. Project-local `.agents/skills`
remain owned by the shared worktree. Disable/remove changes usage only:
`codex-switch` invokes no plugin-remove and directly deletes no cache artifact.
This is not a retention promise; installed-version cache lifecycle belongs to
the native backend.

The next functional internal CLI invocation prints a flushed source-attestation
line to stderr before it scans the shared Plugin identities. If the generation
needs target work, it prints a second line with the target profile and enabled
Plugin count before querying the target catalog/backend. These messages make a
cold bootstrap visible; `--help` and `--version` remain read-only, and a
committed unchanged generation still performs no target materializer call or
network operation.

For a local `portable_exact` Plugin, a catalog record's reported version is the
target's installed state, not source-version authority. A safely resolved
newer source is accepted only when its selector manifest and complete tree
exactly match the desired identity; the target backend may then update its own
independent cache, may replace its prior installed version, and must have the
result attested again before backend execution. `codex-switch` itself never
copies, links, deletes, garbage-collects, or recreates those cache artifacts. A
safe manifest/tree drift reports
`shared_configuration.materialization.source_mismatch`; `unsafe_cache` remains
reserved for unsafe paths, links, file kinds, cache identities, or traversal.

For `backend_managed` Plugins, the official source identity and the internal
installed target identity are deliberately independent. The source manifest
and tree must still match the desired generation, but a changed generation
always reconciles through the internal backend even when an older internal
cache is already inspectable. Native add/update owns whether that prior version
is retained, replaced, or removed. After all such adds finish, preflight reads
one fresh target catalog for the batch, requires one installed target cache key
per selector, and independently attests that cache's selector manifest, tree, and
contributed Skill roots. Revision cache keys may differ from the manifest
version; both are recorded in the receipt. A valid catalog without provable
installed target state reports
`shared_configuration.materialization.unverified_target`, while command/JSON/
schema failure remains `unverified_catalog`. This CLI-side reconciliation and
explicit `sync-shared` both target only the internal home, so neither requires
quitting or modifying the official App.

Each ready receipt is re-attested against the target cache; the sidecar never
trusts an old receipt after its manifest/tree/Skill roots disappear or drift.
Before an external target backend can materialize a Plugin, a private durable
materialization intent binds the exact target config and allowed selector
activations. Shared publication then uses the prepared commit journal,
Official-source re-attestation, internal-target CAS, and post-materialization
target verification. `state.json` is the commit point; a later functional
preflight recovers either interrupted intent under the store lock, while
status/Doctor/verify only report the recovery boundary. Every external
materializer command inherits that store lock lease, so a backend that survives
parent `SIGKILL` keeps new applies fail-closed until it exits; only then may a
later apply classify a late selector write and retire the intent.

These recovery journals are local rollback evidence, not shared desired state.
To restore an interrupted write exactly they temporarily contain target
`config.toml` bytes, including any profile-local values already in that file.
They stay inside the private codex-switch store (`0700` directories, `0600`
files), are never projected or copied to the other profile, and are deleted
after committed cleanup or successful recovery. Canonical
`state.json` and immutable generation files remain secret-screened. The
recovery files use Base64 only as JSON-safe encoding; it is not encryption.

The broader configuration review is deliberately fail-closed:

| Configuration surface | Ownership in App/CLI split | Synchronization rule |
| --- | --- | --- |
| Plugin selectors, non-secret marketplace descriptors, configured Skills | Official-authoritative desired state | Render and independently materialize only into internal; direct internal shared edits are repaired drift |
| Global Home support (`AGENTS.md`, `prompts/`, `rules/`, `skills/`) | Exact V1 generic allowlist | Plan only these four names; preserve and ignore every unknown target |
| Personal standalone Skills | Official personal Skills root | One validated internal directory link |
| Plugin-contributed Skills and plugin code | Per-profile cache | Independently materialize and attest; never share/symlink caches |
| Project-local Skills | Repository worktree | Naturally shared by opening the same project; no profile migration |
| Repository project instructions/config (`AGENTS.md`, `.agents/skills`) | Repository worktree | Naturally shared; never copied into either profile home |
| Desktop `.codex-global-state.json` | App/profile-local | Excluded from split planning and sync; synchronized same-profile switches retain their existing explicit settings projection |
| Model, provider, endpoint, reasoning, personality | Profile-local | Never copied by the shared desired-state layer |
| Auth, credentials, tokens, OAuth, sessions, history, SQLite, logs | Profile-local/private | Never projected or synchronized; opaque target-config bytes may exist only in the private terminal recovery journal |
| MCP server/App/connector declarations | Deferred candidate | Require field-level protocol/version review; secret env, tokens, and OAuth remain private |
| Hook commands and hook trust | Deferred candidate | Require digest-bound executable/path and permission review before sharing |
| UI/TUI/Desktop preferences, features/agents, memories settings | Deferred | Share only after per-field cross-version compatibility review |
| Project trust, permissions, sandbox/approval, cloud/account/update state | Profile/host-local | Do not synchronize implicitly |
| Automations, process/browser/thread routing, catalogs and caches | Runtime-local/derived | Never synchronize as configuration |

`status`, `doctor`, and `verify internal` read the same shared-generation report.
They report bootstrap/readiness drift, recovery, unsafe cache, personal-Skill
ownership, automatic actions, secret-safe changes, and exact remediation
without repairing them.

Profile-level plugin repair does not refresh project-local DevFlow/OpenSpec
configuration, generated guidance, or skill links. Run project refresh
separately from the target project when it is explicitly required.

For legacy users who have always used the internal profile in `~/.codex`,
`internal` can adopt that existing home:

```bash
codex-switch internal --internal-codex-home ~/.codex
```

When `internal` adopts the same path that `openai-official` would otherwise use,
codex-switch assigns `openai-official` to
`~/.codex-switch/homes/openai-official` and persists both home bindings in the
profile manifests. If both profiles are explicitly assigned the same directory,
codex-switch prompts for a different directory in an interactive terminal and
rejects the switch before mutation when no prompt is available. Interactive
selection prompts for the target profile first, lists the recommended directory
first with a recommendation marker, then offers the other profile's current
directory and a custom path. The semantic defaults are `~/.codex` for
`openai-official` and `~/.codex-switch/homes/internal` for `internal`; those
defaults are recommended first unless the directory is forbidden by the current
active-profile conflict. Persisted home bindings created by migration or
automatic collision handling are prompted for confirmation the next time an
interactive switch can ask. When switching from one active profile to another,
the target profile must use a different home from the current active profile;
interactive switches ask for a different target home, while non-interactive
real switches fail before mutation.

Every non-dry-run switch creates a backup first. The backup includes all paths
that the switch plans to write, delete, replace, link, or unlink, plus a
`backup.json` manifest with path metadata. If backup capture fails, switching
aborts before applying the mutation plan. Use `--dry-run` to print both the
backup plan and mutation plan.

Restore a switch backup explicitly:

```bash
codex-switch restore <backup-id> --dry-run
codex-switch restore <backup-id> --apply
codex-switch restore <backup-id> --apply --force
```

By default restore refuses to overwrite paths that no longer match the
post-switch state recorded in the backup. Use `--force` only when you have
reviewed the dry-run output and accept replacing those current paths.

## Development

```bash
bash -n scripts/codex-switch
bash -n scripts/codex_env_setup
python3 -m py_compile scripts/*.py
python3 scripts/test_codex_profile_switch.py
bash -n install.sh
bash -n run.sh
```

Package a release tarball:

```bash
scripts/package-release.sh
```
