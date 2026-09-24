---
name: codex-switch
description: Use when managing Codex official/internal profiles, auth snapshots, CLI/App binary bindings, internal updates, or the supported internal-CLI/official-App split.
metadata:
  short-description: Manage Codex profile switching
---

# Codex Switch

## Overview

Use this skill to operate the project-agnostic Codex workstation switcher. The
deterministic implementation is bundled with this repository under `scripts/`
and installed as a single public CLI: `codex-switch`.

`codex-switch` manages:

- profile files such as `~/.codex/internal.config.toml`
- optional file-backed `auth.json` profile stores
- profile-specific Codex CLI binary bindings
- a PATH-friendly `~/.codex-switch/bin/codex` shim
- Codex Desktop `CODEX_CLI_PATH` through a user LaunchAgent

It also owns internal Codex CLI checks and updates. The `internal` one-key
command automatically runs `codex-switch update-internal` when it detects that
the internal profile's bound CLI is older than the latest internal release.
The `split` preset performs the same ordered detection but promotes the selected
binary as a digest-bound CLI-only generation; it does not claim internal App
compatibility. Direct `update-internal` uses full Desktop parity when Desktop
is available or an internal App binding already exists. With neither, it can
update the CLI while leaving App readiness unverified. Explicit staged apply
always requires full Desktop parity.
Normal official/internal update checks additionally show a non-blocking
comparison with the latest stable `openai/codex` release. That advisory never
selects an internal install target, and prereleases are not the default
baseline.

## When to Use

Use for requests like:

- "initialize Codex profile switching"
- "switch to OpenAI official profile"
- "switch back to internal profile"
- "use the internal CLI while ChatGPT uses the official Codex"
- "check which Codex profile, CLI, and app binary I am using"
- "bind openai-official to this codex binary"
- "run codex login in the official profile"
- "make Codex Desktop use the official app bundled CLI"

Do not use this skill for configuring project MCPs, app-specific hooks, Figma,
Bits, Aiden, or OpenSpec assets.

## Command Map

Prefer the wrapper:

```bash
scripts/codex-switch status
scripts/codex-switch internal --dry-run
scripts/codex-switch split
scripts/codex-switch split --keep-version --dry-run
scripts/codex-switch internal --app-profile official
scripts/codex-switch sync-shared --dry-run
scripts/codex-switch sync-shared
scripts/codex-switch official
scripts/codex-switch check-update
scripts/codex-switch update-internal --dry-run
scripts/codex-switch update-internal stage --version 1.2.3 --json
```

If the skill has been installed into `$CODEX_HOME/skills` and the wrapper has
been installed into PATH, use:

```bash
codex-switch status
codex-switch internal
codex-switch split
codex-switch split --keep-version
codex-switch internal --app-profile official
codex-switch sync-shared --dry-run
codex-switch sync-shared
codex-switch official
codex-switch update-internal
```

Release-installed local commands self-sync the `codex-switch` implementation
before ordinary command execution, except the two split-preview forms which
remain zero-write and zero-network. The wrapper checks only when it is running
from the release implementation directory under
`~/.local/share/codex-switch/current`, and re-execs the original command after a
successful sync. When a check runs, it prints self-update status to stderr,
including "checking latest release", "already up to date <version>", a synced
version transition, or a warning before continuing. Source checkout commands do
not rewrite the repository.

Use these controls when scripting or debugging:

```bash
codex-switch --skip-self-update status
CODEX_SWITCH_SKIP_SELF_UPDATE=1 codex-switch status
CODEX_SWITCH_TARBALL_URL="https://example.com/codex-switch.tar.gz" codex-switch status
CODEX_SWITCH_SOURCE_TARBALL_URL="https://github.com/cYz26/codex-switch/archive/refs/tags/v0.1.3.tar.gz" codex-switch status
```

Self-update failures are non-blocking warnings for ordinary commands. If the
release bundle is unavailable and a source archive fallback is configured, the
wrapper copies only the fixed release allowlist and syncs from it. The
downloaded `scripts/package-release.sh` is copied as inert data and is not
executed to build the staged implementation.

For direct remote use from any project without creating a PATH command:

```bash
curl -fsSL "https://github.com/cYz26/codex-switch/releases/latest/download/run.sh" | bash -s -- status
curl -fsSL "https://github.com/cYz26/codex-switch/releases/latest/download/run.sh" | bash -s -- internal
curl -fsSL "https://github.com/cYz26/codex-switch/releases/latest/download/run.sh" | bash -s -- official --dry-run
```

The remote runner bootstraps the release into
`~/.local/share/codex-switch/current` so generated Desktop wrappers reference a
stable implementation path. It does not create the `~/.local/bin/codex-switch`
symlink. If the release bundle is missing, it can fall back to
`CODEX_SWITCH_SOURCE_TARBALL_URL` or a versioned GitHub source archive. The
fallback uses the trusted fixed allowlist copy without executing archive-owned
staging helpers, then runs the copied `scripts/codex-switch` as the requested
command.

