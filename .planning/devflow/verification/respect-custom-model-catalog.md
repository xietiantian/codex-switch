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
- PR delivery is pending. Logs owned by this run use the prefix
`.planning/devflow/verification/local/custom-catalog-`.
