## Context

Baseline: `956197209b2d`, branch `fix/standalone-installer-runtime`. The requester
approved the complete generic staged update, isolated testing and delivery to
the existing PR. This is a new Full OpenSpec change; previous archived repairs
are not reopened. See `proposal.md` for motivation and the three delta specs
for observable behavior.

Current source evidence, inspected on 2026-09-23:

| Source | Observed contract | Consequence |
|---|---|---|
| `scripts/codex-switch`, `run_internal_update` | Resolves live profile, allocates a sibling candidate, invokes helper, then calls `promote-internal-update` | Separate durable staging from apply while retaining version policy and helper isolation |
| `scripts/codex_env_setup`, `scripts/codex_switch_internal_runtime.py` | Private noninteractive install preserves complete runtime and generates a validated launcher | Reuse the installer/runtime adapter; do not add an installer implementation |
| `scripts/codex_switch_bindings.py`, `cmd_set_bin` | Loads the live manifest, chooses homes, captures canonical profile config and shared input, prepares parity, then commits a runtime-binding bundle | Extract explicit candidate preparation and publication so a missing/old live profile need not be replaced to validate a new one |
| `scripts/codex_switch_parity.py`, `ConfigInputs`, `_config_identity` | Canonical `profiles/internal/config.toml` is both destination identity and source seed; expected sources are the profile and official config | Distinguish canonical destinations from private seed bytes and their original source identities |
| `scripts/codex_switch_parity.py`, `_source_catalog_from_candidate`, `_validate_catalog_kind_receipt` | Managed source provenance depends on manifest fields plus a historical healthy receipt | Retain verified source identity independently when capture invalidates current health |
| `scripts/codex_switch_transaction.py` | Directory locks, executable swaps, text-artifact role/path guards and recoverable journals already exist | Extend these exact publication/recovery contracts, including explicit absent old state; do not create a competing rollback engine |
| `scripts/codex_switch_first_install.py`, `check_empty`, `publish` | Rejects existing target/profile/backup/pending transaction; uses no-replace first publication | Preserve strict bootstrap and reuse its absence/ownership guards for first full apply |
| `scripts/codex_switch_runtime_binding.py` | `DesktopRoots` and `discover_desktop_hosts` verify actual canonical Desktop; official manifests cannot choose a different App bundle | Freeze and revalidate actual inventory and saved bindings rather than accepting a caller-supplied substitute |
| `scripts/codex_profile_switch.py` | Existing public home/store arguments; init/capture `--app-cli-path` refers to CLI binding, not an App-root override | Preserve explicit compatibility bindings; independently freeze the actual Desktop reference and reject changes to either snapshot |
| `scripts/codex_switch_release_bundle.py` | Required runtime module/path inventories and import checks govern package completeness | Register any new runtime module and test it after an isolated prior-to-candidate package upgrade |

The global OpenSpec command is 1.3.1. The repository retains an independently
installed OpenSpec 1.7.0 CLI for its generated 1.7 skills; planning and final
validation use that CLI without changing its cache or the global executable.

## Goals / Non-Goals

Target State: a caller can obtain a verified complete runtime, finish its own
configuration, and apply it exactly once with durable recovery and no partially
published profile. Existing one-shot callers use the same engine. Full apply
requires actual Desktop compatibility proof; empty CLI-only bootstrap remains
a separate explicit behavior with no Desktop-ready claim.

Scope includes the generic command interface, private versioned update records,
candidate config/provenance, existing runtime-binding transaction integration,
first full publication, tests, package inventory and neutral public docs.

Non-goals: configuration recipes or business defaults, arbitrary hooks,
deployment manifests, caller auxiliary-file lifecycle, Desktop installation,
new dependencies, profile types beyond existing supported products, automatic
live activation/restart, provider-backed workstation testing, release
publication, unrelated workflow refresh and deletion of historical artifacts.
An unsupported external configuration location is not converted silently into
the supported `DIR/config.toml` contract.

## Skill Routing Ledger and Capability Evidence

- kind: behavior/API/persistence and compatibility change; mode: Full OpenSpec.
- artifact-status: final approved planning contract; implementation and the section 8 correction verified and delivered.
- capability-research: used; current CLI help/source, complete-runtime adapter,
  real bundle selection, package inventories and existing source/reference
  research establish the available primitives. No claim depends on a new
  platform capability or on absence from local code alone.
