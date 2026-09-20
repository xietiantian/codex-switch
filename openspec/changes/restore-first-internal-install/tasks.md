## 1. First-install command
- [x] 1.1 Add a failing fresh-install regression through the public CLI.
- [x] 1.2 Implement safe classification, version selection, absent-state candidate preparation, and no-replace publication.
- [x] 1.3 Verify legacy and complete standalone first installs with isolated HOME/store/config paths.

## 2. Failure boundaries
- [x] 2.1 Verify partial/dangling state, preparation failure, wrong version, concurrent state, and installed-path failure.
- [x] 2.2 Verify dry-run and automatic version policy; preserve existing upgrade behavior.

## 3. Delivery
- [x] 3.1 Run update/profile regressions, package verification, syntax, and strict OpenSpec validation.
- [x] 3.2 Review generic public docs/diff and record evidence.
- [ ] 3.3 Commit, push, and update the existing PR.

Owner: primary agent. Write set: scripts/codex-switch, scripts/codex_env_setup,
first-install helper, release module allowlist/trust hashes, focused tests,
README, this change, TASK_LEDGER.md, DevFlow state and verification record.
Evidence: .planning/devflow/verification/restore-first-internal-install.md.
