## 1. Installer adaptation
- [x] 1.1 Add and run a failing standalone installer regression at the helper CLI.
- [x] 1.2 Implement noninteractive child execution and complete runtime materialization before scratch cleanup; retain legacy signing behavior.
- [x] 1.3 Run the standalone regression and legacy isolation/failure tests to GREEN.

## 2. Safety and transaction coverage
- [x] 2.1 Add and verify unsafe-link/layout/resource, tamper, environment/argument, and reuse cases.
- [x] 2.2 Verify full/CLI-only update integration and rollback with a real materialized candidate in a temporary store.
- [x] 2.3 Confirm runtime helper inclusion in generated release packages and document the stable launcher behavior.

## 3. Verification and delivery
- [x] 3.1 Run focused update/transaction suites, full profile regression, syntax, packaging, and strict OpenSpec validation.
- [x] 3.2 Review the complete diff and verify all added public text is generic; record results and remaining limits.
- [x] 3.3 Commit, push to an authorized fork, and open a pull request to main.

Owner: primary agent. Execution source: this file. Evidence:
`.planning/devflow/verification/support-standalone-installer-runtime.md`.
Write set: installer helper, runtime materializer, package reference validation,
bootstrap trust hashes, focused tests, README,
OpenSpec change, ledger, state, and verification record. No user runtime writes.

Validation commands:
```bash
python3 -B scripts/test_codex_update_release.py CodexStandaloneRuntimeTests
python3 -B scripts/test_codex_update_release.py
python3 -B scripts/test_codex_transaction.py
python3 -B scripts/test_codex_profile_switch.py
bash -n scripts/codex_env_setup scripts/codex-switch install.sh
openspec validate support-standalone-installer-runtime --strict --no-interactive
git diff --check
```
