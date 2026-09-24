# Staged Internal Update: Planning and Verification

## Current status

- Change: `openspec/changes/support-staged-internal-update/`.
- Baseline: `956197209b2d`, branch `fix/standalone-installer-runtime`.
- Planning date: 2026-09-23.
- Requester authority: complete generic stage/apply implementation, isolated
  tests and update of the existing PR. No live workstation mutation, release,
  destructive cleanup, dependency addition or workflow expansion.
- Current status: implemented, reviewed, verified and delivered to the existing
  PR. All active tasks are complete; release/archive/live acceptance are excluded.
- Execution source: the new change's `tasks.md`. Completion checkboxes remain
  conservative until the integrated native, recovery and package evidence is complete.
- Previous archived changes, local verification roots and untracked
  `update-reference-inputs-research.md` remain intact.

## Source and capability evidence

The native AGENTS, ENGINEERING_POLICY, workflow state and project-local
feature-intake/change-plan/ai-native-tech-plan/capability-research/OpenSpec
instructions were read. The design contains the full Skill Routing Ledger,
Target State, exact write scope, comparison, completion/Goal contract, dependent
slices, TDD matrix, artifact strategy and authority boundary.

Inspected public wrapper/parser, bindings, parity, first-install, runtime-binding
discovery, transaction and package runtime inventory. Key facts:

- `cmd_set_bin` loads a live internal manifest and prepares from canonical
  profile config; `ConfigInputs` couples that config's source and destination.
- Current managed catalog validation resolves original source from manifest
  metadata and checks a historical healthy receipt.
- Empty bootstrap already has strict no-profile/no-target/backup/pending-state
  guards and no-replace command publication.
- `DesktopRoots` selects canonical actual Desktop. Existing `--app-cli-path`
  is a binding path, not an arbitrary Desktop-root option; a mismatching saved
  official manifest currently yields a warning while actual inventory wins.
- Runtime swaps consume the publication candidate, so retained stage paths need
  a distinct operation-owned publication facade while their complete generation
  remains referenced.
- Package required module inventories and installed imports must include the new
  runtime engine.

The project-local codebase-design copy is absent. Its pinned vendored guidance
was read; the approved fallback is repository rules, current source and native
OpenSpec design evidence. No skill or workflow is installed or changed.

## Planning validation log

| Check | Result | Evidence |
|---|---|---|
| Baseline Git identity/status | observed | HEAD `956197209b2d`; only pre-existing local verification and research were initially untracked |
| Baseline workflow JSON | `ok: false` | archived `respect-custom-model-catalog` was still the current change; no active directory existed |
| Global OpenSpec version | 1.3.1 | native generated skills target 1.7; switched subsequent instructions/validation to retained pinned CLI |
| Pinned OpenSpec version | 1.7.0 | existing retained local npm cache executed directly with node; no cache/global update |
| New change artifact instructions | read | proposal, design, specs and tasks use repo-local spec-driven resolved paths |
| Strict new-change validation, pinned 1.7 | passed, exit 0 | `validate support-staged-internal-update --strict --no-interactive`: change valid |
| Strict all validation, pinned 1.7 | passed, exit 0 | `validate --all --strict --no-interactive`: 28 passed, 0 failed |
| Final native workflow JSON | passed, exit 0 | `ok: true`, `issues: []`; one pre-existing readiness guidance warning retained |
| Pinned apply instructions | ready, exit 0 | `state: ready`; total 22, complete 0, remaining 22; all proposal/spec/design/task dependencies exist |
| Diff whitespace | passed, exit 0 | `git diff --check` |
| Delta/task inventory | observed | 3 capability deltas, 9 requirements, 33 scenarios, 22 unchecked tasks; 0 implementation tasks marked complete |

Baseline workflow warning: AGENTS lacks the `Project-Directed Implementation
Readiness` heading. No external implementation provider is selected, so this
warning is unrelated to the required runtime behavior; preserve it as a
non-blocking finding rather than expanding the workflow scope.

## Required implementation evidence

