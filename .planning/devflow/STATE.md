---
workflow_version: 0.4.0
project_mode: brownfield
current_stage: complete

current_change:
  id: support-staged-internal-update
  status: complete

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
  verification_passed: true
  state_updated: true
  archive_allowed: false
  release_allowed: false

implementation_readiness:
  required: false

context_management:
  compact_policy: checkpoint_boundary
  last_checkpoint_id: 2026-09-24-staged-internal-update-delivered
  last_checkpoint_file: .planning/devflow/verification/support-staged-internal-update.md
  compact_recommended: false
  compact_status: not_needed
  last_compact_result_file: none
  compact_source: openspec
  compact_updated_at: 2026-09-24
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
  id: support-staged-internal-update
  required: true
  status: complete
  reason: the native Goal Contract is satisfied by complete source and installed proof plus existing PR delivery
  suggested_goal: none

context_health:
  last_report: .planning/context-health/reports/20260629130742-context-health.json
  last_risk: medium
  last_confidence: medium
  last_decision: verified_staged_update_delivered_to_existing_pr
  last_goal_status: aligned
  goal_summary: Deliver durable stage and private transactional apply with source provenance, recovery, isolated proof and existing PR delivery.
---

# Workflow State

## Active Staged Update Plan

New change: `support-staged-internal-update`; baseline `956197209b2d`.
Native proposal/design/tasks and three capability delta specs are written.
Implementation and independent Spec/Standards review are complete. All 1,246
source cases are covered, including explicit reruns of opt-in cases. Exact
implementation `ef69b01` passed prior-package upgrade, 113 installed tests and
unchanged manifests without bytecode residue, and is delivered to the existing
PR. All active change tasks are complete. Historical results below are preserved
and do not substitute for current acceptance evidence.

The requester approved the complete generic target, isolated verification and
existing PR commit/push. Execution is auto-until-terminal, using only the new
OpenSpec task list. No live workstation/config/provider/Desktop changes,
release, destructive cleanup or unrelated workflow update is authorized.
Main owns final planning/integration and the runtime Goal, if used; the durable
Goal Contract is in the design. No external implementation provider is selected.

## Current Next Action

No approved implementation or delivery work remains. Retain the exact package
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