- decision-resolution: used; approved scope resolves staging, caller ownership,
  recovery commands, source policy and first-install behavior.
- decision-grilling: skipped; no unresolved product choice remains.
- implementation-planning: used; native `feature-intake`, `change-plan` and
  `ai-native-tech-plan` produce this contract and `tasks.md`.
- architecture-guidance: used through current source and native OpenSpec design
  decisions. The project-local `codebase-design` copy is absent; its pinned
  vendored guidance was read as method evidence. The approved fallback is these
  repository rules/source/OpenSpec artifacts; no skill install, workflow
  expansion or claim of a ready project-local primitive is made.
- domain-language-modeling: skipped; update, profile, binding, receipt and
  transaction already have established repository meanings.
- openspec-routing: used; native `openspec-propose` with 1.7 artifact instructions.
- test-first-execution: required at apply; public command and existing parity/
  transaction seams are the test surface.
- completion-proof/change-review: required after implementation, not satisfied
  by this planning document.

Comparison: caller orchestration of today's one-shot/capture commands cannot
provide isolation or durable same-ID recovery. A temporary real-profile swap
exposes unvalidated state and is rejected. A new deployment engine would take
ownership of caller configuration and is outside scope. A bounded update module
over the existing runtime/parity/transaction adapters is the systemic solution:
one implementation owns identity, publication and recovery for both interfaces.

Assumptions: existing installers and runtime probes retain their current bounded
interfaces. Native Desktop/provider acceptance on a live workstation remains
unverified and outside this delivery; isolated native runtime evidence must be
fresh. Present unsafe caches never become an excuse for a fallback.

## Decisions

### 1. A small persistent update interface

Add one runtime module, `scripts/codex_switch_update.py`, for durable session
identity and engine orchestration. The public Bash wrapper routes the four
subcommands and the existing one-shot adapter into it. Keep installer/runtime
validation in existing modules and parity/publication logic in their existing
modules. Further module splitting requires a concrete locality benefit and
updated write-set/package evidence, not a parallel workflow.

Supported command shapes:

```text
codex-switch update-internal stage --version V --json
codex-switch update-internal stage --current --json
codex-switch update-internal apply ID --from-codex-home DIR --json
codex-switch update-internal status ID --json
codex-switch update-internal cancel ID --json
```

Reuse existing store/home/target option conventions. `--version` and `--current`
are mutually exclusive. A successful stage emits exactly one JSON object on
stdout, including `schema_version`, `update_id`, `state`, `runtime_path`,
`actual_version`, `runtime_digest`, `source` (`staged` or `current`),
`profile_present`, `internal_app_bound`, `safe_to_restore`, and the
observed Desktop reference identity or explicit absence. Diagnostics go to
stderr; config/auth contents and secret-derived values are never emitted.
Apply/status/cancel reuse that stable identity and add terminal outcome,
transaction reference and structured safe error/recovery codes. Human output
remains available without `--json`; parser errors fail before mutation.

Staging saves observed store/home/target/profile/binding/reference identities or
absence. It does not require config/auth or a profile. Existing profile target
selection remains authoritative; incompatible explicit target arguments fail.
Without a profile, use the existing validated target-location contract. Full
apply is permitted only when the observed context still matches. Stage does not
perform parity or imply a healthy Desktop binding.
An existing complete runtime from successful strict CLI-only bootstrap is a
valid `--current` source without a profile. Resolve this as verified adoption,
with existing target identity and absent profile, rather than applying the
empty-target bootstrap predicate to it.

### 2. Durable records with explicit ownership

Use private versioned records below the selected store's update area, with
opaque path-safe IDs and atomic, fsynced updates. Directories are private and
config/auth snapshots use mode 0600. Record validation rejects symlinks,
unexpected path escapes, unsupported schemas and changed record/candidate
identity. Existing store metadata is not rewritten merely to allocate a stage.

Minimum record content: schema/version/ID, owning run identity and operation
state; canonical store/home/target identities; candidate kind/path/complete
manifest digest/actual version/ownership; selected Desktop identity and binding
snapshots; prior profile/selection/target identities or absence; bound apply
fingerprint and private input/source snapshots; transaction ID and confirmed
terminal receipt. Public JSON is an allowlisted view of this private record.

