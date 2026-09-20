# First internal installation verification

## Contract and baseline

Approved first-install restoration on fix/standalone-installer-runtime; baseline
ee86708329e7be9f9fd662be3387d8a6a1706387. Execution source:
openspec/changes/restore-first-internal-install/tasks.md. All probes use isolated
temporary HOME, CODEX_HOME, store, target, installer, and synthetic credentials.
No live installation, provider request, profile activation, or release occurs.

## Red / Green

- Public CLI fresh-install test before implementation: 1 test, failed with exit
  2 and `the internal profile must exist before a parity-safe update`.
- After absent-state staging and no-replace publication: the same test passes.
- First boundary matrix: 12 tests pass, covering wrong version, installer exit
  17, concurrent target/profile, partial/dangling state, final-path failure,
  dry-run, and latest-version resolution/failure.
- Final public first-install matrix: 22/22 passed, including complete standalone
  assets, cancellation of the public PID, signal immediately after publication,
  a concurrent equal hard link, strict tag/blocked policy, and explicit target.

## Final verification

- Update/release: all 196 cases executed. The initial run passed 187 and exposed
  9 fixture errors: test bundles omitted the new referenced helper. After adding
  the helper to those fixture builders, all 9 affected cases passed on recheck.
- Profile/wrapper: all 227 cases executed; the same fixture omission affected
  13 cases. The other 214 passed, and all 13 affected cases passed after repair.
- Fresh source-archive package builds and validates. Both legacy and standalone
  first-install tests pass through its packaged public CLI (2/2).
- Published native Codex 0.155.0: first installation, stable command version,
  and app-server schema generation pass with temporary HOME/store/install roots.
  Subsequent configuration and real profile capture pass in the same fixture.
- Bash syntax, Python 3.9 syntax compatibility, and git diff whitespace pass.
- Strict OpenSpec validation: 24 items passed, 0 failed.

## Review

Independent Spec and Standards reviews found cancellation, semantic-version,
publication-interruption, directory-durability, and concurrent-link ownership
issues. Each was repaired and verified; both reviewers closed their findings.
Additional target-argument tests preserve explicit --internal-bin selection and
reject conflicting --install-dir inputs. Public additions contain only generic
CLI behavior and this repository's own planning/evidence conventions.

## Delivery

Implementation commit e41c134e06887e575255ac9243ab31cea1a97b70 was pushed to
the existing PR https://github.com/cYz26/codex-switch/pull/1, whose title and
description now cover both standalone runtimes and first installations.
The PR is open and mergeable. No release or live installation is part of this
delivery. The first-install change remains active for upstream review.

## Artifact ownership

This run owns newly generated logs under
.planning/devflow/verification/local/first-install-*.log. Fixture roots belong
to each TemporaryDirectory lifetime; no other temporary roots are reclaimed.

## Remaining limits

Existing-profile Desktop parity and model-reference acquisition are unchanged.
First-install completion does not claim Desktop readiness or configure a profile.
