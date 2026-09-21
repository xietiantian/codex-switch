# Task Ledger

## Completed PR Repair Archive

- Authorized on 2026-09-21: archive the five completed changes introduced by PR #1,
  synchronize their capability specs, and update the existing PR.
- All 39 implementation tasks and the existing isolated verification/review
  contracts are complete. The subsequent current-runtime repair resolves the
  separate feature/protocol findings recorded by the earlier grammar repair.
- Archive location: `openspec/changes/archive/2026-09-21-<change-name>/` for the
  five repairs listed below. Main specs preserve all 13 requirements and 37 scenarios.
- Evidence: `.planning/devflow/verification/archive-pr-fixes.md`.
- Runtime source code is unchanged by the documentation-only archive.

## Archived Repair: Current Runtime Optional Extensions

- Approved: precise optional classification plus isolated core-path verification,
  with commit/push to existing PR #1; baseline 59cfe33.
- Execution source: `openspec/changes/archive/2026-09-21-classify-current-runtime-extensions/tasks.md`.
- Scope/evidence: that change’s design and `.planning/devflow/verification/classify-current-runtime-extensions.md`.
- Status: exact classification and native probes implemented; 112 parity/current
  cases, native regression, 227 profile cases and transaction guards pass.
  Both review axes pass after bounded fixes. Implementation a645137 is pushed
  to PR #1; clean package upgrade, nine installed tests and local test-entrypoint
  refresh are complete. No live installation or release publication occurred.
- No live install/Desktop mutation, external provider call or file-ID emulation.
- Incidental finding TYPED-EVENTS-2026-09-21: CONTINUE_WITH_MINIMAL_GUARD.
  Native exec JSON lacks the previously assumed typed role/source fields.
  The approved core-proof contract requires app-server event and thread/read
  evidence; scope, failure guards and isolation are recorded in the design.

## Archived Repair: Namespaced Feature Keys

- Approved: continue the runtime compatibility repair with isolated tests and
  updates to existing PR #1; baseline 250c0ca.
- Execution source: `openspec/changes/archive/2026-09-21-support-namespaced-feature-keys/tasks.md`.
- Scope: shared feature-name grammar, public-seam regressions and evidence.
- Evidence: `.planning/devflow/verification/support-namespaced-feature-keys.md`.
- Status: RED/GREEN, 102 parity and 227 profile cases, native inventory,
  installed-package checks and both reviews pass. Implementation a899eed is
  pushed to PR #1; its verified package and local test runner are refreshed.
- Feature classifications, live installation and provider requests are excluded.

## Archived Repair: Custom Model Catalog Parity

- Approved: skip official-model comparison for validated custom catalogs,
  preserve runtime/source checks, isolate verification, and update PR #1.
- Execution source: `openspec/changes/archive/2026-09-21-respect-custom-model-catalog/tasks.md`.
- Baseline: 9c9bc73; scope and write set are recorded in that change's design.
- Evidence: `.planning/devflow/verification/respect-custom-model-catalog.md`.
- Status: implementation, isolated regressions, package validation and both
  review axes pass. Commits f116f6d and 3a92076 are pushed to PR #1; its
  description includes custom-catalog applicability and final verification.
- Live installation, provider requests, and release publication are excluded.

## Archived Repair: First Internal Installation

- Approved: restore first-install behavior on the existing PR branch, isolated
  verification, and PR update. No live installation or release publication.
- Execution source: `openspec/changes/archive/2026-09-21-restore-first-internal-install/tasks.md`.
- Scope: public update routing, absent-state candidate preparation, safe first
  publication, tests, and generic public documentation.
- Evidence: `.planning/devflow/verification/restore-first-internal-install.md`.
- Status: implementation, isolated verification, and independent review pass;
  22 first-install tests, all 196 update/release and 227 profile cases accounted
  for, including successful rechecks of corrected package fixtures.
- Delivered: PR #1 updated; implementation commit e41c134. No release/live install.
- Historical records and unrelated research remain preserved.

## Archived Repair: Standalone Runtime Installation

- Approved: generic package-preserving installer repair, isolated testing, and PR.
- Execution source: `openspec/changes/archive/2026-09-21-support-standalone-installer-runtime/tasks.md`.
- Scope: installer adapter, runtime validation/launcher, tests, public docs.
- Non-goals: installed workstation changes, release publication, unrelated cleanup.
- Evidence: `.planning/devflow/verification/support-standalone-installer-runtime.md`.
- Delivered: https://github.com/cYz26/codex-switch/pull/1; implementation and isolated verification complete.
- Historical goals below are retained as prior records, not current authority.

## Goal Contract

- goal_id: `019f8f8f-e64c-7093-af73-2c0247cf2891`
- objective: Repair the remaining 10 P1 and 3 P2 core findings for the official
  and internal product profiles through four strictly validated Full OpenSpec
  changes, with TDD, failure injection, isolated runtime evidence, and no false
  completion claim.
- scope_in: transactional official/internal switch/capture/restore; versioned
  backup and lock; canonical ChatGPT Desktop/runtime binding; internal managed
  launcher/backend attestation; schema-scoped proxy; version-safe config writes;
  semantic offline TOML merge; canonical launcher home sync; fail-safe package,
  install/self-update, internal update, plugin repair, release planning, and
  bounded sanitized verification.
- scope_out: arbitrary/custom profile expansion; current Codex.app as a healthy
  host; ChatGPT Classic; live workstation profile/App switch except the
  explicitly authorized scoped internal rebind recorded below; live
  install/update/plugin mutation; release/tag/commit/push; provider/root-state
  migration; legacy skill cleanup; signing; new production dependency.
- acceptance_criteria: every active finding maps to a delta scenario and a RED
  then GREEN regression; the four OpenSpec task lists complete; focused suites
  and `scripts/test_codex_profile_switch.py` pass; `openspec validate --all
  --strict --no-interactive` reports zero failures; shell/Python syntax and
  `git diff --check` pass; only isolated temporary-home/store/process smokes run.
- stop_conditions: stop for a required public CLI/persistence contract beyond
  the approved specs, a destructive workstation/external action, a production
  dependency, an unsupported required schema decision, or evidence that the
  selected architecture cannot preserve rollback/version safety.
- knowledge_update_target: none; durable truth is this ledger, the four OpenSpec
  changes, `.planning/STATE.md`, and per-change verification records.

### 2026-07-28 Active Incident Goal Addendum

- objective: repair internal-profile switching so staged installation cannot
  reset live Plugins/UI/config state or place a failed candidate ahead of the
  codex-switch shim, then prove one real internal switch binds the configured
  internal backend through manifest, wrapper, App environment, proxy child,
  and running app-server ownership.
- scope_in: hermetic installer environment in `scripts/codex_env_setup`;
  staged-update regression in `scripts/test_codex_update_release.py`; exact
  backup and removal of three confirmed `.zshrc` blocks; recoverable move of
  three confirmed candidate directories; verified-source install; one
  update/rebind/switch and bounded ChatGPT restart; config/plugin/runtime
  verification.
- scope_out: Protocol Adapter changes, plugin refresh, Desktop global-state
  allowlist expansion, credential or identity migration, provider/model/API
  changes, legacy skill migration/cleanup, dependency changes, destructive
  deletion, provider-backed Desktop task, Git effects, release, and archive.
- acceptance_criteria: harmful fake installer RED becomes GREEN on Python 3.12
  and system Python 3.9; focused/full update tests, shell/static/OpenSpec/
  workflow/package/diff gates pass; private scratch leaves no residue; live
  config/plugins remain complete; bare shell and Desktop ownership resolve the
  configured internal binary after restart; status/Doctor/verify are clean for
  this incident chain.
- stop_conditions: any target identity or shell-block drift; need to edit
  proxy/global-state/plugin policy; credential/identity migration; destructive
  overwrite; public contract or dependency expansion; failure outside the
  recorded incident chain.

## Scope Decisions

- Supported product profiles are `openai-official`/`official` and `internal`.
  The review's arbitrary-profile path traversal item is deferred by explicit
  user scope and is not counted among the active 10 P1 findings.
- `/Applications/ChatGPT.app` with bundle id `com.openai.codex` is the only
  current Desktop host. `/Applications/Codex.app` is migration observation
  only. `/Applications/ChatGPT Classic.app` is never a candidate.
- AppServer owns config write/version state. The proxy will not patch
  `config.toml` after a response. A temporary-home internal `0.142.4` probe
  proved versioned writes preserve unrelated MCP/marketplace/plugin/skill data;
  unknown behavioral capability fails before forwarding.
- Implementation tasks are serialized in dependency order. Read-only review or
  test-result review may use parallel agents; production write sets do not
  overlap.

## Skill Routing Ledger

- request_kind: broad compatibility, state-safety, error-handling, release, and
  verification repair
- workflow_mode: Full OpenSpec
- capability-research: required / used; installed CLI/App/schema/process/update
  ownership and isolated config-write behavior were verified
- decision-resolution: required / used; prior review routes plus user scope and
  approval resolve profile/App boundaries
- decision-grilling: skipped; no open product question remains
- implementation-planning: required / used; AI-native Target State,
  Completion Contract, Capability Slices, and Execution Ledgers are recorded
- architecture-guidance: required / used; transaction, runtime binding,
  protocol adapter, promotion, policy, and bounded process seams are explicit
- test-first-execution: required / used; TPS closure is 25/25 covered and
  207/207 dual-runtime verified, with the remaining changes still serialized
- root-cause-diagnosis: required / used in the source review and isolated probes
- execution-orchestration: required / used through this ledger and validated
  Agent Task Contracts
- change-review: required / used; the stable 154-test TPS review found release
  blockers and reopened implementation
- completion-proof: required / used; `VER-001` final source matrix passed
- domain-language-modeling: skipped; existing runtime/profile/release terms are
  sufficient
- openspec-routing: required / used through four Full OpenSpec changes
- gsd-routing: skipped; this is one approved repair goal, not a new roadmap phase

### 2026-07-28 Incident Skill Routing Addendum

- request_kind: bug, compatibility, update-safety, and error-handling repair
- workflow_mode: Full OpenSpec through `internal-official-feature-parity`
- capability-research: required / used; current installer, config/plugin
  snapshots, shell PATH, candidates, installed source, and runtime ownership
  were inspected
- root-cause-diagnosis: required / used; inherited installer
  `HOME`/`CODEX_HOME`/`PATH` is the first unauthorized mutation
- decision-resolution: required / used; the user selected hermetic Scheme A
- implementation-planning: required / used; target, completion, slices,
  write-set, validation, recovery, and rollback are recorded
- architecture-guidance: skipped; reuse the existing staged installer seam
- test-first-execution: required / used; harmful-installer RED and dual-runtime
  GREEN/full adjacency are complete through task 8.3A.3
- completion-proof: required / pending after the real restart verification
- openspec-routing: required / used; existing active change updated
- gsd-routing: skipped; this is a bounded incident in the active change

### 2026-08-04 Independent App/CLI Profiles Addendum

- objective: make internal shell CLI plus official ChatGPT Desktop a managed,
  recoverable, and diagnostically healthy selection without changing existing
  synchronized `internal`/`official` commands.
- scope_in: additive `--app-profile official`; explicit CLI/App identities in
  active state; one selection interface; transactional switch integration;
  split-aware status, Doctor, verify, wrapper, docs, skill, and package tests.
- scope_out: another profile pairing; custom split profiles; internal parity,
  proxy, provider, model, API, auth, or update-policy changes; live install,
  switch, App restart, dependency, Git, release, archive, cleanup, or
  destructive effect.
- acceptance_criteria: every delta scenario has RED/GREEN evidence; isolated
  split commit and rollback pass; synchronized/legacy behavior stays green;
  status/Doctor/verify use the correct surface; complete suites, static,
  strict OpenSpec, workflow, plugin-eval, package, and diff checks pass.
- stop_conditions: required write-set or public-state expansion, another
  pairing, parity/proxy/update change, live/external effect, dependency, severe
  ambiguity, or unrelated dirty-work overlap that cannot be preserved.
- canonical_execution_source:
  `openspec/changes/independent-app-cli-profiles/tasks.md`
- approval: the user's explicit implementation request authorizes only the
  source/test/docs/OpenSpec/ledger/namespaced-state/verification write set.

#### Skill Routing

- request_kind: feature, runtime binding, state compatibility, error handling
- workflow_mode: Full OpenSpec
- capability-research: required / used; current CLI, App, manifests, wrapper,
  process ownership, transaction, diagnostics, tests, and package were audited
- decision-resolution: required / used; internal binary/home and official
  bundle/home are the resolved target
- decision-grilling: skipped; no Open Question remains
- implementation-planning: required / used through the complete change
- architecture-guidance: required / used; the selection seam is canonical
- domain-language-modeling: skipped; existing domain terms are sufficient
- test-first-execution: required / used; all five slices have recorded RED/GREEN
- change-review: required / used; two-axis spec and standards review is clean
- completion-proof: required / used; fresh complete matrices, package identity,
  strict/static/workflow/diff gates, review, and residual risks are recorded
- openspec-routing: required / used

### 2026-08-10 Live Bootstrap Portable-Identity Repair Addendum

- objective: restore the required next-functional-CLI synchronization when the
  internal backend reports an older installed `portable_exact` version while
  the safely resolved marketplace source already matches the newer official
  desired artifact, and make the cold preflight visibly active.
- scope_in: existing `independent-app-cli-profiles` OpenSpec task 12;
  production Plugin catalog/source interpretation; exact source and target
  attestation; precise materialization finding; functional-preflight stderr
  progress; focused tests; README/SKILL; ledger/state/verification evidence.
- scope_out: physical cache sharing/copy/delete; live Plugin installation or
  cache mutation; another codex-switch install; functional `codex` or split
  retry; App stop/restart; internal update; project migration; dependency;
  Git; release; archive; cleanup; credential or destructive effects.
- acceptance_criteria: a public-seam regression using the live JSON shape is
  RED before production edits and GREEN afterward; an older installed version
  cannot override a source manifest/tree that exactly matches desired; source
  mismatch and real unsafe cache remain distinct; post-add target attestation,
  config/receipt rollback, help/version read-only behavior, unchanged
  generation zero-write/zero-network behavior, and flushed progress pass;
  focused/broad/static/spec/workflow/package/diff evidence is fresh.
- stop_conditions: target backend cannot produce the exact independent
  artifact; repair requires cache copying/deletion, a public persistence schema
  expansion, dependency/project migration, live mutation, or another excluded
  effect.
- canonical_execution_source:
  `openspec/changes/independent-app-cli-profiles/tasks.md` task 12.
- approval: the user's confirmation authorizes this source/test/docs/OpenSpec/
  ledger/state/verification write set only.

#### Repair Skill Routing

- request_kind: live acceptance bug, compatibility, error handling, and
  startup observability
- workflow_mode: Full OpenSpec
- capability-research: required / used; actual official/internal homes,
  sanitized projection, both cache layouts, target backend JSON, source
  manifests, runtime binding, and elapsed phase timings were inspected read-only
- decision-resolution: required / used; the user confirmed the systemic repair
- decision-grilling: skipped; the live evidence and confirmed solution leave no
  product question open
- implementation-planning: required / used through updated proposal, design,
  specs, and task 12
- architecture-guidance: skipped; the existing materializer/preflight seams
  remain the correct owners and no new module boundary is needed
- domain-language-modeling: skipped; installed target state, portable source
  identity, desired identity, and materialization receipt already cover the
  repair
- test-first-execution: required / ready; public seams are production shared
  materialization/reconcile and managed functional preflight progress
- root-cause-diagnosis: required / used; the production parser/materializer
  conflates an installed version with portable source identity
- change-review: required / pending after GREEN and broad proof
- completion-proof: required / pending
- openspec-routing: required / used; strict validation is green and task 12.1
  is dependency-ready

### 2026-08-11 Backend-Managed Functional Acceptance Repair Addendum

- objective: make the next functional managed internal CLI invocation complete
  successfully when current official Plugin sources and compatible internal
  installed targets intentionally have different versions and bytes.
- scope_in: existing `independent-app-cli-profiles` OpenSpec task 13; catalog
  installed/available provenance; exact desired-source proof; batched native
  target reconcile and fresh post-call catalog; independent target-cache/Skill
  proof; precise findings; focused tests; README/SKILL; ledger/state/evidence;
  exactly one non-interactive command through the current managed shim.
- scope_out: split retry or another install; App stop/restart/mutation; internal
  binary update or internal-App compatibility; cache copy/link/delete/cleanup;
  dependency/project migration; Git; release; archive; credential/provider or
  destructive effects.
- acceptance_criteria: real-shape installed `browser@openai-bundled`
  `26.721.41059` plus exact official source `26.803.61601` is RED before
  production edits and GREEN afterward; installed provenance is
  order-independent; every pending backend-managed selector invokes native add;
  one fresh catalog proves unique installed target keys; revision-like keys and
  manifest versions remain distinct; unverified target, invalid catalog,
  source mismatch, and unsafe cache stay precise; focused/broad/static/spec/
  workflow/package/review gates pass; the managed functional command exits zero
  while App binding/process state remains unchanged.
- stop_conditions: repair requires public persistent schema expansion, cache
  copying/deletion, dependency/migration, App mutation, split/install/internal
  binary update, or another excluded effect; the backend cannot produce a safe
  independently attestable target after one bounded functional attempt.
- canonical_execution_source:
  `openspec/changes/independent-app-cli-profiles/tasks.md` task 13.
- approval: the user's systemic-repair confirmation authorizes the scoped
  source/test/docs/control-plane changes and one functional managed-shim command
  with its bounded internal backend/cache/config/shared-generation effects.

### 2026-08-11 Managed Runtime-Config Idempotence Addendum

- objective: make repeated managed runtime rendering byte-idempotent so the
  internal config does not grow one blank line per unchanged render.
- scope_in: existing `independent-app-cli-profiles` OpenSpec task 14;
  managed-comment cleanup in `scripts/codex_switch_home_sync.py`; one focused
  repeated-render regression; adjacent config/profile verification; OpenSpec,
  ledger, namespaced-state, and verification evidence.
- scope_out: live config rewrite, profile switch, installation, App
  stop/restart/mutation, Plugin/cache operation, dependency activation or
  project migration, credentials, Git, release, archive, cleanup, or unrelated
  formatting normalization.
- acceptance_criteria: the focused regression fails before production edits
  because two consecutive last-runtime renders differ, then passes with
  byte-identical repeated output; unrelated user comments and non-adjacent
  blank lines remain preserved; focused and adjacent suites, strict OpenSpec,
  static checks, and `git diff --check` pass.
- stop_conditions: the repair requires changing TOML semantics, broad
  whitespace normalization, shared/profile ownership, a dependency, or any
  excluded live/external effect.
- canonical_execution_source:
  `openspec/changes/independent-app-cli-profiles/tasks.md` task 14.
- approval: the user's request to optimize the diagnosed blank-line growth
  authorizes this bounded source/test/control-plane repair only.

#### Repair Skill Routing

- request_kind: live acceptance bug, compatibility, catalog provenance, error
  handling, and functional verification
- workflow_mode: Full OpenSpec
- capability-research: required / used; live sanitized catalog shape, source
  manifests, target caches, runtime binding, App process, and production seams
  were inspected read-only
- decision-resolution: required / used; the user confirmed the systemic repair
  and functional acceptance boundary
- decision-grilling: skipped; source and target ownership are already explicit
- implementation-planning: required / used through updated proposal, design,
  specs, and task 13
- architecture-guidance: required / used; one catalog projection preserves the
  two identities without adding a persistence schema or new subsystem
- domain-language-modeling: skipped; available source, installed target,
  materialization receipt, and backend-managed policy are sufficient
- test-first-execution: required / ready; agreed public seams are catalog JSON,
  production materializer, and managed functional shim
- root-cause-diagnosis: required / used; source/target identity conflation and
  lost installed provenance reproduce the live failure
- change-review: required / pending after GREEN and broad proof
- completion-proof: required / pending through source/package matrices and the
  one authorized functional invocation
- openspec-routing: required / used; task 13 is dependency-ready after strict
  validation

### 2026-08-11 Failed Release-Upload Starter Recovery Addendum

- objective: make automatic release reconciliation recover the exact GitHub
  failed-upload state where `v0.1.14` exposes no custom downloadable assets but
  rejects `install.sh` because a same-name asset remains reserved.
- scope_in: existing `independent-app-cli-profiles` OpenSpec task 15;
  `scripts/release_auto.py`; focused update/release tests; OpenSpec, ledger,
  namespaced-state, and verification evidence.
- scope_out: live GitHub Release deletion/upload, workflow rerun, DevFlow
  migration apply, dependency, commit, push, archive, cleanup, or unrelated
  release-policy expansion.
- acceptance_criteria: a deterministic public-seam harness reproduces `asset
  under the same name already exists: [install.sh]`; the adapter inventories
  exact asset ID/name/state/size; only a canonical zero-byte `starter` can be
  deleted after tag validation; readback precedes upload; uploaded, non-zero,
  duplicate, and unsupported states remain fail closed; focused/full/static/
  strict/workflow/diff gates pass.
- stop_conditions: repair requires `--clobber`, deleting an uploaded or
  ambiguous asset, changing tag identity, a dependency, live external effects,
  or any excluded authority.
- canonical_execution_source:
  `openspec/changes/independent-app-cli-profiles/tasks.md` task 15.
- approval: the user's failed-Action report authorizes the bounded
  source/test/control-plane repair only.

#### Repair Skill Routing

- request_kind: bug, release compatibility, idempotence, and error handling
- workflow_mode: Full OpenSpec
- capability-research: required / used; current GitHub release page, failed
  Action, release-assets API behavior, local adapter, and exact tag were
  inspected
- decision-resolution: required / used; exact zero-byte starter recovery is
  selected over clobber or broad deletion
- decision-grilling: skipped; the failure evidence leaves no product decision
  open
- implementation-planning: required / used through task 15
- architecture-guidance: skipped; the existing release adapter/reconciler owns
  the boundary
- domain-language-modeling: skipped; release, uploaded asset, and starter asset
  are sufficient
- test-first-execution: required / used; RED and GREEN were recorded through
  the public release adapter/reconciler seams
- root-cause-diagnosis: required / used; hidden failed-upload residue explains
  the visible-empty/upload-conflict contradiction
- change-review: required / used; exact-ID deletion, state/size guards,
  readback, checksum verification, and no-clobber behavior were reviewed
- completion-proof: required / used; focused, full, profile-adjacent, static,
  strict OpenSpec, workflow, and diff gates passed
- openspec-routing: required / used

### 2026-08-11 Post-Submit Release Disappearance Follow-up

- observed_result: commit `85dc960` reached `origin/main`; Auto Release run
  `31500533015`, job `93809040291`, passed steps 1-10 and failed step 11,
  `Reconcile existing release assets`, with exit code 2.
- evidence_boundary: the complete authenticated step log is unavailable. The
  post-run tag-based Release inventory is unavailable while tag `v0.1.14`
  remains at `19a243342ef9f78776b3fad0b2292198845147d3`; combined with the
  reconciler's only post-delete failure branch, Release disappearance is the
  leading hypothesis, not a claimed verbatim remote error.
- required_behavior: if exact starter deletion makes the Release readback
  missing, revalidate the tag, create one verified-tag draft, require an empty
  draft readback, then upload, publish, and verify the canonical checksums.
- fail_closed_guards: creation failure, missing/non-draft/non-empty readback,
  and tag movement stop before later mutation; existing no-clobber and asset
  conflict rules remain unchanged.
- authority: local task 15 source/test/OpenSpec/control-plane continuation only.
  Commit, push, workflow rerun, live Release mutation, migration, dependency,
  archive, and cleanup remain gated; the prior submit authority is consumed.

### 2026-08-13 Draft Release Discovery Follow-up

- observed_result: commit `6a5fa85` reached `origin/main`; Auto Release run
  `31666160863` failed after draft creation and five readback attempts with
  `GitHub release v0.1.14 is missing after draft creation`.
- remote_state: `origin/main` remains `6a5fa85`; tag `v0.1.14` remains
  `19a243342ef9f78776b3fad0b2292198845147d3`; `v0.1.15` is absent; the
  published Releases index still marks `v0.1.13` latest; and `install.sh`,
  `run.sh`, and `codex-switch.tar.gz` for `v0.1.14` return 404.
- root_cause: `GitHubCliAdapter.inspect_release()` treated a 404 from
  `/releases/tags/{tag}` as definitive absence. That endpoint does not reliably
  expose the newly created draft, while GitHub's authenticated Releases
  collection includes drafts for callers with push access.
- required_behavior: use the tag endpoint as the normal fast path; on explicit
  404 only, paginate the Releases collection and select one exact `tag_name`
  match. Preserve missing on no match and fail duplicate, malformed, non-404,
  invalid-JSON, or unbounded states before any later mutation.
- authority: gate `614cc025...` was consumed by commit `6a5fa85`, its push, and
  run `31666160863`. The user's failed-run report authorized the bounded
  source/test/OpenSpec/control-plane follow-up, and the subsequent `授权`
  decision resolved gate `a40cea2a...` for one verified commit/push plus the
  exact `v0.1.14` recovery and atomic `v0.1.15` publication effects.
- verification: draft-discovery adapter matrix passes 6/6; complete Python
  3.12 Update/Release passes 171/171 in 316.787 seconds; Profile/Wrapper passes
  227/227 in 296.864 seconds; Python AST 61/61, Bash 5/5, repository JSON 29/29,
  active/all strict OpenSpec 22/22, DevFlow validation, and diff checks pass.
  After authorization, fresh no-TTY pre-submit reruns pass Update/Release
  171/171 in 313.754 seconds and Profile/Wrapper 227/227 in 284.600 seconds.
- human_gate: `a40cea2a...` is resolved through
  `draft-release-discovery-submit-authority-grant.json` for one verified
  commit/push and the push-triggered `v0.1.14` recovery plus atomic `v0.1.15`
  publication. It does not authorize force push, manual broad Release edits,
  migration, install, cleanup, archive, dependency/credential changes, or
  unrelated runtime effects.

### 2026-08-05 Shared Plugin/Skill Configuration Reopen Addendum

- objective: extend the supported internal-CLI/official-App split with one
  secret-safe Plugin/Skill desired generation, independent per-backend plugin
  materialization, automatic App-to-next-functional-CLI readiness, and an
  explicit stopped-App apply boundary for CLI-originated pending changes.
- scope_in: store-owned canonical projection and receipts; three-way baselines
  and fail-closed conflict; `marketplaces.*`, `plugins.*`, and
  `skills.config`; personal/plugin/project Skill ownership; conditional target
  repair; internal shim preflight; `sync-shared`; status/Doctor/verify; docs,
  package, tests, and the other-config ownership matrix.
- scope_out: shared physical plugin cache; running-session hot load; supervisor,
  watcher, daemon, or official App wrapper; hook-trust portability; auth,
  credentials, MCP/apps/connectors, sessions/history/databases, models/providers,
  permissions, automation data, broad config migration, cache cleanup, live
  install/switch/restart/repair, dependency, Git, release, or archive.
- acceptance_criteria: every added delta scenario has RED/GREEN evidence;
  functional internal backend execution is blocked until desired config and
  independent plugin/Skill materialization verify; unchanged generations are
  zero-write/zero-network; App-running pending and stopped-App explicit sync
  are covered; conflicts and secrets are fail-closed; complete source/static/
  strict/workflow/package/plugin-eval/review gates pass.
- stop_conditions: need for a new secret/public schema, shared mutable cache,
  supervisor/watcher/App wrapper, destructive migration, live profile/cache
  mutation, dependency, or overlap with unrelated dirty work that cannot be
  preserved.
- canonical_execution_source:
  `openspec/changes/independent-app-cli-profiles/tasks.md`
- approval: the user's explicit implementation request authorizes the reopened
  source/test/docs/OpenSpec/ledger/state/verification write set only.

#### Skill Routing

- request_kind: feature, persistence, compatibility, synchronization, and
  error handling
- workflow_mode: Full OpenSpec
- capability-research: required / used; official documentation plus current
  config, cache, shim, repair, process, diagnostic, and package evidence audited
- decision-resolution: required / used; official App is initial bootstrap
  authority and automatic acceptance ends at the next functional internal CLI
- decision-grilling: skipped; local evidence and the user's App-originated
  direct-use requirement resolve the bounded lifecycle
- implementation-planning: required / used; OpenSpec slices 6-9 are canonical
- architecture-guidance: required / used; neutral sidecar, three-way baseline,
  explicit artifact policy, and one deep reconcile interface
- domain-language-modeling: skipped; desired generation, projection, receipt,
  pending, conflict, and materialization extend existing terms sufficiently
- test-first-execution: required / used; tasks 6-8 record public-seam RED/GREEN,
  failure injection, and the real subprocess SIGKILL lease regression
- change-review: required / used; independent final review closed every P1/P2,
  including the Decision 15 orphan-backend race, and returned `APPROVE`
- completion-proof: required / used; fresh focused/full/static/spec/workflow/
  package/plugin-eval evidence is recorded without a live apply
- openspec-routing: required / used

## Target State

Official/internal state mutation is one locked transaction with v2 evidence and
rollback. Every consumer resolves a single immutable binding whose current host
is ChatGPT.app and whose internal Desktop chain includes both managed launcher
and attested backend. Proxy transformations are exact and capability-scoped;
config writes keep backend version authority; offline TOML and launcher sync use
one semantic policy. Installation/update/release/plugin/verification paths are
contained, typed, recoverable, bounded, and sanitized.

## Completion Contract

