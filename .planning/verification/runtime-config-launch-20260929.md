# Runtime configuration launch repair verification

Scope: `revalidate-runtime-config-at-launch`. Source repair and isolated fake/loopback verification only. Previous optional feature/protocol policy remains intact. New Desktop packaging and capability adaptation is excluded by the requester.

## Final passing checks

| Check | Result |
| --- | --- |
| Profile/Wrapper complete module | 227 passed, 361.970s |
| Update/Release complete module | 200 passed, 523.385s |
| Semantic/launch/concurrency/deadline boundaries | 10 passed, 12.780s |
| Release bundle focused checks | 40 passed, 39.628s |
| Exact installed runtime, including native loopback and old-package upgrade | 26 passed, 25.650s; manifests valid before/after; no bytecode |
| Strict OpenSpec, shell syntax, diff check | passed |

Fresh final commands:

```bash
PYTHONPATH=scripts python3 -B -m unittest test_codex_profile_switch
PYTHONPATH=scripts python3 -B -m unittest test_codex_update_release
PYTHONPATH=scripts python3 -B -m unittest \
  test_codex_runtime_config \
  test_codex_runtime_binding.RuntimeBindingTests.test_internal_launch_accepts_bookkeeping_without_restoring_removed_market \
  test_codex_runtime_binding.RuntimeBindingTests.test_changed_runtime_uses_existing_preparation_and_preserves_failed_input \
  test_codex_runtime_binding.RuntimeBindingTests.test_runtime_refresh_rejects_manifest_changed_after_artifact_validation \
  test_codex_parity.ParityProbeTests.test_backpressured_large_request_obeys_overall_deadline
openspec validate revalidate-runtime-config-at-launch --strict --no-interactive
bash -n scripts/codex-switch
git diff --check
```

Generic source logs are retained under `runtime-config-launch-20260929/`, with pre-copy ownership/hash records under `.planning/devflow/generated-artifacts/`. The validated archive SHA-256 is `ddabbc068b0a1d0ba8ee27098efab9c918b8fddb182cead09b6907d823ab6ff0`, policy 8. Test doubles run under temporary homes; native probes use real backends with a local synthetic model service. This is not live Desktop UI or remote-provider acceptance.

## Earlier failures and closure

The initial broad invocation ran 562 real cases (561 passed, one existing skip) but also selected a nonexistent module. The bad selector produced an additional unittest loader error; it was not a runtime failure. The actual release module is covered by the fresh complete 200-case run above.

An attempted expansion of release required-path metadata rejected the old installer and broke old package fixtures. That change was fully removed: the required-path contract is unchanged, while the new module is packaged by the existing file manifest and checked through its parity import. Fresh full profile/release runs and exact installed upgrade pass.

One of 241 focused cases timed out under concurrent load before its fixture marker. The unchanged deadline test passed in isolation and again in the final ten-case suite; no production timeout or test budget was relaxed. The failed attempt is retained, not presented as a clean run.

## Review and completion

Spec review closed stale snapshot restoration and auth deletion. Standards review closed original official-home routing, validated-generation snapshot races, bounded manifest reads and same-home configuration overwrite. Final read-only reviews report no remaining blocker.

Successful tests prove evidence reuse for reviewed bookkeeping edits, same initial compatibility policy on runtime edits, safe failure for unsupported/invalid inputs and integrity damage, preservation of current config/auth, and the public actual-launch verification entry. All active tasks are complete. No live installation, configuration/account mutation, Desktop activation, commit, push, release, archive or history rewrite occurred.
