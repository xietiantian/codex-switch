## Why

Internal updates with an explicit model catalog currently require the official
model cache and compare entries by model slug. A provider-defined model is not
the official model merely because its slug matches. Missing caches or unrelated
official metadata therefore block otherwise compatible candidates.

## What Changes

- Treat a validated explicit source catalog as provider-owned model evidence.
- Omit official-model metadata comparison and cache acquisition for that path,
  recording that comparison as not applicable.
- Preserve source validation, overlay provenance, runtime protocol and feature
  checks, bounded behavior probes, and transactional revalidation.
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