| Contract | Required proof | Status |
|---|---|---|
| Transactional profile state | corruption/conflict/effect failure injection plus full rollback | done; Desktop no-op focused 4/4, transaction 219/219, profile 198/198, strict/static/package gates, supported live recovery, official App launch, and restored internal ownership passed |
| Canonical runtime binding | ChatGPT/Classic/legacy fixtures, process parser, launcher/backend attestation | done; 33/33 OpenSpec rows, runtime 55/55 on both runtimes, transaction 215/215 |
| Schema-scoped proxy/config | generated schema fixtures, behavioral receipt, exact-path negatives, real-chain E2E | done; 34/34 OpenSpec rows, protocol 37/37 on both runtimes, lifecycle closure verified |
| Fail-safe update/release | containment/promotion/update/catalog/verify/release fake-adapter tests | done; same/older metadata gate, strict newer validation, clean installed status, and restart guard verified |
| Regression compatibility | complete profile suite zero failures | done; final post-integration profile suite 195/195 |
| Planning integrity | all four changes complete and strict-valid | done |
| Final source integrity | shell/Python checks, package isolation, diff check | done; Bash 5/5, AST 54/54 and imports 46/46 on both runtimes, package and diff gates green |
| Hermetic internal installer and live recovery | harmful installer cannot mutate live shell/config state; precise residue repair and one real internal ownership proof | exact live residue recovery complete through task 8.3A.4 at 74/84; runtime proof pending |

## Finding-to-Change Map

| Finding | Change | Completion evidence |
|---|---|---|
| Snapshot isolation/unrestorable backup | `transactional-profile-state` | v2 snapshot/restore RED/GREEN |
| Directory restore/preflight | `transactional-profile-state` | recursive state, payload containment, zero-mutation failures |
| Switch/capture atomicity and stale auth | `transactional-profile-state` | injected rollback and cloned capture |
| Runtime/App ownership | `canonical-runtime-binding` | ChatGPT-only current resolution and shared finding codes |
| Internal rebind/proxy child drift | `canonical-runtime-binding` | staged smoke and backend child attestation |
| Schema-blind proxy/config stale recovery | `schema-scoped-app-proxy` | exact transform, no post-write patch, identity tests |
| Plugin/skill uninstall revival across restart/switch | `schema-scoped-app-proxy` | current runtime exact replacement, stale snapshot non-revival, bidirectional usage-state tests |
| Desktop memory-history UUID replay | `schema-scoped-app-proxy` | strip only top-level ResponseItem IDs on `thread/resume.params.history`, matching disk resume |
| Install/self-update/package/release safety | `fail-safe-update-release` | containment, immutable promotion, reconciliation |
| Update false success/downgrade | `fail-safe-update-release` | ordered policy and postcondition tests |
| Plugin catalog mass-disable | `fail-safe-update-release` | typed catalog and zero-write uncertainty tests |
| Verify false-pass/secret exposure | `fail-safe-update-release` plus runtime binding | state machine, bounded sanitizer, manifest authority |
| TOML partial fallback | `schema-scoped-app-proxy` | real parser and complete spans |
| Wrapper home-sync duplication | `schema-scoped-app-proxy` | canonical entrypoint and symlink E2E |
| Missing store lock | `transactional-profile-state` | directory-inode contention and release tests |
| Known runtime socket directory misclassified as shared support | `transactional-profile-state` | official shared dry-run with real `ipc` and `mcp-oauth-locks` Unix sockets |
| Desktop global-state and shared-support byte-identical effects falsely claimed transaction ownership | `transactional-profile-state` | concurrent App-write regression, strict legacy no-op classifier, real-write fail-closed guard, and supported live recovery |

## Tasks

| task_id | summary | owner | write_set | required_evidence | review_gate | status |
|---|---|---|---|---|---|---|
| PLAN-001 | Complete four Full OpenSpec proposals/design/specs/tasks | main + read-only design reviewers | `openspec/changes/{transactional-profile-state,canonical-runtime-binding,schema-scoped-app-proxy,fail-safe-update-release}/**` | status complete, strict validate | main design reconciliation | done |
| TPS-001 | Apply `transactional-profile-state` by TDD | main, serialized | transaction/backup/restore/capture/switch/effect/lifecycle modules and focused test | 25-row RED/GREEN log, focused/full tests, rollback receipts | main change review | done; 25/25 rows, 36/36 scenarios, 207/207 on Python 3.9 and 3.12, 123/123 legacy, all final gates green |
| TPS-002 | Close the official-switch runtime-socket incident in the existing transactional change | main, serialized | `codex_switch_home_sync.py`, `codex_switch_transaction.py`, transaction regression, TPS artifacts/evidence, ledger/state | real Unix-socket RED/GREEN, filtered entry-set drift guards, unknown-special fail-closed guard, full transaction/profile suites, strict OpenSpec, read-only source dry-run | install/live official switch remains a separate Human Gate | done; 9/9 focused, 213/213 transaction, 175/179 adjacent profile baseline with four FSR fixture errors, strict/static gates, and source dry-run |
| TPS-003 | Close the Desktop global-state no-op ownership and retained live recovery incident | main, serialized | transaction planner/recovery tests plus TPS artifacts/evidence and authorized rollout | 4/4 focused, real-write negative guard, full suites, strict/static/package checks, supported live recovery, official App launch, restored internal ownership | consumed authorized install/live official/internal acceptance; publication remains gated | done; 219/219 transaction, 198/198 profile, 17/17 strict OpenSpec, payload `ed5d74c1...28ab`, official backup `20260725T171620Z...`, internal backup `20260725T172136Z...` |
| CRB-001 | Apply `canonical-runtime-binding` by TDD | main, serialized after TPS seam | runtime binding/process/lifecycle/status/Doctor/bindings modules and focused test | adapter/attestation/rebind log | main change review | done; 33/33 OpenSpec rows, runtime 55/55 on Python 3.9 and 3.12, transaction 215/215, profile 195/195 |
| SAP-001 | Apply `schema-scoped-app-proxy` by TDD | main after runtime receipt seam | adapter/proxy/config/TOML/wrapper/home-sync modules and focused test | schema/probe/E2E log | main change review | done; 34/34 OpenSpec rows, protocol 37/37 on Python 3.9 and 3.12, backend-early-exit lifecycle closure, profile 195/195 |
| FSR-001 | Apply `fail-safe-update-release` by TDD | delegated worker after shared runtime/verify seams | promotion/package/CLI/plugin/verify/release/workflow modules and focused test | containment/rollback/sanitizer/reconciliation log | main change review | done; implementation 35/35 and OpenSpec 42/42, update/release 107/107, profile 193/193, strict/static/package/evidence gates green |
| FSR-002 | Repair same-version legacy self-update handling exposed by rollout | main, serialized | `scripts/codex-switch`, focused self-update tests, FSR artifacts/evidence, ledger/state, rollout evidence | same/older malformed RED/GREEN, newer malformed fail-closed, Python fail-before-write, clean installed status | no release publication or App restart | done; dual-Python 5/5, Python selection/fail-before-write 3/3, update/release 113/113, profile 198/198, installed status clean |
| INT-001 | Integrate shared seams and remove duplicates | main | shared callers/tests only; preserve Runtime Binding, Protocol Adapter, capability-receipt, and verify extension boundaries without implementing parity behavior | `rg` call map and focused regressions | scope/diff review | done; protocol 37/37, runtime 55/55, verifier 22/22, advisory 6/6 on Python 3.12 and 3.9, focused catalog/auth regressions, unique-authority and retired-path scans passed |
| VER-001 | Run final verification and update evidence/state/tasks | main | OpenSpec tasks, ledger, state, verification records; record queued parity follow-up only | exact commands/results | completion-proof review | done; transaction 215/215, profile 195/195, update/release 108/108, strict/static/package/diff gates green |
| ROLLOUT-001 | Install the verified current source, run live official acceptance, then restore internal | main after VER-001 | supported local install path plus live profile/App/runtime bindings; no source edits unless acceptance fails | exact install/switch commands, PATH/App/profile/wrapper/running app-server ownership, task-entry proof, restored internal proof | explicitly authorized final rollout; commit/push/tag/release/destructive cleanup remain gated | done; internal restored, final source installed, normal status clean, ChatGPT/proxy/backend ownership stable, no restart job |
| PARITY-001 | Propose and apply `internal-official-feature-parity` as an independent Full OpenSpec after the previous Goal is complete and stable | main in the active parity Goal | planning artifacts first, then serialized TDD across runtime/protocol/verify/catalog/release/control-plane seams | parity inventory, fixed allowed-difference whitelist, core/optional classification, finding codes, capability/overlay/adapter decisions, update/rebind matrix, automated regressions, Desktop Subagent smoke | design/spec/task review before task 1.1; no commit/push/release/destructive cleanup/dependency additions; internal binary/provider/model/API/auth remain fixed differences | task 8.3 source/package milestone is verified but remains unchecked at 70/79. Its thirteen-error repair now provides structured method/rule evidence, nullable-union equivalence, exact optional-extension dispositions, two-pass policy/probe ordering, receipt-v2, and versioned acceptance-trace binding. Dual-runtime producer/consumer suites and isolated package identity pass. The user is prioritizing official, so stop at `OFFICIAL_FIRST_PAUSE_READY` and `PARITY-8.3-LIVE-RETRY`; task 8.4 remains unavailable. |
| PARITY-INC-001 | Isolate trusted-installer side effects and recover the 2026-07-28 failed internal candidates | main, serialized before renewed parity live preparation | source: `scripts/codex_env_setup`, `scripts/test_codex_update_release.py`; canonical parity artifacts/control plane; live: exact `.zshrc` backup/three blocks, three candidate moves, supported install/profile/runtime artifacts/restart | harmful-installer RED/GREEN on two runtimes; focused/full/static/OpenSpec/workflow/package/diff; exact backup audit; config/plugin/manifest/wrapper/App/proxy/app-server verification | Scheme A consumed; proposed `codex_switch_parity.py`/`test_codex_parity.py` expansion requires explicit approval | blocked after safe internal-0.145 RED; tasks 8.3A.1-8.3A.4 done at 74/84, exact-source install valid, no promotion/restart |
| SPLIT-001 | Apply `independent-app-cli-profiles` by serialized TDD | main | selection/record/parser, transaction/switching, shared config/materialization, status/Doctor/verify, wrapper/docs/skill/package, focused tests, and main-owned control plane named by the change | public-seam RED/GREEN, rollback/drift/materialization matrix, complete suites, strict/static/workflow/plugin-eval/package/diff evidence | source implementation approved; live switch/install/restart/plugin mutation/Git/release/archive/cleanup remain gated | task 12 source repair is done; live install/functional acceptance tasks 10.3-10.4 remain separately gated |
| SPLIT-SHORTCUT-001 | Add and install the fixed `split` preset plus `--keep-version` | main, serialized | wrapper, public wrapper tests, README, SKILL, active OpenSpec task 11, ledger/state/evidence, contract-bound package, immutable local install | ordered public-seam RED/GREEN, focused/full/adjacent tests, static/spec/package identity, release-counterpart Plugin Eval, installed/source identity, status/Doctor/process attestation | user authorized source plus immutable install only; App stop/restart, live activation, internal update, parity repair, Git/release/archive/cleanup remain gated | done; source and installed payload `b88326ff...f49d` expose the shortcut, while active CLI/App deliberately remain `openai-official` |
| SHARED-SWITCH-OPT-001 | Apply `optimize-shared-switch-transaction` by serialized TDD | main | generic Home selector, switch transaction validation/preflight/progress, focused tests, README, SKILL, and main-owned OpenSpec/ledger/state/evidence | public-seam allowlist/preflight/progress RED-GREEN, effect-bounded recursive work, source/final CAS rollback, conditional App preserve/rebind, complete adjacent suites, strict/static/workflow/package/plugin-eval/diff evidence | source/test/docs/control-plane approved; live switch/App stop/install/dependency/Git/release/archive/cleanup remain gated | done in source at 12/12; healthy official App yields a running CLI-only preserve path with no App/global-state effect, real rebinds retain stopped proof and rollback, 545/545 source plus package-local 10/10 + 4/4 pass; install/live activation remain separate |
| INTERNAL-CLI-ONLY-001 | Apply `internal-cli-only-runtime` by serialized TDD | main | existing OpenSpec change; split auto-update routing; internal manifest CLI generation/App readiness metadata; runtime-rebind transaction; internal shell generation; promotion validator; managed runtime smoke; conditional App guidance; focused tests; README/SKILL; namespaced evidence | CLI-only candidate commit and rollback, production-sized streaming runtime validation, promotion/managed-shim validator parity, zero App-owned writes while official App runs, preserve/rebind guidance mapping, unchanged full-parity path, focused/full/static/strict/diff proof | combined source/test/docs/control-plane repair approved by the user; no new promotion/switch/install/App restart/dependency/Git/release/archive/cleanup/provider effect | done in source at 25/25: streaming executable SHA with 2 GiB bound, prepared fresh-shim probe and byte-exact rollback, final actual-store-shim smoke, unbuffered progress, conditional restart guidance; 997/997 source, 9/9 package focus, strict/static/package/plugin-eval/diff, and two-axis rereview pass; no install/live activation |
| SPLIT-BOOTSTRAP-001 | Apply `independent-app-cli-profiles` task 12 by serialized TDD | main | `scripts/codex_switch_plugins.py`, shared preflight/runtime seam, focused shared/runtime tests, README, SKILL, active OpenSpec task 12, ledger/state/verification evidence | live-shape stale-installed/current-source RED/GREEN, precise finding, exact post-add attestation, flushed progress, zero-write fast path, focused/broad/static/spec/package/diff evidence | source/test/docs/control-plane only; no live cache/install/codex/split/App/dependency/Git/release/archive/cleanup effect | done in source at 4/4; final shared 81/81, runtime 90/90, profile 226/226, packaged 23/23, strict/static/workflow/package/Plugin-Eval/diff gates complete; live activation remains gated |
| SPLIT-BACKEND-MANAGED-001 | Apply `independent-app-cli-profiles` task 13 by serialized TDD | main | catalog adapter, shared materializer, focused tests, README, SKILL, active OpenSpec task 13, ledger/state/verification evidence | real-shape source/target divergence, installed precedence, mandatory reconcile, one fresh post-call batch catalog, precise findings, full/static/spec/package/review proof, managed functional exit zero with App unchanged | one managed functional command approved; no split/install/App mutation/internal binary update/direct codex-switch cache copy-link-delete/dependency/Git/release/archive/cleanup | done at 4/4: functional CLI exits zero, 18 receipts are current with App unchanged, native backend cache lifecycle is explicitly accepted, codex-switch direct cache mutation remains forbidden, source and package shared matrices pass 94/94 |
| SPLIT-PROACTIVE-SYNC-001 | Apply `independent-app-cli-profiles` task 18 by serialized TDD | main | wrapper, public shared-lifecycle/profile tests, README, SKILL, active OpenSpec task 18, ledger/state/verification evidence | successful apply ordering for both split forms, sync-failure stop/remediation, dry-run zero-write, focused/full/static/spec/workflow/package/review proof | source/test/docs/control-plane only; no live split/config/cache/backend/App/install/migration/dependency/Git/release/archive/cleanup effect | done in source at 4/4; lifecycle 24/24, shared aggregate 149/149, profile 227/227, update/release 165/165, isolated package focus 30/30, static/spec/workflow/diff and two-axis review pass; install/live activation remain gated |
| SPLIT-CONFIG-IDEMPOTENCE-001 | Apply `independent-app-cli-profiles` task 14 by serialized TDD | main | managed runtime annotation cleanup, focused config/profile tests, active OpenSpec task 14, ledger/state/verification evidence | repeated-render RED/GREEN, user-format preservation, adjacent suites, strict/static/diff proof | no live config/switch/install/App/plugin/cache/dependency/Git/release/archive/cleanup effect | done at 3/3; config 31/31, focused profile 4/4, complete profile 226/226, strict OpenSpec 22/22, workflow/static/diff gates pass |
| RELEASE-STARTER-RECOVERY-001 | Apply `independent-app-cli-profiles` task 15 by serialized TDD | main | release adapter/reconciler, focused update-release tests, active OpenSpec task 15, ledger/state/verification evidence | hidden-starter, disappearing-Release, and stale multi-starter ID RED; per-delete readback/recreate/upload GREEN; conflict guards and full proof | no live Release mutation/workflow rerun/migration/dependency/Git/archive/cleanup effect | done at 6/6; focused 19/19, update/release 154/154, profile 226/226, AST/Bash/OpenSpec 22/22/workflow/diff pass; second submit gated |
| RELEASE-RECREATION-PROPAGATION-001 | Apply `independent-app-cli-profiles` task 16 by serialized TDD and authorized external effects | main | release adapter/reconciler, wrapper/shared diagnostic guards found by final review, focused tests, active OpenSpec task 16, authority evidence, ledger/state/verification | typed bounded create/readback retries, draft-list fallback, terminal 4xx precedence, forced-close/deadline coverage, single-line diagnostics, complete source/package/static/spec/workflow/review proof, remote ref/Release/asset readback | gate `614cc025...` was consumed by commit `6a5fa85`, push, and failed run `31666160863`; gate `a40cea2a...` now authorizes the task 16.9 commit/push and exact Auto Release targets; no migration/archive/install/cleanup/force push | first submission failed acceptance; task 16.7 source proof passes focused 6/6, update/release 171/171, profile 227/227, static/spec/workflow/diff gates; task 16.9 is ready for external effects |

## Dependency and Execution Order

1. `TPS-001`: establishes lock, journal, and rollback seam.
2. `CRB-001`: consumes the transaction seam for internal rebind.
3. `SAP-001`: consumes canonical backend digest/receipt and launcher seams.
4. `FSR-001`: consumes canonical runtime expectations in verification and
   promotion handshakes.
5. `INT-001` then `VER-001`.
6. `ROLLOUT-001` after final source verification and integrated review.
7. Complete and stabilize the previous core-repair Goal, including TPS-003.
8. Start `PARITY-001` as the active independent Full OpenSpec Goal.
9. Install the verified result of `SHARED-SWITCH-OPT-001` before any further
   split live-activation retry; preview then decides whether App preservation
   is possible or a stopped-App rebind is required.
10. Complete `SPLIT-BOOTSTRAP-001` source verification before any separately
    authorized functional CLI or split retry.
11. Complete `SPLIT-BACKEND-MANAGED-001` source verification before its single
    authorized functional managed-shim acceptance; stop after one bounded
    attempt if the target cannot be independently proved.

Each step is selected only after its focused verification and main-agent review
passes. No production write task runs concurrently with another production
write task.

## Subagent Strategy

- Every delegated write task requires a validated Agent Task Contract in
  `.planning/devflow/agent-contracts/` with exact write set, dependency input,
  RED/GREEN commands, prohibited live effects, escalation triggers, and final
  status schema.
- Workers are not alone in the repository, must preserve unrelated changes, and
  must not edit OpenSpec, ledger, state, shared evidence, or another task's files.
- Main reviews every diff and independently reruns focused tests before the next
  production slice.

## Incidental Finding Register

`CORE-STDIN-2026-09-21`: `CONTINUE_WITH_MINIMAL_GUARD`. Native current-runtime
verification reproduced premature stdin closure in the existing core probe.
Repair request/response sequencing under the existing timeout/output bounds
within classify-current-runtime-extensions; preserve all core result checks.

Namespaced-feature follow-up: `FEATURE-2026-09-21` is
`DEFER_AND_CONTINUE` for this syntax repair. Empty-home feature-only comparison
of official 0.155.0-alpha.9 and candidate 0.155.0 finds seven unclassified
differences (analytics_plan_history, guardian_ext, personality,
realtime_conversation, send_message_to_user_async, use_xaa, worktrees). Retain
the existing blocking policy and make no successful full-update claim.
An offline schema comparison with current coverage also reports incompatible
thread/queue/add, thread/queue/update, thread/resume, turn/start and turn/steer,
plus unclassified internal-only thread/rollback. Classification/adapter
behavior repair needs separate capability evidence;
this register does not authorize exceptions. See the namespaced verification
record for the bounded local observation.

| id | finding | disposition | reason / residual risk | follow-up |
|---|---|---|---|---|
| INC-028 | Historical migration fixture retained the first-install helper after removing its update-policy dependency | CONTINUE_WITH_MINIMAL_GUARD | Test-only removal restores the old-version layout; both installer/runner subcases pass on recheck | Resolved in custom-catalog repair; production behavior unchanged |
| INC-001 | arbitrary profile name containment | DEFER_AND_CONTINUE | user confirms only official/internal product profiles; unsupported legacy paths remain less hardened | revisit only if custom profiles become product scope |
| INC-002 | snapshot plus `--shared-config-base` semantics | DEFER_AND_CONTINUE | not needed by approved product paths; silent reinterpretation is forbidden | separate behavior decision if requested |
| INC-003 | historical Codex.app exact identity | DEFER_AND_CONTINUE | bundle absent; path-only observation cannot certify health | add provenance fixture before any migration execution |
| INC-004 | source authenticity/signing | DEFER_AND_CONTINUE | manifest proves integrity only; signing is a separate architecture/dependency decision | dedicated security change if required |
| INC-005 | live Desktop/install/release smoke | SCOPED_REBIND_EXECUTED_RESTART_PENDING | user authorized only the canonical internal rebind; no install, rollout edit, app restart, release, or publication ran | fully quit and reopen ChatGPT, then attest the proxy/backend child and retry continuation |
| INC-006 | compare profile CLI versions with the latest official `openai/codex` release, especially before internal switches | DONE_SEPARATE_OPENSPEC | the user authorized the separate behavior; `official-release-version-advisory` implements a stable-only, bounded, non-blocking comparison without coupling it to internal installation policy | source verification complete; install/release/archive remain separately gated |
| INC-007 | internal Desktop response-stream reconnect loops can leave a turn retrying without forward progress | DEFER_AND_CONTINUE | optional resilience behavior is not required by the active SAP Completion Contract; it changes turn lifecycle and must not expand the current main-review baseline | after the active Goal completes, create a dedicated Full OpenSpec change for profile-scoped app-stream recovery; keep official behavior unchanged |
| INC-008 | downloaded source archives are extracted by system `tar` before fixed-allowlist validation | DEFER_AND_CONTINUE | task 1.3 proves archive-owned scripts are not executed and required top-level symlinks are rejected, but does not prove member path-traversal containment or a policy for symlinks nested inside allowlisted directories | update the Full OpenSpec design/tasks before adding archive-member prescan or contained extraction; do not treat 1.3 as source-authenticity or traversal proof |
| INC-012 | final isolated release-counterpart Plugin Eval reports 54/100 (grade F, high risk): two static token-budget failures, four warnings for progressive disclosure/top-level README layout, Python complexity, and seven long lines, plus unavailable coverage artifacts | DEFER_AND_CONTINUE | the shared App/CLI repair is runtime safety work; reducing the current 4,684-token active estimate or 1,075,529-token deferred support tree requires a material benchmarked Skill/package architecture refactor outside this Completion Contract | create a dedicated Skill/package optimization change with observed-usage benchmarks before restructuring `SKILL.md`, release contents, or runtime modules; retain the 54/100 score and residual risk in this change's verification record |
| INC-013 | existing switch-time shared TOML policy includes almost every non-profile table, so MCP/apps/connectors or future tables can carry credentials or runtime-specific paths across homes | DEFER_AND_CONTINUE | the new canonical layer uses a narrow allowlist and secret guard, so the Plugin/Skill Completion Contract does not require changing legacy switch semantics; silently narrowing existing behavior could break users | create a separate Full OpenSpec compatibility/security migration with field-level schema evidence, authoritative deletion rules, rollback, and explicit approval |
| INC-014 | generic home support uses a denylist and therefore treats unknown future entries as shareable | DONE_SOURCE | `optimize-shared-switch-transaction` now selects only `AGENTS.md`, `prompts`, `rules`, and `skills`, preserves ignored targets, bounds deep validation independently of unrelated effects, and retains final CAS rollback | source/package verification is complete; installation and live split remain separate authority, with App exit required only if preview derives `rebind` |
| INC-015 | Desktop global-state sharing currently includes permission/account/cloud/update semantics in addition to cosmetic UI/workspace values | DEFER_AND_CONTINUE | these keys are outside Plugin/Skill desired state and reclassification could alter permissions or update policy | perform a field-level ownership audit and approved migration before removing or sharing additional global-state keys |
| INC-016 | live deployment rejected exact prior 20-path `current`/`rollback` manifests because historical validation recognized only the older 16-path generation | CONTINUE_WITH_MINIMAL_GUARD_DONE | this directly blocked safe installation of the approved split build; one public installer RED/GREEN added exact 20-path compatibility without accepting subsets, supersets, or reordering | focused historical/promotion 3/3 and package 2/2 pass; new 22-path payload installed with the prior 20-path current retained as rollback |
| INC-017 | the running official App mutates `.codex-global-state.json` during the live split transaction's frozen-input window | DONE_SOURCE | the supported split no longer freezes, backs up, projects, or writes Desktop global state; a healthy canonical official binding produces no App-owned effect and may remain running, while a true rebind still fails closed | install the verified source separately, run a fresh split preview, and require App exit only if that preview reports `App action: rebind` |
| INC-018 | DevFlow 0.4.0 read-only audit reports broader project refresh migration pending and the dependency checker still sees legacy project-local DevFlow source paths | DEFER_AND_CONTINUE | installed DevFlow cache is byte-current, OpenSpec 1.7 and project-local Matt TDD are available, and the drift does not invalidate shortcut behavior; applying project migration would exceed the confirmed install scope | run `dev-flow-refresh`/`plugin-project-migration` only after separate project-migration authorization; do not clean legacy paths implicitly |
| INC-019 | an invocation-only `py_compile` check generated or refreshed `scripts/__pycache__/codex_switch_transaction.cpython-312.pyc` and `scripts/__pycache__/test_codex_transaction.cpython-312.pyc` at 2026-08-10T17:00:20+08:00 | DEFER_AND_CONTINUE | both files are ignored, absent from the verified release package, and do not affect source/test outcomes, but no Generated Artifact Contract was sealed before creation and the pre-existing directory prevents retrospective ownership proof | retain both exact paths; delete or quarantine them only under separately approved generated-artifact cleanup with current identity evidence |
| INC-020 | one generated-wrapper protocol fixture still expected arbitrary `shared-support.txt` to cross homes after the exact V1 generic support allowlist was implemented | CONTINUE_WITH_MINIMAL_GUARD_DONE | the stale assertion blocked the required broad suite; changing only the fixture to allowlisted `AGENTS.md` preserves the approved allowlist and tests the same wrapper/proxy integration | focused protocol test and complete support matrix must pass; no production sharing rule changes |
| INC-021 | the immutable-promotion success fixture used a 1.0-second candidate smoke budget instead of the production 5.0-second budget and flaked twice under cumulative suite load | CONTINUE_WITH_MINIMAL_GUARD_DONE | the unrelated flake blocked required broad verification; only the test helper now uses the production smoke budget, while a deterministic 1.2-second success RED and an explicit 0.05-second timeout guard preserve both contracts | focused promotion matrix and complete update/release suite must pass; production timeout behavior is unchanged |
| INC-022 | an abrupt signal during wrapper App-action capture can leave the mode-0600 temporary receipt | DEFER_AND_CONTINUE | normal success/failure paths retire the exact file and it contains only `preserve` or `rebind`; signal-safe trap restoration is not required by the current output/rollback contract and changing wrapper signal semantics needs dedicated coverage | add a separately planned signal-delivery test and trap-lifecycle design before changing interrupt behavior; retained temp files may be removed only under exact-path cleanup authority |
| INC-023 | managed generation validation has a same-user path-replacement window between the stable streamed digest and backend `execve` | DEFER_AND_CONTINUE | descriptor identity is stable throughout hashing and the managed backend is user-owned, but eliminating the later path window requires descriptor-based execution or a platform-specific immutable handoff beyond this compatibility repair | design a separate runtime-exec identity contract with macOS/Linux behavior, failure semantics, and regression coverage before changing the execution seam |
| INC-024 | the latest read-only local-reference audit reports an unconfigured upstream for the OpenAI plugins mirror and a preserved local `hatch-pet` divergence; Workshop now verifies `matches-source` | DEFER_AND_CONTINUE | neither remaining item affects the codex-switch source/package Completion Contract; applying updater, marketplace changes, or overwriting a local Skill is outside the confirmed authority | review ownership of the mirror and local Skill in a separate maintenance task before any apply or cleanup |
| INC-025 | the one task-13 live command succeeded while native internal `plugin add` replaced seven upgraded cache versions and removed their old version directories | DONE_CONTRACT_RECONCILED | the user explicitly assigned installed-version lifecycle to the native backend; OpenSpec now permits backend retention/replacement/removal while codex-switch still performs no direct cache copy/link/delete or garbage collection | no restoration, retry, or cleanup is required; future retention guarantees require a separately approved preservation architecture and cache-mutation authority |
| INC-026 | the authorized repair push would select Auto Release `reconcile_then_prepare`, repairing `v0.1.14` and then creating a release commit/tag plus published Release for `v0.1.15` | BLOCKED_AWAITING_HUMAN | the current grant explicitly covers the repair commit/push, Auto Release execution, and `v0.1.14` mutation, but does not name the additional `v0.1.15` Git tag or Release target | authorize or reject the `v0.1.15` release commit, atomic main/tag update, and publication before consuming the pending push authority |
| INC-027 | Auto Release run `31558709842` removed the failed `v0.1.14` starter state but failed while recreating/reconciling the Release; the REST release-by-tag endpoint was 404 and the release list stopped at `v0.1.13` | DONE_FIRST_REPAIR_FAILED_ACCEPTANCE | authority gate `614cc025...` produced verified commit `6a5fa85` and run `31666160863`, proving the bounded create retry was not sufficient because draft discovery still depended only on the tag endpoint | superseded by INC-028; preserve the first repair and do not rerun or manually edit the Release |
| INC-028 | Auto Release run `31666160863` created the `v0.1.14` draft but five tag-endpoint readbacks reported it missing | READY_FOR_EXTERNAL_EFFECT | the adapter now falls back on explicit 404 to the authenticated paginated Releases collection and selects one exact `tag_name` match; public adapter regressions also reject malformed, invalid-JSON, unbounded, duplicate, and non-404 states; full source proof is green and gate `a40cea2a...` is resolved | execute task 16.9 once; verify both Releases and canonical assets before claiming completion |
| INC-009 | installed `0.1.13` official switch attempted to back up internal `ipc/ipc.sock` and failed before commit | DONE_SOURCE | `ipc` and `mcp-oauth-locks` are known profile-local runtime state already inside TPS shared-support ownership; exact-name exclusion preserves unknown-special fail-closed behavior | source repair is verified; installation and live official switch require separate approval |
| INC-010 | internal and official can drift outside the approved internal binary/model/API/provider/auth differences; current Subagent drift selects v1 because internal Azure model metadata lacks `multi_agent_version=v2` | ACTIVE_FULL_OPENSPEC_PLAN_REVIEW | the previous Goal and TPS live recovery are complete; the parity proposal, design, delta spec, and implementation ledger are complete and strictly valid, but production execution has not started | review the active change, then begin task 1.1 RED through `openspec-apply-change`; capability-gate the v2 overlay and forbid silent fallback |
| INC-011 | installed strict `0.1.13` validates the same-version historical `v0.1.13` asset before comparing versions and emits `source_invalid` | DONE_SOURCE_AND_ROLLOUT | trusted version metadata now short-circuits same/older releases before download or staging; newer malformed candidates remain fail closed | final source installed; normal `status` prints `already up to date 0.1.13` with no `source_invalid` or sync warning |

