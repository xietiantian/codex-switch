# Archive completed PR repairs

User intent on 2026-09-21 authorizes archiving completed changes added by PR #1 and updating that PR. The five selected changes are support-standalone-installer-runtime, restore-first-internal-install, respect-custom-model-catalog, support-namespaced-feature-keys, and classify-current-runtime-extensions. All implementation task checklists are complete, and their existing verification records document passing isolated checks and closed reviews.

Scope: synchronize each added capability specification, archive only these five changes, update their ledger/state references, validate specifications and the scoped diff, and commit/push documentation to the existing PR. Production code, other changes, workstation installation and release publication are outside this archive operation.

The global OpenSpec executable is 1.3.1, whereas project skills target 1.7.0. Use the pinned 1.7.0 CLI through an isolated retained npm cache; do not upgrade the global executable. The tooling contract is archive-pr-fixes-tooling.contract.json.

Workflow sources: repository AGENTS.md, ENGINEERING_POLICY.md, REVIEW_CHECKLIST.md, and the project-local openspec-archive-change and openspec-sync-specs skills. Archive context and spec instructions were read with OpenSpec 1.7.0 before syncing. No additional operation guidance or artifact rules were returned.

## Archive operation

- OpenSpec 1.7.0 artifact graphs: every artifact is done in all five changes.
- All five change-level strict validations passed before movement.
- Main specs were absent; copied the reviewed ADDED requirements into standard main specs with concrete Purpose sections. No existing capability was replaced.
- Pre-archive strict validation: 32 passed, zero failed.
- Rechecked all 13 requirements and 37 scenarios against the deltas; no remaining sync differences.
- All archive destinations were absent. File hashes before and after each move match, including the existing .openspec.yaml.
- Updated execution-source references in TASK_LEDGER.md and resolved the archive authorization gate in STATE.md.
- support-standalone-installer-runtime: `openspec/changes/archive/2026-09-21-support-standalone-installer-runtime`.
- restore-first-internal-install: `openspec/changes/archive/2026-09-21-restore-first-internal-install`.
- respect-custom-model-catalog: `openspec/changes/archive/2026-09-21-respect-custom-model-catalog`.
- support-namespaced-feature-keys: `openspec/changes/archive/2026-09-21-support-namespaced-feature-keys`.
- classify-current-runtime-extensions: `openspec/changes/archive/2026-09-21-classify-current-runtime-extensions`.

## Final verification

- Post-archive `openspec validate --all --strict --no-interactive`: 27 passed, zero failed, using the isolated project-compatible OpenSpec 1.7.0 CLI.
- All 21 tracked archived files are byte-identical to their pre-archive Git contents.
- Every main requirement/scenario block exactly matches its archived delta.
- `git diff --check` passed. The scoped diff changes only specifications, archive locations, and repository workflow records; runtime source is unchanged.
- No incomplete tasks or unresolved review blockers remain within these five repairs. Earlier grammar-only compatibility findings are resolved by the subsequent current-runtime extension repair.
- Existing implementation verification remains in each linked repair record. No runtime test, live installation, provider call or new release is claimed by this documentation-only operation.
- Delivery target remains the existing PR #1; unrelated active changes and local research are preserved.


## Cache-free completion correction

The original respect-custom-model-catalog change was reopened again for legitimately
missing runtime caches, corrected under section 5, and re-archived after native,
isolated regression, exact package and two-axis review. Evidence is recorded in
respect-custom-model-catalog.md; implementation 0e3a011 is on PR #1. The original
four change files were moved without content changes after task completion and
spec sync. All five archived repairs now total 47 completed tasks, 13 requirements
and 46 scenarios, each matching its main-spec requirement blocks. The other four
archives remain untouched.
