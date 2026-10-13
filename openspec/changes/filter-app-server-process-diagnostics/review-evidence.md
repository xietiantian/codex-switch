# CLI process attribution review and recovery (2026-10-13)

Baseline d7efb37724e1778551668c592ee4f655f50e9916. User authorized the
diagnosed process ownership repair and existing PR commit/push. Original tasks
1–3 are historical; current tasks 4–7 own this follow-up.

## Evidence and recovery

- RED: public snapshot -> observation -> attestation reports backend mismatch
  and proxy bypass for independent terminal CLI daemon children.
- GREEN: six observation/attestation tests pass, including real Desktop errors,
  proxy precedence, nested children, ambiguous ancestry and concurrent owners.
- Generated evidence: `.planning/verification/independent-cli-processes-20261013/`.
- Main owns source, test, README, change/control-plane/evidence edits. Preserve
  unrelated untracked artifacts. No runtime goal requested; durable contract
  and tasks remain authoritative.

## Agent Ledger / review contracts

Required by the repository's code-review skill. Reviews run independently;
baseline-to-working-tree diff is used before the source commit.

| Agent | Scope / Goal | Write set | Verification / Evidence | Human gate | Status |
|---|---|---|---|---|---|
| switch_spec_review | Review collector, tests and README against this change's follow-up | none (read-only) | `git diff d7efb37724e1`; report missing/wrong/extra behavior with source lines | report scope expansion; no mutations or external effects | closed; zero unresolved findings |
| switch_standards_review | Review same diff against AGENTS, ENGINEERING_POLICY, REVIEW_CHECKLIST and code-review smell baseline | none (read-only) | `git diff d7efb37724e1`; distinguish standards from judgement calls | report risks; no mutations or external effects | closed; zero unresolved findings |

## Recovery & Reconciliation History

- Task start: correct external PR branch, clean tracked state at baseline;
  unrelated untracked research retained. Reused existing process diagnostic
  change. No main application content introduced.

## Review closure

Spec identified ambiguous CLI syntax and proxy payload/option attribution.
All findings have public RED/GREEN regressions. Final Spec recheck: 11 input
scenarios pass, zero unresolved findings. Standards unknown-syntax finding is
closed; no additional hard standard violations. Ordinary positional TUI prompt
handling follows the current CLI help grammar, not a guessed subcommand list.

Final source/package checks are recorded in the linked verification record.
Main owns the remaining exact-commit acceptance and delivery.