Public update states are `staging`, `staged`, `applying`, `applied`, `failed`,
`stale`, `cancelled`, or `recovery_required`. Preparation and publication
checkpoints remain in the existing transaction journal; its confirmed `committed`
terminal receipt maps to public `applied`. A transition records
enough intent before an external child or live mutation so that process death
can be classified later. Revalidation failures before publication produce a
terminal `stale` record. Unknown interrupted publication is never called success.

Stage retains a complete candidate generation while any update record or active
binding references it. Because the existing executable swap consumes a command
candidate, create only a transaction-owned publication facade/copy for that
swap; do not retire the retained staged runtime path returned to the caller.
`--current` records an externally owned reference and cannot delete or move it.
Retention is preferred to a new garbage collector; cancellation need not reclaim
runtime assets. Private abandoned preparation files remain bounded and owned.

### 3. Explicit private candidate profile context

Refactor the internal preparation seam to accept a validated candidate manifest,
source seed, auth snapshot, provenance and canonical final destinations without
loading those values from a temporarily rewritten profile. Existing `cmd_set_bin`
can adapt its live-profile inputs to that seam; staged apply supplies the private
captured home. The transaction still publishes to canonical profile/runtime
paths, and wrappers/receipts are rendered against final paths.

`ConfigInputs` must distinguish source bytes/identity from destination identity.
Do not relax path validation into arbitrary output paths or simply substitute a
runtime path for `profile_config`. Apply validates the whole home contract and
locations before capturing private inputs. Missing auth is represented as
absence only when current authentication semantics allow it; malformed, unsafe
or changed auth is blocking. Caller auxiliaries stay outside the capture set.

Preserve the existing runtime/profile/shared-config precedence for one-shot and
same-binary rebind. For explicit apply, the supplied home is the final seed;
saved profile values cannot silently override its selected model/provider/catalog
or reintroduce removed settings. Capture exact inputs, preserve private identity
records, project privately, and revalidate original files before the transaction
takes ownership of any same-path writes.

Probe processes use isolated homes and the existing bounded native protocol/
feature/config-write tests. They must not execute configured caller/business
hooks or load unrelated application state. Hook-bearing config must be projected
into a probe-safe configuration without mutating the final requested config or
claiming untested hook behavior. No hook runner interface is introduced.

### 4. Source provenance and current health are separate

Persist an independently validated original catalog provenance record containing
kind (`custom`, `runtime-cache`, `runtime-bundled`), original path/digest,
associated overlay identity and source-specific runtime binding evidence.
Provenance can be retained across capture even when a health receipt is removed
or invalidated. It is not a health receipt and cannot waive new parity checks.

During capture, recognize only this store's validated managed overlay. Verify
source/overlay identities before preserving provenance; reject unknown, missing
or inconsistent origin. Existing genuine legacy manifest/receipt provenance can
be validated and imported once without adopting its healthy flag as current
acceptance. A default overlay is never inferred to be custom from key presence.

Custom sources remain authoritative even for an official-looking slug. Default
sources reselect a valid cache or, only on recorded cache absence, bounded
offline output from their own corresponding runtime. Keep source snapshots and
binary-bound evidence transactional. Preserve exactly the established active
entry missing `multi_agent_version` to `v2` rule; no extra catalog normalization
or field changes enter this change.

### 5. Freeze actual reference selection and publish once

At stage and again at apply, resolve the same canonical Desktop inventory and
saved official/internal binding context. Existing `init --codex-bin` and
`--app-cli-path` do not authorize a different Desktop root: current resolver
selects the actual verified bundle. Persist its root, bundle ID, executable
identity and bundled CLI digest/version separately from saved binding state.
Before preparing and publishing, compare each object with its own stage
snapshot. Do not require saved profile paths to equal the resolved Desktop CLI
at the start of an update. Existing resolver warnings remain warnings; actual
reference identity and full compatibility requirements remain mandatory.

The released v0.1.15 `init --codex-bin` / `--app-cli-path` can create an
`explicit-compatibility` official manifest whose paths differ from the actual
Desktop CLI. The initial equality gate added in ef69b01 is a regression against
that supported state, not a prerequisite callers must repair. Preserve the
saved profile's mode and intent; staging must not rewrite it. Canonical Desktop
reference discovery never accepts an arbitrary saved CLI as a replacement.
Whether Desktop is required remains a full-apply versus CLI-only decision.