### INC-007 Optional Follow-up Contract

- Classification: optional error-handling and compatibility feature requiring
  Full OpenSpec before implementation.
- Runtime owner: the `codex-switch` managed internal app-server proxy, not the
  DevFlow runtime or Stop hook.
- Profile boundary: default `off`; support `off`, `observe`, and `recover`
  modes through internal profile metadata. The official profile remains
  unchanged and does not acquire a recovery wrapper.
- Trigger: at least three eligible response-stream retry notifications for the
  same turn plus 180 seconds without forward progress. Message/reasoning deltas,
  item transitions, tool output, or plan updates reset the inactivity timer.
- Recovery budget: at most three automatic recoveries per thread recovery
  chain. A new turn inherits the chain count; only a successfully completed
  turn resets it.
- Safety boundary: do not interrupt while command, MCP, file-change, or
  approval work is active. Do not restart the app-server process in the first
  version. Exhausting the budget interrupts the stuck turn and leaves it
  stopped for user action.
- Recovery action: issue `turn/interrupt`, wait for terminal `interrupted`
  state, then issue `turn/start` on the same thread with a transparent
  continuation prompt that inspects durable state and avoids repeating
  non-idempotent effects.
- Required evidence: fake-backend retry/progress matrices, synthetic request-ID
  isolation, no duplicate external effects, observe-mode zero mutation,
  official-profile no-op behavior, attempt-budget exhaustion, and successful
  same-thread continuation.
- Non-goal: changing provider `request_max_retries` or `stream_max_retries` in
  the same change. Retry-policy tuning requires separate evidence.
- Exact next action: after `VER-001` completes, propose
  `internal-app-stream-auto-recovery` as a dedicated Full OpenSpec change unless
  the user explicitly reprioritizes it into the active Goal.

### INC-010 Internal/Official Feature Parity Follow-up Contract

- Classification: independent Full OpenSpec compatibility change, queued after
  the current Goal completes and stabilizes.
- Fixed allowed differences: internal keeps the internal `codex_bin`, model,
  API endpoint, provider, and auth. It must not bind to the official bundle
  binary.
- Parity target: outside the whitelist, internal and official protocol,
  features, and Desktop experience should remain aligned. Official additions
  enter parity inventory, compatibility assessment, and synchronization.
- Health policy: core experience drift makes internal unhealthy. Optional
  capability drift may degrade temporarily only with explicit reporting,
  stable finding codes, a synchronization queue entry, and mandatory recheck
  after every internal binary update or rebind.
- Initial confirmed finding: official model metadata declares
  `multi_agent_version=v2`; the internal Azure catalog omits it and selects v1,
  causing random Subagent nicknames. The proxy does not remove v2 agent fields.
- Intended solution boundary: design a controlled model-catalog overlay or
  metadata sync that preserves internal model/provider/API fields, enables v2
  only after internal binary capability and behavior probes pass, and never
  silently falls back.
- Required future evidence: official/internal feature list, experimental
  app-server schema, model catalog metadata, CLI/App protocol capability
  inventory; allowed-difference list; core/optional matrix; capability receipt
  and overlay/adapter decision; update/rebind/verify integration; regression
  matrix; real Desktop Subagent smoke proving task-oriented names and
  descriptions.
- Current-Goal boundary: `INT-001` and `VER-001` may preserve extension seams
  and record this queue only. They must not implement parity behavior or create
  the new OpenSpec.

## Execution Log

- 2026-07-25: A live installed `codex-switch 0.1.13` switch from internal to
  official failed with `Operation not supported on socket` while backing up
  internal `ipc/ipc.sock`. The incomplete
  `20260725T022012Z-switch-internal-to-openai-official` backup retained an empty
  `29-ipc` and no terminal `backup.json`; shim, LaunchAgent, and `active.json`
  were not committed. The bundled official CLI independently passed app-server
  smoke, so TPS-002 classifies exact `ipc` and `mcp-oauth-locks` ownership
  before shared-support planning while preserving fail-closed handling for
  unknown special objects. No install, cleanup, live switch, App restart, or
  Git action is authorized by this repair.
- 2026-07-25: TPS-002 task 7.2 reproduced the incident through the supported
  transaction dry-run seam with real `AF_UNIX` sockets. The focused Python 3.12
  command ran two tests in 0.066 seconds: the known-runtime case failed at
  `internal/ipc/ipc.sock` with `Unsupported filesystem object kind`, while the
  unknown `unknown-runtime.sock` fail-closed guard passed. No backup,
  destination, active-state, live profile, App, or installed release mutation
  occurred.
- 2026-07-25: TPS-002 task 7.3 made the two incident tests GREEN by classifying
  exact `ipc` and `mcp-oauth-locks` names as runtime state and replacing
  recursive whole-home parent freezing with identity-bound filtered entry-set
  evidence. The observation recursively captures shared candidates and
  top-level stale-link candidates, re-enumerates for additions/removals, and
  never traverses runtime directories. Four focused tests passed in 0.412
  seconds, including unknown socket rejection, shared-entry-set drift, and
  late shared-source drift. Python 3.12 compile and scoped whitespace checks
  passed.
- 2026-07-25: TPS-002 task 7.4 closed the target-home follow-up and full source
  verification. Extending the real-socket regression to both source and target
  homes first failed at `official/ipc/oi.sock` because an existing target home
  still received a no-op `target_home_ensure` and whole-tree capture. The
  transaction now creates ensure effects only for truly missing directories.
  Focused directory/runtime coverage passed 9/9, and the complete transaction
  suite passed 213/213 on Python 3.12. The adjacent profile suite passed
  175/179; all four errors were existing FSR fake-CLI fixtures rejected by the
  current fail-closed plugin catalog as `invalid_json`, after their switch and
  app-server checks had succeeded. Strict TPS OpenSpec, Bash syntax, Python
  compile, and diff checks passed. The repository-source official dry-run
  returned `Outcome: DRY RUN OK`; a 33,717-entry canonical control-plane,
  backup, managed-target, and runtime-socket snapshot retained SHA-256
  `f079b653f75690bff3aad70a69e3e48a41db599245166f7a00e811d0defe7382`
  before and after, and `ipc.sock` retained device/inode identity. No install,
  live switch, App restart, failed-backup cleanup, or Git action ran.
- 2026-07-24: SAP tasks 3.1-3.2 completed by TDD. The initial focused run
  failed 8/8 because `codex_switch_config_document.py` did not exist; a second
  table-span RED failed 1/9 after assignment spans were green. The final module
  uses `tomllib` for semantics and a source-only scanner for complete
  assignment/table spans, quoted/dotted key paths, CRLF/comments, multiline
  string/array/inline-table replacement, reverse-offset edits, result reparse,
  and byte-identical semantic no-ops. Focused tests pass 9/9; Python 3.9 fails
  closed without `tomllib`; dual-runtime compile, strict SAP OpenSpec, and
  `git diff --check` pass. Array-table replacement remains disabled until task
  3.3 supplies explicit identity. No caller or live state was changed.
- 2026-07-24: SAP tasks 2.1-2.5 passed the main change-review gate. Review
  repairs covered the missing `stat` import, schema/probe descendant cleanup,
  bounded schema output, receipt symlink TOCTOU, and dangling rebind-marker
  recovery. Isolated protocol reruns passed 27/27 on Python 3.9 and 3.12; the
  prior two failures did not reproduce outside six-suite concurrency. Adjacent
  runtime binding passed 55/55 on both interpreters and transaction passed
  211/211 on both. Strict SAP OpenSpec, dual-runtime `py_compile` for 9 affected
  files, `git diff --check`, legacy remember/restore absence, and all eight
  review-baseline SHA-256 values passed. Tasks 2.1-2.5 are checked; task 3.1 is
  next. No App restart, live switch, install/update, plugin mutation, release,
  commit, tag, push, or rollout edit ran. Checkpoint:
  `.planning/checkpoints/2026-07-24-sap-config-write-verified.md`.
- 2026-07-24: The user requested that automatic recovery from prolonged
  internal-profile reconnect loops be tracked without destabilizing the active
  proxy work. It is recorded as INC-007 `DEFER_AND_CONTINUE` with a dedicated
  optional follow-up contract: internal-only, profile-scoped, default off,
  three retry events plus 180 seconds without progress, and at most three
  recoveries per thread recovery chain. No SAP scope, code, live profile,
  provider retry policy, App process, or Git state was changed for this intake.
- 2026-07-24: SAP tasks 2.1-2.5 are implemented in the worktree and stopped at
  the main change-review gate. The implementation generates a sanitized
  schema-v2 capability receipt from isolated backend schema generation and a
  temporary-home versioned config-write probe, binds it to backend and schema
  SHA-256 values, passes the receipt through the managed launcher, rejects
  unproven writes before backend or filesystem mutation, forwards proven writes
  exactly once, preserves backend response versions, and removes the old
  post-response compensating file write. Fresh
  `scripts/test_codex_protocol_config.py -v` runs passed 23/23 on Python 3.9
  and 3.12. Strict SAP OpenSpec, Python 3.12 compile, and `git diff --check`
  passed. Tasks remain unchecked until review closes; broader SAP completion,
  live acceptance, and all FSR work remain pending. Checkpoint:
  `.planning/checkpoints/2026-07-24-sap-config-write-review-gate.md`.
- 2026-07-23: A live internal Desktop continuation failed because raw history
  item 77 was a synthetic hook user message with local UUID
  `019f8dfe-5fb3-7443-9889-6d89991bd9e8`; isolated 0.144.6 CLI and app-server
  disk-resume captures removed all top-level history item IDs. SAP task 1.4 now
  includes exact `thread/resume.params.history` normalization. No live session
  file was edited.
- 2026-07-23: Repeated `rs_… not found` failures established a second
  continuation defect: current internal rollouts frequently contain reasoning
  items with `encrypted_content = null` and no portable content or summary.
  The production JSONL path now uses only the exact protocol adapter and
  direction-aware tracker, removes resume-history top-level IDs, and omits
  only opaque reasoning references. A generated-wrapper RED also proved the
  live command shape `-c features.code_mode_host=true app-server
  --analytics-default-enabled` bypassed the old `$1 == app-server` check.
  The wrapper now sends all invocations through one dispatcher; app-server argv
  reaches the proxy unchanged and non-app commands exec the backend once.
  Dual Python 3.9/3.12 results are protocol 17/17, runtime 53/53, and profile
  127/127. The affected thread fixture retained 43/56 items after removing 13
  opaque reasoning entries with source SHA-256 unchanged. The installed
  release and running ChatGPT process remain untouched pending INC-005 approval.
- 2026-07-23: Independent standards/spec review found direction-insensitive
  config snapshot restoration, text-mode JSONL normalization, legacy
  marketplace/model-list regressions, non-Codex process false positives, and
  proxy `PYTHONPATH` leakage. The proxy now gates restoration on the matching
  config-write response, uses binary stdio, retains evidence-backed legacy
  filtering and `result.models`, rejects non-Codex app-server observations,
  and restores the caller environment before backend exec/spawn. Full
  17/17 protocol, 53/53 runtime, and 127/127 profile suites passed again on
  Python 3.9 and 3.12.
- 2026-07-23: The user authorized the scoped INC-005 workstation rebind.
  Before mutation, the internal manifest and launcher were copied to
  `/Users/cY/.codex-switch/backups/manual-20260723T103336Z-resume-proxy-rebind`.
  The repo command `./scripts/codex-switch set-bin internal
  /Users/cY/.local/bin/codex` staged and smoked the proxy/backend chain, then
  transactionally committed the canonical managed launcher. The backend is
  `codex-cli 0.144.6`; the committed manifest SHA-256 is
  `a60648ce4819ff7ba28fb825fa725ac62388fc44f089dcbe565440dea41aaeaf`
  and launcher SHA-256 is
  `f3854fe0b509b09cdccd79722c5b1c35e904812ef0310e14afbe511642920f6d`.
  A second smoke against the committed launcher passed and attested
  `["app-server", "--analytics-default-enabled"]` with the requested backend.
  ChatGPT was deliberately not restarted; its existing pid still owns the raw
  backend until the user fully quits and reopens the app.

- 2026-07-22: Targeted DevFlow cache/project refresh completed and baseline
  review recorded. Existing 123-test and 11-change strict baselines were green.
- 2026-07-22: User constrained profiles to official/internal and confirmed the
  merged current Desktop product is ChatGPT. Active review count is 10 P1 + 3
  P2; arbitrary-profile hardening is deferred.
- 2026-07-22: Verified ChatGPT.app bundle id `com.openai.codex`, version
  `26.715.70719`, bundled CLI `0.145.0-alpha.27`; internal CLI is `0.142.4`;
  Codex.app is absent; ChatGPT Classic has bundle id `com.openai.chat` and no
  bundled codex.
- 2026-07-22: Generated both installed AppServer schemas. Both expose current
  canonical dynamic-tools, write `keyPath`/`edits`, response version fields, and
  remote marketplace kind. The current running command shape places global
  `-c` before `app-server`, which the old parser misses.
- 2026-07-22: Isolated internal `0.142.4` temporary-home initialize/write probe
  returned a schema-valid versioned response and preserved unrelated config.
  Design changed from post-response recovery to backend-owned writes with a
  digest-bound behavioral receipt and fail-closed unknown state.
- 2026-07-22: Four Full OpenSpec changes reached complete artifact status and
  each passed strict validation; production implementation had not yet begun at
  that planning checkpoint.
- 2026-07-22: Implementation dependency check initially reported missing
  project-local `tdd` and `code-review`. DevFlow activation ran dry-run then
  apply with official installs and legacy migration disabled; both methodology
  skills are now ready and the four triggered capabilities report ready.
- 2026-07-22: Full strict validation after planning passed 15/15 items. The
  validated `transaction-core-restore-implementation` Agent Task Contract
  started the first serialized RED/GREEN slice.
- 2026-07-22: The transaction/restore core passed independent review after two
  repair rounds. Directory-inode locking, recursive v2 state, explicit v0/v1
  boundaries, complete restore preflight, canonical product target checks,
  payload attestation, per-target/pre-commit rechecks, safety backups, ordered
  apply/reverse rollback, parent cleanup journaling, and durable failure
  receipts are covered by 41 tests under both default Python 3.9 and Python
  3.12. A fresh full 123-test legacy suite also passed after the final focused
  hardening. Snapshot writer finalization and real switch/capture lock ownership
  intentionally remain open and prevent tasks 2.1/2.2 from being closed.
- 2026-07-22: The validated `transaction-capture-implementation` Agent Task
  Contract started the next serialized slice. Its exclusive write set is the
  transaction test/module plus capture and lifecycle adapters; no switch,
  Desktop, runtime, update, release, or live workstation effect is authorized.
- 2026-07-22: Initial capture implementation passed 50/50 transaction tests on
  Python 3.9 and 3.12; main independently reran both plus the complete legacy
  suite at 123/123 and strict OpenSpec validation. Independent spec and
  engineering reviews nevertheless reproduced managed-symlink escape,
  required-auth TOCTOU, rollback-attestation, and false post-commit failure
  paths. Tasks 3.1-3.4 remain open while the validated
  `transaction-capture-review-fixes` contract adds focused RED/GREEN guards for
  those findings, profiles-parent/root durability, recovery independence,
  causal errors, schema typing, and fsync ordering.
- 2026-07-22: Transactional capture tasks 3.1-3.4 passed the review-fix gate on
  stable SHA-256 `807a95249b06547c` for the transaction module,
  `fe8f4dfcbe3c4a5a` for its tests, and `902e57b9723eddae` for the capture
  adapter. The final implementation uses a pinned profiles-workspace
  descriptor, immutable staged/unmanaged and canonical journal expectations,
  a durable `prepared -> committed` state machine, verified-previous-first
  rollback/recovery, terminal artifact-vector checks, canonical evidence
  repair, and read-only capture preview. Main independently passed 86/86
  transaction tests on Python 3.9 and 3.12 plus 123/123 legacy tests; two
  independent reviewers reported no actionable P0-P3 after a 20-case finalize
  exception/crash matrix, an 8-case journal-downgrade interruption matrix, and
  dry-run zero-write checks. Switch/snapshot writer integration remains open,
  so TPS as a whole is still in progress.
- 2026-07-22: The first full switch/restore integration reached a stable
  154/154 transaction baseline on Python 3.9 and 3.12 plus 123/123 legacy,
  strict OpenSpec, syntax/import, contract, and diff checks. Independent final
  review rejected completion: it reproduced store-wide gate bypasses,
  pre-lock init writes, incomplete rollback/terminal evidence, unjournaled
  restore parent cleanup, missing recovery durability and full preflight,
  weak terminal reread, read-to-freeze and staged-identity gaps, path-based
  restore materialization, repeated-path recovery failure, and strict metadata
  holes. The 25-row audit found 14 covered, 6 weak, and 5 missing.
- 2026-07-22: Canonical `transactional-profile-state` proposal, design, spec,
  and tasks were updated with the complete marker/journal, immutable plan,
  descriptor-relative recovery, init-lock, strict metadata, and acceptance
  contracts; strict validation passes. The new machine-valid
  `transaction-final-review-closure` contract (SHA-256 `de6548ce...`) awaits
  two independent contract reviews before one serialized production writer
  resumes. CRB/SAP/FSR remain blocked behind TPS acceptance.
- 2026-07-23: Both contract reviews accepted and DevFlow's triggered execution
  gate was refreshed. Dry-run then apply copied only the missing project-local
  pinned `diagnosing-bugs` methodology resource; official installs, provider
  changes, and legacy `.codex/skills` migration remained disabled. Dependency
  recheck reports `workflowReady: true`.
- 2026-07-23: TPS final-review task 6.2 completed by TDD. One classifier now
  runs under the store lock for switch/capture/restore and is reused by the
  custom/init gates. It distinguishes bound markers, marker-required missing
  markers, effect-free safe closure, markerless legacy switch recovery,
  pre-marker restores, capture journals, and corrupt/multiple evidence before
  dispatch. Three focused tests first failed on the exact bypasses, then the
  expanded matrix and full transaction suite passed 164/164 on Python 3.9 and
  Python 3.12. Strict OpenSpec, dual-runtime AST checks, and diff checks passed.
  `cmd_init` atomic lock ownership remains task 6.10; terminal-marker behavior
  remains task 6.3. No live profile/App/install/update/Git mutation ran.
- 2026-07-23: TPS final-review task 6.3 completed by TDD. Switch rollback now
  writes outer lifecycle and journal terminal state atomically. If the primary
  manifest becomes unreadable after a verified rollback, only a `failure.json`
  bound to the trusted marker's operation, IDs, marker name, and prepared
  journal digest can prove `rolled_back`; mismatched and rollback-failed records
  remain blocking and byte-preserving. Retained-marker guidance names the real
  outcome, dry-runs leave markers unchanged, and the next applying supported or
  custom route validates and retires terminal evidence. Both Python 3.9 and
  3.12 passed 168/168 twice against stable hashes; strict OpenSpec,
  dual-runtime AST, and diff checks passed. CLI rendering remains task 6.11;
  no live profile/App/install/update/Git mutation ran.
- 2026-07-23: TPS final-review task 6.4 completed by TDD. Every switch
  rollback/recovery materialization reaches a file/tree and parent-directory
  durability boundary before terminal publication; the terminal manifest and
  backup directory are then explicitly synced. All effects receive a
  conservative `recovered|rollback_failed` terminal state. Prepared recovery
  preflights target-home ensure identity before its first write, accepts an
  already-restored Desktop state without replay, and is idempotent after a
  second interruption. Prepared, marker, intent, action, applied, terminal,
  unlink, and parent-sync checkpoints have direct coverage; uncertain unlink
  republishes the same bound marker. Both Python 3.9 and 3.12 passed 176/176
  twice against stable hashes; strict OpenSpec and diff checks passed. Restore
  parent cleanup remains task 6.5; no live or Git mutation ran.
- 2026-07-23: TPS final-review task 6.5 completed by TDD. Restore-created parent
  cleanup is now journaled as an identity-bound intent/action/applied effect and
  made durable before commit. Catchable rollback and next-invocation recovery
  use the same engine, recreate the exact prior mode, checkpoint recovery
  identity, and remain idempotent after a second interruption. Recovery fully
  preflights cleanup identity before its first write. A committed restore is
  accepted only after strict bound terminal reread validates entries, payloads,
  effects, live target state, removed parents, and stage cleanup. Python 3.9 and
  3.12 each passed 181/181; strict OpenSpec, dual-runtime AST, stable hashes,
  and tracked/untracked diff checks passed. Task 6.6 is next; no live or Git
  mutation ran.
- 2026-07-23: TPS final-review task 6.6 completed by TDD. One planning-input
  tracker brackets each producing read with exact path state and device/inode
  identity, immediately rejects read-time drift, persists that same evidence in
  the switch journal, and revalidates it before intent and after action. The
  14-case matrix covers manifests, active, profile/base/target/composite config,
  auth, plugin snapshots, Desktop source/target RMW state, shell, shared entry
  sets, stale links, and executable bindings; every case initially completed
  without raising and now rejects before backup while preserving newer bytes.
  Python 3.9 and 3.12 each passed 182/182; strict OpenSpec, dual-runtime AST,
  stable hashes, and diff checks passed. Task 6.7 is next; no live or Git
  mutation ran.
- 2026-07-23: TPS final-review task 6.7 completed by TDD. Replacement files now
  install the exact phase-specific durable stage through a pinned parent
  descriptor and verify the adapter-recorded produced inode before `applied`.
  Shared file/tree/link actions are descriptor-relative, tree contents are
  synced before rename, and a same-content foreign file or same-tree foreign
  directory is rejected. Repeated paths retain independent stages and recover
  through intermediate state/identity predecessors. Hard-interruption coverage
  spans all 15 deterministic file phases, target-home creation, and
  shared-directory copy. Python 3.9 and 3.12 each passed 187/187; strict
  OpenSpec, dual-runtime AST, stable hashes, and tracked/untracked diff checks
  passed. Task 6.8 is next; no live or Git mutation ran.
- 2026-07-23: TPS final-review task 6.8 completed by TDD. Restore apply and
  recovery now bind the attested lexical/canonical route, predecessor identity,
  staged identity, and produced identity to descriptor-relative actions. Stable
  lexical symlink ancestors remain supported; changed routes, replaced stages,
  changed targets, and foreign empty parents fail closed or are preserved.
  Parent cleanup is descriptor-relative and inode-bound. Recovery journals its
  own action identity, accepts an authorized exact-mode parent recreation, and
  resumes idempotently after a file or consumed-directory-stage interruption.
  Python 3.9 and 3.12 each passed 192/192; strict OpenSpec, dual-runtime AST,
  stable hashes, and diff checks passed. Task 6.9 is next; no live or Git
  mutation ran.
- 2026-07-23: TPS final-review task 6.9 completed by TDD. Uniform state
  validation now rejects boolean, negative, and above-`0o7777` modes wherever
  v1/v2 before, after, or committed metadata records them, while retaining
  legal special bits. Schema-v2 directory entry counts are checked against the
  attested recursive tree even under `--force`. Both supported adopted homes
  require absolute normalized authority outside `backups/`; a final-component
  symlink or lexical `..` is rejected while stable macOS ancestor aliases remain
  compatible. Failed nested-home rollback now removes only the journaled inode
  and preserves non-empty or replaced later state as `rollback_failed`.
  Python 3.9 and 3.12 each passed 195/195, and the legacy suite passed 123/123;
  strict OpenSpec, dual-runtime AST, stable hashes, and tracked/untracked diff
  checks passed. Task 6.10 is next; no live or Git mutation ran.
- 2026-07-23: TPS final-review task 6.10 completed by TDD. `cmd_init` now
  creates only a missing lock root before acquisition, then holds one
  inode-revalidated store lock across recovery classification, store layout,
  official manifest/config creation, optional capture, and final output.
  Capture accepts an explicit active locked-store dispatcher and never
  reacquires the lock. Held-lock and pending-capture cases preserve exact store
  bytes and guidance; successful stdout remains capture receipt followed by
  the two init lines. Python 3.9 and 3.12 each passed 197/197, and the legacy
  suite passed 123/123; strict OpenSpec, dual-runtime AST, stable hashes, and
  tracked/untracked diff checks passed. Task 6.11 is next; no live or Git
  mutation ran.
- 2026-07-23: TPS final-review task 6.11 completed by TDD. Successful supported
  switch receipts now carry structured guidance and append outcome-correct
  `committed` retained-marker recovery text after the existing CLI output.
  Direct regressions prove supported switch lock contention and pending capture
  blocking custom apply are byte-preserving and arm no new transaction marker.
  AST plus `rg` caller checks proved only the obsolete
  `managed_profile_app_cli_path` import in `codex_switch_switching.py` dead; it
  was removed while the live transaction and app-wrapper callers remain.
  Python 3.9 and 3.12 each passed 200/200, and the legacy suite passed 123/123;
  strict OpenSpec, dual-runtime AST/import, stable hashes, and diff checks
  passed. Task 6.12 is next; no live or Git mutation ran.
- 2026-07-23: TPS final-review task 6.12 completed. A new RED proved switch
  terminal reread accepted unbound and incomplete committed evidence; strict
  marker-bound entry/payload/identity/effect validation now fails closed and
  retains the marker. A second RED proved failed init capture retained seven
  managed layout entries and changed root mode; init now restores the exact
  pre-init managed tree and modes under its single lock without stdout. Direct
  regressions also cover restore committed-marker cleanup retry, later external
  switch-target preservation, failed existing-home mode, and final dead-helper
  caller proof. The durable evidence maps 25/25 required rows and 36/36
  OpenSpec scenarios to 109 existing test methods; the name/sequence validator,
  9/9 focused selection, and strict OpenSpec validation passed. Task 6.13 is
  next; no live profile/App/install/update/Git mutation ran.
- 2026-07-23: TPS final-review task 6.13 completed. Completion review first
  reproduced two additional terminal-evidence failures: staged metadata was not
  reattested to its persisted object, and a silent corrupt terminal writer could
  bypass on-disk reread. The first 206-test full rerun then reproduced a false
  rejection of valid nested adopted-home ancestry in both interpreters. Shared
  terminal validation now reattests actual stage state/inode/route before marker
  cleanup on normal and exception paths; terminal and prepared recovery admit
  only the contiguous canonical missing-predecessor `created_parent_paths`
  chain covered by `target_home_ensure`. Final results are 207/207 on Python
  3.9 and 3.12 plus 123/123 legacy, followed by another 207/207 dual-runtime
  rerun against unchanged hashes. TPS and all OpenSpec validation passed
  (15/15), as did Bash syntax, dual-runtime AST/import, Agent Task Contract,
  coverage-map, caller, tracked diff, and relevant untracked whitespace checks.
  TPS is complete; CRB-001 is next. No live profile/App/install/update/release,
  network, or Git publication action ran.
- 2026-07-23: CRB tasks 1.1-1.4 completed by TDD. An injected exact-root
  Desktop inventory recognizes only a valid ChatGPT.app shape with bundle id
  `com.openai.codex`, executable `Contents/MacOS/ChatGPT`, and executable
  bundled `codex` as current. Codex.app is migration-only and Classic is
  excluded. Canonical official binding ignores manifest/PATH/active drift and
  uses the bundled CLI for shell, Desktop, and backend; canonical internal
  binding uses the manifest backend plus managed launcher and rejects invalid
  or recursive managed backends. The initial missing-module RED became 17/17
  on Python 3.9 and 3.12; dual compile, leaf-dependency, contract, strict
  OpenSpec, and diff checks passed. CRB task 2.1 is next; no live or Git effect
  ran.
