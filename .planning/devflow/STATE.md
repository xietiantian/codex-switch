---
workflow_version: 0.4.0
project_mode: brownfield
current_stage: implementation

current_change:
  id: respect-custom-model-catalog
  status: implementing

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
  implementation_done: false
  verification_passed: false
  state_updated: true
  archive_allowed: false
  release_allowed: false

implementation_readiness:
  required: false

context_management:
  compact_policy: checkpoint_boundary
  last_checkpoint_id: 2026-09-20-first-internal-install
  last_checkpoint_file: .planning/devflow/verification/restore-first-internal-install.md
  compact_recommended: false
  compact_status: not_needed
  last_compact_result_file: none
  compact_source: openspec
  compact_updated_at: 2026-09-20T17:07:51+08:00
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
  id: restore-first-internal-install
  required: true
  status: satisfied
  reason: the active repair contract authorizes implementation, isolated verification, and a pull request
  suggested_goal: none

context_health:
  last_report: .planning/context-health/reports/20260629130742-context-health.json
  last_risk: medium
  last_confidence: medium
  last_decision: respect_custom_model_catalog
  last_goal_status: aligned
  goal_summary: Respect custom model catalogs while preserving runtime compatibility checks.
---

# Workflow State

## Active Repair Status

Custom-catalog parity correction is approved on the existing PR branch.
The execution source is respect-custom-model-catalog/tasks.md. The absent-cache
repair, focused regressions and independent reviews pass. Broad update/release
and exact package checks are in progress.

First-install restoration is implemented and passes its 22-case isolated
command matrix, package/native checks, regressions, and independent review.
The existing PR is updated with implementation commit e41c134.
The canonical execution source is restore-first-internal-install/tasks.md.

The standalone installer compatibility repair and isolated verification pass. Its
OpenSpec tasks and verification record are authoritative for this branch.
No live installation or release is authorized; isolated tests and a PR are.

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

Implement and verify custom-catalog parity, then update PR #1.
Release publication and live installation remain separate.
