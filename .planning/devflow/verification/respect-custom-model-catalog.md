# Custom model catalog verification

## Lifecycle correction completed (2026-09-21)

Baseline 757c0cd; section 6 of the reopened original change is the execution
source. The requester approved final capture, same-binary preparation and
verified activation, including the independently reproduced home/projection
failures. Source/isolated-test/PR delivery authority is standing; no workstation
state, provider, credentials or Desktop process is a test target.

Native isolated RED: capture removes manifest receipt references; rebind with
an adopted internal home uses the same file for shared and runtime inputs,
then invalidates its shared digest. Selecting a separate official home allows
rebind verification, but the next switch rematerializes different runtime and
profile bytes. These results do not establish a healthy lifecycle.

Current test owner: primary. The new parity lifecycle tests allocate and clean
only their own TemporaryDirectory roots. Retain `local/parity-lifecycle-*`
diagnostics. Home-selection RED: three public-contract tests fail because no
common runtime home resolver exists. Native acceptance is retained privately;
public fixtures use a loopback provider and no private business configuration.

Review contract: compare baseline 757c0cdd37461a929abd11c3f617bfc9c6ebe218 with
the staged and unstaged working tree (`git diff 757c0cd`); no intervening commits.
Spec reviewer owns the original change's section 6 requirements and lifecycle
correctness. Standards reviewer owns ENGINEERING_POLICY.md, AGENTS.md and the
complete code-review smell baseline. Both are read-only; neither may modify
files, use live configuration or contact a provider. Review scope is the five
production modules, four test modules, README and lifecycle planning/evidence
artifacts shown by that diff. Primary owns fixes, tests and final delivery.

Fresh isolation: runtime-binding 90/90 and transaction 257/258 (one existing
Python 3.9 interpreter skip) passed. Native first/repeated activation, stale
profile detection and fresh/existing/explicit-home rollback pass with final
source and installed packages. Full profile and parity checks used temporary homes.

## Lifecycle final source verification (2026-09-21)

- Final combined transaction/parity/model-source/current-policy run: 406 cases,
  405 passed and one existing Python 3.9 interpreter skip. The earlier timing
  fixture miss under concurrent load passed its isolated recheck and this full
  combined run; deadlines were not relaxed.
- Profile: all 227 cases exercised. A harness-level skip-self-update variable
  incorrectly suppressed nine self-update scenarios; removing that variable
  made all nine pass. Production code and those test expectations were unchanged.
- Runtime binding: all 90 cases exercised. Two complete artifact-set assertions
  needed the newly required official manifest publication; their expectations
  now also check the selected official home. The other 88 passed unchanged; both corrected cases passed their focused recheck.
- Native lifecycle: 4 tests passed (three actual-backend subcases: first setup,
  recaptured active profile, first explicit official home). Each exercises failed
  publication rollback, successful same-binary rebind and two switches. Mutated
  profile input is rejected before activation writes; broken official manifests
  yield a stable unhealthy report. The existing standalone native probe test
  also passed after extracting the shared loopback fixture.
- Review RED: broken official manifest escaped the report API. Spec review also
  identified omitted explicit-home publication; Standards review identified
  projection-only checking before activation. All three were fixed and verified.
  Independent Spec and Standards reviews now have no open findings.
- Python 3.9 grammar, shell syntax, diff checks and 28 strict OpenSpec items pass.
  Main specs include the lifecycle requirement and explicit-home scenario.
- No live installation, configuration, cache, LaunchAgent, provider account or
  Desktop UI was changed. This is native loopback acceptance, not provider-backed
  Desktop UI acceptance. Exact committed-package verification and delivery follow.

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


## Second reopened correction: absent default caches

Baseline 1019312; the original change section 5 remains the execution source.
The cache-only correction did not cover a legitimate cache-free runtime home.
No Python bytecode cleanup can satisfy this missing model-data input. The scope
is ordinary source selection; explicit custom catalogs retain their exemption.
The existing authorization covers implementation, isolated testing, PR updates
and original-change archive after completion. Live installation/config changes,
Desktop activation and external provider calls remain excluded.

### RED / GREEN and behavior

- Complete preparation with both caches absent failed with the reported model
  cache inspection error before the change; the same seam now succeeds using
  each corresponding binary's offline full model catalog.
- The transaction rejected new source snapshot roles before the role/path allowlist
  update. Publication plus crash recovery after either source and after manifest
  activation now restore the old bundle, including prior/absent source files.
- Both review axes found that current receipts could omit model_sources. Two
  regression tests first failed, then passed with mandatory two-sided provenance.
  Missing/empty sources, repeat-update tampering and either binary digest mismatch
  are rejected. Genuine policy 4 cache provenance is re-prepared under policy 5.
- A present unsafe/malformed cache never falls back. Missing caches alone select
  bundled exports. Export failure, timeout, output overflow, invalid JSON, absent
  active model, newly appearing cache and source/binary drift remain blocking.
- Repeated updates validate recorded origin and select fresh cache/binary evidence;
  managed default overlays never acquire custom-catalog applicability.
