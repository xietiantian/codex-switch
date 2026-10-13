---
workflow_version: 0.4.0
project_mode: brownfield
current_stage: verification

current_change:
  id: filter-app-server-process-diagnostics
  status: verifying

standing_milestone:
  status: inactive
  contract_path: none
  contract_sha256: none
  goal_id: none
  change_id: none
  candidate_digest: none
  validation_digest: none
  review_digest: none

authority_gate:
  key: none
  status: resolved
  resolution_digest: none
  evidence_digest: none
  next_question: none
  missing_authority: []

gates:
  workflow_initialized: true
  spec_approved: true
  plan_written: true
  tests_baseline_known: true
  implementation_done: true
  verification_passed: false
  state_updated: true
  archive_allowed: false
  release_allowed: false

implementation_readiness:
  required: false

context_management:
  compact_policy: checkpoint_boundary
  last_checkpoint_id: 2026-10-13-independent-cli-processes
  last_checkpoint_file: .planning/verification/independent-cli-processes-20261013.md
  compact_recommended: false
  compact_status: not_needed
  last_compact_result_file: none
  compact_source: openspec
  compact_updated_at: 2026-10-13
  compact_skip_reason: bounded_change_context_is_healthy
  compact_error: none
  compact_after:
    - project_setup_completed
    - codebase_mapping_completed
    - design_saved
    - openspec_change_planned
    - verification_passed
    - change_archived
  skip_compact_for:
    - small_task_update
    - typo_fix
    - docs_only_micro_change
  require_before_compact:
    - state_updated
    - durable_context_written
    - next_action_recorded
    - risks_recorded
    - validation_recorded_if_applicable

goal_gate:
  id: none
  required: false
  status: durable_contract
  reason: bounded authorized source repair uses current OpenSpec tasks and evidence; no runtime goal requested
  suggested_goal: none

context_health:
  last_report: .planning/context-health/reports/20260629130742-context-health.json
  last_risk: medium
  last_confidence: medium
  last_decision: approved_independent_cli_process_repair_and_existing_pr_push
  last_goal_status: aligned
  goal_summary: Exclude confirmed independent CLI children while preserving Desktop and ambiguous process diagnostics.
---

# Workflow State

## Current process attribution follow-up

Use existing `filter-app-server-process-diagnostics` tasks 4–7. Diagnosis and scope are approved; main owns implementation, tests, reviews and existing PR delivery. Evidence: `.planning/verification/independent-cli-processes-20261013.md`.

## Current default-difference follow-up

Use the existing allow-optional-setup-parity-differences tasks 9–13. Supported defaults use a generic warning; specialized, explicit-dependency and protocol checks retain precedence. Implementation 6afbfded6575 is verified and pushed; tasks 9–13 are complete. Final discovery, explicit conditional tests, installed package and current-runtime acceptance pass. Evidence: .planning/verification/feature-default-differences-20261010.md. Release/archive remain excluded.

## Current packaged Desktop repair

Implementation a490786e5a9c and both review axes are complete. Exact-package
installation and 39 installed tests pass; source is pushed to the existing PR
fork branch. Fresh discovery, focused and native results are recorded in
`.planning/verification/packaged-desktop-cli-20261010.md`. No required source
work remains. Release and archive are not requested.
Historical exclusions below are superseded only by the current approved task.


## Commit and retest request (2026-10-08)

The requester now authorizes committing the existing repairs and refreshing the
dependent test package from that exact commit. Implementation is committed as
`5a6d9fc3a628`; fresh 279-case complete-source and 19-case installed runtime tests
pass. Current Desktop path discovery and six feature/five protocol differences
remain unresolved and are documented in `.planning/verification/commit-retest-20261008.md`. The
historical no-publication statements below describe the earlier repair scope.
Live workstation changes, new compatibility adaptations and archive remain
outside this task. See the current task at the top of `TASK_LEDGER.md`.

Current approved execution: `revalidate-runtime-config-at-launch/tasks.md`. All six source/isolated-verification tasks are complete. Final Profile/Wrapper 227, Update/Release 200, boundary 10 and installed 26 checks pass; both review axes are closed. Strict OpenSpec and diff checks pass. Evidence: `.planning/verification/runtime-config-launch-20260929.md`. Previous optional repair remains intact. New Desktop packaging and capability adaptation is explicitly excluded; no live readiness, publication or archive is claimed or authorized.

## Current Optional Runtime Parity Repair

Approved policy 7 optional system-proxy retry handling and the two protocol-test startup budgets are implemented. Fresh regression passes 187 cases; repeated timing checks pass 6; exact installed runtime checks pass 17 with valid manifests and no bytecode. Strict OpenSpec and diff checks pass. Spec review found no code blocker; stale documentation is corrected. No caller-specific evidence belongs here. No live installation, Git publication or archive is authorized.

## Previous Staged Update Plan

