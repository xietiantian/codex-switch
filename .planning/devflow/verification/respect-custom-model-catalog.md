# Custom model catalog verification

## Contract

Baseline 9c9bc73; execution source respect-custom-model-catalog/tasks.md.
The requester authorized custom-model comparison changes, isolated testing,
commit/push and updating the existing PR. No live install, config/profile
mutation, provider request or release publication is part of this repair.

## Red / Green

- Removing the official cache from the complete preparation test reproduced
  `official model cache cannot be inspected safely` (FileNotFoundError).
- The same complete preparation test passes after removing cache coupling.
- The preparation fixture now uses a provider-only model slug and verifies
  conflicting same-slug/unrelated/invalid/symlink official caches, cache changes
  after preparation, receipt round trip, previous-policy rejection, invalid
  custom catalog shapes/entries and failed runtime probes.
- Existing repeat-overlay preparation and source/config identity checks remain
  in that fixture. Focused policy tests verify explicit applicability evidence
  and preserved runtime protocol/feature failures.

## Review contracts

Two read-only reviews compare baseline 9c9bc73 with the working-tree change.
Spec reviewer owns requirement/behavior assessment; Standards reviewer owns
repository engineering rules and code-quality assessment. Neither may write
files, invoke a live runtime/provider, or change the task scope. Their inputs
are the new OpenSpec change, parity source/tests, README and scoped diff.
Each reports concrete file/line findings and severity. Main owns all fixes and
final verification; no filesystem write scope overlaps exist.

## Final verification

- Parity: 99/99 passed, including the complete custom-catalog preparation matrix.
- Transaction: 258 cases, 257 passed and one existing skip.
- First installation: 22/22 passed.
- Strict OpenSpec: 25/25 items passed.
- Python 3.9 AST compatibility, Bash syntax and diff whitespace passed.
- Independent Spec and Standards reviews report no open findings. Spec review
  independently reran six focused cases successfully.
- Profile: 227/227 passed.
- Update/release: all 196 cases executed; 195 passed initially. One historical
  fixture failed in its installer and runner subcases because it retained the
  new first-install helper after removing the helper's dependency. Correcting
  that fixture's historical file set made both subcases pass on recheck.
- Exact implementation commit f116f6d was archived and packaged. In fresh
  temporary roots, the previous PR package installed, upgraded to the new
  package, and ran status. The complete custom-catalog preparation fixture then
  passed using installed modules; the installed bundle remained manifest-valid.
- Both reviewers accepted the bounded historical-fixture correction with no
  additional findings. No production bytes changed after package verification.
- PR #1 contains implementation f116f6d and historical fixture fix 3a92076.
  Its title/body and remote head were read back after push. The local test
  runner selects the exact verified implementation package. No runtime bytes
  changed in the follow-up fixture/evidence commits. Logs owned by this run use the prefix
`.planning/devflow/verification/local/custom-catalog-`.

## Reopened on 2026-09-21

The original archive preserved all artifacts. Its complete-preparation tests
covered explicit catalogs only, while the ordinary comparison test exercised
only the policy function. The requester clarified that catalog-free updates
must reach ordinary comparison. Prior results remain valid for their tested
scope, but do not prove that end-to-end branch. The original change is active
again, with four pending correction tasks. No implementation changed at reopen.

## Correction RED / GREEN and review

- Full preparation without model_catalog_json reproduced the incomplete-config
  error. It now passes through the default-cache comparison path. Invalid explicit
  values never fall back, and explicit valid custom catalogs remain independent
  of both default caches, including when the source filename is models_cache.json.
- Additional RED/GREEN cases caught repeat-overlay custom reclassification, a
  manifest source-kind flip, a manifest-only legacy policy downgrade, and malformed
  policy metadata. Policy 4 binds origin to the recorded receipt hash, policy and
  source before regenerating current evidence. Genuine legacy custom receipts
  remain usable as provenance, not as current acceptance.
- Routing matrix: 16/16 passed. Parity: 107/107 passed. Current runtime policy:
  5/5 passed. Profile: 227/227 passed. Transaction: 258 cases, 257 passed and
  one existing skip. Logs use the catalog-routing- prefix below local/.
- The pre-existing large-write deadline fixture repeatedly failed before reaching
  its intended backpressure condition. A read-only passthrough diagnostic observed
  cold core startup at 0.550 seconds, beyond its old 0.5-second budget. The bounded
  test-only guard extends the budget/peer hold and asserts real BlockingIOError.
  One initial post-adjustment full run also missed the marker; focused, diagnostic
  full and final direct full-suite verification then passed. Production runner
  deadlines were not changed. This evidence is a test-fixture limitation, not a
  model-routing production failure or a claim that repeated failures passed.
- Spec review's P1 manifest downgrade finding and Standards review's P2 malformed
  policy finding each gained a failing test before their fixes. Both reviewers
  closed all model-routing findings on recheck. The final incidental fixture
  review also passed; it preserves the intermittent load-related test limitation.
  Exact committed-package verification is pending below.
- Formal custom-model-catalog-parity specs now preserve both requirements and all
  six original scenarios, adding five correction scenarios. The original change
  remains active until delivery and package verification finish.

## Generated artifact contract for correction delivery

Owner: the primary catalog-routing correction run. Command: git archive of its
implementation commit, package-release.sh, then install.sh with explicit local
install/lib roots and file URLs. Allocate the absent root
.planning/devflow/verification/local/catalog-routing-package/ for source archive,
distribution, installation and verification logs. The private local test-entrypoint
artifact roots may additionally receive source/distribution copies named for the
same commit. Do not use installed workstation paths, change shell startup files,
or call a provider/Desktop runtime. Retain all artifacts and unrelated local
research; no automatic cleanup or deletion is authorized by this contract.

## Correction delivery and archive

- Implementation 9d0c8e2 was archived with git archive and built using the existing
  release packager. In fresh isolated install/lib roots, the previous PR package
  installed and upgraded successfully to the candidate. All 16 routing tests and
  the real-backpressure regression passed against installed modules (17/17).
  Distribution, archive and post-test installed manifests validate, with no .pyc
  files in the source or installed bundle.
- OpenSpec 1.7 strict validation passed all 28 active/spec items after main-spec
  sync; Python 3.9 grammar and diff checks passed. Both independent review axes
  have no open findings. Full provider-backed Desktop UI behavior is not claimed
  by deterministic preparation fixtures or temporary installation tests.
- Native git ls-remote preflight found the expected 3a32b4e PR head. The named
  preflight helper was not available in the repository or installed skill roots;
  direct native transport was used. Push succeeded and GitHub readback confirmed
  PR #1 at 9d0c8e2. The stable local test entrypoint now selects the exact verified
  source/archive; its Bash syntax and copied archive manifest passed validation.
- Tasks 4.1-4.4 are complete. The requester previously authorized archiving
  completed PR changes and selected correction of this original change. Reuse
  its original archive location after verifying all two requirements and eleven
  scenarios match the main spec; preserve the original history and this correction.
- Re-archive completed at the original location. Fresh post-archive OpenSpec 1.7
  validation passed 27/27 items; Bash syntax and diff checks passed. Across the
  five PR repairs, 43 completed tasks, 13 requirements and 42 scenarios reconcile.
  This final evidence/archive update changes no packaged runtime or README bytes.
- Live installed state, model/cache/config contents and Desktop processes were
  not changed. Missing default caches still fail: this correction restores the
  comparison branch, rather than manufacturing evidence or bypassing checks.