- 2026-07-23: CRB tasks 2.1-2.5 completed by TDD. Token-aware parsing accepts
  supported global options before `app-server` and rejects exec/shell payload
  mentions. Desktop processes are recognized from the discovered ChatGPT or
  legacy host executable, not a fixed Codex.app marker. One immutable snapshot
  now carries process, GUI env, LaunchAgent, and launcher-fingerprint evidence;
  supplied snapshots are reused by status output and Doctor/verify process
  checks. Full-chain attestation rejects a stale child backend, proxy bypass,
  stale LaunchAgent, unset GUI env, legacy host, and launcher-byte drift. The
  RED had 5 failures plus 6 errors; GREEN is 30/30 on Python 3.9 and 3.12, six
  legacy focused tests and the full 123-test Python 3.9 suite pass. CRB task 3.1
  is next; no live or Git effect ran.
- 2026-07-23: `canonical-runtime-binding` completed. Lifecycle, capture,
  switch, status, Doctor, and verify now share canonical manifest-derived
  binding and immutable observation semantics. Official defaults certify only
  ChatGPT.app; explicit official fixture paths remain compatibility-only.
  Internal rebind stages and smokes the managed proxy/backend chain, attests
  the requested child, persists the launcher SHA-256, and promotes the
  manifest/launcher pair through a durable recoverable journal. Concurrent
  foreign state and symlink recovery markers fail closed. A focused legacy RED
  exposed PATH leakage when only `--app-cli-path` was explicit; the corrected
  14-case set, 53/53 runtime tests on Python 3.9 and 3.12, 207/207 transaction
  tests on both, and 123/123 legacy tests on both pass. Strict OpenSpec, Shell,
  dual-runtime AST/import, obsolete-authority scans, and diff checks pass.
  `schema-scoped-app-proxy` is next; no live profile/App/launchctl/install/
  update/release/network/Git mutation ran.
- 2026-07-23: A user-requested fast handoff boundary was created before
  switching the development conversation to the internal profile. TPS and CRB
  remain the production-usable completed baseline. SAP tasks 1.1-1.3 add an
  isolated, not-yet-wired protocol adapter with exact method/path model
  transforms, direction-aware ID tracking, independent tri-state dynamic-tool
  and marketplace capability handling, and backend/schema digest binding.
  The initial missing-module RED became 14/14 on Python 3.9 and 14/14 on Python
  3.12; strict SAP OpenSpec, dual-runtime AST/import, and diff checks pass.
  Task 1.4 is deliberately still open: the existing proxy remains authoritative
  until adapter migration, config-write gating, receipt generation, and
  real-chain E2E complete together. No partial adapter behavior is active.
- 2026-07-23: SAP tasks 3.3, 3.4, and 4.2 were expanded for the reproduced
  Plugin/Skill usage-state regression. The current internal runtime is
  authoritative on App restart, and the active source runtime is authoritative
  on official/internal switches. `[plugins.*]` and `[[skills.config]]` must be
  replaced exactly, including deletion and `enabled = false`; stale snapshots
  may recover marketplace/hook support only. Focused RED/GREEN tests and source
  implementation are next. No live profile, App, plugin, install, or Git
  mutation is authorized.
- 2026-07-23: The SAP authoritative Plugin/Skill usage-state sub-slice is
  implemented. `[plugins.*]` and `[[skills.config]]` now replace destination
  usage state exactly on profile switches and internal Desktop restart;
  target runtimes, legacy profile layers, and stale snapshots can recover
  non-usage support metadata only. Snapshot refresh copies current runtime
  usage exactly, so removal and `enabled = false` survive without duplicate
  skill paths. RED was 8/8 expected failures; GREEN is 8/8 focused plus
  129/129 full profile tests on Python 3.9 and 3.12, protocol 17/17, focused
  transaction 3/3, strict SAP OpenSpec, Python compile, and diff check. The
  full transaction suite is 205/207 because two existing store-mode tests
  expect `0755` under the current `0077` process umask; neither failure touches
  config/snapshot/switch behavior. No live profile, App, plugin, install,
  release, network, or Git mutation ran.
- 2026-07-24: SAP Config Document tasks 3.1-3.5 completed after main review.
  `tomllib` is the only semantic parser; complete spans, quoted/dotted keys,
  CRLF, multiline values, lexical Skill identity, protected paths, exact usage
  replacement, and stable ambiguous-identity diagnostics are covered by 24/24
  focused tests. Offline merge/overlay callers use the document seam, malformed
  TOML no longer has a basic-scanner fallback, the shell resolves Python 3.11+,
  generated app wrappers pin a validated interpreter, and direct Python 3.9
  entry fails before store mutation. The first 211-test transaction run exposed
  two invalid adopted-home sentinel fixtures; replacing them with valid TOML
  retained byte-exact restore and mode assertions, and the focused test plus the
  full 211/211 rerun passed. Profile 136/136, runtime binding 55/55, protocol
  27/27 on Python 3.9 and 3.12, strict SAP OpenSpec, Bash syntax, dual-runtime
  syntax compile, and `git diff --check` pass. Dead line-only helper definitions
  remain only for task 5.2 cleanup. Task 4.1 is next; no live profile/App/plugin/
  install/update/release/network/Git mutation ran.
- 2026-07-24: SAP canonical launcher tasks 4.1-4.3 completed after main review.
  The initial four-test RED reproduced embedded shell policy, incomplete
  isolated-link removal, unsafe relative/dangling/profile-home symlink
  propagation, and mutation before TOML failure. `codex_switch_home_sync.py`
  now owns `prepare-launch`, real-target symlink classification, authoritative
  Plugin/Skill restart state, complete config/snapshot/Desktop preflight, and
  launch-time auth policy. Normal switch planning and its pinned filesystem
  adapter consume the same classification. The generated wrapper pins a
  validated Python 3.11+ interpreter and contains no `find`, symlink classifier,
  inline TOML Python, or direct `rm`; it preserves argv and caller `PYTHONPATH`
  through the proxy dispatcher. Review added RED/GREEN guards for a target-home
  symlink alias and malformed canonical profile config before mutation. Fresh
  results are launcher-focused 10/10, profile 139/139, transaction 211/211,
  Config Document 24/24, runtime binding 55/55, and protocol 27/27 on Python
  3.12. Python 3.9.6 and 3.12 syntax compile, strict SAP OpenSpec, Bash syntax,
  and `git diff --check` pass. Task 5.1 real-chain E2E is next; no live
  profile/App/plugin/install/update/release/network/Git mutation ran.
- 2026-07-24: SAP task 5.1 completed by TDD and main review. Generated wrappers
  now exercise `prepare-launch -> proxy -> fake backend` for modern `0.142.4`,
  legacy `0.140`, unknown capabilities, write gating, response masking, raw
  CRLF JSONL, stderr, pre-EOF flush, EOF, bounded timeout, and nonzero exits.
  RED reproduced a lost 20 MB final response, an inherited-pipe hang beyond
  five seconds, and an early-exit `BrokenPipeError` traceback. GREEN drains
  stdout/stderr against one two-second deadline, preserves backend exit status,
  and suppresses only expected closed-pipe errors. The seven-test E2E matrix
  and complete protocol suites pass 34/34 on Python 3.9 and 3.12; strict SAP
  OpenSpec, dual-runtime compile, and `git diff --check` pass. Task 5.2 is next;
  no live profile/App/plugin/install/update/release/network/Git mutation ran.
- 2026-07-24: SAP tasks 5.2-5.3 completed after caller proof and focused
  regression. `rg` found no supported production or test caller for the eleven
  superseded recursive/line-only TOML helpers, so their definitions were
  removed while the explicit `0.140` compatibility transforms and diagnostics
  remained. Config Document passes 24/24, protocol passes 34/34 on Python 3.9.6
  and 3.12, profile passes 139/139 on Python 3.12, affected modules compile on
  both runtimes, and `git diff --check` passes. Task 5.4 is next; no live
  profile/App/plugin/install/update/release/network/Git mutation ran.
- 2026-07-24: SAP final verification exposed and repaired two current-version
  fixture drifts before completion. Installed internal `0.144.6` and bundled
  official `0.146.0-alpha.3` use `PluginListMarketplaceKind`, and reject the
  historical marketplace `source_type = "github"` probe fixture. RED reproduced
  marketplace capability `None` and config-write probe `None`; GREEN recognizes
  historical/current schema names and uses a network-free local marketplace
  fixture. Fresh isolated receipts for both binaries prove dynamic tools,
  remote marketplace, and versioned config-write preservation. Final SAP
  results are protocol 35/35 on Python 3.9.6 and 3.12, Config Document 24/24,
  runtime 55/55, transaction 211/211, profile 139/139, OpenSpec 16/16, dual
  Python AST 47/47 and imports 42/42, Bash/caller/diff checks passed. SAP is
  32/32 complete; FSR-001 is next. No live profile/App/install/update/plugin/
  release/network/Git mutation ran.
- 2026-07-24: FSR bundle-containment tasks 1.1-1.2 completed by delegated TDD
  and main review. The new release-bundle module rejects repository/root/
  ancestor destinations, direct output/package symlinks, foreign directories,
  and missing markers before mutation; copy and partial-finalization failures
  preserve prior outputs. A main-review RED exposed stale classification during
  staging: an unmarked directory swapped into the public package path could
  have been moved into an owned backup and deleted. GREEN now reclassifies at
  finalization, binds each approved output by `lstat` device/inode/type,
  validates the moved bundle before promotion and cleanup, and preserves
  unbound evidence. Python 3.9.6 and 3.12 pass 14/14 focused tests; the adjacent
  troubleshooting package regression, dual compile, Bash syntax, strict
  OpenSpec, isolated two-pass packaging, and diff checks pass. Task 1.3 is next.
  No live install/update/profile/plugin/App, network, release, or Git mutation
  ran.
- 2026-07-24: FSR source-fallback task 1.3 completed by delegated TDD and main
  review. Installer, remote runner, and self-update now validate and copy only
  the eight-item source allowlist and never execute the downloaded
  `scripts/package-release.sh`; extra root files and `scripts/__pycache__` are
  excluded while executable modes remain `0741/0751/0701`. Main review added
  two blocking REDs: installer/runner reported success after allowlist-cleanup
  failure, and self-update promoted a partially staged source candidate because
  Bash conditional context suppressed the staging failure. Both now propagate
  nonzero before replacing `current`. Python 3.9.6 and 3.12.13 each pass 14/14
  release tests and 7/7 focused source-fallback tests; Bash syntax, strict
  OpenSpec, no-packager-invocation `rg`, and `git diff --check` pass. System
  `tar -p` is retained because the workstation umask otherwise reduces all
  three source executable modes to `0700`. Task 1.4 is next. Archive-member
  traversal/deep-symlink hardening is recorded as INC-008, not silently added.
  No live install/update/profile/plugin/App, network, release, or Git mutation
  ran.
- 2026-07-24: FSR immutable-promotion tasks 1.4-1.5 completed by delegated RED
  tests, main implementation, and main review. `codex_switch_promotion.py`
  delegates bundle authority, validates shell/Python/import/smoke readiness in
  isolated copies, publishes immutable `releases/<digest>` roots, manages
  relative atomic `current`/`rollback` refs under a directory-inode lock, and
  requires an exact structured handshake before original-command replay. Review
  added RED/GREEN guards for macOS canonical path aliases, foreign state/ref
  replacement, active-state rollback, replaced staging ownership, pre-move and
  post-ref legacy interruption, pre-handshake candidate interruption, and JSON
  Boolean schema versions. Python 3.9.6 and 3.12.13 each pass 34/34 update/
  release tests; dual AST, strict OpenSpec, no-adapter-caller `rg`, an isolated
  two-release receipt, and `git diff --check` pass. Progress is 6/38; task 2.1
  is next. No live install/update/profile/plugin/App, network, release, or Git
  mutation ran.
- 2026-07-24: FSR installer/runner tasks 2.1-2.2 completed by adapter RED tests,
  main implementation, and spec/standards review. `install.sh` and `run.sh`
  now stage candidates, resolve only explicit or real script-local bootstrap
  modules directly, require embedded SHA-256 matches for installed/archive
  modules, copy the two verified modules into temporary trusted staging, call
  immutable promotion, and combine primary and cleanup failures explicitly.
  The promotion CLI replays the original command exactly once from the
  promoted digest root, not mutable `current`; signal exit status remains
  preserved. Candidate packaging now requires `codex_profile_switch.py`,
  `codex_switch_release_bundle.py`, and `codex_switch_promotion.py`. Review REDs
  covered piped `BASH_SOURCE`, malicious bootstrap modules, missing production
  modules, a concurrent `current` change, masked archive-root selection, and
  ignored cleanup statuses. Final results are update/release 47/47 on Python
  3.12.13, installer/runner 10/10 on Python 3.9.6, six adjacent profile adapter
  tests on both interpreters, dual compile, Bash syntax, strict OpenSpec,
  isolated package validation, digest binding, and `git diff --check`. Progress
  is 8/38; task 2.3 is next. No live install/self-update/profile/App/plugin,
  network release, commit, push, tag, or archive action ran.
- 2026-07-24: FSR self-update task 2.3 added six public-wrapper RED contracts
  for invalid structure, expected-version mismatch, handshake field mismatch,
  handshake timeout, concurrent promotion, and nonzero user-command replay
  exactly once. Python 3.12.13 and system Python 3.9.6 each produced the same
  6/6 expected behavior failures: legacy self-update replaced the immutable
  `current` symlink with a directory instead of using promotion/rollback.
  Dual-runtime AST and test-file whitespace checks passed. Progress is 9/38;
  task 2.4 is next. No production code, live install/self-update/profile/App/
  plugin, network release, commit, push, tag, or archive action ran.
- 2026-07-24: FSR self-update task 2.4 migrated the installed wrapper to the
  trusted bundle and immutable promotion seams. Candidate structure and expected
  version are validated before promotion; the promotion receipt is checked
  against version, digest, and `releases/<digest>` root; re-exec disables
  recursion and uses that receipt root even when another promotion changes
  `current`. Sync failure still runs the prior implementation, while a replayed
  user command's nonzero status is returned exactly once. Main review migrated
  legacy directory-only test fixtures to canonical bundles and found one
  restrictive-umask defect: release archives extracted without `tar -p` changed
  manifest `0755` modes to `0700`. The explicit `umask 0077` regression is now
  GREEN. Final results are update/release 53/53 and focused profile self-update
  10/10 on both Python 3.12.13 and system Python 3.9.6, plus strict OpenSpec,
  Bash syntax, dual-runtime AST, obsolete in-place-path scan, and
  `git diff --check`. Progress is 10/38; task 3.1 is next. No live install,
  self-update, profile/App switch, plugin mutation, network release, commit,
  push, tag, or archive action ran.
- 2026-07-24: FSR ordered internal-update task 3.1 recorded eight public policy
  contracts at `decide_internal_update(...)`: equal healthy current, healthy
  newer current, healthy older current, blocked-current fallback, blocked
  latest with newer and older healthy current, missing/unparseable inputs, and
  SemVer prerelease ordering. Python 3.12.13 and system Python 3.9.6 each
  produced 8/8 expected RED failures solely because
  `scripts/codex_switch_update_policy.py` is absent. Progress is 11/38; task
  3.2 is next. No shell adapter, live update, install/self-update, profile/App
  switch, plugin mutation, network release, commit, push, tag, or archive
  action ran.
- 2026-07-24: FSR ordered internal-update task 3.2 added the standard-library,
  Python 3.9-compatible `codex_switch_update_policy.py`. It implements strict
  SemVer core/prerelease ordering and immutable structured decisions; healthy
  newer current versions are retained, older healthy versions upgrade only to
  an unblocked latest, and downgrade is possible only for an explicitly
  blocked current with a valid unblocked fallback. The eight policy contracts
  pass on both interpreters, followed by complete update/release results of
  61/61 on Python 3.12.13 and 61/61 on system Python 3.9.6. Strict FSR OpenSpec,
  dual-runtime AST/compile, and `git diff --check` pass. Progress is 12/38;
  task 3.3 is next. No shell adapter, live update, install/self-update,
  profile/App switch, plugin mutation, network release, commit, push, tag, or
  archive action ran.
- 2026-07-24: FSR internal-update adapter task 3.3 added four public wrapper
  RED contracts. On both Python 3.12.13 and system Python 3.9.6, helper exits
  17 and 23 were incorrectly swallowed, helper success with unchanged
  `1.0.0` was reported as a successful `1.1.0` update, normal upgrade omitted
  its explicit target version, and compatibility-smoke failure occurred only
  after the wrapper had already printed update completion. Both valid focused
  runs produced 4/4 behavior failures; the system-Python run used Python 3.12
  only for the existing Config Document initialization prerequisite. Dual test
  compile and `git diff --check` pass. Progress is 13/38; task 3.4 is next. No
  production adapter, live update, install/self-update, profile/App switch,
  plugin mutation, network release, commit, push, tag, or archive action ran.
- 2026-07-24: FSR internal-update adapter task 3.4 made the RED contracts
  GREEN and closed the follow-up review findings. The wrapper now uses ordered
  policy targets, propagates helper and version-probe status, verifies the exact
  installed version, runs app-server compatibility before completion, parses
  the helper's complete value-option grammar so `--dry-run` cannot be consumed
  as a value, and fails closed for malformed existing internal manifests rather
  than updating a default binary. Python 3.12 passed 26/26 focused profile
  tests; system Python 3.9 passed 20/20 shell/adapter tests; complete
  update/release suites passed 64/64 on both interpreters. Strict FSR OpenSpec,
  Bash syntax, and `git diff --check` passed. Progress is 15/38; task 4.1 is
  next. No live update, install/self-update, profile/App switch, plugin
  mutation, network release, commit, push, tag, or archive action ran.
- 2026-07-25: FSR plugin catalog and repair tasks 4.1-4.4 completed by TDD and
  main review. Catalog command stdout, stderr, and status remain separate;
  verified-empty, command failure, empty output, invalid JSON, warning-bearing
  success, unsupported schema, and complete results are distinct. Only a
  verified catalog can authorize install, stale refresh, or
  `--disable-unavailable`. Installed caches require a version directory plus a
  matching `.codex-plugin/plugin.json`; temporary files, `.DS_Store`, payload-
  only directories, and marker-only directories remain missing. Repair now
  returns a typed `PluginRepairPlan`, precomputes and validates every config
  update, rejects plan drift before the first write, and rolls back all
  attempted config writes on failure. Python 3.12 passed 35/35 plugin-related
  regressions and the six task 4.3/4.4 contracts; system Python 3.9 passed the
  catalog/zero-write/cache-marker subset 3/3 and imported the module. Strict
  FSR OpenSpec, focused diff integrity, and no-live-mutation boundaries passed.
  Progress is 20/38; task 5.1 is next. No live update, install/self-update,
  profile/App switch, plugin mutation, network release, commit, push, tag, or
  archive action ran.
- 2026-07-25: FSR structured verification tasks 5.1-5.5 completed by TDD and
  main review. `SmokeOutcome` now records `passed|failed|not_run`; all ordinary
  verify subprocesses use a monotonic deadline, independent bounded stdout and
  stderr rings, process-group TERM/KILL escalation, and descendant-pipe cleanup.
  Reports use exclusive no-clobber names and persist structured outcome metadata
  without raw commands or prompts. An allowlist-first sanitizer removes
  authorization, bearer, API-key, cookie, credential, password, and signed-query
  values before terminal or JSON output while retaining only conservative
  routing identifiers. App-server smoke now uses a bounded JSONL parser and an
  explicit initialize/plugin state machine; malformed, oversized, missing,
  error, and pre-initialize auth responses fail, while the known
  post-initialize plugin-auth response remains permitted. Python 3.12.13 and
  system Python 3.9.6 each passed 17/17 focused verification tests; existing
  verify tests passed 12/12 and the runtime initialize-error regression passed
  1/1. Strict FSR OpenSpec, dual-runtime syntax, and focused diff integrity
  passed. Progress is 26/38; task 6.1 is next. No live smoke with secret input,
  profile/App switch, plugin/install/update mutation, network release, commit,
  push, tag, or archive action ran.
- 2026-07-25: The separately authorized
  `official-release-version-advisory` change completed 13/13 tasks. Normal
  official/internal checks now compare the selected CLI with the latest stable
  `openai/codex` tag through a pure SemVer policy and a 3-second connect /
  8-second total bounded redirect lookup. Internal output runs after any
  auto-update, stays advisory-only, and cannot call the helper or write the
  profile store. Prerelease stable tags, unparseable versions, lookup failure,
  and skip-update paths are covered by focused tests; missing curl is handled
  by an explicit command-availability guard. Review found and repaired one
  release gap: the new module was copied incidentally but not required by the
  bundle manifest; incomplete candidates now fail, and installer/runner trusted
  bundle hashes match
  `6d7a37ddc4df5d58c19afc99eaa205761fe14b0f81005be744235871cda50274`.
  Final results are policy 4/4, wrapper 7/7, and release dependency 1/1 on
  Python 3.12.13 and 3.9.6; full profile 171/171 and update/release 64/64 on
  Python 3.12.13; strict OpenSpec 17/17; AST 53/53 and production imports 46/46
  on both runtimes; Bash, isolated bundle, and diff checks passed. Read-only
  live checks reported internal `0.144.6` behind stable `0.145.0` and bundled
  official `0.146.0-alpha.3` ahead. No live switch/update/install/App restart/
  plugin/release/Git mutation ran. Evidence:
  `.planning/verification/20260725013044-official-release-version-advisory.md`.
- 2026-07-25: FSR release-planner task 6.1 recorded seven planner and
  fake-GitHub contracts. Python 3.12.13 and system Python 3.9.6 each produced
  the same single expected RED failure: a complete, published latest tag at
  `HEAD` was incorrectly selected for reconciliation instead of no action.
  Non-ancestor ancestry, tag identity conflict, missing-asset reconciliation,
  remote-main race, asset checksum drift, and publish-failure rerun contracts
  passed. The previously written historical-layout tests were also executed
  and repaired to 10/10: missing manifests now require explicit historical
  mode, only `v0.1.12`/`v0.1.13` layouts are trusted, and bounded macOS
  AppleDouble metadata is discarded during deterministic canonicalization.
  Those repairs are partial evidence only; tasks 6.5 and 6.7 remain open.
  Dual-runtime compile and focused diff integrity passed. Progress is 27/38;
  task 6.2 is next. No live install/profile/App/plugin mutation, network
  release, commit, push, tag, or archive action ran.
- 2026-07-25: FSR release-planner task 6.2 made all seven planner contracts
  GREEN on Python 3.12.13 and system Python 3.9.6. A complete, published latest
  tag now selects no action; missing/draft releases still reconcile, and
  ancestry, tag identity, remote-main race, checksum drift, and publish-rerun
  guards remain intact. Updating the release-bundle module invalidated the
  installer/runner bootstrap digest; both adapters now pin the exact current
  SHA-256
  `bf6d221ff937cb9d66e9a4c8cd0705c9f76f37333982d2989b399e9d8a226228`,
  and the dual-runtime planner/bootstrap group passes 8/8. Strict FSR OpenSpec,
  dual-runtime compile, Bash syntax, hash equality, and diff integrity pass.
  The complete Python 3.12 release suite reached 82/87 before the hash refresh;
  the two hash failures are now closed and the remaining three failures are
  the pending task 6.5 RED contracts. Progress is 28/38; task 6.3 is next. No
  live install/profile/App/plugin mutation, network release, commit, push, tag,
  or archive action ran.
- 2026-07-25: FSR release-workflow task 6.3 added four static ordering
  contracts. The inherited workflow draft already orders package, deterministic
  asset validation, remote-base confirmation, atomic main+tag push, and
  reconciliation; manual recovery validates before reconciliation and never
  creates refs. Critical paths contain no `continue-on-error`, `|| true`, or
  `--clobber`, and both workflows share one serial concurrency group. The tests
  passed immediately because the workflow implementation predated this resumed
  validation slice. Planner/workflow results are 11/11 on Python 3.12.13 and
  system Python 3.9.6; strict FSR OpenSpec and focused diff integrity pass.
  Progress is 29/38; task 6.4 is next. No live install/profile/App/plugin
  mutation, network release, commit, push, tag, or archive action ran.
- 2026-07-25: FSR reconciliation task 6.4 added four fake-GitHub contracts.
  Missing-asset recovery uploads only the missing file, complete same-tag
  reruns are read-only, existing checksum conflicts stop before mutation, and
  tag identity conflicts stop before any GitHub adapter call. The inherited
  reconciliation implementation satisfied all four without production edits.
  Planner/reconciliation results are 11/11 on Python 3.12.13 and system Python
  3.9.6; strict FSR OpenSpec, dual-runtime compile, and focused diff integrity
  pass. Progress is 30/38; task 6.5 is next. No live install/profile/App/plugin
  mutation, network release, commit, push, tag, or archive action ran.
- 2026-07-25: FSR commit-tree authority task 6.5 closed six RED failures.
  Release validation now reads exact Git tree/blob/mode evidence and rejects
  content or executable-bit drift hidden by `assume-unchanged` or
  `skip-worktree`. Strict bundles protect nested manifest-named payload, reject
  special files, and require root mode `0755`; only trusted explicit historical
  layouts may omit the root manifest. The final bundle SHA-256 pinned by
  installer/runner is
  `a301822fc5347c2225c4a73c9be2f31a05bebf4fac2c80083cd4f3698f49c9b3`.
  Bundle/asset/bootstrap results are 33/33 on Python 3.12.13 and system Python
  3.9.6; strict FSR OpenSpec, dual-runtime compile, Bash syntax, and diff
  integrity pass. Progress is 31/38; task 6.6 is next. No live install/profile/
  App/plugin mutation, network release, commit, push, tag, or archive action
  ran.
- 2026-07-25: FSR remote-tag identity task 6.6 passed four focused contracts
  on Python 3.12.13 and system Python 3.9.6. Manual recovery now stages trusted
  tooling from `main`, resolves an exact remote semantic tag before target
  checkout, disables persisted checkout credentials, and checks the remote tag
  immediately before every release mutation plus final verification. Tag
  movement aborts before later writes. Progress is 32/38; task 6.7 is next.
  No live install/profile/App/plugin mutation, network release, commit, push,
  tag, or archive action ran.
- 2026-07-25: FSR historical release retry task 6.7 added an explicit
  `--allow-legacy` assets CLI gate and wired it only into exact-tag manual
  recovery. Missing manifests still fail by default, unsupported tags remain
  rejected, and actual `v0.1.12`/`v0.1.13` packages canonicalize different raw
  timestamped archives to identical per-tag SHA-256 values. Historical/CLI/
  workflow coverage passes 13/13 on Python 3.12.13 and system Python 3.9.6;
  strict FSR OpenSpec, dual-runtime compile, and focused diff integrity pass.
  Progress is 33/38; task 6.8 is next. No live install/profile/App/plugin
  mutation, network release, commit, push, tag, or archive action ran.
- 2026-07-25: FSR reconcile-then-prepare task 6.8 closed the planner path that
  previously discarded pending release-relevant HEAD changes whenever the
  latest tag needed reconciliation. Independent boolean outputs now drive
  isolated reconciliation and pending-release paths; the workflow restores the
  exact original source commit between them and never shares dist roots or
  asset manifests. Planner/workflow coverage passes 21/21 on Python 3.12.13
  and system Python 3.9.6, with an additional prepare-only guard on both.
  Strict FSR OpenSpec, dual-runtime compile, workflow YAML parsing, and focused
  diff integrity pass. Canonical progress is 30/35 implementation tasks and
  34/42 OpenSpec checkboxes; task 7.1 is next. No live install/profile/App/
  plugin mutation, network release, commit, push, tag, or archive action ran.
- 2026-07-25: FSR cleanup task 7.1 proved the five retired fail-open path
  families have no production definitions or supported callers. Exact scans
  found no mutable self-update temp-current markers, internal version
  inequality branch, catalog parse-to-empty helper, app-server stdout response
  inference, direct non-atomic release push, or clobber upload. The only
  current/backup renames remain inside tested reversible legacy promotion.
  No production deletion was needed; a static regression guard was added.
  Cleanup/workflow coverage passes 7/7 on both Python runtimes, with strict FSR
  OpenSpec, dual-runtime test compile, and focused diff integrity. Canonical
  progress is 31/35 implementation tasks and 35/42 OpenSpec checkboxes; task
  7.2 is next.
- 2026-07-25: FSR full update/release task 7.2 passed 107/107 tests on Python
  3.12.13 in 191.044 seconds with no failures or skips. This includes bundle
  containment, immutable promotion, installer/runner and self-update adapters,
  ordered internal update, release planner/reconciliation/workflow, exact
  historical retries, and obsolete-path guards. Canonical progress is 32/35
  implementation tasks and 36/42 OpenSpec checkboxes; task 7.3 is next.
- 2026-07-25: FSR full profile task 7.3 passed 193/193 tests on Python 3.12.13
  in 183.009 seconds. Duplicate release tests were aligned with the approved
  no-op complete-release plan, isolated prepare/reconcile workflow gates,
  trusted manual tooling path, and exact commit-tree authority. Update-only
  fixtures now skip unrelated fail-closed plugin repair; the dedicated plugin
  repair failure compatibility contract remains active. No production
  fail-closed behavior was relaxed. Candidate/copy/handshake and
  package-before-ref/reconciliation Completion Contract rows are checked.
  Canonical progress is 33/35 implementation tasks and 39/42 OpenSpec
  checkboxes; task 7.4 is next.
- 2026-07-25: The user approved `PARITY-001` as a subsequent independent Full
  OpenSpec but prohibited overlap with the active Goal. The fixed difference
  whitelist, core/optional health policy, Subagent v2 metadata root cause,
  no-silent-fallback requirement, expected inventory/receipt/overlay/update
  matrix, and dependency after current Goal stabilization are recorded in
  `INC-010`. No parity implementation or OpenSpec directory was created.
