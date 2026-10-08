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