Release assets are generated by the repository's GitHub Actions release
workflow for `v*` tags. The workflow runs verification, packages the bundle, and
uploads `install.sh`, `run.sh`, and `codex-switch.tar.gz`.

Advanced commands can call the Python switcher directly:

```bash
python3 scripts/codex_profile_switch.py doctor
python3 scripts/codex_profile_switch.py switch openai-official --dry-run
```

## Workflow

1. For first-time setup, run `scripts/codex-switch status` or `list`; if the
   store is missing, run `scripts/codex-switch init --capture-current internal`.
2. For one-key switching, run `scripts/codex-switch internal` or `official`.
   The wrapper always runs a dry-run plan, checks the target profile for CLI
   updates, compares the final selected CLI with the latest stable
   `openai/codex` release, automatically installs only updates selected by the
   internal release source, performs the real switch, then runs `doctor` and
   `status`. When switching to `official`, the wrapper automatically runs
   `login openai-official` first if the profile uses file auth and no stored
   `auth.json` exists yet. The login runs in a clean temporary `CODEX_HOME` and
   copies only the resulting `auth.json` back to the stored profile, so legacy
   profile config does not break newer Codex login. Use `--skip-login` for
   non-interactive scripts. Use `--skip-update-check` when both update probes
   and the upstream stable advisory should be omitted.
   For the supported independent mode, run `scripts/codex-switch split`, the
   concise preset for
   `scripts/codex-switch internal --app-profile official`. A real `split` apply
   retains codex-switch self-update and internal update detection, while either
   split preview form bypasses both update layers to remain zero-write and
   zero-network. Use
   `split --keep-version` when a controlled activation must retain both current
   versions; it skips no shared readiness, repair, verification, App-effect, or
   CAS check. The
   preview reports `App action: preserve` when the active identity,
   LaunchAgent, GUI environment, and running owner already match the canonical
   official binding; apply then leaves the App, official Home, and Desktop
   global state untouched while synchronizing the internal CLI side. Only
   `App action: rebind` requires fully quitting ChatGPT/Codex App and keeping it
   closed; running or unprovable App/app-server state then fails before backup.
   Apply reports counted allowlisted support progress. The fixed preset rejects
   `--app-profile`. In split mode, Plugin preparation and runtime/exec
   verification remain on `internal`, while Desktop observation uses
   `openai-official`; internal App parity is reported as not applicable. If an
   ordered internal update is selected, promotion commits only the stable CLI
   binary plus its digest/version manifest generation and then forces bounded
   CLI runtime smoke. It neither rewrites Desktop/parity artifacts nor requires
   the official App to exit. CLI-only promotion and final runtime smoke validate
   the same managed shell generation: promotion probes a private freshly
   rendered shim before commit, and final smoke uses the actual store shim.
   Executable digests use a stable streaming read with an independent 2 GiB
   safety bound instead of the 16 MiB config/receipt reader. Apply keeps both the
   Python producer and action filter unbuffered. Omitting `--app-profile` from
   the explicit profile command preserves synchronized behavior and the full
   internal-App parity
   path. Successful `App action: preserve` output has no App restart step;
   `rebind` retains the restart guidance.
   After a real split transaction commits, the wrapper invokes the existing
   `sync-shared` apply exactly once before Plugin repair, verify, Doctor, or
   status. Later skip options do not bypass this mandatory readiness boundary.
   A split dry-run reports that readiness will follow a successful switch but
   never invokes shared apply. If sync fails, stop every later wrapper step,
   return its exit code, preserve the committed split identity and shared
   last-known-good state, and print exact preview/apply/Doctor remediation.
   In split mode, every later functional managed internal CLI invocation also
   reconciles the official App's Plugin/Skill desired generation as a fallback
   and proves an independent internal cache before backend execution. It flushes a
   source-attestation line to stderr first and, when target materialization is
   needed, a second line with the target profile and Plugin count before the
   target catalog/backend call. Help/version remain read-only, and an unchanged
   committed generation makes no target materializer or network call. For
   `portable_exact`, treat a catalog record's version as installed target state:
   authorize an update only from a safely resolved source whose selector
   manifest and tree exactly match the desired identity, then re-attest the
   independent target artifact. Let the native backend own whether prior
   installed versions remain, are replaced, or are removed; `codex-switch`
   itself never directly copies, links, deletes, garbage-collects, or recreates
   Plugin cache artifacts. Report safe
   manifest/tree drift as `shared_configuration.materialization.source_mismatch`
   and reserve `unsafe_cache` for unsafe structure. For `backend_managed`,
   attest the desired official source independently from the compatible
   internal target, reconcile every changed selector through the internal
   backend, then use one fresh batch catalog to require a unique installed
   target key and attest that target's manifest/tree/Skill roots. Keep revision
   cache keys distinct from manifest versions. Report valid-catalog target
   proof failures as `shared_configuration.materialization.unverified_target`
   and catalog command/schema failures as `unverified_catalog`. This
   internal-CLI reconciliation may run while the official App remains open and
   does not mutate it. Treat the Official App projection as authoritative:
   direct internal shared-Plugin/Skill edits are target drift, while unrelated
   internal model/provider/auth, MCP, feature, and runtime settings stay local.
   Functional preflight repairs safe drift automatically. Use
   `scripts/codex-switch sync-shared --dry-run` for a zero-write preview and
   `scripts/codex-switch sync-shared` for an explicit Official-to-internal apply;
   never prompt for a source, reverse-sync into the App, or require the App to
   stop for this operation. On unsafe failure, preserve last-known-good state
   and surface the finding message plus preview/apply/Doctor remediation.
