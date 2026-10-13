# PR review fixes (2026-10-13)

Baseline: 6dc06760b638f94fd761684940a491adb5d276e8.
User authorizes fixing the three reviewed defects, protecting the existing
setup flow and pushing to the existing PR branch. User explicitly opts out of
OpenSpec: use this lightweight execution ledger; do not create, reopen, update
or archive OpenSpec changes.

Main owns source/tests/evidence and delivery. Scope: shared internal file-auth
activation, first-stage recovery-backup classification, read-only candidate
identity in status, and regressions at the already agreed stage/apply/switch/
status interfaces. Changes are limited to scripts/codex_switch_transaction.py,
scripts/codex_switch_update.py, a config auth helper if necessary, corresponding
existing test modules, TASK_LEDGER.md and .planning/devflow/STATE.md. No parity
policy relaxation, installer/version pin, Desktop discovery, recipes or caller
business behavior is added to this external repository.

Preserve refreshed internal auth and official/internal isolation; keep env-key
behavior. Distinguish strict empty installation from verified stage --current
adoption and existing-profile updates. Check only uncommitted runtime identity
in status, without revalidating mutable config/hooks or losing committed results.

Validation: vertical RED/GREEN at each interface; focused and affected broad
suites; isolated native first-install/apply/switch and lifecycle checks; exact
package installation; refresh the existing consumer test entry to the committed
build and execute full setup plus installed verification. Consumer-specific
commands and output remain in the consumer repository's local artifacts.

Status: source repair and bounded review complete; package/live acceptance and PR delivery pending. Preserve unrelated untracked research.

## Bounded review contract

Reuse pr1_full_code_review for read-only review of `git diff 6dc0676 -- scripts`.
No OpenSpec workflow or artifact writes. Scope is these three repairs only;
review auth isolation/refreshed credentials, first/current/update distinction,
status observational semantics and regression coverage. Main owns all edits,
validation and final delivery. Return concrete findings or no findings; do not
repeat the entire prior PR review or run live commands.

## Source verification

- Auth RED reproduced deletion of required auth; the semantic-storage expansion
  then reproduced eight failures for comments, quoted keys, auto and omitted
  storage. Final GREEN covers five representations and four auth ownership
  states, twice each, plus existing internal/official contracts and native
  apply/switch/verify: 4 tests pass. Runtime refreshes win over profile snapshots;
  absent runtime credentials come only from the selected internal profile.
- Backup RED reproduced profileless stage accepting a recovery backup; GREEN
  covers file/dangling symlink/directory, late and pre-fix apply records, normal
  existing-profile updates, successful first apply and verified current adoption.
- Status RED reported staged after candidate modification; GREEN checks changed
  and missing candidates without execution/journal writes, mutable home settings,
  committed results and interrupted terminal recovery.
- Affected suites: 494 tests, exit 0, one existing conditional skip. Profile:
  227 tests, exit 0. Native first apply, released binding lifecycle and file auth:
  3 tests, exit 0; the final semantic-auth native case was repeated in the 4-test
  final GREEN above. Update/Release remains running at this checkpoint.
- Python 3.9 grammar for all six changed Python modules and git diff --check pass.
- Bounded independent Standards/Spec review initially found a lexical TOML gate;
  provider authentication now uses semantic TOML parsing consistent with private
  apply. Follow-up review closes that P2 with no additional findings.
- Raw logs/results: registered retained local root
  `.planning/verification/pr1-review-fixes-20261013/`.

No OpenSpec artifacts changed. No installer, feature/parity policy, Desktop
reference, mutable setup ownership or caller recipe behavior changed.
