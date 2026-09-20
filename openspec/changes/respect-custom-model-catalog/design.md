## Target State and Approval

The requester approved the custom-model comparison correction, isolated tests,
and another commit to the existing PR branch. Baseline: 9c9bc73. Public parity
preparation, policy evaluation, receipt load/revalidation, and the existing
profile-update transaction are the approved verification seams. No installed
workstation state or external provider is used for testing.

## Decisions

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