The approved repair uses production init output as migration input, then runs
stage/apply and switch/verify with unchanged official bindings. Separate tests
change the official manifest, Desktop bundle and bundled CLI after staging:
each must reject publication as stale without replacing concurrent changes.
A path-specific exception or caller-side manifest rewrite would conceal the
contract error and is rejected. No public interface or persistence schema
change is needed.
Return the selected reference so a caller can compare its own discovery.

There is no new Desktop-path override, App download or arbitrary official
reference binary. Explicit home/store parameters use the existing resolver and
are frozen, including separate official/internal home bindings and collision
rules. If an input layout cannot be represented, fail before capture/publication.

Do slow install/parity work outside the store publication lock, with short
record transitions and guarded per-update ownership. Before commit acquire the
existing mutation lock and revalidate every frozen dependency: complete
candidate, record, config/auth/source catalog, reference bundle, store and home
directories, saved profile/active selection/bindings, target/backup and absence
observations. Busy or stale operations fail explicitly. No lock spans the time
between commands.

Extend `commit_runtime_binding_bundle` and its artifact allowlist/journal as
needed to include candidate profile config/auth and independent provenance.
Prior absent values are explicit transaction state, not fabricated old files.
The publication transaction owns all runtime/manifest/wrapper/config/parity/
binding effects and installed-path postconditions. A durable transaction terminal
receipt precedes publication of the update's final result, resolving the crash
window without an additional publish attempt.

### 6. Idempotence and bounded interrupted recovery

The first accepted apply binds ID to an input fingerprint containing canonical
source home, exact config/auth presence and digests, catalog origin and relevant
location identities. Later different input is rejected. A confirmed committed
same-input call returns the original terminal receipt without probes/capture/
republish. A later drifted active environment is reported distinctly; it cannot
cause replay. Failed or cancelled IDs also return their saved terminal outcome;
a new stage is required to try different inputs.

`status` is observational: inspect record/journal/owned paths and report their
state without changing active files or executing the candidate. If commit is
durably confirmed but the update record lags, derive the terminal response from
the validated transaction receipt. An interrupted precommit owner becomes
cancelable unpublished work; incomplete/ambiguous publication becomes
`recovery_required`. Merely seeing new bytes is not proof of committed health.

`cancel` uses existing transaction recovery to reverse only uncommitted,
unchanged operation-owned writes. It cannot roll back a confirmed commit, delete
caller input or a `--current` runtime, or overwrite drifted recovery targets.
Repeated cancel returns the same terminal state. Conflicting live state remains
untouched with a concrete recovery diagnostic. There is no separate public
resume, rollback, retry or deployment command and no implicit apply replay.

### 7. Preserve bootstrap and one-shot compatibility

One-shot normal update supplies the existing effective profile context to the
shared engine. Preserve explicit and ordered automatic versions, helper exit
status, existing target validation, dry-run and full/CLI-only selection rules.

For no profile and no target, ordinary one-shot bootstrap remains CLI-only and
does not create a profile or fake Desktop readiness. Explicit staged full apply
with final inputs and a verified actual Desktop uses absent-aware candidate
context and no-replace transactional first publication. Any partial profile,
target, dangling link, backup or pending transaction blocks empty-state routing.
No Desktop means full apply fails; it does not silently downgrade to CLI-only.
An existing App binding always retains its required checks.

The required adoption sequence is: successful strict CLI-only bootstrap ->
caller makes actual Desktop available -> `stage --current` -> full apply of the
final home. The CLI already exists and the internal profile can remain absent
until apply commits. Validate the complete current-runtime identity and completed
bootstrap/publication evidence; target existence or a matching `--version`
response alone does not establish adoption eligibility. This path does not
rerun `check_empty`, reinstall the CLI or require creating a temporary profile.
It freezes the existing runtime and profile absence, prepares full parity, and
transactionally publishes first profile/config/auth/binding artifacts. Preserve
the existing runtime on failure/cancel and roll back only unchanged newly
created adoption artifacts. Partial/tampered profiles, unowned targets,
unknown backup/residue and pending transactions remain invalid state.

