## Goal Contract

After a successful internal bind, ordinary Desktop bookkeeping and valid user configuration edits must not be rejected solely because config.toml bytes changed. Installation and launch use the same compatibility policy. Invalid configuration, unsupported required capabilities and damaged immutable artifacts still fail with specific diagnostics.

Approval: requester confirmed the proposed repair on 2026-09-29. No unresolved product decision remains. No new dependency, live installation, account mutation, release, Git publication or legacy-feature classification change is authorized.

Subsequent scope decision: newer Desktop packaging and additional feature/protocol adaptation are excluded. Keep this repair verifiable against its existing reference version; do not change discovery or classify those new differences in this change.

## Skill Routing Ledger

- artifact-status: final
- capability-research: used; actual launch stderr and source trace establish the local pre-initialize failure.
- decision-resolution: used; requester approved shared installation/startup rules.
- decision-grilling: skipped; no unresolved decision.
- implementation-planning: used; this contract and tasks own execution.
- architecture-guidance: used; reuse existing preparation/transaction rather than duplicate policy.
- domain-language-modeling: skipped; existing generation and receipt concepts suffice.
- openspec-routing: used; Full OpenSpec behavior repair.

## Architecture

Policy 8 records a versioned semantic runtime digest alongside existing byte digests. Parse TOML and canonicalize values; comments and ordering do not affect compatibility. Exclude only reviewed bookkeeping (marketplaces/plugins/skills, TUI/history, hooks.state and per-project trust_level). Preserve all unknown and runtime-affecting settings in the digest. Legacy receipts remain readable; changed legacy inputs require fresh preparation.

Read-only generation validation never refreshes state. Launch validates immutable artifacts and refreshes changed input before home sync, then rechecks reusable evidence after synchronization. If compatibility inputs changed, a launch-specific operation captures the current runtime/config/auth in a private home and invokes existing cmd_set_bin private preparation, schema/feature policy, native probes and atomic promotion. Frozen source/config/auth checks prevent overwriting concurrent edits. Failure preserves the current user input and previous generation; stderr explains preparation failure. Concurrent startup may fail safely on a changed snapshot and retry normally. Full revalidation can take the same time as initial preparation; unchanged/bookkeeping-only starts do not make provider calls.

The generated wrapper requests authoritative-runtime synchronization: continue shared support/usage synchronization but preserve runtime config/auth and do not merge missing shared-home/profile/plugin-support settings back into the prepared runtime. Private captured final seeds are also authoritative during preparation. Keep legacy wrapper behavior unchanged. Shell generation checks use the same refresh operation.

An explicit `verify-app-launch` executes the real launcher with its actual home and without the private rebind-smoke bypass. It performs initialize and plugin/list, makes no model turn, and reports failure. The existing isolated smoke contract remains unchanged.

## Validation / Completion

RED then GREEN tests cover bookkeeping edits through the real wrapper, semantic/runtime changes entering existing preparation, invalid/unknown changes failing, legacy receipts, immutable tampering, missing/deleted old snapshot settings, and the public launch-smoke option. Run runtime-binding/home-sync/parity/verify/transaction/profile regressions. Native loopback verification exercises successful launch and post-bind edits. Review along Spec and Standards axes. No claim of live Desktop UI acceptance.

## Ownership and Recovery

Root owns runtime modules, independent tests, OpenSpec/ledger and review evidence. Preserve earlier approved optional policy work. Reverting this source repair restores the prior validator; do not edit live receipts to simulate repair. Dependent callers own their own ordering and final verification adoption.

Review guards: retain actual official home in both launchers, bind the frozen manifest to the validated generation, and compare source config/auth before atomic promotion. Source and promotion paths are distinct.