- 2026-07-25: FSR static/package task 7.4 passed strict OpenSpec 17/17, Bash
  syntax 5/5, Python AST 54/54 and production imports 46/46 on Python 3.12.13
  and system Python 3.9.6, workflow YAML 2/2, release static contracts 6/6,
  and `git diff --check`. The supported package adapter produced and validated
  an isolated `0.1.13` bundle with 64 manifest files, package mode `0755`,
  archive size 370922 bytes, and payload SHA-256
  `6dab0fc4e820d5f5e511e0115154d28ccfbd5e7a9db75468174a0baefd014ede`.
  Canonical progress is 34/35 implementation tasks and 41/42 OpenSpec
  checkboxes; task 7.5 is next.
- 2026-07-25: FSR task 7.5 consolidated the authoritative evidence file with
  RED/GREEN logs, immutable promotion/rollback receipts, sanitizer and bounded
  process evidence, changed files, fake Git/GitHub call ordering, current
  hashes, final validation commands, and residual risks. The change is complete
  at 35/35 implementation tasks and 42/42 OpenSpec checkboxes. This closes the
  FSR source change only; integrated review, INT-001, VER-001, and ROLLOUT-001
  remain required for the overall goal.
- 2026-07-25: `INT-001` completed after post-FSR integrated review. The review
  repaired receipt refresh/transaction ownership, managed Desktop smoke,
  canonical official advisory and manifest-drift repair, stderr-bearing catalog
  fail-closed behavior, and transient release Git authentication. Fresh serial
  results are protocol 37/37, runtime binding 55/55, verifier 22/22, and
  official advisory 6/6 on Python 3.12 and system Python 3.9, plus catalog 2/2
  and release-auth 1/1 on both runtimes. Static call-map checks prove one
  authoritative Runtime Binding, Protocol Adapter, Capability Receipt, typed
  catalog, and bounded process implementation; retired proxy helpers, the old
  canonical manifest hard rejection, and post-response config repair have no
  production path. The earlier FSR 193-test profile result predates these edits
  and is not final evidence. `VER-001` is next; no live install, switch, App
  restart, release, or Git publication ran.
- 2026-07-25: `ROLLOUT-001` restored a stable internal runtime but exposed
  `INC-011`: installed strict `0.1.13` downloads the historical latest
  `v0.1.13` asset and invokes strict source validation before comparing its
  version, producing `source_invalid` for the missing release-bundle module and
  then continuing on the current implementation. The same rollout also used an
  ad hoc `launchctl submit` restart job; macOS documents that submitted jobs are
  kept alive after failure, which caused repeated ChatGPT relaunches until the
  job was removed. Current launchd state contains no such job, and the single
  ChatGPT/internal proxy/backend chain is stable. FSR-002 reopens only the
  self-update/rollout evidence slice. Existing Python regressions already prove
  automatic Python 3.11+ selection and explicit Python 3.9 rejection before the
  switch script or store write. No further App restart is authorized or needed
  for this repair.
- 2026-07-25: `FSR-002` and `ROLLOUT-001` closed. Fresh serial verification
  passed update/release 113/113 and profile 198/198. Strict OpenSpec passed
  17/17; Bash passed 5/5; both Python 3.12.13 and system Python 3.9.6 passed
  AST 54/54 and production imports 46/46; workflow YAML passed 2/2 and release
  static contracts 7/7; isolated package validation produced payload
  `9e9c9cd4...fcecbd3`; `git diff --check` passed. The supported local-source
  install completed, and normal `codex-switch status` now prints
  `already up to date 0.1.13` without `source_invalid` or `sync failed`.
  ChatGPT pid 4983 and its internal proxy/backend pids 5332/5346 remained
  stable; launchd contains only the normal ChatGPT application job and
  `com.openai.codex-cli-path`. No App restart, release, or Git publication ran.
- 2026-07-26: After the Codex restart, the active Goal remained
  `internal-official-feature-parity`; `main`, `HEAD`, and `origin/main` remained
  at `0a9400d`, and the pre-existing dirty worktree was preserved. OpenSpec
  reports proposal, design, specs, and tasks done. The delta has 12
  requirements and 66 scenarios; the dependency-ordered ledger has 79
  unchecked implementation tasks. Initial AI plan lint found only the missing
  explicit `Acceptance Criteria` heading; the criteria were promoted into
  `tasks.md`, after which lint passed. Fresh strict validation passed for the
  active change and all 18 OpenSpec items, DevFlow workflow validation returned
  `ok: true` with only the existing legacy-root-state migration warning, and
  `git diff --check` passed. No production implementation, live profile/App
  mutation, retained-probe cleanup, dependency change, Git effect, release, or
  archive action ran. The design/spec/task review gate is next; task 1.1 remains
  unchecked.
- 2026-07-26: The parity implementation review gate was cleared by the Goal
  continuation and tasks 1.1-1.3 completed through TDD. The new focused suite
  first failed with the expected missing `codex_switch_parity` module. The
  initial implementation then exposed one canonical-JSON error because a
  read-only mapping was passed directly to `json.dumps`; converting only that
  serialization boundary to a plain dictionary made the suite GREEN. Final
  parity results are 7/7 on Python 3.12.13 and system Python 3.9.6, existing
  Runtime Binding is 55/55 on Python 3.12.13, both runtimes pass AST/import
  checks, and the focused diff check passes. The independent parity module now
  owns the verified ChatGPT reference, exact five-field allowed-difference
  whitelist, immutable foundation records, and canonical official/internal
  fingerprint freshness. No caller integration, live effect, dependency,
  retained-probe cleanup, release, archive, or Git history mutation ran. Task
  1.4 is next.
- 2026-07-26: Parity feature-inventory tasks 1.4-1.5 completed through the
  recorded RED/GREEN cycle. The RED failed on the missing
  `FeatureCommandRequest` public seam. GREEN added strict three-column parsing,
  separate isolated-default and effective-state collection, deterministic
  canonical inventory bytes, side-only feature retention, and bounded injected
  command results that fail closed on malformed, duplicate, stage-changing,
  nonzero, timeout, or truncated evidence. The parity suite passes 12/12 on
  Python 3.12.13 and system Python 3.9.6; Runtime Binding passes 55/55 on both
  runtimes; dual-runtime AST/import checks and the TDD capability gate pass.
  The existing legacy skill-layout recommendations remain nonblocking and were
  not applied. No live config, profile/App, provider, retained-probe,
  dependency, release, archive, or Git history mutation ran. Progress is 5/79;
  task 1.6 is next.
- 2026-07-26: Parity protocol-inventory task 1.6 added eight behavior tests
  defining the new parity-owned seam for all four protocol directions,
  transitive local-reference closure, documentation-insensitive canonical
  bytes, side-only method retention, producer-direction compatibility,
  additive optional fields, required/enum incompatibility, reference cycles,
  and unsupported constructs. Syntax passed; the focused suite failed at the
  expected missing `ProtocolInventory` import. No production code changed in
  this RED item. Progress is 6/79; task 1.7 is next.
- 2026-07-26: Parity protocol-inventory task 1.7 completed through TDD and a
  retained real-schema smoke. The parity module now expands local references,
  strips documentation-only fields, preserves deterministic method closures,
  compares the actual client/server producer direction, and fails closed on
  unsupported constructs. Follow-on RED guards closed Draft-07 boolean-schema
  false health for properties, array items, combinator branches, and explicit
  `additionalProperties` restrictions. Python 3.12.13 and system Python 3.9.6
  each pass 25/25 parity tests and the retained 347-official/337-internal
  comparison with identical inventory digests: 208 official methods, 202
  internal methods, 208 merged entries, 15 raw native incompatibilities, and
  six retained side-only methods. Runtime Binding passes 55/55 on both
  runtimes; dual-runtime AST/import, the source-owned TDD capability check,
  authority scan, and `git diff --check` pass. Protocol Adapter remained
  unchanged. No classification, live profile/App/provider effect, retained
  schema cleanup, dependency, release, archive, or Git history mutation ran.
  Progress is 7/79; task 1.8 is next.
- 2026-07-26: Parity adapter-evidence task 1.8 added two RED tests without
  changing Protocol Adapter production code. The tests require one
  deterministic lowercase SHA-256 seam, canonical mapping-order handling,
  sensitivity to every existing request/response/notification model-path
  table, config-write method set, and remote-marketplace literal, plus a static
  guarantee that Protocol Adapter does not import parity policy. The focused
  two-test run and complete 39-test suite failed only because
  `protocol_adapter_rule_set_digest` is missing; all 37 pre-existing Protocol
  Adapter tests passed. Test AST parsing and `git diff --check` pass. No
  production adapter/parity code, live state, dependency, retained evidence,
  release, archive, or Git history mutation ran. Progress is 8/79; task 1.9 is
  next.
- 2026-07-26: Parity adapter-evidence task 1.9 completed through two bounded
  RED/GREEN cycles. Protocol Adapter now hashes canonical JSON over the exact
  request, response, and notification model-path tables, config-write method
  set, and remote-marketplace literal. The stable digest is
  `b9ac004f3801eaf094745e6e45754c7e5a058b33775746e49682aef7c240f849`.
  `ParityCandidate` now requires that lowercase SHA-256 as caller-supplied
  evidence and rejects malformed values with
  `parity.candidate.adapter_rule_set_digest_invalid`; neither production
  module imports the other. Protocol Adapter passes 39/39 and parity passes
  26/26 on Python 3.12.13 and system Python 3.9.6. Dual-runtime AST/import,
  strict active OpenSpec validation, ownership scans, and `git diff --check`
  pass. No classification, caller integration, live state, dependency,
  retained-evidence cleanup, release, archive, or Git history mutation ran.
  Progress is 9/79; task 1.10 is next.
- 2026-07-26: Parity policy task 1.10 added eight RED contracts for baseline
  protocol core closures, `multi_agent_v2` plus active-model v2 metadata, the
  six exact optional-unless-observed methods, `skill_search`, four known
  under-development features, stage/default-only drift, pending-provider
  `tool_mode`, observed escalation, unknown feature drift, stable finding-code
  families, health/severity semantics, and deterministic finding/queue order.
  Python 3.12.13 and system Python 3.9.6 each ran 34 parity tests: all 26
  pre-existing tests passed and the eight new tests failed only because
  `evaluate_parity_policy` is missing. Test AST and `git diff --check` pass.
  No production classification or other implementation changed. Progress is
  10/79; task 1.11 is next.
- 2026-07-26: Parity policy task 1.11 implemented one immutable version-`1`
  classification table and parity-owned `evaluate_parity_policy` result. Nine
  policy tests pass after a bounded review guard proved known optional labels
  cannot whitelist internal-only drift. Stable core, optional, observed-core,
  pending-provider, and unclassified code families drive error/warning health
  and deterministic queue order. Parity passes 35/35, Protocol Adapter 39/39,
  and Runtime Binding 55/55 on Python 3.12.13 and system Python 3.9.6.
  Retained real-schema classification is identical on both runtimes: unhealthy
  with 8 core incompatibilities, 6 exact optional queue items, and 3
  unclassified methods (`account/login/start`, `externalAgentConfig/import`,
  and `plugin/share/updateTargets`). Strict OpenSpec, AI plan lint,
  dual-runtime AST/import, authority scan, and `git diff --check` pass. No
  caller or live-state integration ran. Progress is 11/79; task 1.12 is next.
- 2026-07-26: Parity serialization task 1.12 passed three focused contracts.
  Feature inventory ignores bounded runner stderr and paths; protocol inventory
  strips sensitive documentation context. Receipt-facing policy bytes now
  serialize only health, policy version, finding category/code/severity, and
  grammar-validated queue identity. Free-text message/expected/observed fields,
  a credential value and digest, Authorization/query secrets, raw config,
  prompt/model output, an absolute probe path, and 128 KiB process text are
  absent. Unsafe identifiers fail with
  `parity.policy.serialization_invalid`. Parity passes 38/38 on Python 3.12.13
  and system Python 3.9.6; strict OpenSpec, AI plan lint, dual-runtime
  AST/import, and `git diff --check` pass. Progress is 12/79; task 1.13 is next.
- 2026-07-26: Parity verification task 1.13 reran the complete focused suite
  after the restart boundary. Python 3.12.13 passed 38/38 in 0.066s and system
  Python 3.9.6 passed 38/38 in 0.071s. This closes the Reference/inventory
  slice without receipt, overlay, probe, caller, live-state, dependency,
  release, archive, cleanup, or Git history mutation. Progress is 13/79; task
  2.1 is next.
- 2026-07-26: Parity receipt task 2.1 added eight RED test methods for the
  canonical provider-bound payload, current policy/schema version, complete
  fingerprints and evidence digests, profile-local `0700`/`0600` paths,
  manifest metadata, optional-only health, missing/unsafe/mode-invalid/
  oversized/malformed/unsupported receipt states, payload digest drift, and
  official/provider/runtime staleness. Python 3.12.13 and system Python 3.9.6
  each ran 46 tests: all 38 pre-existing tests passed and the eight new methods
  failed only because the six planned receipt/path/write/load seams are
  missing. Subtests produce 12 failure reports and zero errors. Test compile
  and focused `git diff --check` pass. No production code or caller changed.
  Progress is 14/79; task 2.2 is next.
- 2026-07-26: Parity receipt task 2.2 implemented schema-v1 canonical receipt
  serialization, complete evidence/fingerprint binding, exact
  `profiles/internal/parity/{receipt.json,model-catalog.json}` resolution,
  descriptor-relative atomic receipt writes, `0700` directory and `0600` file
  enforcement, bounded no-follow identity-checked loading, duplicate-key and
  non-canonical JSON rejection, manifest metadata, digest validation, and
  official/provider/runtime/adapter staleness rejection. Parity passes 46/46,
  Protocol Adapter 39/39, and Runtime Binding 55/55 on Python 3.12.13 and
  system Python 3.9.6. Strict OpenSpec, AI plan lint, dual-runtime AST,
  ownership scans, and `git diff --check` pass. No caller imports or promotes
  the receipt. Progress is 15/79; task 2.3 is next.
- 2026-07-26: Parity overlay task 2.3 added eight RED test methods for unique
  active-model selection, the sole absent-to-v2 JSON-pointer addition,
  already-v2 zero semantic diff, deep preservation, byte/mode-preserved source,
  missing/duplicate slug, unsupported and unsafe sources, `tool_mode` and
  broader mutation rejection, and source digest/identity races. Python 3.12.13
  and system Python 3.9.6 each ran 54 tests: all 46 pre-existing tests passed,
  and the new methods produced 17 expected subtest failures only because
  `ParityOverlayArtifact`, `prepare_parity_overlay`, and
  `validate_parity_overlay` are absent. Dual-runtime compile and focused
  `git diff --check` pass. No production code or caller changed. Progress is
  16/79; task 2.4 is next.
- 2026-07-26: Parity overlay task 2.4 implemented a 16 MiB bounded no-follow
  source reader with pre/open/post inode, size, mode, mtime, ctime, and expected
  digest checks; duplicate-key/constant-safe JSON parsing; unique active-model
  selection; structured deep copy; exact JSON-pointer diff validation; and an
  immutable canonical `ParityOverlayArtifact`. Main review added guards for
  non-canonical/directly forged artifacts and explicit-null source metadata.
  Overlay 8/8 and parity 54/54 pass on Python 3.12.13 and system Python 3.9.6;
  Protocol Adapter 39/39 and Runtime Binding 55/55 pass on both runtimes.
  Strict OpenSpec, AI plan lint, dual-runtime AST, ownership scans, and diff
  checks pass. A read-only real Azure catalog smoke preserved source SHA-256
  `75c9e8d4...41f9a` and mode `0600` while producing only
  `/models/0/multi_agent_version = v2`, overlay SHA-256
  `24377577...0d85f`. No caller or promoted file changed. Progress is 17/79;
  task 2.5 is next.
- 2026-07-26: Parity manifest-candidate task 2.5 captured the RED contract for
  `ParityBundle` and `prepare_parity_bundle_artifacts`. Four tests require the
  complete receipt/overlay target paths and payload digests, parity policy,
  official reference, source catalog, Protocol Adapter, capability receipt,
  and internal fingerprint evidence; reject every missing or mismatched
  manifest field; keep generated receipt/overlay bytes in one private `0700`
  staging root with regular `0600` files; and leave the final profile paths
  absent before the transaction task. Python 3.12.13 and system Python 3.9.6
  each ran 58 tests with the same four expected failures only because the two
  production seams are absent; all 54 prior tests passed. Focused diff checks
  passed. No production code, profile artifact, caller, live state, dependency,
  release, archive, cleanup, or Git history changed. Progress is 18/79; task
  2.6 is next.
- 2026-07-26: Parity bundle tasks 2.6-2.7 implemented and verified immutable
  `ParityBundle` plus `prepare_parity_bundle_artifacts`. Receipt and overlay
  evidence must describe one source/model/change set; the manifest candidate
  has an exact 12-key path/digest/policy/fingerprint contract and rejects every
  missing or mismatched field. Generated receipt/overlay bytes are fsynced as
  regular `0600` files under one private `0700` temporary staging root, while
  final profile paths remain absent. Main review added full path/schema/internal
  fingerprint tamper coverage and receipt/overlay cross-evidence guards.
  Parity passed 58/58, Protocol Adapter 39/39, and Runtime Binding 55/55 on
  Python 3.12.13 and system Python 3.9.6. Structural source-preservation checks
  passed 3/3 on both runtimes; strict OpenSpec, AI plan lint, dual-runtime
  AST/import, no-caller authority scan, and diff checks passed. No caller,
  transaction, live profile/App/provider state, dependency, release, archive,
  cleanup, or Git history changed. Progress is 20/79; task 3.1 is next.
- 2026-07-26: Parity config-projection task 3.1 added two parity RED tests and
  two Config Document RED tests. They require internal-only
  `model_catalog_json` and `features.multi_agent_v2 = true`, exact scalar
  `[agents].max_threads` removal, preserved section/sibling/unrelated settings,
  source byte preservation, and already-clean idempotence. Python 3.12.13 and
  system Python 3.9.6 each ran 60 parity tests: all 58 prior tests passed and
  the two new tests failed only because `ConfigInputs`, `ConfigProjection`, and
  `prepare_parity_config_projection` are missing. Python 3.12.13 ran 26 Config
  Document tests: all 24 prior tests passed and the two new tests failed only
  because `ConfigDocument.remove_exact_scalar_assignment` is missing. Test
  compile and focused diff checks passed. No production code, source config,
  caller, live state, dependency, release, archive, cleanup, or Git history
  changed. Progress is 21/79; task 3.2 is next.
- 2026-07-26: Parity config-ambiguity task 3.2 added seven RED contracts for
  duplicate, dotted, non-scalar, invalid-TOML, multiply sourced, symlinked, and
  concurrently replaced `agents.max_threads` inputs, plus two direct Config
  Document rejection tests. Every invalid parity case requires an unhealthy
  error finding, empty changed paths, no authoritative removal source, and no
  source writes. Python 3.12.13 and system Python 3.9.6 each ran 67 parity
  tests: all 58 pre-projection tests passed and the nine projection tests failed
  only because the three planned projection seams are absent. Python 3.12.13
  ran 28 Config Document tests: all 24 pre-removal tests passed and the four
  removal tests failed only because the exact removal method is absent. Compile
  and focused diff checks passed. No production code or external state changed.
  Progress is 22/79; task 3.3 is next.
- 2026-07-26: Parity config-projection task 3.3 implemented immutable
  `ConfigInputs`/`ConfigProjection`, exact scalar-assignment removal, bounded
  no-follow source reads, internal-only overlay/v2 projection, and stable
  unhealthy findings without adding a caller or write path. Main review found
  and closed one cross-source TOCTOU false-health gap: replacing an earlier
  source with the same bytes while a later source was read now fails final
  all-source identity/digest revalidation. The new regression was RED then GREEN
  on Python 3.12.13 and system Python 3.9.6. Fresh parity passed 69/69 on both
  runtimes, Config Document 29/29 on Python 3.12.13, Protocol Adapter 39/39 and
  Runtime Binding 55/55 on both runtimes. AI-native plan lint, strict active/all
  OpenSpec validation, dual-runtime production imports/AST, `py_compile`,
  no-caller authority scans, and `git diff --check` passed. No source config,
  caller, transaction, live state, dependency, release, archive, cleanup, or
  Git history changed. Progress is 23/79; task 3.4 is next.
- 2026-07-26: Parity home-sync task 3.4 added two RED contracts for an
  unmaterialized and an already-materialized internal managed home. They require
  projected profile model/overlay/v2 values to override absent or stale runtime
  values, preserve shared feature and unrelated profile/provider settings,
  remove only stale `agents.max_threads`, retain sibling agent/TUI settings,
  return staged runtime text without writing it, and leave profile/shared source
  bytes unchanged. Python 3.12.13 and system Python 3.9.6 each produced exactly
  two assertion failures and zero errors: the old home-sync path omits the
  overlay in the first case and selects `stale-model` in the second. Six adjacent
  home-sync/profile regressions passed on Python 3.12.13; their direct system
  Python 3.9 route remains unavailable because those existing tests require
  Python 3.11+ `tomllib`. Test compile and focused diff checks passed. No
  production code, source/runtime config, caller, transaction, live state,
  dependency, release, archive, cleanup, or Git history changed. Progress is
  24/79; task 3.5 is next.
- 2026-07-26: Parity home-sync task 3.5 added the narrow
  `ConfigProjection` seam to canonical internal home derivation. A healthy
  projection supplies projected profile and shared texts; the projected
  profile contributes both profile-specific values and shared v2 feature state,
  while a materialized runtime is treated as derived output and cannot restore
  stale model, feature, or `agents.max_threads` values. The function returns
  staged managed-home text and writes no source or runtime file. Focused
  projection tests passed 2/2 on Python 3.12.13 and system Python 3.9.6,
  adjacent home-sync/profile tests passed 6/6, full profile passed 200/200, and
  transaction passed 219/219 on Python 3.12.13. Parity remained 69/69 on both
  runtimes and Config Document passed 29/29 on Python 3.12.13. Dual-runtime
  import/compile and focused diff checks passed. A direct full Config Document
  run on system Python 3.9 reproduced its established unsupported
  Python-3.11+-`tomllib` baseline rather than a change regression; the new
  focused path supplies the bounded parser fixture and passes on 3.9. No live
  profile/App/provider mutation, transaction promotion, dependency, release,
  archive, cleanup, or Git history changed. Progress is 25/79; task 3.6 is next.
- 2026-07-26: Parity probe task 3.6 added ten RED contracts for exact
  candidate backend/home/config/overlay/capability inputs, initialize then
  `collaborationMode/list`/`thread/start` ordering, typed explorer v2 child and
  parent markers, explicit v1 rejection, timeout, malformed or missing
  responses, early exit, oversized output, full process-group termination,
  post-run candidate staleness, and bounded secret-stable evidence. Python
  3.12.13 and system Python 3.9.6 each ran 79 parity tests: all 69 prior tests
  passed and all ten new tests failed only because `ParityProbeInputs`,
  `ParityProbeRequest`, `ParityProbeCommandResult`, `ParityProbeResult`,
  `ParityProbeReport`, and `run_parity_probes` are absent. Test compile and
  focused diff checks passed with zero errors. No production probe, provider
  traffic, source/runtime write, live state, dependency, release, archive,
  cleanup, or Git history changed. Progress is 26/79; task 3.7 is next.
- 2026-07-26: Parity probe task 3.7 implemented immutable candidate input,
  request, command-result, result, and report records; candidate artifact
  snapshot/revalidation; bounded default execution with complete process-group
  termination; ordered core response validation; exact typed explorer v2
  markers; stable finding/result codes; and secret-stable evidence digests.
  The initial ten probe contracts passed 10/10 on both runtimes after replacing
  a startup-racy Python descendant fixture with a deterministic shell process
  group. Main review then added RED/GREEN guards that reject mixed v1/v2,
  repeated v2 spawns, and out-of-order spawn/child/parent completion, and aligned
  probe sanitization with the existing verifier coverage for headers,
  assignments, URL userinfo, bearer tokens, and signed query values. Fresh
  parity passes 81/81 on Python 3.12.13 and system Python 3.9.6. Dual-runtime
  AST/import/export checks, focused `git diff --check`, and process-residue
  inspection pass. The TDD methodology is ready; the broader dependency
  diagnostic still reports the established unrelated DevFlow skill-layout
  source conflicts, which were not mutated. No caller integration, retained
  probe fixture, provider traffic, source/runtime write, live profile/App
  mutation, dependency, release, archive, cleanup, or Git history change ran.
  Progress is 27/79; task 3.8 is next.
- 2026-07-26: Parity retained-probe task 3.8 added the checked-in
  `testdata/parity/retained-v2-probe-redacted.json` fixture outside the release
  allowlist. It preserves synthetic Azure provider/model/wire/query structure,
  exact core and typed-v2 event shapes, a safe routing marker, and only
  `[REDACTED]` credential placeholders. The focused test was RED on the missing
  fixture, then GREEN on Python 3.12.13 and system Python 3.9.6. It substitutes
  two different transient API-key values into isolated temporary candidate
  configs, proves report bytes and result digests are identical, proves neither
  secret/config/path is retained, and compares the complete candidate file tree
  before and after to prove no copy or write. Fresh parity passes 82/82 on both
  runtimes; dual-runtime JSON/AST/secret scans and focused diff checks pass. The
  old retained v2 directory was inspected only for filenames/modes; its
  credential-bearing config was not read, copied, rewritten, or deleted. No
  provider traffic, live profile/App mutation, source/runtime write,
  dependency, release, archive, cleanup, or Git history change ran. Progress is
  28/79; task 3.9 is next.
- 2026-07-26: Parity slice verification task 3.9 completed with no production
  source change. Complete parity passed 82/82 on Python 3.12.13 and system
  Python 3.9.6. Config Document passed 29/29 on Python 3.12.13; its direct
  system-Python run reproduced the established unsupported `tomllib` baseline
  exactly at 3 failures and 22 errors. The two bounded-parser home-sync
  projection tests passed 2/2 on both runtimes. Verifier passed 22/22 on Python
  3.12.13 and on the supported system-Python route with
  `CODEX_SWITCH_PYTHON=/opt/homebrew/bin/python3.12`; the two sanitizer tests
  also passed 2/2 under native system Python 3.9.6. Strict active OpenSpec,
  dual-runtime AST of all 56 scripts, production imports for parity, Config
  Document, home sync, and verify, and `git diff --check` passed. Generated
  bytecode was removed. No live provider call, profile/App mutation,
  source/runtime write, dependency, release, archive, migration, cleanup, or
  Git history effect occurred. Progress is 29/79; the Config/probes slice is
  complete and task 4.1 RED is next.
- 2026-07-26: Runtime bundle task 4.1 added tests-only schema-v3 RED contracts
  in `scripts/test_codex_transaction.py`. `RuntimeBindingTextArtifact` and
  `commit_runtime_binding_bundle` must produce a prepared marker whose exact
  required roles are manifest, launcher, capability receipt, parity receipt,
  parity overlay, and profile config. A four-case matrix permits shared config
  and active runtime config only when explicitly supplied. Caller artifact
  order is reversed to require role/path authority rather than input order, and
  a hard interruption at `after_marker` proves the marker can be inspected
  before any target write. Python 3.12.13 and system Python 3.9.6 each ran two
  methods with five expected failure reports and zero errors, all naming only
  the two missing bundle seams. Existing runtime-rebind interruption, unsafe
  receipt, symlink-swap, and legacy v1 recovery tests pass 4/4 on both
  runtimes. Strict active OpenSpec, dual-runtime test compile, whitespace, diff,
  and generated-bytecode cleanup checks passed. No production code, runtime
  target, live state, dependency, release, archive, migration, cleanup, or Git
  history changed. Progress is 30/79; task 4.2 is next.
- 2026-07-26: Runtime bundle task 4.2 added tests-only target-safety contracts
  in `scripts/test_codex_transaction.py`. Commit-time matrices reject duplicate,
  unexpected, parent/child-overlapping, missing-required, symlinked, directory,
  wrong-mode, and larger-than-catalog payload bundles before marker creation or
  target writes. Recovery matrices reject invalid old/new state digests and
  prepared/committed foreign target mixtures before changing any other target.
  Python 3.12.13 and system Python 3.9.6 each ran five new methods with 13
  expected failure reports and zero errors; the combined task 4.1-4.2 RED set
  is seven methods with 18 expected reports on each runtime. Every report names
  only the missing `RuntimeBindingTextArtifact` and
  `commit_runtime_binding_bundle` seams. Existing v1/v2 runtime-rebind tests
  pass 4/4 on both runtimes, and dual-runtime AST plus trailing-whitespace
  checks pass. Strict active OpenSpec, `git diff --check`, and generated-
  bytecode cleanup also pass after the control-plane updates. No production
  code, runtime marker/target, live state, dependency, release, archive,
  migration, cleanup, or Git history changed. Progress is 31/79; task 4.3 is
  next.
