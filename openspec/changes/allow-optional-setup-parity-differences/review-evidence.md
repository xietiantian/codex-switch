# Runtime parity evidence

## Current authority

The requester approved the reviewed system-proxy retry exception and two protocol-test startup-budget adjustments. Policy 7 preserves candidate-request, core-dependency, unknown-definition and removed-feature guards. No live runtime/configuration change or Git publication.

## RED/GREEN

Before implementation, the new public policy test fails: missing system_proxy_fallback returns parity.feature.unclassified_drift for the reviewed reference. After implementation, fresh policy/protocol/staged/native regression passes all 187 tests (93.456s). The native fixture remains independent and contains no caller-specific smoke or fault behavior.

## Verification

Fresh focused and broad module checks: 187 passed. The two timing-sensitive tests were repeated three times (6 passed, 19.529s) concurrently with other verification activity. Production timeouts and descendant cleanup assertions are unchanged.

The exact release package upgrades from policy 6 to policy 7 in an isolated home; 17 installed independent runtime tests pass (16.365s), manifests validate before and after, and no Python bytecode remains. Strict OpenSpec and diff checks pass. Source hashes match the packaged allowlist.

Read-only Spec review found no code blocker or caller-specific responsibility; its three stale-document findings were corrected (README policy number, current state summary, historical rollback wording). Log: .planning/verification/optional-runtime-parity-20260928.log. The previous policy-6 rollback and pre-rollback policy-7 suite results remain historical and are not current acceptance evidence.

Final read-only Spec recheck confirms the three documentation corrections and current completion records are consistent, with no caller-specific evidence.
