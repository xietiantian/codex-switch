## Why

Current Desktop and candidate runtimes differ in seven feature states, five
image-reference request schemas and one candidate-only request. The supported
core coding contract can retain strict checks while explicitly classifying
reviewed extensions, instead of rejecting every new difference or emulating
unsupported media capabilities.

## What Changes

- Classify only the seven reviewed feature-state pairs, escalating observed
  dependencies and rejecting unexpected activation/metadata changes.
- Bind five image file-reference extensions to exact normalized method-schema
  pairs and prove their remaining URL/common request shape is compatible.
- Classify only the exact candidate-only thread/rollback client request.
- Refresh the versioned core acceptance contract and policy, retain required
  probes, preserve source/binary identity checks and atomic promotion.

## Capabilities

### New Capabilities
- `current-runtime-extensions`: evidence-bound optional runtime differences.

### Modified Capabilities
- None; this delta extends the existing core parity policy without removing it.

## Impact

Parity policy, public-seam tests/fixtures, documented acceptance evidence and
public usage guidance. No file-ID resolver, dependency, live Desktop mutation,
provider credential reuse or setup bypass is included.
