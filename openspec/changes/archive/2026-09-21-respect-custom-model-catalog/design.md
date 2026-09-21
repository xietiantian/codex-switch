## Target State and Approval

The requester approved the custom-model comparison correction, isolated tests,
and another commit to the existing PR branch. Baseline: 9c9bc73. Public parity
preparation, policy evaluation, receipt load/revalidation, and the existing
profile-update transaction are the approved verification seams. No installed
workstation state or external provider is used for testing.

## Initial implementation decisions (historical; corrected below)

1. The current preparation path already requires a complete explicit provider
   and model_catalog_json configuration. _config_identity resolves and validates
   the original catalog, including provenance behind a managed overlay. Every
   candidate accepted through this path therefore has explicit custom model
   evidence. Do not add a slug/provider-name heuristic, a bare key-presence
   shortcut, or a new public bypass flag.
2. Preparation does not read or export an official model cache. Missing,
   malformed, unrelated, or changed official model caches are immaterial to
   this path. Official binaries remain authoritative for Desktop runtime
   schemas and features, not provider-owned model capability metadata.
3. Policy evaluation has an explicit custom-catalog context. It omits only the
   official-model metadata comparison and emits a stable informational finding
   declaring that comparison not applicable. Ordinary policy callers retain
   their comparison behavior. Persist the finding using the existing receipt
   shape and bump policy version; old receipts fail stale/unsupported checks.
4. Remove cache-only preparation/revalidation fields and dependencies. Retain
   source catalog identity/digest, provider/config fingerprints, source-preserving
   overlay rules, official/internal binary identity, schema coverage, feature
   policy, and real bounded probe requirements. A custom catalog cannot turn
   failed runtime probes or source drift into success. The existing v2 runtime
   projection remains subject to its own behavior proof, not model-name equality.
5. A managed overlay without original-source provenance remains invalid. Repeated
   updates still resolve the original source and do not infer a new official
   model relationship from generated model_catalog_json assignments.

## Completion and Verification

First reproduce the absent-cache failure through prepare_parity_bundle. Verify
absent/invalid/symlink cache, same-slug conflicting metadata, custom-only slugs,
cache changes between preparation and promotion, repeat preparation from an
overlay, receipt round trip and old-policy rejection. Verify bad/missing/changed
custom catalogs and failed protocol/behavior probes still fail. Run parity,
profile, transaction, update/release and first-install suites as applicable,
clean package validation, syntax, strict OpenSpec and two-axis review. Refresh
the local PR test artifact only after the committed package passes isolation.

## Scope and Artifacts

Primary agent owns scripts/codex_switch_parity.py, focused tests, README.md,
this change, TASK_LEDGER.md, .planning/devflow/STATE.md and its verification
record. No source file is delegated. Reviewers are read-only. Newly allocated
TemporaryDirectory roots belong to individual tests. This run owns
.planning/devflow/verification/local/custom-catalog-*.log; preserve unrelated
untracked research and all historical artifacts.

The complete update/release regression exposed a historical fixture containing
the new first-install helper after deliberately removing its update-policy
dependency. The historical-migration test now also removes that anachronistic
helper from its old-version fixture. This bounded test-only correction in
scripts/test_codex_update_release.py changes no runtime or packaging policy.

## Skill Routing Ledger

- artifact-status: final; workflow: Full OpenSpec.
- capability-research: used; direct source, config provenance, cache and policy evidence.
- decision-resolution: used; requester approved custom-model comparison scope.
- decision-grilling: skipped; no unresolved user choice.
- implementation-planning: used; this design and dependency-ordered tasks.
- architecture-guidance: used; separate provider-owned metadata from runtime compatibility.
- domain-language-modeling: skipped; existing model/catalog/receipt terms suffice.
- openspec-routing: used; this change is the current execution source.
- test-first-execution: used; approved public preparation and policy seams.

## Risks and Alternatives

Fetching an official bundled catalog repairs availability but not applicability,
so it is rejected for custom models. Skipping every Desktop check is rejected.
Policy versioning requires old successful receipts to be regenerated; their
existing source provenance permits ordinary re-preparation. Preserve cache
bytes and fail closed on custom-source changes. No live compatibility claim
is made from the isolated deterministic provider-boundary fixtures.

## Reopened correction: default model comparison

The requester clarified that absent model_catalog_json uses ordinary comparison.
This is a completion gap in the original repair, not an independent capability.
The original archive preserved all bytes but its acceptance covered only policy
callers without custom context, not complete catalog-free preparation. Reopen
this change; retain the original RED/GREEN evidence and completed tasks as
historical proof of the custom path. Baseline for this correction: 3a32b4e.

Target state: an explicit valid custom catalog retains the comparison exemption;
an absent key uses the internal Runtime Binding home/models_cache.json as model
source and the official Runtime Binding home/models_cache.json as reference.
Neither cache is fabricated, downloaded, or overwritten. Missing, unsafe,
malformed, ambiguous or changed evidence fails with a model-data diagnostic.
Explicit null/non-string/empty catalog values are invalid, not fallback requests.
Keep existing overlay, feature/protocol/probe and transactional requirements.

Record source kind in parity manifest metadata. A generated overlay retains its
original custom/runtime-cache kind on repeated preparation. Prior manifests
without the new kind are legacy custom-only evidence; current-policy manifests
must supply a valid kind. Default-cache paths remain bound to the selected
Runtime Bindings. Restore official-cache identity checks during preparation and
promotion only for the default path. Bump policy version; preserve prior custom
source provenance so old receipts can be prepared again. No schema migration or
user-home changes are needed to prepare the candidate.

Execution owner: primary agent. Write set: scripts/codex_switch_parity.py,
scripts/test_codex_parity.py, scripts/test_codex_model_catalog_routing.py,
README.md, this existing change, TASK_LEDGER.md, .planning/devflow/STATE.md,
and .planning/devflow/verification/respect-custom-model-catalog.md. Public seams
remain prepare_parity_bundle, receipt serialization/revalidation and existing
promotion, as previously approved. No live provider, installed configuration,
Desktop process, dependency or release mutation. Commit/push to PR #1 and
refresh the isolated PR package after verification.

Validation: first reproduce catalog-free preparation failure, then verify default
comparison success and metadata incompatibility, explicit invalid catalog errors,
custom cache independence, missing/symlink/malformed data, preparation/promotion
drift, repeated managed overlays, source-kind corruption and legacy provenance.
Run all parity/current/transaction suites, the profile regression, source/package
checks and OpenSpec 1.7 strict validation. Standards and Spec reviews are read-only
parallel agents per the repository code-review skill; primary owns all fixes.
Any required behavior not passing remains unchecked and prevents re-archive.

Bounded incidental test repair: the existing large-request backpressure fixture
can exhaust its 0.5-second deadline in cold Python startup before exercising the
intended write. Within the existing test write set, enlarge its test-only deadline
to two seconds and peer hold to five seconds. Keep the emitted-large-ID and typed
timeout checks, and observe real BlockingIOError through a passthrough os.write
wrapper. No production timeout, process or protocol behavior is modified.

Skill routing: final design; capability research used (source/history/contracts);
decision resolution used (requester chose absent-catalog comparison); grilling
skipped (no unresolved decision); implementation planning used (this addendum);
architecture guidance used (separate source origin from generated overlay);
domain language modeling skipped (existing catalog/receipt vocabulary);
OpenSpec routing used (reopened original change); TDD uses approved public seams.
Unavailable higher-level DevFlow skill entrypoints are represented by these
canonical intake, planning and execution records, as in the initial repair.
