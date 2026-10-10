# Supported default differences verification (2026-10-10)

Existing change: allow-optional-setup-parity-differences, tasks 9–13.
Baseline: c0ebda44dea68ee6428b09009964f0da180cdc25.

Policy 10 permits supported default-only differences with explicit diagnostics,
without writing configuration or synchronizing accepted defaults. Known exact
reviewed pairs, core/item-ID rules, explicit requests, observed dependencies,
inactive/stage/missing/override differences and protocol failures retain their
existing checks.

RED: instant_interrupt official default/effective false versus internal true
was unclassified. GREEN: initial setup suite 9, expanded setup/current 20 pass.
Spec review found a reviewed-pair fallback gap; existing current-parity tests
reproduced 3 failures. Excluding reviewed names and adding negative cases fixed
it. Spec and Standards rechecks have zero remaining findings.

The original native fixture fails after copying the signed packaged Desktop
CLI outside its bundle (exit -9 during version probing, before policy). It now
re-signs only the owned temporary copy and retains original runtime/App state
checks. This is a test fixture correction, not a production signing change.

Final checks pass as recorded below.
The superseded discovery run, which includes the 3 fixed failures, is retained
as partial historical evidence. Its completion is not claimed.

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