- 2026-07-26: Runtime bundle task 4.3 added
  `RuntimeBindingTextArtifact` and `commit_runtime_binding_bundle()` in
  `scripts/codex_switch_transaction.py`. The schema-v3 marker stores the exact
  required and optional role/path entries in deterministic overlay/receipt,
  config, launcher, manifest-last activation order. Commit validates duplicate,
  overlap, allowlist, mode, target type, and a 16 MiB text-artifact bound before
  marker creation, then revalidates remaining old states before each write.
  Recovery validates marker fields, embedded old/new payload digests and modes,
  preflights every current target before writing, and rechecks each target
  immediately before convergence. Main-agent review found and fixed a
  post-preflight foreign-state TOCTOU gap; a bounded guard proves a late foreign
  manifest is retained rather than overwritten. The legacy
  `commit_runtime_binding_pair()` and schema-v1/v2 recovery behavior remain
  unchanged. Focused schema-v3 tests pass 9/9 on Python 3.12.13 and system
  Python 3.9.6; complete transaction passes 228/228 on both; Runtime Binding
  passes 55/55 on both. No live profile/App/provider mutation, dependency,
  release, archive, migration, cleanup, or Git history effect occurred.
  Progress is 32/79; task 4.4 is next.
- 2026-07-26: Runtime bundle task 4.4 added an eleven-phase hard-interruption
  matrix in `scripts/test_codex_transaction.py` and the corresponding bounded
  fault hooks in `commit_runtime_binding_bundle()`. Prepared phases cover the
  durable marker plus parity overlay, capability receipt, parity receipt,
  shared config, profile config, active runtime config, launcher, and manifest.
  Every phase asserts the exact partial prefix before recovery and complete old
  bytes/modes after lock-entry recovery. Committed phases cover committed-marker
  publication and marker retirement; both retain or recover complete new
  bytes/modes and retire the marker. The initial RED produced ten expected
  failures and zero errors on each runtime because only `after_marker` existed.
  GREEN focused schema-v3 passes 11/11 on Python 3.12.13 and system Python
  3.9.6; complete transaction passes 230/230 on both. Dual-runtime AST/import,
  strict active OpenSpec, whitespace/diff, and generated-bytecode checks pass.
  No live profile/App/provider mutation, dependency, release, archive,
  migration, cleanup, or Git history effect occurred. Progress is 33/79; task
  4.5 is next.
- 2026-07-26: Runtime bundle task 4.5 added a direct legacy marker
  characterization in `scripts/test_codex_transaction.py` with four fixtures:
  schema-v1/schema-v2 crossed with prepared/committed state. Each fixture writes
  the exact legacy field set and embedded old/new payload format, starts from a
  mixed valid generation, and proves recovery converges bytes and modes to the
  established old or new generation. Schema v1 leaves its unrelated receipt
  byte- and mode-unchanged; schema v2 owns and converges the receipt. A parser
  routing guard proves neither legacy schema enters the schema-v3 validator.
  The fixture method passes all four subtests on Python 3.12.13 and system
  Python 3.9.6. Complete transaction passes 231/231 on both. No production
  source, live state, dependency, release, archive, migration, cleanup, or Git
  history changed. Progress is 34/79; task 4.6 is next.
- 2026-07-26: Runtime bundle task 4.6 added tests-only internal `set-bin`
  contracts in `scripts/test_codex_runtime_binding.py` and
  `scripts/test_codex_profile_switch.py`. Two runtime-binding methods require
  `prepare_parity_bundle()` before launcher smoke, prohibit the legacy pair
  commit, and require manifest, launcher, capability receipt, parity receipt,
  parity overlay, profile config, shared config, and active runtime config in
  one schema-v3 bundle. One profile method covers failed, unknown, core, and
  unclassified parity evidence and snapshots every promotion target plus the
  configured source catalog bytes/mode. Both runtimes produce the intended
  RED: two runtime failures plus four unhealthy-evidence subtest failures, with
  zero errors. Four established rebind tests pass on both runtimes; dual-runtime
  AST and whitespace checks pass. No production source, live state, dependency,
  release, archive, migration, cleanup, or Git history changed. Progress is
  35/79; task 4.7 is next.
- 2026-07-26: Runtime bundle task 4.7 integrated
  `prepare_parity_bundle()` into internal `set-bin` before launcher smoke,
  rejected failed, unknown, core-incompatible, unclassified, and incomplete
  evidence before any promotion, generated the launcher and manifest from the
  same parity/capability/config metadata, attested the smoke child backend, and
  committed parity overlay, capability receipt, parity receipt, shared config,
  profile config, active runtime config, launcher, and manifest through the
  schema-v3 bundle with manifest last. Review hardening added stable no-follow
  `active.json` reads, exact-integer schema validation, bounded marker reads,
  marker-generation checks before writes, and identity-bound marker retirement.
  Fresh Python 3.12.13/system Python 3.9.6 results are parity 83/83, Runtime
  Binding 60/60, transaction 238/238, and the four-case unhealthy profile guard
  1/1 on each runtime. Source-owned DevFlow diagnosis is
  `ready_with_recommendations`; legacy skill-layout cleanup remains out of
  scope. No live state, dependency, release, archive, migration, cleanup, or
  Git history effect occurred. Progress is 36/79; task 4.8 is next.
- 2026-07-26: Runtime bundle task 4.8 added tests-only launcher/shell
  equivalence and mixed-generation contracts in
  `scripts/test_codex_runtime_binding.py`. The first method executes the real
  active internal shell shim and managed Desktop launcher against one observed
  managed-home config, overlay, v2 setting, backend generation, and capability
  receipt generation. The second method restores an old launcher against the
  new manifest and a new launcher against the old overlay and requires
  rejection before the backend marker is written. Both Python 3.12.13 and
  system Python 3.9.6 produce the intended RED: two methods, three assertion
  failures, and zero errors. The shell still reports the old runtime and no
  expected receipt digest; both mixed-generation subtests exit zero and write
  their backend markers. Ten established internal rebind tests and the
  four-case unhealthy promotion guard remain green on both runtimes. No
  production source, live state, dependency, release, archive, migration,
  cleanup, or Git history changed. Progress is 37/79; task 4.9 is next.
- 2026-07-26: Runtime bundle task 4.9 removed
  `commit_runtime_binding_pair()` and all production/test callers after `rg`
  proved the schema-v3 bundle is the sole current commit interface. The
  internal shell shim now resolves the active manifest dynamically, and shell
  plus parity-aware Desktop launchers call one shared Runtime Binding validator
  before backend execution. Zero parity fields retain the established legacy
  launch path; any parity field requires a complete launcher, backend,
  capability receipt, parity receipt, overlay, projected config, and v2
  generation. Review found one same-contract gap: missing receipt `overlay` or
  `internal_fingerprint.capability_receipt_sha256` bindings were skipped. A
  bounded RED produced two assertion failures with zero errors, and GREEN now
  rejects both before the backend marker. Fresh Runtime Binding is 63/63 on
  Python 3.12.13 and system Python 3.9.6; transaction is 234/234 on both; the
  complete Python 3.12 profile suite is 201/201. Schema-v1/v2 fixture recovery,
  dual-runtime AST, active/all strict OpenSpec, `git diff --check`, adapter
  authority, and generated-bytecode checks pass. The first unisolated runtime
  run exposed an existing test-only shell-profile leak and temporarily wrote a
  deleted temporary store path into the managed `~/.zshrc` block. The block was
  immediately restored to `/Users/cY/.codex-switch/bin`; status re-attested the
  active internal PATH/GUI/LaunchAgent/proxy/backend chain, and the test now
  binds its shell profile to its temporary directory. Repeated focused and full
  runs preserve the repaired `~/.zshrc` digest and metadata. No profile, App,
  provider, install, dependency, release, archive, migration, cleanup, or Git
  history effect remains. Progress is 38/79; task 4.10 is next.
- 2026-07-26: Runtime bundle task 4.10 completed the integrated slice review
  and fresh serial verification. Review found one required same-contract false
  health gap: runtime generation detection recognized only five of the twelve
  parity manifest fields, so a manifest retaining only
  `parity_adapter_rule_set_sha256` was treated as legacy and both Desktop and
  shell started the backend. The bounded RED failed one method with two route
  failures and zero errors on Python 3.12.13 and system Python 3.9.6. GREEN
  reserves the `parity_` manifest namespace for generation evidence, so any
  parity field invokes the existing complete shared validator before backend
  execution; genuinely zero-parity manifests retain the legacy path. Fresh
  final results are parity 83/83 on both runtimes, Protocol Adapter 39/39 on
  both, Runtime Binding 65/65 on both, transaction 234/234 on both, complete
  profile 201/201 on Python 3.12.13, and focused supported profile routes 2/2
  on system Python 3.9.6. Direct profile CLI execution on Python 3.9 exits 2
  before creating the store with the required Python 3.11+ message. Active and
  all strict OpenSpec pass at 18/18, dual-runtime AST passes 56/56, retired
  pair/authority scans, `git diff --check`, and bytecode-residue checks pass.
  The isolated shell profile preserved the real `~/.zshrc` SHA-256
  `8a144f4d2221437b65119b343b356958fb3155a63e3037956c0ce8aa9da224b4`
  and inode/size/mtime/ctime. No live profile, App, provider, install,
  dependency, release, archive, migration, cleanup, or Git history effect
  occurred. Progress is 39/79; the runtime bundle/rebind slice is complete and
  task 5.1 is next.
- 2026-07-26: Staged internal update task 5.1 added seven command-boundary RED
  tests in `scripts/test_codex_update_release.py`. They require a validated
  mode-0700 private sibling candidate directory, unchanged bound bytes during
  installer and candidate probes, exact intended-version rejection, code-sign
  before final candidate validation, exact installer failure propagation,
  complete zero-mutation dry-run reporting, and no bound replacement before a
  failed candidate parity/compatibility path. Focused Python 3.12 and system
  Python 3.9 each ran seven methods with the same expected RED results and no
  fixture/import errors. The complete Python 3.12 update/release suite ran
  120 tests: all 113 pre-existing tests remained green and the only failures
  were 12 assertions inside the seven new RED methods. The real `~/.zshrc`
  remained byte- and metadata-identical. No production source, live profile,
  App, provider, install, dependency, release, archive, cleanup, or Git history
  effect occurred. Progress is 40/79; task 5.2 is next.
- 2026-07-26: Staged internal update task 5.2 refactored
  `scripts/codex_env_setup` so a real helper invocation requires an explicit,
  absent `.codex-internal-update-*` directory beside the bound binary, creates
  it as mode `0700`, and never moves, backs up, replaces, or deletes the bound
  path. The helper preserves model, Azure endpoint, auth, and intended version;
  code-signs before final exact-version validation; and returns the installer
  pipeline's nonzero status unchanged. Main review added one bounded guard for
  a trusted installer that mutates the bound binary. The helper now snapshots
  the bound executable with no-follow descriptor reads and
  dev/inode/mode/size/mtime/ctime/SHA-256 evidence, then compares it after the
  installer, candidate code-sign, and candidate version probe. The guard
  reproduced the missing check on Python 3.12 and system Python 3.9 before
  implementation and passes afterward. Six helper tests pass 6/6 on Python
  3.12 and with the helper forced to native system Python 3.9. The complete
  eight-test staged class has only the two planned wrapper RED methods:
  complete dry-run reporting and parity-before-bound-replacement. A corrected
  selection harness ran all 113 pre-existing update/release tests GREEN on
  Python 3.12; an earlier ad hoc selector attempt omitted `scripts/` from
  `sys.path` and was discarded after producing import-only errors. Bash syntax,
  focused diff checks, and main review pass. The real `~/.zshrc` remains
  byte- and metadata-identical. No live profile, App, provider, install,
  dependency, release, archive, cleanup, or Git history effect occurred.
  Progress is 41/79; task 5.3 is next.
- 2026-07-26: Staged internal update task 5.3 added five tests-only
  executable-swap journal methods in `scripts/test_codex_transaction.py`.
  The approved seam is a typed `RuntimeBindingExecutableSwap` passed to
  `commit_runtime_binding_bundle()`, with exact bound/candidate/backup paths,
  old/new modes and SHA-256 digests, and no binary payload in the schema-v3
  marker. The matrix covers rejection before marker publication, interruptions
  before and after both renames, prepared rollback, committed roll-forward,
  backup retention, and foreign bound/candidate/backup preservation. Focused
  Python 3.12.13 and system Python 3.9.6 each ran five methods with 23 expected
  assertion failures and zero errors; every failure is the missing typed seam.
  All 234 pre-existing transaction tests pass on both runtimes when
  `CODEX_SWITCH_SHELL_PROFILE` is isolated. One earlier unisolated baseline
  run atomically rewrote the real `~/.zshrc` with byte-identical content:
  SHA-256, size, mode, and mtime remained unchanged, while inode changed from
  `48028504` to `48362010` and ctime to `1785062545`. Subsequent isolated full
  runs preserved the complete current stat tuple. No profile, App, provider,
  install, dependency, release, archive, cleanup, or Git history effect
  occurred. Progress is 42/79; task 5.4 is next.
- 2026-07-26: Staged internal update task 5.4 implemented the typed
  schema-v3 executable swap in `scripts/codex_switch_transaction.py`.
  `RuntimeBindingExecutableSwap` binds exact bound/candidate/backup paths,
  old/new executable modes, and SHA-256 digests; the marker never embeds
  binary bytes. Commit requires a mode-0700 `.codex-internal-update-*` sibling
  stage, verifies the initial old/new/missing triple, writes the prepared
  marker, durably renames old-to-backup then candidate-to-bound, promotes the
  text bundle, publishes committed, and retains the old backup for the later
  handshake. Prepared recovery restores old bound plus staged candidate;
  committed recovery converges to new bound plus old backup. Any foreign
  bound/candidate/backup state blocks recovery before mutation. Main review
  hardened stage validation through a no-follow parent descriptor and
  revalidates the path contract at every binary phase. Focused swap tests pass
  5/5 on Python 3.12.13 and system Python 3.9.6. Fresh complete transaction
  results are 239/239 in 22.863s and 120.646s respectively. Strict OpenSpec,
  dual-runtime AST/import, interface/authority, `git diff --check`, and
  bytecode-residue checks pass; schema-v1/v2 and schema-v3-without-swap remain
  covered by the full suite. All accepted commands used an isolated shell
  profile and preserved the real `~/.zshrc` current hash/stat tuple. No new
  live profile, App, provider, install, dependency, release, archive, cleanup,
  or Git history effect occurred. Progress is 43/79; task 5.5 is next.
- 2026-07-26: Staged internal update task 5.5 added wrapper-level RED
  coverage in `scripts/test_codex_update_release.py`. The dry-run contract now
  names candidate capability/parity receipts, catalog overlay, projected
  profile/shared/active-runtime configs, launcher/manifest, the complete
  locked fingerprint set, and post-promotion-only bound smoke. A candidate
  capability failure must invoke staged
  `app-server generate-json-schema` without executing the bound binary. A
  temporary promotion driver requires exact bound/candidate/backup paths,
  complete artifact roles, and binary/manifest/reference/catalog/capability/
  config revalidation while holding the store-root lock, then exits 73 before
  mutation. Python 3.12.13 and system Python 3.9.6 each report the same four
  expected behavior failures with zero errors. The six task-5.2 installer
  methods pass 6/6 on both runners and all 113 pre-existing update/release
  tests pass on Python 3.12 in 225.396s. Dual-runtime syntax, focused diff
  integrity, and bytecode-residue checks pass. Every accepted run used an
  isolated shell profile and preserved the real `~/.zshrc` stable hash/stat
  tuple. No production implementation, live profile/App/provider/update,
  dependency, release, archive, cleanup, or Git history effect occurred.
  Progress is 44/79; task 5.6 is next.
- 2026-07-26: Staged internal update task 5.6 integrated the wrapper with the
  production `promote-internal-update` boundary. Public `--install-dir` and
  `CODEX_INSTALL_DIR` inputs still identify and validate the bound profile
  directory, but the trusted helper always receives a fresh private
  `.codex-internal-update-*` sibling. The promotion path validates the staged
  version, prepares candidate capability/parity/overlay/config/runtime
  artifacts, revalidates immutable inputs under the store mutation lock,
  commits the typed executable swap and schema-v3 runtime bundle together, and
  emits success only after installed version, canonical Runtime Binding,
  app-server child attestation, capability receipt, and parity receipt all
  pass. Fresh staged update results are 10/10 on Python 3.12.13 and system
  Python 3.9.6; update/release is 123/123 on Python 3.12; profile update routes
  are 30/30 on both runtimes; complete profile is 201/201 on Python 3.12;
  parity is 83/83 on both; Runtime Binding is 65/65 on both; executable swap is
  5/5 on both. The real `~/.zshrc` hash/stat tuple remained unchanged and no
  bytecode residue was created. Active strict OpenSpec is valid, all strict
  OpenSpec is 18/18, Bash syntax and dual-runtime 56-file AST checks pass, and
  `git diff --check` passes. No live profile/App/provider/update,
  dependency, release, archive, cleanup, or Git history effect occurred.
  Progress is 45/79; task 5.7 is next.
- 2026-07-26: Staged internal update task 5.7 added seven tests-only
  post-promotion handshake rollback methods in
  `scripts/test_codex_runtime_binding.py`. The approved seam is the hidden
  production `promote-internal-update` command, not the private validator.
  Every case first installs a complete old generation, then promotes a
  byte-distinct candidate binary plus manifest, launcher, capability receipt,
  parity receipt, overlay, profile config, shared config, and active runtime
  config. Version, canonical Runtime Binding, app-server, capability-receipt,
  overlay, config, and parity checks fail independently only after promotion.
  Python 3.12.13 reports 7 expected failures and 0 errors in 37.428s; system
  Python 3.9.6 reports the same 7 expected failures and 0 errors in 89.100s.
  Every failure reaches the intended `SwitchError` and then reports the same
  nine unrestored targets, proving the current defect is missing rollback
  rather than fixture setup. Adjacent complete-bundle/rebind tests pass 3/3
  and executable-swap tests pass 5/5 on both runtimes. The real `~/.zshrc`
  hash/stat tuple remains unchanged, generated bytecode residue was removed,
  and no production source, live profile/App/provider/update, dependency,
  release, archive, cleanup, or Git history effect occurred. Progress is
  46/79; task 5.8 is next.
- 2026-07-26: Staged internal update task 5.8 moved the complete
  post-promotion version, canonical Runtime Binding, app-server child,
  capability-receipt, overlay/config artifact, and parity-receipt handshake
  into the schema-v3 prepared transaction window. A failed validator now
  recovers the old binary and all eight old runtime-bundle targets before
  returning nonzero, leaving the candidate restaged and printing neither
  success nor restart-required output. After every handshake postcondition
  passes, commit converges the new generation, retires the exact old backup,
  removes the marker, and only then prints verified success and one restart
  notice. The seven task-5.7 methods are GREEN, and success coverage proves
  backup retirement plus output ordering. Fresh complete Runtime Binding is
  74/74 in 109.873s and 244.134s on Python 3.12.13 and system Python 3.9.6.
  Fresh transaction is 239/239 in 24.153s and 140.002s on the same runtimes.
  Main review found no blocking spec, foreign-state, output-order, or scope
  issue. One unisolated Python 3.12 transaction validation run passed 239/239
  but atomically rewrote the real `~/.zshrc` with identical bytes, size, mode,
  and mtime, changing inode `48362010` to `48897140` and ctime `1785062545` to
  `1785074064`; the immediately started system-Python run was stopped. The
  test harness now assigns every transaction test a private temporary shell
  profile, and both final full runs passed without an external isolation
  variable while preserving the new stable hash/stat tuple. No shell content,
  profile, App, provider, internal install/update, dependency, release,
  archive, cleanup, or Git history effect occurred. Progress is 47/79; task
  5.9 is next.
- 2026-07-26: Staged internal update task 5.9 added one focused
  characterization proof in `scripts/test_codex_runtime_binding.py` for an
  externally owned internal backend. At the durable prepared-marker boundary,
  the test requires the same eight parity/runtime artifact roles as normal
  rebind, no executable-swap entry, and no artifact target at the external
  path. After success it requires the external file's device, inode, full
  mode, link count, size, mtime, ctime, bytes, and parent directory entries to
  remain exact, the canonical manifest/binding to reference that path, no
  same-byte managed store copy, and no remaining marker. The first focused run
  reached the final copy scan and failed only because the new test omitted the
  `stat` import; after that test-only correction, the proof passes 1/1 in
  2.669s and 5.989s on Python 3.12.13 and system Python 3.9.6. The adjacent
  complete-bundle and commit-rollback selector passes 3/3 in 7.752s and
  17.639s. No production code changed. The real `~/.zshrc` stable hash/stat
  tuple, live profile/App/provider state, dependencies, release/archive state,
  and Git history remain unchanged. Progress is 48/79; task 5.10 is next.
- 2026-07-26: Staged internal update task 5.10 closed the slice with fresh,
  serial, privately isolated verification and no production edit. Python
  3.12.13 passes update/release 123/123 in 221.574s, transaction 239/239 in
  24.524s, Runtime Binding 75/75 in 111.688s, parity 83/83 in 2.096s, and
  complete profile 201/201 in 280.376s. System Python 3.9.6 passes
  update/release 123/123 in 252.430s, transaction 239/239 in 139.227s,
  Runtime Binding 75/75 in 253.637s, and parity 83/83 in 34.185s. Its direct
  complete profile attempt reproduced only the established Python
  3.11+-`tomllib` boundary (201 tests, 1 failure and 83 errors); the two
  supported projection routes pass 2/2 in 4.924s, and direct profile CLI exits
  2 before creating its temporary store. The active plan now records a layered
  validation budget: focused task-local regressions, complete slice matrices
  only at slice closure, and full-project verification only at 7.x/9.x.
  Adjacent complete-suite reruns require changed related code, an uncollected
  result, or a real failure. Shared abstractions, extra fault axes, and
  non-contract edge tests require a named failing acceptance criterion;
  otherwise they are `DEFER_AND_CONTINUE`. The real `~/.zshrc` remains at its
  stable post-5.8 tuple. No live profile/App/provider/internal update,
  dependency, commit, push, release, archive, cleanup, or Human Gate 8.1
  effect occurred. Progress is 49/79; task 6.1 is next.
- 2026-07-26: Diagnostics task 6.1 added five tests-only RED methods in
  `scripts/test_codex_verify.py`. The tests define one read-only parity result
  flowing through the existing collect, structured-report, and command seams:
  missing/malformed/stale evidence retains stable codes; core, unclassified,
  and probe failures are unhealthy; an optional-only queue stays healthy and
  deterministic; report text sanitizes finding messages while preserving
  structure; and `--repair=none` loads once and leaves the store byte-exact.
  Python 3.12.13 runs 5 tests in 0.021s with 5 expected assertion failures and
  0 errors; system Python 3.9.6 runs the same 5 tests in 0.017s with the same
  failures and 0 errors. Both interpreters compile the changed test. Active
  strict OpenSpec, tracked/untracked whitespace checks, and the stable
  `~/.zshrc` tuple pass; generated bytecode from the explicit compile check was
  removed exactly. The DevFlow dependency diagnostic confirms the project
  `tdd` methodology resource is ready but still reports pre-existing unrelated
  source conflicts/legacy layout recommendations. Per the existing approval
  boundary, no dependency activation, legacy cleanup, or conflict resolution
  ran. No production source, live profile/App/provider/internal update,
  dependency, commit, push, release, archive, cleanup, or Human Gate 8.1
  effect occurred. Progress is 50/79; task 6.2 is next.
- 2026-07-26: Diagnostics task 6.2 implemented the bounded read-only parity
  receipt path in `scripts/codex_switch_verify.py`. The verifier now recognizes
  an old manifest with no receipt as `parity.receipt.missing`, validates
  existing receipt bytes and manifest ownership through the parity loader,
  current Protocol Adapter digest, canonical runtime generation, official and
  internal binary fingerprints, source catalog, and projected config digests,
  and converts those results into one `ParityReport`. Collection adds only
  error findings to verification problems, keeps optional-only drift healthy
  with deterministic queue ordering, structured reports preserve codes while
  sanitizing messages, and `cmd_verify --repair=none` collects exactly once.
  One focused loader RED first failed with only the absent
  `collect_parity_report` entry point on Python 3.12.13 and system Python 3.9.6.
  The final dual-runtime result is 6/6 `ParityVerificationTests` on each
  interpreter; four adjacent canonical-smoke/report regressions also pass on
  each. Dual-runtime AST parsing covers both touched Python files. Per the
  layered validation budget, no unchanged verifier/full-project matrix was
  repeated. No receipt, overlay, config, profile, App, provider, runtime,
  dependency, release, archive, cleanup, or Git history mutation occurred.
  Progress is 51/79; task 6.3 tests-only RED is next.
- 2026-07-26: Diagnostics task 6.3 added two profile-level tests-only RED
  contracts in `scripts/test_codex_profile_switch.py`. They require status,
  Doctor, and verify to collect the same injected `ParityReport` exactly once;
  print the same healthy/unhealthy state and stable finding codes; keep
  optional-only drift healthy; and print the deterministic feature-before-
  protocol synchronization queue. The first run exposed only a verify fixture
  error because the temporary store had no manifest. A tests-only empty
  manifest stub let verify reach the existing parity seam without weakening
  any behavior assertion. The final Python 3.12.13 result is 2 tests in 0.027s
  with 6 expected assertion failures and 0 errors; system Python 3.9.6 runs the
  same 2 tests in 0.026s with the same 6 failures and 0 errors. Status and
  Doctor do not yet call parity collection; verify collects once but does not
  print the shared health/finding/queue lines. Per the layered validation
  budget, no unchanged complete profile, verifier, diagnostics-slice, or
  project-wide suite was repeated. No production source, live profile/App/
  provider/internal update, dependency, commit, push, release, archive,
  cleanup, or Human Gate 8.1 effect occurred. Progress is 52/79; task 6.4 is
  next.
- 2026-07-26: Diagnostics task 6.4 implemented the smallest shared parity
  command presentation in `scripts/codex_switch_verify.py`. The verifier owns
  health derivation, sanitized error messages, stable finding-code output, and
  deterministic queue formatting. `cmd_verify` prints that result from its
  single preloaded report; status collects it only for the active internal
  profile; Doctor collects it only when its existing active Runtime Binding is
  internal and adds only sanitized error findings to its failure list. A
  healthy receipt resolves a missing caller binding only when full runtime-
  generation validation is needed; missing or already-unhealthy evidence does
  not trigger extra binding discovery. Python 3.12.13 and system Python 3.9.6
  pass the two shared diagnostics tests 2/2 and parity verifier tests 6/6.
  Three adjacent Python 3.12 status/inactive-internal Doctor regressions pass
  3/3; dual-runtime AST passes 4/4 touched files. Two direct system-Python
  adjacent subprocess checks stop at the established Python 3.11+ `tomllib`
  boundary, while the supported explicit-interpreter Doctor route passes.
  One legacy one-key internal fixture now fails at the intended
  `parity.receipt.missing` gate; its explicit staged repair behavior belongs
  to tasks 6.5/6.6 and is recorded `DEFER_AND_CONTINUE`, not implemented here.
  No public CLI, repair route, parity policy, live profile/App/provider effect,
  dependency, commit, push, release, archive, cleanup, or Human Gate 8.1
  effect occurred. Progress is 53/79; task 6.5 tests-only RED is next.
- 2026-07-26: Diagnostics task 6.5 added two verifier repair-routing RED
  methods and one profile-level read-only characterization. Explicit parity
  repair must expose `repair_internal_parity(args, binding)`, delegate to the
  existing `cmd_set_bin` with `name=internal`, the canonical current backend,
  and `preserve_app_cli=False`, then recollect parity and succeed only from the
  fresh result. Python 3.12.13 runs the two RED methods in 0.015s with two
  expected assertion failures and zero errors; system Python 3.9.6 runs them
  in 0.014s with the same result. The failures are exactly the absent repair
  seam and no post-rebind recollection. Separately, status, active-internal
  Doctor, and `verify --repair=none` preserve manifest, receipt, overlay,
  profile config, and managed launcher inode, mode, size, timestamps, and
  bytes; that test passes 1/1 in 0.018s and 0.020s respectively. No production
  source, public CLI, live profile/App/provider/internal update, dependency,
  commit, push, release, archive, cleanup, or Human Gate 8.1 effect occurred.
  Progress is 54/79; task 6.6 is next.
- 2026-07-26: Diagnostics task 6.6 implemented the smallest approved repair
  route in `scripts/codex_switch_verify.py`. The new verifier-local seam
  imports `codex_switch_bindings.cmd_set_bin` only when invoked, delegates
  `internal` to the current canonical backend with managed Desktop binding,
  and adds no public command. `verify --repair=safe` invokes that route only
  when the current internal parity report is unhealthy, then reloads the
  manifest/active record, resolves Runtime Binding again, recollects parity,
  and prints/evaluates only the fresh report. The two focused RED methods
  became GREEN on Python 3.12.13 (2/2 in 0.108s) and system Python 3.9.6
  (2/2 in 0.068s). The full `ParityVerificationTests` pass 8/8 in 0.146s and
  0.098s respectively; exact read-only artifact characterization passes 1/1
  in 0.015s on both. Dual-runtime syntax checks and targeted diff hygiene
  pass, and the one generated Python 3.12 bytecode file was removed exactly.
  No live rebind, profile/App/provider/internal update, public CLI, in-place
  artifact patch, dependency, commit, push, release, archive, retained-
  evidence cleanup, or Human Gate 8.1 effect occurred. Progress is 55/79;
  task 6.7 is next.