RED/GREEN and fresh source/native/package verification are pending. Required
coverage includes stage without profile, current reference, complete runtime
digest, explicit private seed, auth/source drift, managed provenance recapture,
custom/default policy, actual Desktop/binding drift, first full publication,
no-Desktop bootstrap, partial-state refusal, every interruption window,
same/different-input replay, concurrent changes, cancel ownership, one-shot
compatibility and installed package completeness. See design's validation map.

Tests must create fresh isolated HOME/store/App fixtures, use Python `-B` and
`PYTHONDONTWRITEBYTECODE=1`, and never affect the live environment. Main registers
new generated output roots before creation. Existing local artifacts are not
owned by this new run and cannot be reclaimed by it.

## Risks and handoff

No unresolved product decision or implementation authority blocker is known.
OpenSpec apply readiness concerns artifact completeness, not runtime success.
Main reviews and assumes final ownership of these planning/control-plane edits,
then executes task 1.1 and continues automatically through the approved target.

## Planning clarification: bootstrap followed by current-runtime adoption

On 2026-09-23 the approved plan was clarified through the native
`openspec-update-change` route. The existing target after successful strict
CLI-only bootstrap is a valid `stage --current` source even when its internal
profile is absent. Once actual Desktop is available, full apply can publish the
first profile/config/auth/binding with fresh parity while preserving that
verified current runtime. This path does not reinstall or rerun empty-target
bootstrap. An unowned/tampered target, partial profile or unresolved residue
remains an error; failed or cancelled adoption preserves the installed CLI.

Changed planning artifacts: design, staged-update spec, first-install spec and
tasks 1.3/3.4/6.2. New task 3.4 requires the complete public-command sequence and
positive/negative rollback assertions. If an earlier stage froze Desktop
absence, obtain a new current stage after Desktop becomes available rather
than silently changing the earlier update ID's reference.

Fresh validation after this clarification:

| Check | Result |
|---|---|
| Pinned OpenSpec 1.7 strict new change | passed, exit 0 |
| Pinned OpenSpec 1.7 strict all | 28 passed, 0 failed, exit 0 |
| Native workflow JSON | `ok: true`, `issues: []`; the same pre-existing AGENTS readiness-heading warning |
| Apply instructions | `state: ready`; total 23, complete 0, remaining 23 |
| Delta inventory | 9 requirements and 37 scenarios across 3 capability specs |
| Diff whitespace | `git diff --check`, exit 0 |

No source/test implementation, live state, dependency, commit or push was changed
by this planning clarification. Main retains final artifact ownership.

## Implementation checkpoint, 2026-09-24

Native implementation authority/readiness check: allowed=true, not applicable.
Bounded worker contracts validated before delegation; main owns integration and
control-plane changes. Public update record states now match the actual two-call
contract; detailed transaction phases remain internal.

Main observed RED for stage (unknown subcommand), then GREEN after implementing
private durable candidates. Current public stage/negative/replay integration suite
has passed 8 tests; subsequent first-publication and process-death cases are in
progress. Fresh staged plus first-install suites passed 29 tests. No complete
source/native/package acceptance or delivery is claimed at this checkpoint.

Worker preparation evidence (awaits final main integrated rerun): 14 preparation,
26 catalog routing, 107 parity, 258 transaction (one skip), 90 runtime-binding,
41 protocol-config, 2 protocol-adapter tests pass. Real native staged integration
is running in an opt-in isolated fixture. Historical package tests exposed a new
required-module fixture omission; it is being repaired rather than bypassed.

Next: finish stage/current first publication, terminal interruption/cancel and
scope regressions; complete real native integration; run full source/profile and
exact package validation; complete independent review; update the existing PR.
No live installation, credentials, provider traffic, release or merge occurred.

## Final integration checkpoint, 2026-09-24

Native Spec and Standards review found and repaired cancellation ownership,
probe process lifetime, bootstrap receipt publication and managed-overlay replay
boundaries. RED cases preceded repairs. Public stage and one-shot cancellation,
including both child-start signal windows, pass. A source-origin fingerprint
now binds the original catalog as well as the overlay before commit; confirmed
replay compares caller inputs without treating its own overlay output as drift.

