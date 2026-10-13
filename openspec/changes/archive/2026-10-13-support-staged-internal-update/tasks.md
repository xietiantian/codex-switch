## 1. Durable staging and current-runtime identity

- [x] 1.1 Add public command RED cases in `test_codex_staged_update.py` for stage without profile, complete runtime path/version/digest, mutually exclusive options, JSON-only stdout, installer failure/interruption and unchanged live state; register isolated fixture ownership before execution.
- [x] 1.2 Implement the private versioned update-record and stage engine in `codex_switch_update.py`, routing through the existing complete-runtime installer adapter; retain the candidate and enforce safe records, ownership, atomic writes and exact requested/actual version checks. Run the stage cases GREEN.
- [x] 1.3 Add RED/GREEN `stage --current` coverage for complete standalone/direct runtimes including successful CLI-only bootstrap with no profile, installer-not-called, caller ownership, incomplete/altered assets and no live writes; freeze target/profile/store/home/binding and actual Desktop reference identity or absence without requiring parity inputs.

## 2. Private final inputs and durable source provenance

- [x] 2.1 Add RED/GREEN preparation tests and extract explicit candidate profile context from live `cmd_set_bin` state, separating captured source bytes/identity from final canonical destinations; use the final home model/provider/catalog and preserve existing one-shot seed precedence without temporary real-profile replacement.
- [x] 2.2 Capture validated config/auth privately with exact presence/digests and supported home semantics; reject unsupported separate config locations, unsafe files, collisions and malformed inputs before capture/publication; verify source drift, permitted absent auth, secrecy and caller auxiliary-file preservation through the apply seam.
- [x] 2.3 Add RED/GREEN managed-overlay capture and apply cases, preserving validated original source path/kind/digest independently from old health acceptance; import only genuine legacy provenance, reject missing/altered origin, retain default cache-to-corresponding-bundled rules, custom same-slug authority and the existing missing multi_agent_version to v2 rule.
- [x] 2.4 Prove native preparation uses isolated probe inputs and cannot execute caller/business hooks or load unrelated home state; keep final requested config intact and run existing parity/catalog/projection regressions.

## 3. Transactional full apply and first publication

- [x] 3.1 Bind the first apply input fingerprint and prepare current parity/capability/runtime projections against the retained candidate and frozen actual Desktop reference; reject unavailable full-mode reference and changes to each frozen official/internal/store/home/target/profile/selection/source identity before publication. Initial explicit compatibility differences are corrected and revalidated in section 8.
- [x] 3.2 Extend the existing runtime-binding transaction, artifact roles and journal only as needed to publish candidate profile config/auth/provenance with runtime, wrapper, binding and parity artifacts; add RED/GREEN rollback and installed-path failure cases and preserve the returned staged runtime path after executable swap.
- [x] 3.3 Add RED/GREEN first full apply with no CLI/profile and an existing verified Desktop, including journaled prior absence, no-replace publication, concurrent target/profile/backup appearance, failed postcondition rollback, partial-state refusal and no-Desktop full-mode failure; preserve strict one-shot CLI-only bootstrap.
- [x] 3.4 Add public-sequence RED/GREEN for successful strict CLI-only bootstrap -> actual Desktop available -> `stage --current` with CLI present/profile absent -> full apply; prove first profile/config/auth/binding publication succeeds without a second installer or empty-target check, failed/cancelled adoption preserves the CLI, and partial/tampered/unowned/residual state or drift is rejected.

## 4. Persistent recovery, concurrency and idempotence

- [x] 4.1 Add subprocess kill/failure-injection RED/GREEN cases for staging, private capture, preparation, each publication checkpoint and commit-before-result persistence; persist sufficient update/transaction identity to distinguish unpublished, confirmed committed and recovery-required outcomes after restart.
- [x] 4.2 Implement read-only `status` and idempotent `cancel` using existing transaction recovery for uncommitted owned writes only; cover repeated cancel, post-commit cancel, `--current` preservation, foreign/tampered records, source/target drift and an unresolved concurrent mutation without overwriting it.
- [x] 4.3 Prove same-ID/same-input committed apply returns the confirmed existing result without capture/probes/republish, different-input reuse fails, failed/cancelled/interrupted IDs cannot silently replay, no lock survives between commands, and competing IDs/profile mutations cause busy or stale refusal.

## 5. One-shot compatibility, documentation and package runtime

- [x] 5.1 Route existing one-shot update through the shared engine while preserving ordered/explicit versions, helper failure status, dry-run, existing target arguments, full/CLI-only checks and saved profile semantics; update public wrapper/parser tests and reject unsupported recipe/hook/deployment options before mutation.
- [x] 5.2 Update neutral README/command documentation for stage/apply/status/cancel, stable output fields, actual Desktop reference, supported home contract, candidate retention, source policy, first-install modes and recovery limits; include no private defaults, business identifiers or unrelated workflow material.
- [x] 5.3 Register every new required runtime module in package/import validation and installer inventory where applicable, adjust historical package fixtures intentionally, and validate source shell/Python syntax plus a fresh isolated prior-to-candidate package upgrade and installed staged-update/import tests without bytecode residue.

## 6. Integrated native validation and review

- [x] 6.1 Run focused staged/parity/catalog/lifecycle/first-install/transaction/runtime-binding/update suites and fresh full native regression including the profile script entrypoint; record exact command results, counts and genuine failures, with fresh HOME/store/App fixtures and `PYTHONDONTWRITEBYTECODE=1` / Python `-B`.
- [x] 6.2 Run complete native stage/apply and current-runtime preparation with verified real runtime collectors in isolated fixtures and a loopback provider, including profileless current-runtime adoption after successful CLI-only bootstrap, cache-free defaults, custom same-slug source, recapture, repeat apply and rollback; do not substitute mocks for schema/feature/export/probe evidence or claim live Desktop acceptance.
- [x] 6.3 Complete independent read-only Spec and Standards review under native bounded contracts, repair in-scope findings, and rerun only affected checks plus required final integration; classify unrelated findings in the tracked register without expanding the critical path.
- [x] 6.4 Run pinned OpenSpec 1.7 strict change/all validation, workflow-state JSON validation, shell syntax and diff checks; reconcile all scenario evidence, update native state/ledger and record generated-artifact retention or terminal cleanup receipts with no false completion claim.

## 7. Authorized existing-PR delivery

- [x] 7.1 Commit the reviewed complete change to the existing branch, verify an exact-commit package and manifest before/after isolated installed tests, then push to the existing PR under standing authorization; preserve all unrelated untracked/historical artifacts and exclude live installation, release and archive.
- [x] 7.2 Read back the delivered revision/PR state, record final validation/review/package evidence and any non-blocking findings, and mark completion only when every required behavior is proven and no approved work remains.

## 8. Preserve released explicit compatibility bindings

- [x] 8.1 Correct the native design/spec and add public init-to-stage RED coverage for released explicit-compatibility state; preserve the actual Desktop resolver and compare saved bindings and resolved reference only to their own snapshots. Run GREEN without rewriting saved official intent.
- [x] 8.2 Exercise released init output through real native stage/apply/switch/verify with isolated runtime copies and a loopback provider. Cover canonical bindings and independent post-stage manifest, bundle and CLI drift; retain no-Desktop/full-mode and first-install regression gates.
- [x] 8.3 Run focused and broad regression, native Spec/Standards review, strict OpenSpec/workflow checks and exact-package installed migration tests. Update evidence and deliver to the existing PR; preserve historical artifacts and the live workstation.
