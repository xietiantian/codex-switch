## Recovery checkpoint

Initial checkpoint, 2026-09-29: previous optional policy 7 repair is retained. Startup contract approved; root owns all writes. Tests run only in isolated homes with fake or loopback providers. No live or Git effect authorized. The final completion below supersedes the initial implementation checkpoint.

## Implementation and review

Semantic evidence is recorded by policy 8; runtime refresh uses the existing preparation and atomic promotion from a private config/auth copy. Wrapper checks precede and follow synchronization. Materialized runtime/auth are preserved. Both launchers retain the original official home. Manifest snapshots use the existing bounded, non-symlink reader; concurrent generation/config changes fail before publication.

Spec review closed auth deletion and stale shared-home restoration findings. Standards review closed wrong official-home selection, manifest interleaving, same-home overwrite and unbounded manifest reads. Each guard has a regression; final reviewers report no remaining blocker.

The new runtime module remains covered by packaged-file hashes and transitive import validation. An attempted required-path-list expansion was removed after an old installer rejected it; required-path metadata is unchanged. Old-package upgrade and exact installed-runtime verification pass 26 cases with valid before/after manifests and no bytecode. No live installation or Git effect.

RED evidence: missing semantic module; real wrapper rejected bookkeeping and restored removed snapshots; current-runtime refresh originally invalidated its own promotion source. GREEN: semantic/boundary cases, actual launcher, same-policy refresh and rejected unsupported configuration now pass.

Initial broad run: 562 executed cases (one existing skip), plus an invalid test-module selector; selector error is not a product failure. Subsequent focused 241-case run had one timing-sensitive probe timeout under concurrent load; isolated deadline recheck passed unchanged. The final ten-case boundary suite also includes that unchanged deadline test and passes.

## Final completion

Fresh final Profile/Wrapper regression passes 227 cases in 361.970s. Complete Update/Release regression passes 200 cases in 523.385s after the required-path-list correction. Final semantic/launch/deadline boundary suite passes 10 cases in 12.780s. Exact installed-runtime checks pass 26 cases with valid manifests and no bytecode; the installed upgrade preserves the old installer's required-path contract. Strict OpenSpec, shell syntax and diff checks pass. Detailed commands, earlier failure attribution and retained generic logs are in `.planning/verification/runtime-config-launch-20260929.md`.

Both independent review axes are complete. Spec review verified auth preservation and authoritative configuration; Standards review verified official-home routing, frozen generation identity, bounded manifest reads and no same-home configuration overwrite. No remaining in-scope blocker. All six tasks are complete for source and isolated verification.

The requester explicitly excludes the newly observed Desktop version's packaging and additional feature/protocol adaptation. This repair changes neither its discovery nor those classifications and does not claim current live Desktop UI readiness. No live install/config/auth/Desktop mutation, release, archive, new commit, push or history rewrite occurred.