Desktop availability is still subject to the frozen-reference contract. If a
prior stage recorded Desktop absence, stage the current runtime again after
Desktop becomes available; do not silently change that earlier ID's reference.
Neither staging nor apply installs Desktop.

## Completion Contract and Critical Path

Completion requires every scenario in the delta specs to pass through public
commands or existing native parity/transaction seams, with RED/GREEN evidence,
fresh full regression, isolated native runtime proof, package upgrade/import
verification, two-axis read-only review, clean diff and delivery to the already
authorized PR. The implementation must not affect the live workstation.

Dependency order: durable staging and identity -> explicit private input and
provenance -> transactional full/first apply -> recovery/idempotence -> one-shot
and packaging integration -> native/full verification and review -> authorized
PR delivery. Every required behavior stays on this path; no delivery phase or
MVP defers it. Canonical execution source is exclusively `tasks.md`.

Incidental Finding Budget: one bounded in-scope RED/GREEN guard when required
for safe completion. Record optional unrelated findings in the tracked
`TASK_LEDGER.md` register as `DEFER_AND_CONTINUE`. Required failing acceptance
cannot be deferred. Severe ambiguity, a new dependency, unsupported public
contract expansion or new external effects require a concrete authority delta.

## Capability Slices and Execution Ledger

The main agent owns all integration and canonical artifacts. Task status lives
only in `tasks.md`; the following table maps its slices to owners and evidence.

| Slice / tasks | Owner and bounded write set | Evidence / automatic next action | Human gate |
|---|---|---|---|
| 1: stage/current and record identity | main; new `codex_switch_update.py`, CLI routing, installer adapter/runtime helpers only where required; new `test_codex_staged_update.py` | command RED/GREEN, package completeness, no live writes; continue slice 2 | none within approved contract |
| 2: private inputs and source provenance | main; `codex_switch_bindings.py`, `codex_switch_parity.py`, `codex_switch_home_sync.py`, `codex_switch_capture.py`, `codex_switch_plan.py`, `codex_switch_record.py`, focused parity/catalog/capture tests | final-seed and provenance regressions, hook-safe native probes; continue slice 3 | none |
| 3: transactional full/first apply | main; update/bindings, `codex_switch_transaction.py`, `codex_switch_first_install.py`, `codex_switch_home_select.py`, `codex_switch_runtime_binding.py`, staged/first-install/transaction tests | frozen source/reference/destination drift, absent-state and rollback evidence; continue slice 4 | none |
| 4: interruption/idempotence/cancel | main; update and existing transaction integration plus focused tests | subprocess failure injection, terminal crash-window and conflict tests; continue slice 5 | none |
| 5: compatible routing/docs/package | main; `scripts/codex-switch`, `codex_profile_switch.py`, `codex_switch_release_bundle.py`, `install.sh`, `scripts/package-release.sh` only if inventory plumbing needs it, README/docs, existing update/release fixtures | one-shot/dry-run compatibility, required-module inventory, installed package imports/tests; continue slice 6 | no release or live install |
| 6: integrated proof/review | main; focused tests, canonical artifacts and evidence; reviewers read-only | native/full matrix, Spec and Standards reviews, concrete findings closed; proceed existing PR delivery | only an actual scope/authority delta |
| 7: delivery | main; tracked change/spec/evidence/ledger only as authorized | exact-commit package checks, commit/push existing branch and PR readback; terminal when complete | standing PR authority; new release/archive/live effects require separate authority |

Source filenames in this table are relative to `scripts/` unless qualified.
No unrelated production file, plugin/skill, workflow tool or dependency is in
the write set. Updating a required runtime-module inventory is part of slice 5,
not release publication. Public docs use generic product language and placeholders.

## Continuation, Delegation and Goal Contract

Execution policy: `auto-until-terminal`. `execute-task` selects one unfinished
dependency-ready task from `tasks.md`; the orchestrator derives
`CONTINUE_NEXT_ITEM`, `CHECKPOINT_AND_CONTINUE`, `VERIFY_ACTIVE_CHANGE`,
`AWAIT_HUMAN`, `READY_FOR_EXTERNAL_EFFECT`, or `COMPLETE`. Continue immediately
for the first three. Routine task/review/checkpoint boundaries are not permission
gates. Existing authorization covers implementation, isolated tests, commit and
push to the current PR branch; it does not cover live workstation mutation,
release, destructive cleanup or automatic archive.