Fresh isolated evidence: 31 bootstrap tests, 90 runtime-binding tests, 17 public
staged engine cases including the startup-window case, 11 real probe cancellation cases,
and 9 opt-in native stage/apply lifecycle cases. Native tests execute actual
schema/features/bundled exports/core/typed-v2 collectors against a loopback
provider, retain source runtime identities, and never run caller hooks. The
new active-model/managed-overlay replay case failed first, then passed with zero
subprocesses/provider requests on replay and original-source drift rejection.

Complete source regression is in progress. It exposed stale Profile/Wrapper
fixtures that still use the previous promotion dispatch and incomplete module
inventories; those fixtures are being adapted to the real shared engine. Do not
count this initial broad run as passed. Canonical and legacy prior-package
upgrades with the new module absent both pass after historical inventory handling
was confined to existing releases; incoming candidates remain strict.

Pinned OpenSpec 1.7 strict all: 28 passed, zero failed. Native workflow JSON:
ok=true, issues=[]. Source and candidate-package scans found no Python bytecode.
Plugin evaluation of the initial candidate: 63/100 versus prior delivered
package 68/100. Both report excessive deferred tokens because the skill package
contains the complete runtime and regression source; restructuring that existing
release layout is outside this update. New staged guidance was shortened to
reference the detailed command document. Re-evaluate the exact final package,
record residual structural/complexity findings, and retain a follow-up in the
incidental register rather than claiming a passing budget score.

No external push, release, merge, live configuration or Desktop acceptance has
been performed for this implementation yet. Next: finish full source/profile
regression, exact-commit package/installed checks, refresh public PR and retain
verified evidence. The diagnostic package is retained; its first generated-
artifact observation lacked the canonical contract seal and is not cleanup
authority. Final package registration will be sealed at its canonical path
before the bound command.

## Frozen regression reconciliation

The first broad discovery exercised 1,242 tests with 20 failures, 26 errors and
11 skips; it is not passing evidence. All non-Profile failures were the nine
signal tests: the parent imported the old harness before the file changed,
then child processes loaded its new result field. Actual process groups were
gone, leaders reaped and handlers restored. The final 11-case suite passed
both alone and after importing every test module. No production assertion or
timeout was relaxed to hide these failures.

The remaining failures/errors came from old Profile/Wrapper fixtures that
intercepted the removed promotion command and faked success markers. Fixtures
now invoke the real shared engine/transaction with only isolated Desktop
discovery and local transport/runtime adapters. Assertions inspect persistent
update scope, real probe requests and unchanged prior artifacts on failure.
Main reviewed the complete fixture diff. Files are frozen for final regression.

To cover every discovered module without reusing the invalid concurrent run,
the final regression executes all Profile cases, all Update/Release cases, all
Transaction cases, and discovery of every remaining `test_codex_*.py` module.
Opt-in real native staged coverage is also run separately with actual copied
runtime assets and a loopback-only provider. Final counts follow at delivery.

Final frozen Profile entrypoint: `PYTHONDONTWRITEBYTECODE=1 python3 -B
scripts/test_codex_profile_switch.py` passed all 227 tests in 435.674 seconds.
The full Transaction entrypoint passed 258 tests in 25.452 seconds with one
existing unavailable-interpreter skip. No failures or errors remain in either
suite. Python 3.9 grammar checks pass for all 74 runtime/test Python files;
runtime commands retain the existing Python 3.11+ requirement. Shell syntax
and `git diff --check` pass.

## Source verification complete

All 1,246 discovered cases are accounted for by fresh frozen runs:

| Command/selection, with `PYTHONDONTWRITEBYTECODE=1 python3 -B` | Result |
|---|---|
| `scripts/test_codex_profile_switch.py` | 227 passed, 435.674s |
| `-m unittest discover -s scripts -p test_codex_update_release.py` | 200 passed, 537.199s |
| `-m unittest discover -s scripts -p test_codex_transaction.py` | 257 passed, one interpreter-selection skip, 25.452s |
| Discover `test_codex_*.py`, excluding only the three modules above | 550 passed, 11 explicit-native-backend skips, 303.280s |
| `scripts/test_codex_native_staged_update.py`, actual candidate 0.155.0 and Desktop 0.155.0-alpha.9.2 | All 9 native cases passed, 122.613s |
| `-m unittest test_codex_native_parity test_codex_parity_lifecycle`, explicit same native backend | All 5 passed, 50.478s, including both remaining skipped native cases |
| `test_transaction_apply_executes_under_python39`, test runner unchanged and subprocess PATH selecting system Python 3.9.6 | Passed with no skip |

The partitioned discovery recursively flattens the standard unittest suite and
excludes exactly `test_codex_profile_switch`, `test_codex_update_release` and
`test_codex_transaction`, each run completely above. No test is dropped from
coverage; every opt-in/interpreter skip is exercised explicitly. Native model
traffic is restricted to the fixture loopback provider. No live configuration,
profile, installed runtime or App state was changed.

The final narrow Standards re-review closed the child-start signal finding with
no new blocking issue. Pinned OpenSpec 1.7 strict-all validation passed all 28
items; workflow JSON reports `ok: true`, `issues: []`. Remaining work is exact-
commit package/import/installed verification, release-target skill evaluation
and the authorized existing-PR delivery. Current source verification does not
claim those later effects are complete.

## Exact package and delivery proof

Implementation commit: `ef69b01dd21e64d806e35268c4ec48e9f5de0cfc`.
Previous PR commit: `956197209b2dfa86ba0a8a948a7e571c4f3ab9ef`.
Both source trees were extracted from exact `git archive` commits. Each used
its own `scripts/package-release.sh`; the previous installer ran first, then
the candidate installer upgraded the same newly created private HOME/lib/bin.
The actual workstation installation was not an install target.

The candidate's public version/stage/apply entrypoints and import closure pass.
Installed suites: staged 17, private preparation 14, catalog routing 26, strict
bootstrap 31 and signal cleanup 11. Installed real native staged/parity/lifecycle
modules pass all 14 cases in 174.596 seconds. Total: 113 installed tests passed,
zero failures, errors or skips. These tests use the installed modules, private
homes and loopback model responses; they do not claim live Desktop UI acceptance.

Distribution, archive and installed manifests validate before and after tests:

- Payload SHA-256: `4011b71ff6f59d6dc5a947c3bd57546291fd74a622a788e5fbe18ac811e102d5`.
- Archive SHA-256: `9f4e37e334b0617c847ff2abe412bd601c665324a979a0187b614e046f1d12ec`.
- Source, distribution and installed Python bytecode files: zero.

Release-target `plugin-eval analyze` reports 63/100, grade D; the exact previous
package reports 68/100, grade D. Both flag excessive deferred input because the
existing skill package includes all runtime/tests. Structural and complexity
warnings are retained in INC-029. The new main skill guidance was shortened;
a broader packaging/function refactor is not part of this completed update.

Generated artifacts were registered and canonically sealed before creation
with `retention=retain`. The diagnostic cleanup planner returns HUMAN_GATE
for archive-preserved macOS birth times and the installed command/current/
rollback symlinks. This is not a cleanable receipt and is not treated as one.
All output, sealed contracts, observations and diagnostic plans are preserved.
An initial observation/plan directory layout was corrected without discarding
its history. Workflow JSON is `ok: true`, `issues: []`, with visible cleanup and
pre-existing readiness-heading warnings. No missing implementation authority
or material cleanup decision is being hidden; no cleanup is attempted.

Native Git preflight confirms the expected previous remote commit and safe
fast-forward to the exact implementation. The existing fork branch was pushed,
and GitHub readback confirms PR #1 remains open with head `ef69b01`. Its title
and description reflect the complete implementation and current evidence.
The final control-plane-only checkpoint does not alter packaged inputs.