3. For login, run `scripts/codex-switch login-official` or
   `scripts/codex-switch login-internal`.
4. For CLI binding, run `scripts/codex-switch set-bin <profile> <absolute-path>`.
5. For Codex Desktop binding, run
   `scripts/codex-switch set-app-bin <profile> <absolute-path>`.
6. For standalone install/update checks, run `scripts/codex-switch check-update`.
   This remains read-only, prints `codex-switch update-internal` when the
   internal release source selects an update, and reports the selected
   profile's relationship to the latest stable `openai/codex` release.

## Durable Internal Updates

Use `update-internal stage --version VERSION --json` (or `--current`), then
`update-internal apply ID --from-codex-home PRIVATE_DIR --json`. Keep formal
config/auth unchanged until apply; configuration generation and auxiliary-file
recovery belong to the caller. Do not insert capture or set-bin in this flow.

Read [staged internal updates](docs/staged-internal-updates.md) before using this
interface for JSON fields, bootstrap/current adoption, actual Desktop reference,
custom/default source provenance, replay and recovery. On interruption query
`update-internal status ID --json`; use cancel only for owned unfinished work.
A confirmed commit is historical evidence, not current Desktop health.

## Internal Parity Contract

Apply this contract only when the App owner is `internal`. For
internal-CLI/official-App split mode, keep CLI/config/Plugin checks active but
do not collect or repair internal App parity.

- Resolve the official parity reference from the canonical Runtime Binding for
  the current verified ChatGPT Desktop bundled CLI. Do not substitute PATH,
  network latest, cached release metadata, or the stable release advisory.
  Internal binary, model, endpoint, provider, and auth are the complete allowed
  identity differences.
- Start with `scripts/codex-switch verify internal --repair=none`. Status,
  Doctor, and read-only verify consume the same receipt: missing, stale,
  malformed, core, probe-failed, or unclassified evidence is unhealthy, while
  known optional drift remains a deterministic synchronization queue unless its
  policy escalates it.
- Require receipt schema v2 for method-scoped parity evidence. It binds sorted
  direction/method coverage, exact adapter-rule proof, optional-extension
  identifiers, and the versioned official Desktop acceptance trace. Treat
  schema v1 as stale/unsupported and regenerate it only through staged repair.
- Treat preparation as two policy passes: fail-closed eligibility before
  probes, then final policy only after both `core_protocol` and
  `typed_subagent_v2` pass and mutable fingerprints are revalidated. Unknown
  or uncovered drift stops before probes; incomplete probe evidence cannot
  produce a healthy receipt.
- Never treat missing or failed multi-agent v2 evidence as permission to use
  v1. After the user approves mutation, use
  `scripts/codex-switch verify internal --repair=safe`; it routes through the
  staged current-backend rebind and never patches parity artifacts in place.
- Treat `update-internal` as prepare-then-promote. The candidate is installed
  beside the bound binary and proved before replacement. The old backup remains
  until the version, binding, app-server, capability-receipt, and parity-receipt
  handshake passes. Failure restores last-known-good without success or restart
  output; durable success retires the backup before printing one
  `Restart required` notice. Use `--dry-run` for a zero-mutation plan.
- Treat split auto-update as a narrower CLI-only promotion. Require an exact
  version and SHA-256-bound generation, leave existing Desktop evidence
  untouched, and mark internal App readiness unverified. A later request to use
  internal as the App owner must fail before mutation until a full rebind
  succeeds and clears that marker.
- Do not infer live Desktop acceptance from source tests, update success, or
  rebind success. Stop for explicit authorization before any acceptance that
  installs/rebinds live state, fully quits and reopens ChatGPT, creates a real
  provider-backed typed `explorer` task, or captures runtime ownership
  evidence.

## Safety

