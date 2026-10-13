# Runtime parity evidence

## Current default-difference follow-up

Requester approved supported default differences, self-test and existing PR delivery. Baseline c0ebda44dea6. Reuse this unarchived change, tasks 9–13. The duplicate draft was folded back before implementation. Final policy-10 acceptance and delivery are recorded below; historical counts remain historical.

## Historical policy-7 authority

The requester approved the reviewed system-proxy retry exception and two protocol-test startup-budget adjustments. Policy 7 preserves candidate-request, core-dependency, unknown-definition and removed-feature guards. No live runtime/configuration change or Git publication.

## RED/GREEN

Before implementation, the new public policy test fails: missing system_proxy_fallback returns parity.feature.unclassified_drift for the reviewed reference. After implementation, fresh policy/protocol/staged/native regression passes all 187 tests (93.456s). The native fixture remains independent and contains no caller-specific smoke or fault behavior.

## Verification

Fresh focused and broad module checks: 187 passed. The two timing-sensitive tests were repeated three times (6 passed, 19.529s) concurrently with other verification activity. Production timeouts and descendant cleanup assertions are unchanged.

The exact release package upgrades from policy 6 to policy 7 in an isolated home; 17 installed independent runtime tests pass (16.365s), manifests validate before and after, and no Python bytecode remains. Strict OpenSpec and diff checks pass. Source hashes match the packaged allowlist.

Read-only Spec review found no code blocker or caller-specific responsibility; its three stale-document findings were corrected (README policy number, current state summary, historical rollback wording). Log: .planning/verification/optional-runtime-parity-20260928.log. The previous policy-6 rollback and pre-rollback policy-7 suite results remain historical and are not current acceptance evidence.

Final read-only Spec recheck confirms the three documentation corrections and current completion records are consistent, with no caller-specific evidence.

## Default-only RED/GREEN

The current instant_interrupt public-policy test first failed with unclassified_drift. The same test passes under the generic default-only rule. Boundary coverage includes reversed defaults, unknown/namespaced names, all active stages, both effective overrides, missing/inactive/stage drift, observed/requested features, multi_agent_v2, item_ids and a simultaneous core protocol failure.

## Default-only independent review

Standards: no violations or required maintenance findings. Spec: one issue was
found where a nonmatching reviewed pair could enter the generic rule; exclusion
of reviewed feature names and four public negative cases resolve it. Both axes
rechecked the correction and owned-copy native signing repair with zero open
findings. Fresh public setup/current regression passes 20 tests. Final broad/native and exact-package acceptance are recorded below.

## Final default-difference verification and delivery

Implementation: 6afbfded6575a1694191780ab12e2ce1d041eab7, pushed to the
existing fix/standalone-installer-runtime PR fork branch; remote SHA read back.

- Final complete discovery: 1,303 tests, OK with 14 conditional skips
  (1247.424 seconds). The skipped cases were explicitly executed: 12 native
  core/typed-v2/staged tests (151.173 seconds), 1 native lifecycle test
  (50.028 seconds), and 1 Python 3.9 transaction test (0.193 seconds), all pass.
- Explicit profile suite: 227 passed (400.278 seconds).
- Exact committed package upgrades an isolated policy-9 installation to policy
  10; 44 installed tests pass (39.814 seconds). Manifests validate after tests,
  with no bytecode residue. Archive SHA-256:
  fa646f3e0c79a2ffcdfb82894d30c891fd6095b455f3314e099dbbf6b10ee53a.
- Actual Desktop 26.1007.21159 / CLI 0.162.0-alpha.17.2 and internal 0.162.1
  complete activation with default-difference warning, healthy managed launcher
  and provider-backed CLI execution. The original Desktop binary stays intact.
  Caller orchestration/configuration and private logs remain outside this repo.
- Strict OpenSpec, shell syntax, diff checks and both independent reviews pass.
  No unresolved findings, release publication, archive or caller-specific code.

Superseded failing/partial runs remain diagnostic evidence, not final acceptance.
The duplicate draft change was removed before implementation; all work remains
in this existing unarchived change.