- Exports run with private CODEX_HOME/cwd, file credential storage and a whitelisted
  environment without credentials. Output/process bounds use existing primitives.
  User cache/config files are not created by collection.

### Independent review

Contract: default-model-review.contract.json, read-only axes at baseline 1019312.
Spec closed the receipt P2 and a P3 stale runtime-cache-only scenario after fixes.
Standards independently closed the same P2; no other actionable smell or correctness
finding remained. Each independently reran focused tests. The main spec now matches
both original requirements and all fifteen scenarios; previous scenarios were
preserved and four cache-free scenarios added. Pinned OpenSpec 1.7 returned valid
spec instructions with no additional rules before this sync.

### Native full-preparation evidence

The actual Desktop bundled CLI 0.155.0-alpha.9.2 and candidate CLI 0.155.0 were used
in temporary homes with no default cache, no custom catalog and no copied credentials.
An isolated loopback Responses fixture supplied deterministic completion events.
The production prepare_parity_bundle path used actual offline model exports,
version/features, schemas, capability probing, core protocol and typed-v2 probes;
no loader/probe substitution was used. Both probes passed, complete preparation
and immutable/input revalidation succeeded, and no model cache was generated.
Both exports contain nine complete model entries including tool_mode; model/list
would omit that field. The thirteen previously classified optional differences
remain informational. No live promotion, Desktop UI session or external provider
behavior is claimed. Private logs use local/default-model-* and are retained.

### Validation before committed-package delivery

- Bundled-source tests: 17 passed; existing model routing: 16 passed.
- Parity: 107 passed; current runtime: 5 passed.
- Transaction: 258 cases, 257 passed and one existing skip.
- Python 3.9 AST grammar, shell syntax and diff whitespace passed.
- Pinned OpenSpec 1.7 strict validation: 28 items passed before re-archive.
- An initial transaction invocation used a nonexistent filename and ran no tests;
  the corrected test_codex_transaction.py invocation produced the results above.
- Profile regression and exact committed package are recorded below when complete.

### Generated artifact and transport contract

Owner: primary cache-free correction run. Allocate absent
local/default-model-package for git-archive source, distribution, isolated install
roots and logs. Build the exact implementation commit, upgrade from the previous
PR package using explicit file URLs/install/lib roots, run installed-module checks
with bytecode disabled, then validate package/archive/installed manifests again.
Refresh the existing private test entrypoint only from those verified bytes.
Retain artifacts and unrelated research. No live install, shell startup mutation,
cleanup or release publication is authorized by this operation.

Native git ls-remote read back the expected fork branch at baseline 1019312.
The repository has no git_transport_preflight.py; native transport is the bounded
fallback, independent of the separately checked GitHub PR control plane.

- Fresh profile regression completed: 227/227 passed in 292.658 seconds with an isolated shell-profile path. Section 5.1-5.3 verification and review are complete; only committed-package delivery and archive remain.


### Cache-free correction delivery and archive

- Implementation 0e3a011 was built from its exact git archive in the predeclared
  absent package root. The previous PR package installed and upgraded to it using
  explicit isolated install/lib roots and local file URLs.
- All 34 installed-module checks passed, including 17 bundled-source, 16 routing
  and the existing backpressure regression. Distribution, archive and installed
  bundle manifests remained valid, with no .pyc residue.
- Native Git pushed 0e3a011 to the existing fork PR branch. The stable private
  test entrypoint now selects this verified source/distribution; its prior runner
  is retained. Copied distribution bytes/manifests and shell syntax passed again.
  The setup script was not executed and its tracked source did not change.
- OpenSpec 1.7 archive context was read; it added no operation guidance. All tasks
  were complete and the main spec matched all two requirements/fifteen scenarios
  before moving the original change back to its original archive location.
  Four files have identical before/after move hashes.
- All five PR repairs now contain 47 complete tasks, 13 requirements and 46
  scenarios. Every archived requirement block matches its corresponding main spec.
  No unrelated change, pre-existing research, live installation or user config
  was modified. This follow-up archive/evidence commit changes no runtime bytes.

- Post-archive strict validation: 27 passed, zero failed. Final diff whitespace and public-content boundary checks passed. No production bytes changed after exact-package verification.

## Lifecycle delivery and archive

Implementation fca9287 was committed and packaged from a clean Git archive.
In private install/lib/home roots, the previous PR package installed and upgraded
to this package. All 40 installed-module tests passed, including three real
backend lifecycle scenarios. Post-test distribution, archive and installed
manifest validation passed, with no Python bytecode residue.

Native Git transport preflight and push to the existing PR branch succeeded.
The stable requester-owned test entrypoint now selects this exact verified
implementation. No source/runtime bytes changed during archive.

The original change is re-archived at its existing dated location. All 21 tasks
are complete, and all three requirements and 22 scenarios match the main spec.
Standing archive/commit/push authorization was reused; no publication release
or live installation was performed. The source-review axes are closed, including
the two adjusted artifact-set assertions. Diagnostics are retained under the
local/parity-lifecycle- prefix.
