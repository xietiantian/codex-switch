# Current runtime optional extensions

Baseline: 59cfe33. Approved scope: exact optional classification plus isolated
core-path verification; delivery continues on PR #1. No live installation,
profile/configuration, Desktop process, or external provider was changed.

## Behavior and evidence

- Official reference 0.155.0-alpha.9.2 and candidate 0.155.0: complete normalized
  inventories contain 145/142 features and 257/258 protocol records. Current
  policy evaluates them healthy with exactly 13 visible optional queue entries.
- Five image file-reference extensions require exact method/direction/schema
  pairs and compatible common URL schemas. No payload conversion or data loss.
- Seven feature exceptions match both complete feature state records. Any
  changed state/presence or core dependence is rejected. Exact backend-only
  thread/rollback rejects every observed dependency, including unknown extension
  identifiers, and changed schema/direction/method.
- Policy 3 and acceptance trace official-desktop-core-v2 invalidate older
  policy receipts. Required probes, fingerprint checks and atomic promotion
  remain required. Current policy-2 receipt rejection/round-trip and failed
  preparation/probe/transaction regressions pass.
- Actual native app-server conversation passed initialize, collaborationMode/list,
  thread/start, turn/start and thread/read. One v2 explorer child is proven by
  subAgentActivity plus thread_spawn parent/path/role and matching completed
  child/parent turns. A separate temporary persistent-thread run also passed
  thread/resume. Both use an explicitly local deterministic Responses provider,
  generated test configuration and no credentials.
- This certifies the supported text/typed-agent core contract. It does not
  establish file-ID image, realtime, worktree-specific behavior, a live external
  provider or Desktop UI compatibility beyond the checked contracts.

## RED/GREEN

Public seams: inventory comparison, build_method_coverage, evaluate_parity_policy,
run_parity_probes, receipt serialization and preparation/transaction entrypoints.

1. Current fixture initially failed five uncovered image pairs, then seven
   features plus the extra request. Exact classifications and common proof pass.
2. Observed extension/feature use, stale schemas and unknown backend dependence
   fail. Review found an unknown extension ID could evade the backend request
   guard; its RED case now passes after checking every matching request dependency.
3. Native stdin EOF lost asynchronous responses. An asynchronous subprocess RED
   passes with ordered replies and session shutdown only after completion.
4. Native exec JSON omits typed source/role fields; native app-server fixture RED
   passes with thread/read evidence. Wrong role/parent/path/turn IDs, duplicate
   replies/spawns, failed turns and reordered markers remain rejected.
5. Review found partial JSONL tails, malformed scalar fields, and backpressured
   request writes. Real subprocess/runner RED cases now pass: only complete
   captured lines are consumed, malformed shapes produce structured failure,
   and nonblocking writes obey the same deadline and process-group cleanup.

## Verification

- 112 parity/current-policy cases passed, including 107 existing/extended parity
  cases and 5 current-inventory cases.
- Opt-in native regression passed against real 0.155.0. Exactly four loopback
  model requests exercised parent spawn/wait/completion and child completion.
- 227 profile cases passed.
- 258 transaction cases: 257 passed, one existing skip.
- Protocol/config and verify: 76 cases executed; one unrelated schema subprocess
  hit its existing three-second timeout under concurrent test load. Focused
  recheck passed in 0.645s; no production timeout was relaxed for it.
- Strict OpenSpec: 27 passed, zero failed. Python 3.9 grammar and diff checks pass.
- Spec review: zero open findings; async fragmentation and earlier backend
  dependency findings are closed after independent public-seam rechecks.
- Standards review: zero open findings; three P2s (partial lines, malformed
  scalars, write deadline) are closed after independent rechecks.
- Clean package verification is the remaining delivery check.

Reproducible commands (from scripts unless stated otherwise):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_codex_parity test_codex_current_parity -v
CODEX_SWITCH_TEST_BACKEND=/absolute/path/to/codex PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_codex_native_parity -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_codex_profile_switch -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_codex_transaction -v
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest test_codex_protocol_config test_codex_verify -v
```

Raw local logs use the registered current-extensions-* evidence prefix and are
not public artifacts. Sanitized runtime fixtures are under evals/fixtures.
The active OpenSpec task list owns delivery; no release or archive is claimed.
