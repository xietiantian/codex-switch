# PR completed-change archive audit (2026-10-13)

PR: https://github.com/cYz26/codex-switch/pull/1
Branch: `fix/standalone-installer-runtime`
PR base: `04374d37a0764286567a66fa87da5a9849d286e6` (`main`)
Pre-archive head: `0c0ee57f00d693f01ddc6bd427326e2a83136a51`

## Scope and authority

The requester authorizes archiving completed changes belonging to this PR;
existing commit/push authorization continues for the same PR branch. Main owns
spec synchronization, archive moves, tracking, verification and delivery. No
production, installer, release, workstation or unrelated research changes.

PR provenance was checked against GitHub's reported base/head and
`git diff <base> HEAD -- openspec/changes`. Four active changes were introduced
by this PR; `filter-app-server-process-diagnostics` existed on main and was
continued by this PR. These five are complete and are the only new archives.
Five earlier PR changes were already archived under `2026-09-21-*` and remain
unchanged.

The initial 22-candidate inventory incorrectly included 17 completed changes
inherited unchanged from main. After the requester questioned this scope,
all 17 moves and their main-spec additions were reversed, including the
packaging checkbox reconciliation. Final preservation checks compare their
entire trees to the pre-audit head. The broad initial audit artifacts are
retained locally as historical evidence; only `final-*` records describe the
corrected archive result.

## Archived changes

| Change | PR provenance | Complete tasks | Evidence |
| --- | --- | ---: | --- |
| [filter-app-server-process-diagnostics](../../openspec/changes/archive/2026-10-13-filter-app-server-process-diagnostics/tasks.md) | Continued | 7 | [independent-cli-processes-20261013](../../.planning/verification/independent-cli-processes-20261013.md) |
| [allow-optional-setup-parity-differences](../../openspec/changes/archive/2026-10-13-allow-optional-setup-parity-differences/tasks.md) | Added | 13 | [feature-default-differences-20261010](../../.planning/verification/feature-default-differences-20261010.md) |
| [support-packaged-desktop-cli](../../openspec/changes/archive/2026-10-13-support-packaged-desktop-cli/tasks.md) | Added | 8 | [packaged-desktop-cli-20261010](../../.planning/verification/packaged-desktop-cli-20261010.md) |
| [revalidate-runtime-config-at-launch](../../openspec/changes/archive/2026-10-13-revalidate-runtime-config-at-launch/tasks.md) | Added | 6 | [runtime-config-launch-20260929](../../.planning/verification/runtime-config-launch-20260929.md) |
| [support-staged-internal-update](../../openspec/changes/archive/2026-10-13-support-staged-internal-update/tasks.md) | Added | 26 | [support-staged-internal-update](../../.planning/devflow/verification/support-staged-internal-update.md) |

Total: five changes, 60 completed tasks. Planning artifacts and task execution
records were reconciled with their verification evidence, including the latest
PR follow-ups. Archived artifacts retain their original bytes.

Nineteen mainline changes remain active and unchanged: the 17 outside this PR's
scope, plus `internal-official-feature-parity` (74/84) and
`independent-app-cli-profiles` (89/92), whose live acceptance/closure is unfinished.
This audit does not mark those mainline tasks complete.

## Main-spec sync

OpenSpec 1.7.0 status snapshots supplied the only delta paths; successful specs
instruction snapshots were obtained before any write. Main synchronously
merged all seven delta files for the five selected changes into seven
capabilities (four new main specs, three existing). All 27 requirement
operations and 74 scenarios are accounted for, preserving existing main
Purpose text, unrelated requirements and existing scenarios. Main specs contain
one Requirements section and no delta-operation headers. No historical
mainline or unfinished change delta is imported.

After synchronization and strict validation, native archive moved the changes
with `--skip-specs --yes` to avoid reapplying an already completed merge.
Final recomparison uses the preserved authoritative deltas at their archive
paths. The older support-ownership and fallback-rule reconciliation considered
in the broad initial audit is entirely reverted and is not part of this change.

## Verification and delivery

Fresh final checks pass:

- OpenSpec 1.7.0 `validate --all --strict --json`: 30/30 (19 active changes,
  11 main specs), no failures.
- All 27 synchronized requirement blocks and 74 scenarios match their selected
  deltas exactly; existing Purpose text and unrelated requirements are preserved.
- All 28 archived files are byte-identical to their pre-archive contents.
- All 19 remaining active trees (97 files) are byte-identical to the baseline.
- Staged scope, report/index links and `git diff --cached --check` pass.
- Production scripts, installer, runner, release workflows and VERSION have no
  changes. Git transport readback confirms the intended PR branch and baseline.

Prior implementation/runtime evidence remains the checked-in verification
linked above; this documentation-only archive does not require reinstalling or
rerunning setup. The six extra source regressions from the
initial broad audit passed, but concern restored mainline changes and are not
used to claim new PR runtime acceptance.

Raw evidence root: `.planning/verification/archive-completed-changes-20261013/`,
registered by the adjacent retain contract. `pr-provenance.json` records the
full 22-candidate base comparison; `final-selection.json` and
`final-sync-actions.json` record the corrected scope.

## Review contracts

Main owns all writes and final validation. `archive_spec_final` and
`archive_standards_final` are read-only reviewers of the staged diff against
`0c0ee57f00d693f01ddc6bd427326e2a83136a51`. Spec input is the user's PR-only
completed-change archive request and this audit. Standards inputs are
AGENTS.md, ENGINEERING_POLICY.md, REVIEW_CHECKLIST.md and the archive/sync skill
contracts. Check provenance, completion, exact artifact preservation, semantic
sync, navigation and absence of unrelated changes. No writes or live commands.
Return concrete findings with anchors, or no findings; main closes findings
before delivery.

Knowledge update: maintain canonical specs and the current archive index/state.
Preserve historical execution logs and unrelated mainline workflow state; no
new change, skill or external publication is needed.

## Final review and delivery record

Standards review: no findings. Main's Spec review and exact delta comparison:
no findings. Independent Spec review was requested as an additional check; its
final result is recorded separately when available. All required archive
preservation and strict checks are complete.
The authorized delivery destination is `pr-fork/fix/standalone-installer-runtime`
for PR #1. The archive is verified and ready for this authorized Git delivery; the remote
head is read back after push.