- 2026-07-26: Diagnostics/package task 6.7 closed the release-runtime gap.
  `codex_switch_parity.py`, `codex_switch_runtime_binding.py`,
  `codex_switch_app_proxy.py`, and `codex_switch_home_sync.py` are now explicit
  manifest-required modules. Release staging checks every generated
  `$SCRIPT_DIR`/`$SWITCH_SCRIPTS` Python reference, imports every packaged
  non-test Python module in an isolated copy, and rejects missing imports,
  payload mutation, or unresolved generated-script targets before public
  finalization. `package-release.sh` now selects Python 3.11+ with `tomllib`;
  legacy immutable canonicalization adds inert placeholders for the same new
  required modules. Python 3.12.13 and system Python 3.9.6 each convert the
  five-method/eight-failure RED contract to 5/5 GREEN in 2.618s and 2.683s.
  Four adjacent manifest/archive, immutable-status, legacy, and installer/
  runner regressions pass 4/4 in 10.687s and 12.311s; profile package adapter
  passes 1/1 in 1.364s and 1.472s. An isolated real package contains 66
  manifest files, 5 directories, 20 required paths, 73 archive members, and
  payload SHA-256 `bbd79beaf00d33a48009630284ac84bb8547e215f2122e2fb1781d59c8428ae5`;
  parity is present while retained probe/config evidence and bytecode are
  absent. No live profile/App/provider/internal update, dependency, commit,
  push, release publication, archive, retained-evidence cleanup, or Human Gate
  8.1 effect occurred. Progress is 56/79; task 6.8 is next.
- 2026-07-26: Diagnostics/docs task 6.8 updated only `README.md` and
  `SKILL.md` for the parity operator contract. Both now bind parity to the
  current verified ChatGPT Desktop bundled CLI rather than PATH, network
  latest, cached metadata, or the stable advisory; preserve exactly the
  internal binary/model/endpoint/provider/auth identity differences; separate
  unhealthy core, unclassified, stale, malformed, and probe failures from the
  deterministic optional synchronization queue; and state that missing or
  failed v2 evidence never falls back silently to v1. Explicit safe repair is
  documented as the staged current-backend rebind rather than in-place
  artifact patching. Staged updates keep the bound binary available, retain
  last-known-good through the complete version/binding/app-server/capability/
  parity handshake, suppress success/restart output on failure, and print one
  restart notice only after durable success and backup retirement. Source,
  update, or rebind success is explicitly insufficient for live acceptance;
  ChatGPT quit/reopen, a real typed `explorer` task, and ownership attestation
  remain behind Human Gate 8.1. Focused contract scans, obsolete
  `Codex.app` wording scan, active strict OpenSpec, and docs diff hygiene pass.
  No complete suite was repeated under the layered validation budget. No live
  profile/App/provider/internal update, dependency, commit, push, release,
  archive, cleanup, or Human Gate 8.1 effect occurred. Progress is 57/79;
  task 6.9 authority scans are next.
- 2026-07-27: Diagnostics authority task 6.9 found one real ownership
  violation: `scripts/codex_switch_verify.py` constructed evidence-error
  `ParityFinding` values and mapped finding codes to categories. A focused
  parity ownership test failed only because `parity_error_report` was absent on
  Python 3.12.13 and system Python 3.9.6. The minimal GREEN moved that
  classification and unresolved report construction into
  `scripts/codex_switch_parity.py`; verifier now imports the single parity
  entry point and supplies only the profile directory, optional current
  backend, code, and message. The ownership test passes 1/1 on both runtimes,
  and `ParityVerificationTests` pass 8/8 in 0.158s and 0.128s. Runtime Binding,
  Protocol Adapter, and Capability Receipt policy scans have zero hits; the
  production caller scan for `ParityFinding`, queue/policy construction,
  evaluation, and overlay preparation has zero hits outside parity. No full
  adjacent suite was repeated. No live profile/App/provider/internal update,
  dependency, commit, push, release, archive, cleanup, or Human Gate 8.1
  effect occurred. Progress is 58/79; task 6.10 diagnostics slice closure is
  next.
- 2026-07-27: Diagnostics/package task 6.10 closed the slice with no
  production edit. Python 3.12.13 passes 9/9 `ParityReferenceTests` in 0.073s,
  8/8 `ParityVerificationTests` in 0.155s, 4/4 profile diagnostics/package
  seams in 1.529s, 37/37 `CodexUpdateReleaseTests` in 22.639s, 10/10 staged
  internal update tests in 9.721s, and 3/3 immutable package/release-workflow/
  historical-installer adjacency tests in 9.260s. System Python 3.9.6 passes
  the same 71 tests: 9/9 in 0.078s, 8/8 in 0.123s, 4/4 in 1.400s, 37/37 in
  24.581s, 10/10 in 7.626s, and 3/3 in 10.123s. The matrix used private
  `CODEX_SWITCH_SHELL_PROFILE` roots and the real `~/.zshrc` retained SHA-256
  `8a144f4d...224b4`, inode `48897140`, size `1959`, mode `600`, mtime
  `1785055828`, and ctime `1785074064`. No acceptance failure, bytecode
  residue, live profile/App/provider/internal update, dependency, commit, push,
  release, archive, cleanup, or Human Gate 8.1 effect occurred. Progress is
  59/79; task 7.1 integrated parity verification is next.
- 2026-07-27: Integrated verification task 7.1 ran the complete parity suite
  fresh with bytecode disabled. Python 3.12.13 passes 84/84 in 2.151s; system
  Python 3.9.6 passes the same 84/84 in 32.688s. This includes the new
  `parity_error_report` ownership regression plus every reference, inventory,
  classification, receipt, overlay, config projection, probe, and bundle
  contract. No production or test source changed, the real `~/.zshrc`
  retained its stable hash/stat tuple, and no bytecode residue or live effect
  occurred. Progress is 60/79; task 7.2 cross-module integrated suites are
  next.
- 2026-07-27: Integrated verification task 7.2 completed the serialized
  cross-module matrix. Python 3.12.13 passes Protocol Adapter 39/39 in
  23.720s, Runtime Binding 75/75 in 112.316s, transaction 239/239 in 23.888s,
  verifier 30/30 in 10.958s, update/release 126/126 in 253.292s, and complete
  profile 204/204 in 273.063s. System Python 3.9.6 passes Protocol Adapter
  39/39 in 22.916s, Runtime Binding 75/75 in 249.496s, transaction 239/239 in
  137.982s, verifier 30/30 in 10.847s, update/release 126/126 in 300.648s, and
  supported profile projection 2/2 in 5.063s. Direct profile CLI exits 2 with
  the required Python 3.11+ message before creating its temporary store. The
  integrated run exposed only bounded fixture/reference drift: trusted
  installer/runner module hashes were refreshed to the current verified
  source, intentionally invalid update candidates are now mutated after valid
  bundle construction and rebuilt, unrelated profile lookup/plugin tests skip
  parity verification explicitly, and Python 3.9 verifier fixtures choose the
  installed Python 3.12/3.11 wrapper runtime. No parity-health production
  behavior was weakened. Dual-runtime AST for the two final touched tests,
  targeted diff hygiene, real `~/.zshrc` identity, and bytecode-residue checks
  pass. No live profile, App, provider, internal update/install, dependency,
  commit, push, release, archive, cleanup, or Human Gate 8.1 effect occurred.
  Progress is 61/79; task 7.3 strict OpenSpec validation is next.
- 2026-07-27: Integrated verification task 7.3 passed both strict OpenSpec
  gates. The active `internal-official-feature-parity` change is valid, and
  `openspec validate --all --strict --no-interactive` reports 18 passed and 0
  failed. No source, live state, dependency, Git, release, archive, cleanup, or
  Human Gate 8.1 effect occurred. Progress is 62/79; task 7.4 DevFlow workflow
  validation is next.
- 2026-07-27: Integrated verification task 7.4 ran the pinned DevFlow
  `validate_workflow_state.py` against the repository. It returned `ok=true`,
  `issues=[]`, and one existing warning that legacy DevFlow root state is
  read-only and should migrate to `.planning/devflow/STATE.md` before the
  1.0.0 sunset. The warning is recorded only; no generated guidance, provider
  selection, dependency, legacy layout, or root-state migration was changed.
  Progress is 63/79; task 7.5 syntax/import verification is next.
- 2026-07-27: Integrated verification task 7.5 passed `/bin/bash -n` for all
  five required shell entrypoints. Python 3.12.13 and system Python 3.9.6 each
  parsed all 56 Python scripts and imported all 47 non-test production modules
  with bytecode disabled. The initial PATH-relative `bash` invocation did not
  execute because the tool shell PATH omitted Bash; the accepted absolute
  `/bin/bash` rerun passes 5/5. No bytecode residue or live/external effect
  remains. Progress is 64/79; task 7.6 isolated package verification is next.
- 2026-07-27: Integrated verification task 7.6 built a real package under
  `/private/tmp/codex-switch-parity-76.tX4jAq` through
  `scripts/package-release.sh`. Independent validation and the release-bundle
  validator agree on 66 manifest files, 5 directories, 20 required paths, 47
  production imports, and 73 archive members. Every file/directory mode, size,
  digest, tar member, and import-time immutable snapshot matches. The archive
  is 525008 bytes with SHA-256 `b9d21c9f...fe598a`; manifest payload SHA-256 is
  `5cb103bb...46758d`. Parity is present; retained probes, `.planning`,
  OpenSpec, testdata, profile config/auth evidence, symlinks, special files,
  and bytecode are absent. No repository `dist/`, live state, dependency, Git,
  release publication, archive operation, cleanup, or Human Gate 8.1 effect
  occurred. Progress is 65/79; task 7.7 diff/write-set/coverage audit is next.
- 2026-07-27: Integrated review task 7.7 passed `git diff --check` and
  classified the complete dirty worktree without mutation: 71 paths belong to
  the active parity change and 130 are preserved as pre-existing or unrelated.
  The canonical write set now names the retained fixture, Runtime Binding/shim,
  release-bundle/promotion modules, and `install.sh`/`run.sh`. The two shell
  entrypoints are owned only for trust-anchor constants matching
  `codex_switch_release_bundle.py` SHA-256 `dd121b4c...baac13` and
  `codex_switch_promotion.py` SHA-256 `561f6720...ad199c`; no update-framework
  expansion occurred. Fresh Runtime Binding, Protocol Adapter, Capability
  Receipt, and production caller scans report zero duplicate parity-policy
  ownership. The delta retains 12 requirements and 66 scenarios: 60 map to
  named automated tests already green in tasks 7.1-7.2, and six map only to
  unexecuted live tasks 8.1-8.7. No adjacent full suite was repeated under the
  layered validation budget. No production/test behavior, live state,
  dependency, Git, release/archive, cleanup, or Human Gate 8.1 effect changed.
  Progress is 66/79; task 7.8 read-only main review is next.
- 2026-07-27: Integrated review task 7.8 completed a read-only main-agent
  inspection against the canonical OpenSpec requirements and active-change
  write set. Receipt diagnostics remain fail-closed: the initial bounded
  candidate parse is not health authority, because the manifest digest-bound
  parity loader safely reopens the canonical profile artifact, rejects
  duplicate/noncanonical payloads, verifies current fingerprints and adapter
  rules, and then validates the active runtime generation, backend, overlay,
  source catalog, and projected config. Catalog/config preparation uses
  non-symlink regular-file reads with identity/digest checks, returns in-memory
  projections, revalidates mutable inputs before marker publication, and lets
  the recovery journal own only the exact approved config targets; the source
  catalog is never a transaction target. Promotion keeps the old executable
  through all seven post-promotion handshake checks; each failure regression
  requires the complete old generation, no marker/backup residue, and no
  restart output, while success retires the backup before its single restart
  notice. Probe/report evidence is bounded and sanitized, persisted receipts
  retain endpoint digest/auth-source kind rather than credentials, and the
  Protocol Adapter remains exact and policy-free. No acceptance criterion
  failed. A real crash between committed-marker retirement and executable
  backup retirement could leave a stale backup without a marker; because the
  new generation is already durable and the approved interruption contract
  does not include this cleanup window, it is recorded `DEFER_AND_CONTINUE`
  with residual disk-residue/manual-inspection risk. Per the layered validation
  budget, no unchanged suite was rerun. No source/test behavior, live state,
  dependency, Git, release/archive, cleanup, or Human Gate 8.1 effect occurred.
  Progress is 67/79; task 7.9 evidence consolidation is next.
- 2026-07-27: Integrated evidence task 7.9 consolidated the complete
  pre-Human-Gate record. The verification file now indexes all 23 named RED
  sections and their GREEN closures; exact task-7.1 parity and task-7.2
  cross-module dual-runtime counts; strict OpenSpec, workflow, shell,
  AST/import, package, diff, authority, coverage, and main-review results;
  stable receipt/reference/overlay/config/adapter, classification, and bounded
  probe finding codes; verified CLI/adapter/source/overlay/synthetic-receipt/
  retained-probe/package digests; the residual optional queue policy; and the
  exact task-8.1 live prerequisites. It explicitly distinguishes synthetic and
  retained artifacts from live receipt/overlay/config evidence, which does not
  exist before an authorized rebind. The canonical active-change set is now 73
  dirty paths, including 35 parity checkpoints, while all 130 pre-existing or
  unrelated paths remain preserved. Scoped `git diff --check` and active
  strict OpenSpec validation pass after the evidence edit. No full suite was
  repeated and no production/test behavior, live profile/App/provider/internal
  update, dependency, commit, push, release, archive, cleanup, or Human Gate
  8.1 effect occurred. Progress is 68/79; stop at task 8.1 for explicit user
  authorization.
- 2026-07-27: The user authorized Human Gate task 8.1, advancing progress to
  69/79. The exact task-8.2 source install then exited 2 after 11.948 seconds
  with `candidate_invalid` and `Release manifest required paths mismatch`.
  Independent package validation proved the new source candidate is valid with
  payload `5cb103bb...46758d`, and a fresh isolated installer layout promoted
  it successfully. The live failure occurs when `_read_ref()` validates the
  installed 0.1.13 `current` release: its schema-v1 manifest has the supported
  historical 16 required paths, while the new validator requires the current
  20-path set including parity, Runtime Binding, App Proxy, and home-sync
  modules. Live `current` remains `ed5d74c1...28ab`, rollback remains
  `9eb07bbc...3f33`, promotion-state SHA-256 remains
  `ad51285c...c85e`, the candidate release is absent, and ChatGPT/proxy/backend
  pids remain `95489`/`95838`/`95842`. This is a failing task-8.2 acceptance
  criterion, so one bounded installer-level RED will cover upgrade from the
  exact historical manifest-v1 path set, followed by the smallest compatible
  validation change. No task-8.3 rebind, restart, provider task, dependency,
  Git effect, release/archive, or cleanup occurred. Progress remains 69/79.
- 2026-07-27: Task 8.2 closed the live install RED with one bounded
  installer-level TDD cycle. The new test reproduces the exact supported
  manifest-v1 16-path current release and failed with
  `candidate_invalid` on Python 3.12.13 and system Python 3.9.6, then passed
  1/1 and with four adjacent package/promotion/legacy/hash-bound contracts 5/5
  on both runtimes. Production accepts that exact historical path set only
  while reading existing immutable references; all new candidates retain the
  current 20-path requirement. Installer/runner trust anchors were refreshed
  to release-bundle `3772b5ba...0437` and promotion
  `fe516e54...7f70`. The exact authorized install promoted
  `d55005d6...4392`, retained `ed5d74c1...28ab` as rollback, and printed no
  restart instruction. Strict installed validation reports 66 files, 5
  directories, and 20 required paths; the prebuilt and installed trees are
  byte-exact, parity is present, and staging/bytecode residue is zero. The
  existing ChatGPT/proxy/backend pids remain `95489`/`95838`/`95842`, so no
  restart or task-8.3 effect occurred. Progress is 70/79; task 8.3 is next.
- 2026-07-27: Task 8.3 ran the supported same-backend command exactly once:
  `/Users/cY/.local/bin/codex-switch --skip-self-update set-bin internal
  /Users/cY/.local/bin/codex`. It exited 2 after 2.342 seconds at the parity
  health gate with two `parity.feature.core_drift`, eight
  `parity.protocol.core_incompatible`, and three
  `parity.protocol.unclassified_drift` findings. The immediate post-failure
  snapshot matched the preflight hashes and stat tuples for manifest, launcher,
  capability receipt, profile config, official config, managed-home config,
  and active record; backend SHA-256 remained `410ebcd3...e8b6`, pids remained
  `95489`/`95838`/`95842`, and no parity directory, `.runtime-rebind-*`
  directory, or `.runtime-binding-rebind.json` marker existed. The running
  Codex session later rewrote only the managed-home config at 12:30, after the
  exact no-mutation snapshot; that concurrent runtime write must be recaptured
  before any future retry and is not attributed to the failed transaction.
  Progress remains 70/79. Next is one read-only, in-process diagnostic that
  stops immediately after bundle preparation and prints sanitized finding
  details; no rebind retry or task 8.4 may start.
- 2026-07-27: The one permitted read-only task-8.3 diagnostic completed in
  1.9558 seconds and stopped before smoke or transaction code. It confirmed
  error findings for `item_ids` and `multi_agent_v2`; core protocol
  incompatibilities on client requests `thread/realtime/start`,
  `thread/resume`, `turn/start`, and `turn/steer`, server requests
  `item/commandExecution/requestApproval` and
  `item/permissions/requestApproval`, and server notifications
  `item/autoApprovalReview/started` and
  `item/autoApprovalReview/completed`; plus unclassified incompatibilities on
  client requests `account/login/start`, `externalAgentConfig/import`, and
  `plugin/share/updateTargets`. Fourteen warnings matched the already planned
  optional protocol/feature/model-metadata queue. The eight core methods were
  already selected by the task-1.11 core namespace policy, but parity evaluates
  them before probes. The current receipt binds only the adapter rule-set
  digest, not exact per-method capability coverage, and the core app-server
  probe exercises only `initialize`, `initialized`,
  `collaborationMode/list`, and `thread/start`; it does not discharge these
  eight incompatibilities. Therefore no finding can be safely downgraded and
  no partial repair can complete task 8.3. All persistent targets, parity and
  marker paths, diagnostic temporary-directory identities, and
  ChatGPT/proxy/backend pids `95489`/`95838`/`95842` matched before and after
  the diagnostic. Progress remains 70/79; task 8.3 is
  `BLOCKED_AWAITING_HUMAN`, task 8.4 is not dependency-ready, and only a
  focused plan/RED-GREEN repair that proves the complete acceptance blocker may
  resume implementation.
- 2026-07-27: The user authorized only the planning revision for the task-8.3
  acceptance blocker. The existing Full OpenSpec now defines one atomic
  thirteen-error RED/GREEN: four server-method errors close only through
  semantic nullable-union normalization; `thread/resume` binds the existing
  exact ID/opaque-reasoning transform while its audio variant remains an exact
  optional extension; realtime/turn audio, Bedrock login, external-agent
  import, and listed sharing use exact schema-pair optional-unless-observed
  records; `item_ids` requires the observed-path adapter proof; and
  `multi_agent_v2` requires final post-probe evidence. Receipt schema v2 binds
  sorted method coverage, and version 1 is regenerated rather than patched.
  The plan names the exact implementation write set, retained RED fixture,
  negative matrix, dual-runtime tests, consumer/package/planning validation,
  rollback, continuation, stop conditions, `PARITY-8.3-IMPLEMENT`, and
  `PARITY-8.3-LIVE-RETRY`. Current ownership is official, so the earlier
  internal pid/config snapshot is historical only and must not be reused.
  Active OpenSpec and AI-native plan lint passed during drafting; complete
  planning validation is recorded in the new checkpoint and parity evidence
  section. No production/test/fixture/operator-doc, installed, live,
  dependency, Git, release, archive, or cleanup action occurred. Progress
  remains 70/79 and task 8.3 remains unchecked. Final planning checks report
  active strict OpenSpec valid, all strict OpenSpec 18/18, AI-native plan lint
  passed, DevFlow `ok=true` with no issues and the known legacy-state warning,
  and tracked/untracked planning diff hygiene passed.
- 2026-07-27: The user consumed `PARITY-8.3-IMPLEMENT`, then selected an
  official-profile-first pause because repeated profile switching is now low
  priority. The bounded implementation closes the saved thirteen-error
  preparation blocker with a canonical 21-rule Protocol Adapter manifest,
  exact method coverage, nullable-union normalization, seven exact
  adapter/optional dispositions, a versioned official Desktop acceptance
  trace, exact post-probe fingerprint revalidation, and canonical receipt-v2.
  All 11 named RED tests became GREEN. Fresh dual-runtime suites pass:
  Protocol Adapter 41/41, parity 93/93, verifier 30/30, and Runtime Binding
  75/75. Read-only reviewers found no task-8.3 spec or engineering-policy
  blocker. The isolated package at
  `/private/tmp/codex-switch-parity-83.HLiOjw` validates with payload SHA-256
  `12c60c7e...a967fb`; six task source/package paths are byte-exact and the
  retained fixture is excluded. No live installation, rebind, provider call,
  ChatGPT restart, profile/App mutation, dependency, Git, release, archive, or
  cleanup occurred. This establishes `OFFICIAL_FIRST_PAUSE_READY`; task 8.3
  remains unchecked at 70/79 and execution stops at
  `PARITY-8.3-LIVE-RETRY`.
- 2026-07-28: Live diagnosis proved the reported missing Plugins/default UI
  and apparent internal-bin bypass share one preparation defect. The trusted
  installer inherited live `HOME`, `CODEX_HOME`, and `PATH`; it overwrote the
  live config and appended each sibling candidate to `.zshrc` before parity
  promotion. Direct official/internal plugin inventories are identical,
  synchronized safe config values agree, the internal manifest and generated
  wrapper name `/Users/cY/.local/bin/codex`, and direct app-server smoke passes,
  so proxy behavior, plugin catalogs, and global-state projection are not the
  repair path. The user selected Scheme A: hermetic installer scratch plus
  exact recoverable live repair and one controlled update/rebind/switch/restart.
  The active change now has 84 tasks and remains at 70 completed until the five
  incident tasks execute.
- 2026-07-28: Installer isolation tasks 8.3A.1-8.3A.3 are complete at 73/84.
  The harmful public-seam test reproduced live-config mutation before the
  production change. The final helper uses one identity-bound mode-0700
  private installer root, private `HOME`/`CODEX_HOME`, candidate-first child
  PATH, explicit signal forwarding/reaping, and exact cleanup on success,
  failure, `HUP`, `INT`, and `TERM`. Final serial update/release suites pass
  132/132 on Python 3.12 and 132/132 on system Python 3.9. Strict active/all
  OpenSpec, AI-plan lint, workflow validation, Bash/dual-runtime AST, isolated
  package validation/source identity, diff/write-set, and scratch/config/
  credential/bytecode residue checks are clean. The earlier parallel full
  runs hit unrelated one-second candidate-smoke timeouts under contention;
  each serial rerun passed without changing production timeouts or unrelated
  tests. Task 8.3A.4 exact backup/repair is next.
- 2026-07-28: Task 8.3A.4 is complete at 74/84. All exact live targets matched
  their diagnosed bytes, text counts, types, owners, modes, inodes, and
  versions before mutation. Recovery directory
  `/Users/cY/.codex-switch/backups/20260728T161949+0800-installer-side-effect-recovery`
  is mode 0700 and contains a byte-identical mode-0600 `.zshrc` copy plus all
  three original mode-0700 candidate directories with their original inodes.
  The live `.zshrc` diff removes only the three named installer blocks; the
  plugin app-server path and canonical shim block remain. A clean-environment
  interactive zsh resolves bare `codex` to
  `/Users/cY/.codex-switch/bin/codex` and reports official
  `codex-cli 0.146.0-alpha.3.1`. Nothing was deleted.
- 2026-07-28: Task 8.3A.5 installed exact source payload
  `275ad2e2...71dab`; strict immutable validation reports 66 files and the
  installed env helper is byte-identical to source. The one authorized update
  staged internal 0.145.0 in private candidate
  `.codex-internal-update-6efc91d3f6359549077f8a00` and failed safely before
  promotion with `parity.feature.core_drift`. Installer isolation is proven:
  `.zshrc`, official/internal configs, and bound 0.144.6 hashes/modes are
  unchanged and scratch is absent. A forced-stop read-only diagnostic shows
  only `item_ids` is unhealthy: the exact `thread/resume.params.history`
  dependency is present, current `client_request:thread/resume` is natively
  compatible with no reasons, and therefore no adapter coverage record exists.
  Current policy incorrectly requires adapter coverage even when the method is
  natively compatible. The exact systemic correction and negative matrix are
  planned, but Scheme A's source write set excludes
  `scripts/codex_switch_parity.py` and `scripts/test_codex_parity.py`.
  Implementation, retry, switch, and restart stop at a new explicit Human
  Gate; no parity bypass, second download, or old-backend completion claim is
  permitted.
- 2026-07-28: The user explicitly consumed
  `PARITY-0.145-NATIVE-RESUME-IMPLEMENT`. The approved source write set is
  exactly `scripts/codex_switch_parity.py` and
  `scripts/test_codex_parity.py`, plus main-owned control-plane evidence.
  Execute the named native-compatible `thread/resume` RED/GREEN and the
  recorded dual-runtime/source/package checks first. A live retry may use only
  the already retained, freshly re-attested 0.145.0 candidate; it may not
  download another candidate or expand proxy/plugin/provider/Git/release/
  cleanup scope.
- 2026-07-28: The named Python 3.12 native-resume test is RED exactly at the
  expected `healthy` assertion (1 run, 1 failure). The fixture supplies both
  current `client_request:thread/resume` schemas, `compatible=true`, no reason
  codes, the sole `thread/resume.params.history` dependency, and no adapter
  coverage. This records the current policy false negative before any
  production edit.
- 2026-07-28: The minimal production correction accepts the sole exact resume
  dependency only when the current direction/method comparison contains both
  schemas and is compatible, or when the existing exact adapter coverage
  remains accepted. The named regression and all four recorded negatives pass
  on Python 3.12/system Python 3.9; `ParityMethodCoverageTests` passes 6/6 and
  the complete parity suite passes 94/94 on each runtime. Update/release
  adjacency and source/package review remain pending before live reuse.
- 2026-07-28: Two-axis read-only review found one in-scope fail-closed guard:
  `ProtocolInventoryComparisonEntry` permits contradictory
  `compatible=true` with non-empty reason codes. The new isolated negative is
  RED with 1 test/1 failure only at that contradiction. Spec evidence was also
  tightened so missing-side checks retain `compatible=true` and assert the
  feature-level `item_ids` core finding, while wrong direction and wrong method
  cannot satisfy the exact resume proof.
- 2026-07-28: The review guard is closed and both final review axes report no
  remaining findings. Method coverage passes 7/7 and complete parity 95/95 on
  Python 3.12/system Python 3.9. Fresh post-review update/release passes
  132/132 in 284.472s and 132/132 in 348.351s respectively. Dual-runtime
  AST/import, active/all strict OpenSpec 18/18, AI-plan lint, workflow
  `ok=true`/zero issues, diff integrity, and an isolated 66-file package with
  byte-exact changed files pass. Checkpoint
  `.planning/checkpoints/2026-07-28-internal-0145-native-resume-source-verified.md`
  is the live-retry authority.
- 2026-07-28: The exact retained-candidate promotion retried after fresh
  source/live attestation and failed safely before transaction with
  `parity.probe.missing_response`. Bound/candidate inodes and hashes, every
  live shell/config/manifest/launcher/active hash, backup absence, marker
  absence, and zero runtime/installer scratch prove no mutation. A
  credential-free differential shows official 0.146, bound 0.144.6, and
  candidate 0.145 all return only initialize when the current runner closes
  stdin immediately; response-paced sends return all three required IDs from
  candidate in order. This is a new core-probe EOF contract, not candidate
  protocol drift. Planning stops at
  `PARITY-CORE-PROBE-INTERACTIVE-IMPLEMENT`; checkpoint
  `.planning/checkpoints/2026-07-28-internal-0145-core-probe-eof-gate.md`.
- 2026-08-04: `independent-app-cli-profiles` intake and planning are complete.
  Live read-only evidence proves Desktop official ownership and an unmanaged
  shell/internal PATH mismatch; `--skip-app-cli` is not a persistent healthy
  selection. The new complete OpenSpec defines the explicit internal-CLI/
  official-App command, additive legacy-compatible state, atomic rollback,
  split-aware diagnostics/smokes, unchanged defaults, and packaged behavior.
  Active/all strict OpenSpec passes 20/20, workflow validation is healthy, and
  the TDD dependency gate is ready. The user's request authorizes the named
  source/test/docs/control-plane write set. No live switch, install, restart,
  parity-gate execution, dependency, Git, release, archive, cleanup, or
  destructive effect occurred. Task 1.1 RED is next.
- 2026-08-04: `independent-app-cli-profiles` completed all 18 tasks through
  serialized RED/GREEN and two-axis review. Fresh final results are Runtime
  Binding 88/88, Transaction 241/241, Verify 33/33 (362 combined), Profile
  210/210, Update/Release 133/133, and Parity 95/95. Python/Bash static checks,
  active and repository-wide strict OpenSpec 20/20, DevFlow workflow validation
  with zero issues/warnings, source/package identity, isolated packaged split
  preview, and diff integrity pass. Release-counterpart Plugin Eval remains
  54/100 and is classified under INC-012 `DEFER_AND_CONTINUE`; fixing the
  pre-existing bundle/token/complexity findings needs a separate benchmarked
  package/skill change. No live install, switch, App restart, parity-gate
  execution, dependency, Git, release, archive, cleanup, or destructive effect
  occurred. The terminal outcome is `READY_FOR_EXTERNAL_EFFECT` only if the
  user separately authorizes installation and the live split switch.
