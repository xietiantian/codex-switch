## Target and Boundary

The update preserves independent supported choices and treats only the approved optional differences as nonblocking. Runtime inventories and app-server schemas are collected from the actual binaries; reviewed policy determines compatibility. The public result transports findings without prescribing caller UI or orchestration.

## Decisions

1. apps, guardian_approval and prevent_idle_sleep require both runtimes to expose the same stage/default; effective boolean values may differ. Observed core dependencies and actual configuration errors remain blocking.
2. nonfatal_clock_read_errors is optional regardless of reference effective state. Candidate configuration requesting an absent feature remains invalid. Read the projected candidate configuration rather than inventing defaults.
3. Only missing client_request rollout/compress receives the maintenance warning. Do not invoke the RPC or synthesize success. Other unknown methods, schema differences and core dependencies retain their existing classification.
4. Preserve bounded, sanitized category/code/severity/message/expected/observed diagnostics in apply and durable status/replay. Compatibility failure has a specific reason; diagnostics cannot change transaction state.
5. Advance to policy 7. Missing internal system_proxy_fallback is optional only for official stable/default=true, either effective boolean, with no candidate request or core dependency. Report unavailable retry after eligible connection failures; actual failures still block. Removed-feature behavior is unchanged.

## Scope

Parity/update modules, their independent unit and native tests, README and this repository's planning/evidence records. Tests use isolated homes and a loopback provider. No dependent repository source, setup functions, authentication flow, configuration migration, translated presentation or caller test results belong in this change.

## Validation

- `PYTHONPATH=scripts python3 -B -m unittest test_codex_setup_parity test_codex_current_parity`
- Public staged apply/status/replay success and failure diagnostic tests.
- Optional real-CLI probes with CODEX_SWITCH_TEST_BACKEND and isolated fixtures.
- Strict OpenSpec validation and git diff --check.

The two timing-sensitive protocol tests use 5-second startup budgets instead of 1 second. Their child-process cleanup assertions remain; production timeouts do not change. Repeat the tests with concurrent test activity.

## Recovery

Historical user-directed rollback restored policy-6 code and tests from a hash-verified local snapshot before the follow-ups were approved. Preserve existing unrelated work and evidence. The native fixture has no dependency on an external caller's installation or smoke-test behavior. No live runtime rollback is required because no live installation occurred.

## Default-only difference follow-up (2026-10-10)

### Context

See this change proposal. Feature inventories already separate isolated defaults from
effective configuration. Policy 9 compares the whole record and rejects an
otherwise unclassified difference. The actual current pair differs only in
instant_interrupt; protocol comparison has no blocking findings.

The internal 0.162.1 source at codex-rs/features/src/lib.rs describes the flag
as preempting responses and yielding foreground code-mode observations on new
user input. core/tests/suite/pending_input.rs covers response interruption,
retry interruption and yielding exec/wait without terminating the code cell.
Allowing defaults does not certify identical interruption behavior.

### Goals / Non-Goals

Use existing inventories and public policy evaluation. Preserve specialized
compatibility classifications and live configuration ownership. Do not add an
instant_interrupt allowlist, force matching settings, relax protocol checking,
change persistence schema, modify Desktop, or introduce caller-specific code.

### Decisions

1. Add a generic classification immediately before the unclassified-feature
   fallback, after all specialized rules, excluding names governed by exact
   reviewed pairs even when those pairs do not match. Independent supported-choice
   features can qualify for either their effective-choice or default-only rule. It requires both records, identical
   active stages (stable, experimental or under development), different
   isolated defaults, and effective values equal to each side's own default.
2. Observed dependencies or explicitly requested internal enabled features
   cannot use this classification. Existing core and item_ids rules execute
   first. Unknown missing features, removed/deprecated stages and configuration
   overrides retain the previous result.
3. Report parity.feature.default_difference with warning severity, the feature
   name, stage, each default and each effective value. Do not enqueue settings
   synchronization: independent defaults are accepted, not work to overwrite.
4. Advance policy to 10 so old receipts are not reused under changed semantics.
   Existing receipt preparation/migration supplies fresh evidence.

The chosen systemic repair covers future supported defaults. A single-name
exception would reproduce the problem on the next feature. Globally ignoring
feature drift would also accept missing capabilities and explicit dependencies.

### Completion Contract and Execution

Requester approved default-difference repair, continued self-test, existing PR
commit/push and the downstream test-entry refresh. Baseline c0ebda44dea6.
Owner: main agent. Execution queue: tasks.md, tasks 9–13. Write set: parity implementation,
public parity tests, README, this change, TASK_LEDGER.md, workflow state and
verification records. Public seams are the already approved inventory/policy,
native preparation, stage/apply and managed-launch interfaces. No new test seam
or dependency is needed. Review uses the repository Spec and Standards axes.

Validation: RED/GREEN public regression; full test_codex_*.py discovery;
test_codex_profile_switch.py; opt-in native current runtime tests; exact package
build/install/manifest checks; strict OpenSpec and git diff --check; actual
unskipped downstream setup and final CLI execution. Keep downstream-specific
logs outside this repository and summarize only generic runtime evidence here.

### Risks / Trade-offs

- Default drift changes behavior: report it explicitly, retain native probes,
  explicit-dependency rejection and all protocol checks.
- Known specialized features need stronger evidence: specialized rules run
  first and regression proves this general rule cannot bypass them.
- Desktop can update during testing: record actual identities and let existing
  staging/receipt checks reject stale evidence.

### Migration Plan

Build and validate the exact committed package, refresh the existing test entry,
run real acceptance, then push the existing PR branch. Rollback selects the prior
package; normal failed-apply rollback owns live runtime/config restoration.
No release or archive is requested.

### Skill Routing Ledger

- artifact-status: final
- capability-research: used (actual inventories and versioned upstream source)
- decision-resolution: used (requester explicitly approved default differences)
- decision-grilling: skipped (scope and existing public seams are resolved)
- implementation-planning: used (this design and tasks)
- architecture-guidance: used (existing public evaluator, specialized precedence)
- domain-language-modeling: skipped (existing default/effective records suffice)
- openspec-routing: used (native propose/apply with retained OpenSpec 1.7)

### Open Questions

None for this bounded repair.

### Native fixture signing correction

The unchanged native staged-update fixture copies the signed Desktop CLI out of
its nested bundle; macOS terminates that detached copy with exit -9 before any
parity evaluation. Re-sign only the owned fixture copy ad hoc, retaining source
App identity checks before/after. Native probes must then execute normally; no
installed App or production signing path changes. This bounded test-fixture
correction is CONTINUE_WITH_MINIMAL_GUARD, with the original failing run retained.
