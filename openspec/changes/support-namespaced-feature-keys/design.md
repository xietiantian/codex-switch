## Target State and Scope

The runtime inventory accepts the actual feature output of Codex 0.155.0 and
the official Desktop binary 0.155.0-alpha.9, including dotted names. Older
flat names retain their meaning. Names remain opaque identifiers throughout
inventory comparison and policy evidence; dots do not imply nested config.

Baseline: 250c0ca. The requester authorized continuing fixes, isolated tests
and updates to existing PR #1. Public test seams are the existing approved
collect_feature_inventory, compare_feature_inventories and policy/evidence
interfaces. The implementation does not touch installed configurations.

## Diagnosis and Decision

In empty temporary homes, official and candidate CLIs exit successfully and
produce respectively 145 and 142 feature rows. Exactly one row in each fails
the existing parser: guardianv2.thread_context, under development, false.
The previous 0.153.4 CLI produces 135 rows with no grammar failures.

Use one shared pattern: the existing alphanumeric-first lowercase/underscore
segment, repeated after a literal dot. Reject leading, trailing and repeated
dots. Apply the same pattern to the line parser and existing identity
validators so parsing cannot succeed only to fail downstream. Do not skip
unrecognized rows, accept arbitrary punctuation, normalize names, change
feature classification or weaken runtime compatibility policy. No receipt
schema/policy revision is needed: prior valid names and policy outcomes retain
their meaning; newly accepted names remain subject to the current policy.

## Execution and Completion Contract

1. Add a failing regression using the actual dotted row through inventory
   collection/comparison; retain a flat parent and differing effective state.
2. Share the bounded grammar; verify canonical names and policy evidence,
   malformed names/rows, duplicate names and unclassified drift rejection.
3. Run real binaries with fresh isolated/effective homes; record any subsequent
   policy incompatibilities separately without granting them exceptions.
4. Run the complete parity suite, relevant profile regression, Python 3.9
   syntax, strict OpenSpec, clean-package install and installed-module checks.
5. Complete read-only Spec and Standards reviews, update evidence, commit/push
   to the existing PR and refresh its exact local test package.

The primary agent owns scripts/codex_switch_parity.py,
scripts/test_codex_parity.py, README.md, this change, TASK_LEDGER.md,
.planning/devflow/STATE.md and this change's verification record. Reviewers
are read-only, with no provider, installation, source or workflow writes.
TemporaryDirectory roots belong to individual tests. Logs allocated by this
run use .planning/devflow/verification/local/namespaced-feature-*.log;
preserve unrelated untracked research and historical artifacts.

## Risks and Alternatives

Ignoring malformed rows would lose compatibility evidence. Broad punctuation
acceptance would weaken identity validation. Accepting only one hardcoded
feature name would repeat the failure for another namespace. A shared bounded
grammar repairs the contract without expanding the runtime policy allowlist.
Passing inventory syntax does not prove complete Desktop parity; actual
feature/protocol differences must remain visible and blocking as appropriate.

## Skill Routing Ledger

- artifact-status: final; workflow: Full OpenSpec.
- capability-research: used; actual bounded CLI output and local grammar consumers.
- decision-resolution: used; existing authorization and observed output define scope.
- decision-grilling: skipped; no unresolved syntax or ownership decision.
- implementation-planning: used; this contract and dependency-ordered tasks.
- architecture-guidance: used; shared grammar with unchanged policy semantics.
- domain-language-modeling: skipped; existing feature identity terms suffice.
- openspec-routing: used; this change is the current execution source.
- test-first-execution: used; existing approved public inventory and policy seams.
