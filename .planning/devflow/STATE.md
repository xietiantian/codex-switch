---
workflow_version: 0.4.0
project_mode: brownfield
current_stage: verification

current_change:
  id: respect-custom-model-catalog
  status: in_progress

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
  last_checkpoint_id: 2026-09-21-parity-lifecycle-correction
  last_checkpoint_file: .planning/devflow/verification/respect-custom-model-catalog.md
  compact_recommended: false
  compact_status: not_needed
  last_compact_result_file: none
  compact_source: openspec
  compact_updated_at: 2026-09-21
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
  id: respect-custom-model-catalog
  required: true
  status: active
  reason: the active repair contract authorizes implementation, isolated verification, and a pull request
  suggested_goal: none

context_health:
  last_report: .planning/context-health/reports/20260629130742-context-health.json
  last_risk: medium
  last_confidence: medium
  last_decision: verify_parity_lifecycle_correction
  last_goal_status: aligned
  goal_summary: Preserve verified parity across final capture, rebind and activation with independent homes.
---

# Workflow State

## Current Lifecycle Correction

Section 6 of the original change is active. Preparation, home binding,
transactional missing-config publication and repeated activation are implemented.
Review-found explicit-home persistence and pre-switch stale-config guards pass
three native isolated lifecycle scenarios, including rollback. Final regression,
package verification, review closure and PR delivery remain in progress.

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

## Next Action

Finish final regression and independent review, verify the exact committed package, update the existing PR and archive the completed original change. Live installation and provider-backed Desktop UI acceptance remain requester actions.
