---
workflow_version: 0.4.0
project_mode: brownfield
current_stage: verification

current_change:
  id: respect-custom-model-catalog
  status: reopened

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
  last_checkpoint_id: 2026-09-21-catalog-routing-correction
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
  status: in_progress
  reason: the active repair contract authorizes implementation, isolated verification, and a pull request
  suggested_goal: none

context_health:
  last_report: .planning/context-health/reports/20260629130742-context-health.json
  last_risk: medium
  last_confidence: medium
  last_decision: verify_reopened_catalog_routing
  last_goal_status: aligned
  goal_summary: Restore catalog-free comparison while preserving custom-source applicability and integrity.
---

# Workflow State

## Reopened Correction

The custom-catalog change has returned to the active changes directory. Its
original evidence remains historical; tasks 4.1 and 4.2 are complete. Routing,
parity/current, profile and transaction regressions pass; both review axes have
no open findings. Exact committed-package verification and delivery remain before
any new completion/archive claim. The four other repairs stay archived.

## Previous Archive Record

The five changes introduced by PR #1 are archived under
`openspec/changes/archive/2026-09-21-<change-name>/`:

- support-standalone-installer-runtime
- restore-first-internal-install
- respect-custom-model-catalog
- support-namespaced-feature-keys
- classify-current-runtime-extensions

All 39 tasks are complete. Main capability specs retain all 13 requirements
and 37 scenarios. Existing verification and independent review records remain
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

Verify the exact implementation package in isolated install/lib roots, then
push the correction to PR #1 and refresh the local test entrypoint. Complete
section 4 and re-archive the same change only after these gates pass. Release publication and workstation installation remain separate
authorization boundaries.