- 2026-08-05: the shared Plugin/Skill reopen reached 40/40 focused GREEN and
  fresh Profile 210/210 plus Update/Release 133/133, but the independent final
  review correctly returned `BLOCKED`. Required Completion Contract failures
  were stale-receipt cache trust, value-level marketplace secrets, Plugin-Skill
  path escape, incomplete stopped-App proof, missing target CAS, uninspectable
  backend-managed receipts, retained-version policy loss, state/generation
  integrity, and non-recoverable multi-file publication. These findings are
  in-scope rather than deferred incidental work. The existing OpenSpec was
  updated with prepared-journal recovery, state commit-point, collision-safe
  orphan generation retention, cache re-attestation, second stopped proof,
  target CAS, and stricter secret/path policy; strict validation passes. New
  isolated RED evidence runs 46 shared config/materialization tests with 21
  expected failures. The production materializer `uninspectable` path is now
  GREEN at 14/14 without invoking native add; the shared-core recovery/safety
  closure and fresh integrated proof remain active. No live App/profile/cache,
  backend/network, Git, dependency, install, release, archive, or cleanup
  effect occurred.
- 2026-08-05: Decision 14 closed the prepared-commit ordering gap with a private
  pre-backend `pending-materialization.json`, exact target/config/selector
  binding, selective owned-delta recovery, target CAS, stopped-App proofs,
  intent/main-journal exclusion, and retained-untrusted cache artifacts. The
  focused shared matrix reached 69/69. Independent review then reproduced one
  final P1: parent `SIGKILL` could leave `codex plugin add` alive after early
  recovery retired an unchanged intent. OpenSpec Decision 15 promoted that
  required behavior instead of deferring it.
- 2026-08-05: Decision 15 is GREEN and independently approved. Locked reconcile
  exposes its exact active store-root flock FD through a private identity-bound
  seam; the production materializer independently validates it and passes it to
  both catalog/list and plugin/add subprocesses. A real task-owned subprocess
  proves parent `SIGKILL`, surviving backend, store-busy early apply with exact
  intent/config preservation, late selector write, backend exit, and Decision
  14 recovery before new planning. Main reruns passed the named race 1/1,
  shared configuration/materialization/lifecycle 72/72, and transaction
  241/241. Final independent review returned `APPROVE` with no P1/P2 blocker.
- 2026-08-05: fresh terminal source evidence passes Runtime Binding 88/88,
  Verify 33/33, Profile 211/211, Update/Release 133/133, Parity 95/95, and
  config-document 29/29. Python compile, five Bash syntax checks, eval JSON,
  active/all strict OpenSpec 20/20, diff integrity, and the final workflow
  validator pass. The isolated package at
  `/private/tmp/codex-switch-final-release.SJEkjM/codex-switch.tar.gz` runs the
  shared matrix 72/72 plus split preview 1/1; key source/package hashes are
  byte-exact. Final release-counterpart Plugin Eval is 58/100, grade D, with
  two budget failures and three warnings under INC-012. The read-only updater
  confirms DevFlow installed cache `matches-source` and project Skill layout is
  current, while broader project migration remains pending and was not applied.
  No install, live switch/sync, App restart, real backend/cache mutation,
  dependency, Git, release, archive, cleanup, or destructive effect occurred.
- 2026-08-06: the user authorized deployment. The first two supported install
  attempts failed before promotion because the live installed references use
  the exact prior 20-path manifest generation while the new validator accepted
  only current 22-path or legacy 16-path lists. INC-016 consumed the change's
  one bounded guard: the public installer RED failed 1/1 with the live error;
  exact 16/20 generation matching plus reordered-list rejection is GREEN 3/3,
  and package checks are GREEN 2/2. A fresh contract-bound package produced
  payload `e6caa91b...e4983` and archive `fc279ac8...b287`; supported install
  now points `current` at the 22-path payload and retains prior `6a23d04f...f132`
  as rollback. The installed wrapper is byte-exact with source and exposes the
  new commands.
- 2026-08-06: two live split attempts explicitly skipped the gated internal
  0.145 update. The running official App changed `.codex-global-state.json`
  during each frozen-input window; backups `20260806T043745Z...` and
  `20260806T044114Z...` rolled back exactly, and read-only status remains
  synchronized `openai-official` with the official App/app-server chain intact.
  INC-017 is `BLOCKED_AWAITING_HUMAN` at `SPLIT-DEPLOY-APP-RESTART`: quit the
  App, run one exact background split switch, reopen it, and verify. No further
  retry, App restart, internal update, Git, release, archive, or cleanup is
  authorized yet.
- 2026-08-10: the user confirmed the concise interface. Public wrapper RED 1
  failed with `Unknown command: split`; RED 2 proved an unnormalized
  `--keep-version` leaked into the Python parser; RED 3 proved the fixed preset
  could be retargeted with `--app-profile`. The wrapper-only GREEN maps
  `split` to the existing `internal --app-profile official` workflow, rejects
  preset overrides, preserves normal self-update plus internal update checks,
  and makes `split --keep-version` suppress only those two update paths while
  retaining Plugin repair, verify, Doctor, status, and result handling.
  Focused wrapper coverage passed 10/10 in 27.496 seconds, the full profile
  suite passed 219/219 in 316.252 seconds, and adjacent update/release coverage
  passed 4/4 in 9.317 seconds. Bash/Python static checks, strict OpenSpec 20/20,
  and `git diff --check` passed.
- 2026-08-10: Generated Artifact Contract
  `SPLIT-SHORTCUT-20260810T120157+0800` retained the final package at
  `/private/tmp/codex-switch-shortcut-20260810T120157+0800`. The 22-path
  package has payload `b88326ff...f49d`, archive SHA-256
  `c38a619f...e174b`, and byte-exact wrapper, README, SKILL, and complete
  profile-test source. Final release-counterpart Plugin Eval is 58/100 under
  INC-012. Supported immutable installation promoted `b88326ff...f49d` and
  retained prior `e6caa91b...e4983` as rollback; no installer staging remains.
  Installed help exposes `split` and `--keep-version`, Doctor passes, and an
  isolated packaged preview passes. Read-only status and process attestation
  prove CLI/App remain `openai-official`, ChatGPT remains pid 2375, and its
  official app-server remains pid 2920. Internal verification remains
  intentionally unhealthy before activation with active-selection mismatches
  and `parity.receipt.missing`. No App stop/restart, live split, internal
  update, parity repair, Git, release, archive, migration, or cleanup ran.

- 2026-08-10: Live split acceptance invalidated `INTERNAL-CLI-ONLY-001` source
  completion. `/Users/cY/.codex-switch/bin/codex --version` deterministically
  failed with `Internal runtime generation invalid: CLI backend exceeds the
  size limit`, while direct `/Users/cY/.local/bin/codex --version` returned
  `codex-cli 0.145.0`; manifest SHA-256 matched the 276,128,448-byte backend.
  Root cause is the shared 16 MiB text-artifact reader in managed generation
  validation plus final verifier routing to the raw backend. The user approved
  the combined repair with conditional final App guidance. OpenSpec now has
  25 tasks with 20 complete and strict validation passes. No install, update,
  split retry, App stop/restart, dependency, Git, release, archive, migration,
  cleanup, or destructive effect ran during reopen.
- 2026-08-10: `INTERNAL-CLI-ONLY-001` live-acceptance repair completed 25/25.
  Public RED/GREEN closed the 16 MiB executable rejection, raw-backend final
  smoke, unconditional `preserve` restart guidance, prepared managed-shim
  omission, and upstream Python buffering. Final source discover passed
  997/997 in 788.299 seconds; package-local focus passed 9/9. Python/Bash/JSON,
  strict OpenSpec 22/22, workflow `ok=true`, source/package identity,
  release-counterpart Plugin Eval 58/100 under INC-012, diff integrity, and
  independent Standards/Spec rereview pass. Read-only live
  `/Users/cY/.codex-switch/bin/codex --version` returns `codex-cli 0.145.0` in
  0.28 seconds. No install, internal update, split retry, App stop/restart,
  cache refresh, dependency/migration, Git, release, archive, cleanup,
  provider, credential, or destructive effect ran.
- 2026-08-10: `SPLIT-BOOTSTRAP-001` completed task 12 at 4/4. Public REDs
  reproduced stale installed-version authority as `unsafe_cache`, collapsed a
  safe source drift into the same code, observed no progress before the target
  materializer, and proved a catalog-source root symlink could reach native add.
  GREEN separates target version from exact source manifest/tree identity,
  reports `source_mismatch`, rejects unsafe roots before backend effects,
  re-attests the independent target after add, retains old cache/config
  evidence, and flushes both source-attestation and counted materialization
  progress. Final source proof is shared 81/81, Runtime Binding 90/90, and
  Profile 226/226; static Python/Bash/JSON, strict OpenSpec 22/22, workflow
  `ok=true`, package identity, Plugin Eval, and diff integrity pass.
- 2026-08-10: Generated Artifact Contract
  `SPLIT-BOOTSTRAP-20260810T213729+0800` was sealed while its exact root was
  absent and now retains the task-owned release counterpart at
  `/private/tmp/codex-switch-bootstrap-repair-20260810T213729+0800`. The
  22-path, 71-file package has payload SHA-256 `809aeda5...2583`, archive
  SHA-256 `3734296c...961e9`, and manifest SHA-256 `c8f3b731...37c0`; the five
  repaired source/test/doc paths are byte-exact and packaged materialization
  tests pass 23/23. Plugin Eval is the existing 58/100 under INC-012. Terminal
  disposition remains `RETAIN`; no wildcard, recursive deletion, installation,
  promotion, cache refresh, release, or cleanup is authorized.
- 2026-08-11: `SPLIT-BACKEND-MANAGED-001` opened under confirmed task-13
  authority. Read-only live evidence proved the internal catalog valid but
  source/target versions intentionally divergent (`browser@openai-bundled`
  source `26.803.61601`, installed target `26.721.41059`). The catalog parser
  drops installed provenance and the backend-managed classifier incorrectly
  requires official and internal manifests/trees to match, producing the false
  `unverified_catalog` pre-add block. OpenSpec now requires independent source
  and target axes, mandatory reconcile for a pending generation, one fresh
  batch catalog, unique installed target proof, revision-key compatibility,
  and precise `unverified_target`. Task 13.1 is next; no production source or
  live Plugin/App/split/install/internal-update effect occurred during planning.
- 2026-08-11: Task 13.3 completed the source/package gate. The raw catalog
  model now preserves source and installed target axes; every pending
  backend-managed selector reconciles through the target backend and receives
  one fresh batch-catalog target proof. Four independent-review boundary REDs
  closed source-path fallback, source identity, dangling-link, and catalog
  spawn classifications. Fresh source results are shared 93/93, Runtime 90/90,
  Profile 226/226, and Update/Release 140/140; package shared is 93/93. Static,
  strict OpenSpec 22/22, workflow, identity, diff, and both rereviews pass.
  Generated Artifact Contract `SPLIT-BACKEND-MANAGED-20260811T113820+0800`
  retains payload `23477b06...428f1`; Plugin Eval is 54/100 under INC-012.
- 2026-08-11: The one authorized managed-shim functional command ran exactly
  once while ChatGPT stayed open. `codex plugin list --json` exited zero after
  visible source-attestation and 18-Plugin materialization. Readback reports
  generation 1, 18/18 current target receipts, CLI-ready true, no pending state,
  and zero findings. ChatGPT PID 68428, official app-server PID 68766, active
  record, shim, LaunchAgent, and official bundle remained unchanged. Internal
  config/cache changed only through the authorized shared/backend path.
- 2026-08-11: That acceptance also proved seven upgraded old version
  directories absent after native `plugin add`; raw help exposes no retention
  option. INC-025 is `BLOCKED_AWAITING_HUMAN` because this contradicts the
  active retention/no-deletion contract. No retry, restoration, copy, cleanup,
  split, install, App action, binary update, migration, Git, release, or archive
  followed. Task 13.4 remains unchecked pending one cache-lifecycle decision.
- 2026-08-11: The user selected native Plugin cache lifecycle. OpenSpec, README,
  SKILL, and characterization tests now make backend retention/replacement/
  removal explicit while preserving the prohibition on direct codex-switch
  cache copy/link/delete/GC. INC-025 is reconciled and task 13 is complete at
  4/4 without another live command. Final source results are shared 94/94,
  Runtime 90/90, Profile 226/226, and serial Update/Release 140/140; strict
  OpenSpec 22/22, static, workflow, diff, and local spec/standards review pass.
  Retained Generated Artifact Contract
  `SPLIT-NATIVE-CACHE-LIFECYCLE-20260811T123157+0800` has payload
  `3f2852e6...48ab`, source/package identity, package shared 94/94, and Plugin
  Eval 54/100 under INC-012. No install, split retry, App action, internal
  update, direct cache mutation, cleanup, migration, Git, release, or archive
  followed. Historical live-deployment tasks 10.3-10.4 remain separate.
- 2026-08-11: `SPLIT-CONFIG-IDEMPOTENCE-001` completed task 14 at 3/3. RED
  proved consecutive last-runtime renders added one blank line. GREEN removes
  only blank lines immediately preceding generated managed annotations and
  preserves unrelated user spacing. Config tests pass 31/31, focused profile
  tests 4/4, and the complete profile suite 226/226; AST, strict OpenSpec
  22/22, workflow, and diff checks pass. A read-only old/new render comparison
  preserves TOML semantics while reducing the maximum blank run from 245 to 1.
  No live config write, switch, install, App, Plugin/cache, dependency,
  migration, credential, Git, release, archive, or cleanup effect occurred.
- 2026-08-11: `RELEASE-STARTER-RECOVERY-001` completed task 15 at 3/3. RED
  reproduced the exact `install.sh` same-name conflict while the uploaded view
  was empty. GREEN inventories paginated asset ID/name/state/size, deletes only
  an exact canonical zero-byte `starter` after tag validation, reads back
  before upload, and retains checksum proof without `--clobber`. Focused tests
  pass 7/7, complete update/release 148/148, and complete profile 226/226.
  Python AST 2/2, Bash syntax 5/5, active strict OpenSpec, all strict OpenSpec
  22/22, and DevFlow 0.4.1 validation (`ok=true`, zero issues) pass. The single
  existing Project-Directed Implementation Readiness guidance warning remains
  covered by INC-018; final `git diff --check` also passes. No live GitHub
  Release mutation, workflow rerun, DevFlow migration, dependency, credential,
  Git, archive, cleanup, or destructive effect occurred.
- 2026-08-11: Auto Release run `31500533015` failed at job
  `93809040291` step 11 after commit `85dc960` reached `origin/main`. Task 15
  reopened at 15.4-15.6. The first RED raised `GitHub release v1.0.1 is
  missing after starter recovery`; the review RED then proved two captured
  starters could reuse stale `run.sh` ID after deleting `install.sh` removed
  the Release. GREEN now reads back after every exact deletion, stops using old
  IDs on disappearance, revalidates the tag, creates and immediately reads
  back one empty draft, then retains canonical upload, publish, and checksum
  proof. Failed creation, missing/non-draft/non-empty readback, changed starter
  records, and tag movement fail before later mutation. Focused tests pass
  19/19, complete Python 3.12 Update/Release passes 154/154 in 319.357s, and
  complete Profile passes 226/226 in 348.999s. A parallel exploratory system
  Python 3.9 run reported one uncaptured package subprocess failure; it is
  non-qualifying, uses the established unsupported runtime, and the same test
  passes in the fresh Python 3.12 suite. No second commit, push, workflow
  rerun, live Release mutation, DevFlow migration, dependency, archive,
  cleanup, historical task 10.3/10.4 effect, or destructive action occurred.
- 2026-08-12: the user authorized the second repair commit/push and
  `v0.1.14` Auto Release reconciliation. The pre-push workflow inspection then
  proved the actual action is `reconcile_then_prepare`: `VERSION` and the
  latest tag remain `0.1.14`, while release-relevant paths since that tag make
  `prepare_required=true` and `next_tag=v0.1.15`. The workflow would therefore
  repair `v0.1.14`, create a release commit, atomically update `main` and tag
  `v0.1.15`, then publish its assets. INC-026 is
  `BLOCKED_AWAITING_HUMAN`; no commit, push, workflow run, tag, or Release
  mutation followed.
- 2026-08-12: after the user authorized the additional `v0.1.15` target,
  commit `2c90db7` reached `origin/main` and triggered Auto Release run
  `31558709842`, job `93996366843`. Historical-source verification completed
  successfully in 8m25s, and reconciliation packaging plus deterministic asset
  validation also passed. Step 11, `Reconcile existing release assets`, then
  failed in 3s with exit code 2; every `v0.1.15` preparation/publication step
  was skipped. Fresh readback keeps `origin/main` at `2c90db7`, tag `v0.1.14`
  at `19a2433`, and reports no `v0.1.15` tag. GitHub REST now returns 404 for
  release-by-tag `v0.1.14`, the release list begins at `v0.1.13`, and all three
  required `v0.1.14` asset URLs return 404. Anonymous log download returns
  `403 Must have admin rights to Repository`, so no verbatim recreation stderr
  is claimed. The code path and terminal state make immediate draft recreation
  inside the post-delete consistency window the leading diagnosis. INC-027 is
  `BLOCKED_AWAITING_HUMAN`; no rerun, manual Release edit, extra push,
  migration, cleanup, or implementation followed.
- 2026-08-12: `OFFICIAL-AUTHORITATIVE-SHARED-READINESS-001` completed OpenSpec
  task 17 at 6/6. Official App Plugin/Skill state is now the only source;
  repairable internal-only, disjoint, overlapping, delete-versus-modify,
  legacy-pending, and secret-bearing target drift converges only into the
  internal home before functional backend `execve`. Unsafe states emit stable
  cause messages and exact preview/apply/Doctor remediation without a prompt.
  Status, Doctor, verify, and `sync-shared` share one read-only value-free
  report. Final source suites pass 1056/1056; strict OpenSpec is 22/22, static
  and diff checks pass, and both independent review axes close all findings.
  Retained package
  `/private/tmp/codex-switch-official-authority-final.p0TWtw` has 72 files,
  payload `3f8fe936...56e8`, archive `402a6b34...6a67`, 12 byte-exact task paths,
  and package-local shared/verify tests 144/144. Plugin Eval remains the known
  54/100 INC-012 debt. No install, live config/cache/backend/App effect,
  migration, dependency, Git/release, archive, cleanup, credential, or
  destructive action occurred; unrelated task-16 WIP was preserved.
- 2026-08-13: task 16.3 completed on the final source bytes. Independent
  Spec/Standards review found and closed terminal-4xx precedence, equals-form
  `--app-profile=official` routing, forced-close/deadline transport
  classification, and diagnostic/preflight control-character injection.
  Fresh Update/Release passes 165/165, Profile/Wrapper 227/227, shared
  configuration/materialization/lifecycle/verify 149/149, Python/Bash/JSON,
  strict OpenSpec 22/22, DevFlow `ok=true`, and diff checks pass. The sealed
  final package passes 30/30 focused tests with 71 files, payload
  `b9d5148c...1770`, archive `1309e706...3e1`, and manifest
  `f73c7c2c...2cd`; release-counterpart Plugin Eval remains the known 54/F
  INC-012 debt. No Git, Release, migration, install, cleanup, archive,
  dependency, credential, or runtime effect occurred. Tasks 16.4-16.5 are next.
- 2026-08-13: tasks 16.6-16.8 completed the draft-discovery source follow-up.
  Auto Release run `31666160863` proved that a just-created draft can remain
  absent from the tag endpoint across all five readbacks. The adapter now uses
  that endpoint as the fast path and, on explicit 404 only, scans at most 1000
  authenticated Releases pages for one exact `tag_name`; no match remains
  missing, while duplicate, malformed, invalid-JSON, unbounded, and non-404
  states fail closed. Focused coverage passes 6/6 in 0.638 seconds, complete
  Update/Release 171/171 in 316.787 seconds, and Profile/Wrapper 227/227 in
  296.864 seconds. One exploratory Profile invocation with a PTY entered the
  expected interactive confirmation and was interrupted; the qualifying
  no-TTY rerun is the recorded result. Python AST 61/61, Bash 5/5, repository
  JSON 29/29, active strict OpenSpec, all strict OpenSpec 22/22, DevFlow 0.4.1
  validation (`ok=true`, zero issues, existing INC-018 warning only), and
  `git diff --check` pass. Read-only remote proof keeps `main=6a5fa85`,
  `v0.1.14=19a2433`, `v0.1.15` absent, public latest `v0.1.13`, and all three
  `v0.1.14` assets at 404. The user's `授权` decision resolves Human Gate
  `a40cea2a...` through the checked-in authority grant and promotion proof.
  Fresh pre-submit no-TTY reruns pass Update/Release 171/171 in 313.754 seconds
  and Profile/Wrapper 227/227 in 284.600 seconds. Final quick gates pass Python
  AST 61/61, Bash 5/5, all repository JSON 82/82, active/all strict OpenSpec
  22/22, DevFlow `ok=true` with only the existing INC-018 guidance warning, and
  `git diff --check`. No commit, push, workflow rerun, Release mutation,
  migration, install, cleanup, archive, dependency, credential, or unrelated
  runtime effect has occurred yet.
- 2026-08-13: tasks 16.9-16.11 replaced historical reconciliation with an
  explicit `v0.1.14` abandonment policy. Commit `5da41a8` reached `origin/main`,
  but Auto Release run `31681550199` failed during planning on duplicate
  `v0.1.14` Release records; no reconciliation or `v0.1.15` effect followed.
  RED covered latest-tag abandonment, required replacement, older-entry
  non-interference, no GitHub inspection, output, and workflow wiring. GREEN
  adds `--abandon-tag v0.1.14`: the real plan is `prepare`, reconciliation is
  false, and `next_tag=v0.1.15`; `v0.1.14` tag/Release mutation remains
  excluded. Focused coverage passes 4/4, complete Update/Release 175/175,
  Profile/Wrapper 227/227, Python 61/61, Bash 5/5, JSON 86/86, strict OpenSpec
  22/22, DevFlow `ok=true`, package preview, and diff checks. The user's
  explicit publication decision resolves gate `d9a08a71...` for one verified
  commit/push and `v0.1.15` publication only. Remote prestate remains
  `main=5da41a8`, `v0.1.14=19a2433`, and `v0.1.15` absent.
- 2026-08-13: task 16.12 consumed gate `d9a08a71...` through commit `7bc2bdf`,
  its fast-forward push, and Auto Release run `31686051375`. The workflow
  correctly skipped all `v0.1.14` reconciliation and bumped the candidate to
  `v0.1.15`, then failed in `Verify release source`; no release commit, tag,
  Release, or asset effect followed. Tasks 16.13-16.14 add a RED/GREEN contract
  that both Release workflows select Python 3.12 with
  `actions/setup-python@v7` before any Python command. Release workflow tests
  pass 9/9, complete Update/Release passes 176/176, and a clean bumped
  `v0.1.15` Profile/Wrapper candidate passes 227/227. Remote proof remains
  `main=7bc2bdf`, `v0.1.14=19a2433`, and `v0.1.15` absent with all three asset
  URLs at 404. A fresh Human Gate is required before task 16.15 commit, push,
  workflow, tag, or Release effects; `v0.1.14` mutation remains excluded.
- 2026-08-13: the user's fresh
  `授权跳过 v0.1.14，修改并推送，发布 v0.1.15` decision resolves gate
  `ff784b1f...` for task 16.15. The exact grant covers one verified
  Python-runtime repair commit, one fast-forward push to `origin/main`, the
  push-triggered Auto Release run, atomic `v0.1.15` ref creation, and
  `v0.1.15` Release publication. It continues to exclude all `v0.1.14`
  tag/Release mutation, force push, migration, dependency/credential change,
  archive, cleanup, install, and unrelated runtime effects.
- 2026-08-13: commit `700aa57` and Auto Release run `31691783338` consumed
  gate `ff784b1f...`. Python 3.12 setup, planning, abandonment, source restore,
  and the `v0.1.15` bump passed, but `Verify release source` exited 1 after
  6m53s before any commit, tag, Release, or asset effect. The floating-runtime
  hypothesis is therefore disproved; the exact remote test remains unknown
  because current unauthenticated log retrieval is unavailable.
- 2026-08-13: task 16.16-16.17 adds one complete, verbose, fail-closed
  Profile/Wrapper retry to automatic preparation, historical reconciliation,
  and tag-triggered release validation. Fresh Python 3.12 Update/Release passes
  177/177 in 310.532s, and a clean `VERSION=0.1.15` candidate passes
  Profile/Wrapper 227/227 in 345.516s. Python/Bash/39 JSON, strict OpenSpec
  22/22, DevFlow `ok=true`, deterministic package/assets, remote ref, and diff
  checks pass. The user's current publication instruction resolves gate
  `3fe75b3f...` for task 16.18 only; every `v0.1.14` mutation remains excluded.
- 2026-08-13: commit `7b797fe` and Auto Release run `31695733067`, job
  `94432969961`, consumed gate `3fe75b3f...`. `Verify release source` ran both
  complete Profile/Wrapper attempts and failed after 13m03s. Every release
  commit, ref, Release, and asset step was skipped; remote state remained
  `main=7b797fe`, `v0.1.14=19a2433`, and `v0.1.15` absent. Public Check Run
  annotations expose only the retry warning and generic exit code.
- 2026-08-14: task 16.19 adds a second-failure public annotation without making
  validation fail-open. RED failed for all three Release validation paths.
  GREEN captures and replays the complete verbose retry log, percent-encodes
  workflow-command control characters in its final 120 lines, emits one
  `::error` annotation, and exits the original status. Focused coverage passes
  2/2, the full Release workflow class passes 11/11, Bash 3.2 preserves a
  synthetic exit 37 and emits `line 1%25%0D%0Aline 2`, both YAML files parse,
  and complete Python 3.12 Update/Release passes 178/178 in 335.604s.
- 2026-08-14: task 16.20 passes a correct asserted `VERSION=0.1.15`
  Profile/Wrapper candidate 227/227 in 402.264s, Python/Bash/YAML static gates,
  strict OpenSpec 22/22, DevFlow `ok=true`, JSON, remote, diff, and
  deterministic three-asset validation. The user's current publication
  instruction is promoted through
  `release-public-profile-error-v0.1.15-submit-authority-grant.json` and resolves
  gate `5cc1e103...` for task 16.21 only. It authorizes one verified
  public-annotation commit/push and the exact `v0.1.15` Auto Release chain,
  while every `v0.1.14` mutation remains excluded.

## Validation Commands

```bash
VALIDATION_ROOT="$(mktemp -d /private/tmp/codex-switch-validation.XXXXXX)"
export CODEX_SWITCH_SHELL_PROFILE="$VALIDATION_ROOT/.zshrc"

PYTHONDONTWRITEBYTECODE=1 python3.12 scripts/test_codex_transaction.py -v
PYTHONDONTWRITEBYTECODE=1 python3.12 scripts/test_codex_runtime_binding.py -v
PYTHONDONTWRITEBYTECODE=1 python3.12 scripts/test_codex_protocol_config.py -v
PYTHONDONTWRITEBYTECODE=1 python3.12 scripts/test_codex_update_release.py -v
PYTHONDONTWRITEBYTECODE=1 python3.12 scripts/test_codex_profile_switch.py
openspec validate --all --strict --no-interactive
bash -n scripts/codex-switch install.sh run.sh scripts/package-release.sh
git diff --check
```

## Human Gates

- The scoped internal rebind and rollout gates were consumed. FSR-002 required
  no additional ChatGPT restart; the existing internal Desktop chain remained
  stable through the final install and status check.
- The user explicitly authorized `ROLLOUT-001` after source implementation,
  integrated review, and release verification pass: install only the verified
  current source through the supported path, run live `official` acceptance,
  and restore/attest `internal`. This approval does not authorize any earlier
  workstation mutation.
- Task 13 authorizes only the single bounded managed-shim acceptance and its
  required internal backend Plugin/config/cache/shared-generation effects.
  Separate approval is still required before any other Plugin mutation,
  commit/push/tag/release, destructive cleanup, provider/root-state migration,
  legacy skill cleanup, dependency addition, or public compatibility expansion.
- `SPLIT-BACKEND-MANAGED-CACHE-RETENTION-DECISION` is the current gate. The
  recommended answer assigns installed-version lifecycle to native add while
  preserving codex-switch's no-direct-cache-mutation rule. Enforcing retention
  instead requires a newly approved crash-safe cache-preservation design,
  cache-mutation authority, and a separate live retry. Neither answer is
  inferred from the successful CLI startup.
- `PARITY-8.3-IMPLEMENT` is required before changing the exact two production,
  two test, one retained fixture, and two operator-contract paths named by task
  8.3. It authorizes no live or external effect.
- `PARITY-8.3-LIVE-RETRY` is separately required after reviewed GREEN/package
  evidence before an exact-source install, one same-backend rebind, or its
  bounded provider-backed typed-v2 probe. It does not authorize ChatGPT
  restart, the Desktop acceptance task, Git, release, archive, or cleanup.
- On 2026-07-28 the user consumed a narrower Scheme A live-repair gate for the
  exact-source install, one internal update/rebind/switch, precise recoverable
  `.zshrc`/candidate repair, and one bounded ChatGPT restart. It does not
  authorize the provider-backed Desktop task, plugin refresh, proxy/global
  state changes, credential/identity migration, destructive deletion, Git,
  release, archive, or legacy cleanup.
- Not required for isolated temporary-directory/process tests, generated-schema
  inspection, fake Git/GitHub adapters, or source changes already specified by
  the four approved OpenSpec changes.
- `SPLIT-DEPLOY-APP-RESTART` remains required before live activation. The user
  must completely quit ChatGPT before running the installed shortcut; this
  gate does not authorize an internal binary update, parity repair, Git,
  release, archive, project migration, or retained-artifact cleanup.