SubAgent Strategy: independent read-only Spec and Standards review can be
delegated under native contracts. Implementation stays serialized unless the
main agent validates disjoint write sets and a Goal/Scope/Constraints/
Verification/Evidence/Human Gate contract first. Main retains final ownership of
OpenSpec, root controls, `.planning/devflow`, packaging and completion claims.
The planning drafter returns to main for review and handoff; that handoff does
not end the approved overall task. Missing worker capability falls back to main.

Goal Contract: deliver the full staged update and preserved one-shot behavior
through all three capability specs, fresh isolated focused/full/native/package
evidence, closed review and the existing PR. Success means all `tasks.md` items
and acceptance criteria have truthful evidence. Stop only on completion,
explicit user stop or a concrete unresolved scope/authority/risk decision.
No live config/provider/Desktop mutation, dependencies, release or cleanup is
authorized. The runtime Goal, if used, is main-agent-owned; this durable contract
and the native state remain the recovery source.

Continue prompt: read this design, `tasks.md`, current native state and latest
verification record; reconcile actual diff and last proven task; execute the
next dependency-ready item; record true results and continue automatically.
Do not reopen archived changes or rerun confirmed committed update effects.

## Generated Artifact Strategy

Planning produces only canonical retained documentation and reads the existing
retained OpenSpec cache. It creates no disposable test roots. Before execution
creates outputs, main seals a native Generated Artifact Contract for a fresh
task/run-specific isolated root, its owner/run command and retention policy.
Tests allocate fresh HOME, store, App and provider fixtures inside that scope;
record child ownership and wait for children before any cleanup.

Use `PYTHONDONTWRITEBYTECODE=1` and Python `-B` for source and installed tests.
Keep real workstation HOME/store/App/config untouched. Retain evidence and
runtime/package fixtures until their verification/review consumers finish.
Cleanup needs the native fresh `AUTO_CLEAN` plan and exact-path terminal receipt;
unknown ownership or drift is retained/reported, never inferred from filenames.
All pre-existing local verification artifacts and research stay untouched.

## Validation Commands and Acceptance Mapping

Run each command with fresh isolated fixtures and record command, baseline,
exit code, counts and diagnostics. No checkbox is satisfied by a historical
result or a generated file's existence.

| Requirement group | Required validator |
|---|---|
| stage/current, stdout contract, ownership and unsafe state | `PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/test_codex_staged_update.py` |
| private seed, custom/default origin, overlay limits and hooks | staged suite plus `test_codex_parity.py`, `test_codex_model_catalog_routing.py`, `test_codex_bundled_model_sources.py`, `test_codex_parity_lifecycle.py` |
| frozen App/store/home/target/profile, first full apply and rollback | staged suite plus `test_codex_first_install.py`, `test_codex_transaction.py`, `test_codex_runtime_binding.py` |
| CLI-only bootstrap -> profileless current stage -> first full adoption | public staged/first-install sequence: completed bootstrap, actual Desktop fixture available before current stage, zero second installer calls, unchanged runtime identity, successful first profile/binding commit; negative partial/tampered/unowned/residue and failed-adoption rollback cases |
| killed stages/apply, commit-result window, repeat input, competing IDs and cancel | subprocess cases in staged suite; failure injection in transaction suite |
| one-shot/dry-run/current CLI rules and native proof | `test_codex_update_release.py`, `test_codex_profile_switch.py`, `test_codex_native_parity.py`, `test_codex_current_parity.py` |
| package completeness and previous-package upgrade | clean candidate package plus installed staged/routing/parity tests and manifest validation before and after tests |

Every Python command uses the `PYTHONDONTWRITEBYTECODE=1 python3 -B` prefix.
The broad source command is
`PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s scripts -p 'test_codex_*.py'`.
Run the profile test script's native entrypoint separately because it owns
additional shell/fixture checks. Native tests use verified runtime binaries in
fresh temporary homes with a loopback provider fixture and actual schema,
feature, bundled export and probe collectors; mocks do not satisfy that gate.
No live provider auth or inference is used. Native Desktop bundle fixtures
retain the real bundle-selection contract without mutating `/Applications`.

