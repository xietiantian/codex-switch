## Context

The installed Desktop CLI is 0.158.0-alpha.2.1; internal is 0.155.0.
The new shell entry simply execs its sibling nested app executable. Discovery
and OfficialReference independently require the retired flat path.

## Goals / Non-Goals

Goals: accept both verified layouts, keep one actual binary identity throughout
discovery/binding/parity/update, report invalid installed hosts accurately,
and validate the real current pair without weakening core or unknown checks.

Non-goals: application-specific setup/configuration, a new provider or dependency,
Desktop bundle modification, arbitrary executable fallback, release or archive.

## Decisions

- Keep resolution in runtime_binding, already shared by reference consumers.
  New layout uses the nested executable as canonical bundled_cli. Both the
  wrapper and nested executable must be regular executable bundle members.
  Selecting the actual executable preserves the existing single-binary receipt
  contract and prevents a stable wrapper from hiding a backend replacement.
- Prefer a present packaged layout; reject it if invalid rather than falling
  back to an old sibling. Legacy layout remains supported when packaged layout
  is absent. Reject symlinked members/parents within the bundle.
- Reference deserialization accepts the two defined executable layouts;
  runtime validation still requires the currently discovered bundle and binary.
  Stage snapshots also freeze the packaged entry wrapper when present.
- Invalid installed-host diagnostics fail before candidate download. Only a
  genuinely absent current host produces present=false. Preserve binding
  validation, frozen-input checks, and actionable error codes/paths.
- Current-runtime exceptions must be exact and evidence-based. Explicitly
  unsupported enabled features and unknown differences remain failures. Native
  core and typed-v2 probes plus actual managed-launch validation remain required.

## Completion Contract and Execution

Requester approved the preceding diagnosis, public discovery/reference/stage /
apply/binding/launch test seams, implementation and commit/push to the existing
PR branch. Execute dependency-ordered tasks in tasks.md without another approval
round. Owner is the main agent; write set is runtime_binding, parity, update,
env/status helpers, focused/native tests, release manifest/docs and this change.
New capability research findings are recorded here before implementation.

Validation: `python3 -B -m unittest discover -s scripts -p 'test_codex_*.py'`,
`python3 -B scripts/test_codex_profile_switch.py`, opted-in native preparation
using the actual installed bundle, packaged install/manifest verification,
`openspec validate support-packaged-desktop-cli --strict`, shell syntax and
`git diff --check`. Record RED/GREEN and separate Spec/Standards reviews.

## Risks / Trade-offs

- New Desktop behavior can expose additional incompatibility → classify from
  runtime/source evidence, retain core probes and fail on unsupported behavior.
- Desktop auto-update can race preparation → freeze actual executable and entry
  identity; require a new stage when changed.
- Existing receipts reference the prior layout → resolve current binding and
  regenerate stale evidence; do not rewrite receipt paths to certify it.

## Migration Plan

Build and verify an exact-commit package, then push the existing PR branch.
Consumers select that new package. Roll back source/package selection only;
do not overwrite the installed Desktop or user-owned configuration as rollback.

## Skill Routing Ledger

- artifact-status: final
- capability-research: used (local actual bundle, direct CLI and code evidence)
- decision-resolution: used (requester approved recommended repair)
- decision-grilling: skipped (scope and public test seams already accepted)
- implementation-planning: used (this design and tasks)
- architecture-guidance: used (single resolver and actual-executable identity)
- domain-language-modeling: skipped (existing binding/reference model sufficient)
- openspec-routing: used (this native change)

## Open Questions

None for layout repair. Record evidence and exact policy decisions before any
current-version compatibility change.

## Current Pair Research and Policy 9

Fresh native preparation reproduces six feature and five protocol differences.
Official tag `rust-v0.158.0-alpha.2.1` resolves to
`9cf62076c17c6a46b7d05dcdb7fe3ecc3c7a9c6f`. Read its features/src/lib.rs
(blob `5ea619d0ab8b7bc3f9e5d54dcd3acadd342cecb7`) and account gateway
processor (blob `c648eeea942acb944fc5add47d9b7b8f2d1be2f2`).

- agent_message_board: disabled development discussion tools; prefer_mxc:
  disabled native Windows sandbox preference; transcript_v2: deprecated ignored
  UI preference, disabled on both sides. Accept only the recorded default pairs.
- daemon_auto_start: starts the shared daemon for eligible interactive launches;
  direct managed app-server launch does not require that startup convenience.
  Accept the exact observed official-on/internal-absent default pair.
- guardian_reuse_parent_compaction and guardianv2.thread_context: optional
  Guardian review-context behavior, present but disabled by default in internal.
  Retain those supported internal defaults without claiming identical Guardian
  context; warn for exact pairs, reject explicit requested/observed dependence.
  Do not disable guardian_approval or alter user configuration.
- Four account/gatewayOAuth methods operate a separate gateway provider login.
  Classify exact official schema digests as optional only while unobserved and
  absent from internal; do not synthesize login success or credentials. Observed
  use and any schema change remain errors.
- MCP elicitation is actually backward compatible: internal userVerification
  payloads omit only an optional `_meta`. A schema subset checker incorrectly
  evaluates the base object and each union variant's required fields separately.
  Flatten only provably mergeable open-object intersections; retain all required
  fields/types and reject new required metadata. This is a checker correction,
  not a protocol bypass or adapter.

Fixture: evals/fixtures/packaged-desktop-extensions.json, exported from actual
binary schemas/features. Policy 9 invalidates older compatibility receipts.
Core/typed probes, unknown-difference rejection and negative tests remain gates.

## Full-suite baseline corrections

Two failures reproduce unchanged at baseline 42af1e820db9. Classify both as
CONTINUE_WITH_MINIMAL_GUARD under the authorized setup/profile repair.
The existing Python 3.11 minimum-version check executes after imports which now
require tomllib. Move that unchanged guard ahead of application imports in
codex_profile_switch.py; the existing direct old-interpreter test is RED and
must become GREEN without creating a store. No supported-interpreter behavior
changes. The private-seed rendering test retained an obsolete expectation that
an explicit replacement imports an old shared setting. Put the retained setting
in the new seed, add a separate stale shared marker, and assert that only the
new seed survives. No production configuration-merge behavior changes.
