## Why

Internal updates must distinguish explicit custom catalogs from default runtime
model data. The initial repair removed official-cache coupling for explicit
catalogs but left catalog-free updates unreachable; the original metadata
comparison must remain available for that path. A provider-defined model is not
the official model merely because its slug matches. Missing caches or unrelated
official metadata therefore block otherwise compatible candidates.

## What Changes

- Treat a validated explicit source catalog as provider-owned model evidence.
- Omit official-model metadata comparison and cache acquisition for that path,
  recording that comparison as not applicable.
- Preserve source validation, overlay provenance, runtime protocol and feature
  checks, bounded behavior probes, and transactional revalidation.
- Route absent model_catalog_json through default runtime model caches and the
  existing official/internal metadata comparison; malformed explicit values fail.
- Preserve model-source kind across managed overlays and repeat updates.
- Version the changed policy so old receipts require fresh preparation.

## Capabilities

### New Capabilities
- `custom-model-catalog-parity`: provider-owned model evidence during updates.

### Modified Capabilities
- None; this delta supersedes the official-model comparison requirement only
  for the explicit custom-catalog path.

## Impact

Parity preparation/evaluation/evidence, isolated tests, and public usage docs.
No new dependency, command-line flag, live installation, cache mutation,
provider request, release, or change to installer/profile capture sequencing.

Latest correction: absent default caches use complete offline catalogs exported by the corresponding runtimes, with durable source provenance and transactional snapshots. Present invalid caches still fail.

Lifecycle correction: a caller may recapture its final configuration after an
update. Capture continues to invalidate prior evidence. A subsequent same-binary
rebind must use independently resolved profile homes, support an absent official
runtime configuration, and produce evidence that survives the following switch.
The switch must retain the prepared profile/runtime projection rather than
silently regenerating different bytes. Existing model applicability and runtime
compatibility checks remain mandatory.