New change: `support-staged-internal-update`; baseline `956197209b2d`.
Native proposal/design/tasks and three capability delta specs are written.
Implementation and independent Spec/Standards review are complete, including
the section 8 released-binding correction. Discovery runs 1,252 cases with 13
conditional skips, each covered by explicit native/interpreter runs; the profile
entrypoint passes 227 cases. Exact implementation `c15bb74` passes prior-package
upgrade, 119 installed tests and unchanged manifests without bytecode residue,
and is pushed to the existing PR. All active change tasks are complete.
Historical results below do not substitute for current acceptance evidence.

The requester approved the complete generic target, isolated verification and
existing PR commit/push. Execution is auto-until-terminal, using only the new
OpenSpec task list. No live workstation/config/provider/Desktop changes,
release, destructive cleanup or unrelated workflow update is authorized.
Main owns final planning/integration and the runtime Goal, if used; the durable
Goal Contract is in the design. No external implementation provider is selected.

## Previous Staged Update Next Action

No implementation remains for approved section 8. Preserve the verified package
and released-init migration evidence. Saved intent and actual Desktop reference
are independently frozen; changes to either after staging still block apply.
The earlier passing counts omitted released explicit-compatibility init state;
fresh public/native and exact installed migration tests close that gap.

Historical delivery: retain the exact package
and its evidence. The cleanup planner refuses automatic reclamation for
timestamp-preserving archives and installed symlinks; no cleanup is authorized
or performed. Skill package layout/complexity remains a documented follow-up.
Release, archive and live Desktop UI acceptance are excluded from this delivery.
Evidence is `.planning/devflow/verification/support-staged-internal-update.md`.


## Completed Lifecycle Correction

Section 6 is complete. Source and installed native tests cover recapture,
rebind, repeated activation, explicit official homes, rollback and drift rejection.
The 406-case combined regression, 227 profile cases and 90 runtime-binding cases
are verified; detailed fixture/environment rechecks are preserved in evidence.
Both review axes are closed. Implementation fca9287 passed a clean package
upgrade and 40 installed checks with valid manifests and no bytecode residue,
then was pushed to the existing PR. The stable test entrypoint selects that
verified implementation. All 21 original-change tasks and synchronized specs
are re-archived. Historical evidence follows.

## Completed Cache-Free Correction

Section 5 source and native verification are complete: 145 parity/source/current
tests, 227 profile tests, 257 transaction tests plus one existing skip; both
review axes are closed. Real cache-free complete preparation passes both native
probes in isolation. Exact implementation 0e3a011 passed an isolated previous
package upgrade, 34 installed checks and manifest validation without bytecode
residue, then was pushed to PR #1. The stable test entrypoint selects that package.
All 16 original-change tasks are complete; the synchronized original change is
re-archived. Across five repairs: 47 tasks, 13 requirements and 46 scenarios.
Historical delivery follows.

## Previous Original-Change Correction

The original respect-custom-model-catalog change was reopened to restore the
complete catalog-free comparison path. Implementation 9d0c8e2 is pushed to PR #1;
its exact archive passed an isolated previous-package upgrade, 17 installed-module
tests and post-test manifest validation without bytecode residue. Routing and
parity/current suites (128), profile (227) and transaction cases (257 passed,
one existing skip) pass. Independent Spec/Standards reviews have no open findings.
The stable local test entrypoint selects the verified package. No live installation,
provider call or Desktop/config/cache mutation occurred.

All 12 tasks in the original change are complete. Its two requirements and eleven
scenarios match the main spec. Re-archive returns it to the original location;
the other four PR repairs remain archived. Across these repairs there are now
43 completed tasks, 13 requirements and 42 scenarios.

## Previous Archive Record

The five changes introduced by PR #1 are archived under
`openspec/changes/archive/2026-09-21-<change-name>/`:

- support-standalone-installer-runtime
- restore-first-internal-install
- respect-custom-model-catalog
- support-namespaced-feature-keys
- classify-current-runtime-extensions

At the original archive, all 39 tasks were complete and main capability specs
retained all 13 requirements and 37 scenarios. Existing verification and independent review records remain
valid for the unchanged implementation. Archive authorization and fresh
specification validation are recorded in
`.planning/devflow/verification/archive-pr-fixes.md`.

## Historical Status

Tasks 16.18-16.20 are complete. Commit `7b797fe` reached `origin/main`, but
Auto Release run `31695733067` exhausted the bounded Profile/Wrapper retry
before every release effect. The three Release validation paths now publish a
percent-encoded final verbose tail through a fail-closed GitHub error
annotation. Fresh Python 3.12 verification passes Update/Release 178/178 and a
clean `VERSION=0.1.15` Profile/Wrapper candidate 227/227; strict OpenSpec,
DevFlow, static, JSON, package, and asset gates pass. Gate `5cc1e103...` is
resolved for one exact commit/push and the `v0.1.15` Auto Release chain, with
every `v0.1.14` mutation excluded.

## Historical Next Action

No implementation tasks remained for the previous repair. Preserve the verified test entrypoint; live installation and provider-backed Desktop UI acceptance remain requester actions.