Additional exact gates:

```bash
bash -n scripts/codex-switch scripts/codex_env_setup install.sh scripts/package-release.sh
git diff --check
PYTHONDONTWRITEBYTECODE=1 python3 -B .dev-flow/skills-source/plugins/dev-flow/scripts/validate_workflow_state.py --repo . --json
```

For the repository-pinned OpenSpec 1.7.0 executable, use
`node .planning/devflow/verification/local/archive-pr-fixes-openspec-1.7-cache/_npx/833275e9fcb28e00/node_modules/@fission-ai/openspec/bin/openspec.js`
followed by `validate support-staged-internal-update --strict --no-interactive`,
`validate --all --strict --no-interactive`, `status --change
support-staged-internal-update --json` and `instructions apply --change
support-staged-internal-update --json` at the appropriate planning/final gate.
Assert the workflow validator's JSON `ok`, not only its process exit status.

Packaging: in the registered isolated output root, run
`PYTHONDONTWRITEBYTECODE=1 CODEX_SWITCH_DIST_DIR="$staged_update_dist" bash scripts/package-release.sh`.
The exact prior-to-candidate install/manifest/native checks use the existing
update/release fixtures and a separately empty installation HOME/prefix; record
their resolved commands and exact candidate commit in verification before
claiming package success. Never run `install.sh` against the live prefix.

## Risks / Trade-offs

- Retained candidates consume disk -> bounded owned records, explicit reference
  retention and no speculative garbage collection or foreign deletion.
- Transaction commit and update result can diverge on crash -> durable linked
  terminal receipt and read-only reconciliation; never replay publication.
- Source snapshots differ from final paths -> explicit source/destination
  context, final-path rendering and precommit input revalidation.
- Capturing managed overlays can erase origin -> independent validated
  provenance; reject ambiguity; default source cannot become custom.
- First full apply broadens absent-state publication -> no-replace guards,
  journaled absence and rollback only of unchanged created artifacts.
- Desktop changes between commands -> frozen actual bundle and saved-binding
  identities; require a new stage after drift rather than use a substitute.
- Installed package may omit a new module -> required inventory, clean
  prior-to-candidate upgrade, installed import/tests and manifest recheck.
- Older switcher cannot understand new update records -> versioned fail-closed
  records; no automatic downgrade migration or deletion of pending work.

## Migration Plan, Review and Final Verification

No eager store migration occurs at startup. New commands allocate their own
versioned records; provenance is imported only from fully validated existing
evidence during an authorized capture/preparation. Old health acceptance is
regenerated. Existing one-shot and CLI-only contracts remain regression gates.
Rollback of a failed apply uses the existing journal; reverting source does
not authorize changing a committed user's runtime or deleting update records.

Project Refresh Impact: not applicable. No DevFlow schema, skill, plugin,
refresh-contract or implementation-provider selection changes. Native state
records `implementation_readiness.required: false`. The pre-existing AGENTS
readiness guidance warning is not applicable to the selected local execution.

Review checklist: spec/task/command agreement; no temporary live-profile swap;
complete runtime/source identity; first publication versus partial state;
same-ID replay and crash-window behavior; no hook execution in probes; exact
rollback ownership; public JSON secrecy and neutral docs; preserved one-shot
rules; correct package module inventory; truthful source/native/installed proof.

Final verification is pending implementation. No runtime behavior, package
success, live Desktop readiness or completion is claimed by this plan.

## Implementation clarification

The wrapper retains strict empty bootstrap and uses the shared engine for normal
one-shot updates. A verified bootstrap receipt proves profileless current-runtime
adoption; arbitrary existing commands do not. The existing CLI-only transaction
receives the same update identity and terminal receipt when no Desktop and no
internal App binding exist. Public full apply never downgrades to CLI-only.

Probe isolation additionally touches `codex_switch_protocol_adapter.py` and
`codex_switch_verify.py` to isolate HOME and cwd only at preparation handshakes.
`test_codex_native_staged_update.py` supplies opt-in real runtime integration using
cloned packages, verified Desktop fixtures and a loopback provider. No healthy
receipt or persistent configuration-authority flag replaces ordinary configuration
selection semantics.