- Treat `auth.json` as a secret. Never print its contents.
- Do not modify the ChatGPT Desktop app bundle; bind to its verified bundled
  CLI path instead.
- Treat `split` / `internal --app-profile official` as one transactional
  selection. Do not emulate it with `--skip-app-cli` or a later `set-app-bin`,
  do not pass `--app-profile` to the fixed `split` preset, and do not combine
  the explicit `--app-profile` form with `--skip-app-cli`.
- Treat post-commit `sync-shared` as a mandatory split-readiness step, separate
  from the profile transaction and earlier than Plugin repair/verify/Doctor/
  status. Do not reinterpret later skip options as authority to bypass it.
- In that split, the shared capability layer owns only the generationed,
  secret-screened `marketplaces.*`, `plugins.*`, and `skills.config` desired
  state. Keep both plugin caches as independent real directories; never link
  the two caches.
- Generic Home support shares exactly `AGENTS.md`, `prompts/`, `rules/`, and
  `skills/`. Every other name is ignored and an existing unknown target is
  preserved.
- Personal standalone Skills may use the official personal Skills root through
  one validated internal link. Plugin-contributed Skills stay in each target
  cache, and project-local `.agents/skills` stay worktree-owned.
- Treat both `pending-materialization.json` and `pending-commit.json` as private
  terminal recovery evidence. Read-only commands report them; only a later
  locked functional apply may selectively recover them before new planning.
  External shared-Plugin materializer commands inherit the active store-lock
  lease, so an orphan backend blocks recovery until it has exited.
- Treat models/providers/auth, MCP/apps/connectors, permissions/trust,
  UI/feature/memory preferences, automations, sessions/history/databases, and
  derived runtime state as profile-local or deferred until a separate
  field-level compatibility and secret review approves them.
- Do not clear live auth when the target profile lacks `auth.json` unless the
  user explicitly asks for `--clear-missing-auth`.
- For `official`, prefer the one-key command's first-run auto-login; use
  `scripts/codex-switch login-official` only when you want to log in without
  switching or need to repair the stored official auth explicitly.
- Do not embed profile-specific model/provider/auth keys into live
  `~/.codex/config.toml`. Switching writes the selected profile layer to
  `<profile>.config.toml` and keeps live `config.toml` as a shared base.
- Outside the supported split, the legacy same-profile switch-time merge
  preserves non-auth workstation config such as hook trust, projects, MCP
  servers, UI preferences, and feature flags. That legacy preservation is not
  canonical App/CLI shared ownership and `sync-shared` must not copy those
  surfaces.
- When the App profile itself is `internal`, switching should refresh the
  legacy Desktop wrapper at `~/.codex-switch/bin/codex-internal-app` from the
  current codex-switch scripts so its app-home config is
  rebuilt from shared `config.toml` plus `internal.config.toml`, not copied from
  a stale profile snapshot. Before each Desktop launch, the wrapper should
  write app-home non-auth shared config changes back to shared `config.toml` so
  plugin installs, hook trust, feature flags, MCP servers, and UI preferences
  survive restarts.
- Do not reintroduce `[profiles.<name>]` or top-level `profile = "<name>"`.
- Already-running Codex Desktop processes may need a restart after App CLI
  binding changes.

## Validation

```bash
python3 -m py_compile scripts/*.py
python3 scripts/test_codex_profile_switch.py
bash -n scripts/codex-switch
bash -n scripts/codex_env_setup
bash -n install.sh
bash -n run.sh
python3 -m json.tool evals/evals.json >/dev/null
```

For isolated runtime validation:

```bash
tmp="$(mktemp -d)"
mkdir -p "$tmp/live"
printf '[features]\nhooks = true\n' > "$tmp/live/config.toml"
scripts/codex-switch \
  --store-dir "$tmp/store" \
  --live-codex-home "$tmp/live" \
  --launch-agent-path "$tmp/agent.plist" \
  init --codex-bin /bin/echo --app-cli-path /bin/echo
scripts/codex-switch \
  --store-dir "$tmp/store" \
  --live-codex-home "$tmp/live" \
  --launch-agent-path "$tmp/agent.plist" \
  switch openai-official --dry-run
scripts/codex-switch \
  --store-dir "$tmp/store" \
  --live-codex-home "$tmp/live" \
  --launch-agent-path "$tmp/agent.plist" \
  switch openai-official --skip-launchctl
scripts/codex-switch \
  --store-dir "$tmp/store" \
  --live-codex-home "$tmp/live" \
  --launch-agent-path "$tmp/agent.plist" \
  official --skip-launchctl --skip-doctor --no-status
scripts/codex-switch \
  --store-dir "$tmp/store" \
  --live-codex-home "$tmp/live" \
  --launch-agent-path "$tmp/agent.plist" \
  doctor
```
