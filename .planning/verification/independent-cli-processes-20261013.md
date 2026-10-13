# Independent CLI process diagnostics (2026-10-13)

Change: filter-app-server-process-diagnostics, follow-up tasks 4–7.
Baseline: d7efb37724e1778551668c592ee4f655f50e9916.

Read-only installed verify reproduced exit 1 with
attestation.app_server.backend_mismatch and attestation.internal.proxy_bypass.
Parity remained healthy. Both observed app-server processes belonged to an
independent terminal CLI, not Desktop. An in-memory diagnostic exclusion removed
both findings; no files/processes were changed by that comparison.

Source implementation is complete. Evidence uses the public process snapshot ->
observation -> attestation seam. All original review findings are closed:
unknown/truncated CLI options, proxy filename in prompts, Python option/code/module
ambiguity, and ordinary TUI initial prompts. Unknown ancestry remains visible.

- Original RED reproduces the same two attestation codes. Each review correction
  has a failing public test followed by GREEN in the registered evidence root.
- Broad runtime/verify/profile regression: 361 passed (485.104 s) on the initial
  implementation. Later parser fixes have fresh full runtime/verify coverage:
  138 passed (137.059 s). The profile suite contributes 227 broad tests; its five
  process/parser tests are also rerun on the final installed package.
- Final isolated installed package: 23 ownership/attestation tests and five
  profile process/parser tests pass. Packaged current-machine verify exits 0
  with healthy parity; no process exclusion was injected into that command.
- Source current-machine verify also exits 0. Source doctor sees only the expected
  source-directory versus installed immutable shim-directory mismatch. Baseline
  installed doctor reproduces the two original attestation errors, with no shim
  mismatch. Final installed doctor acceptance is pending the normal setup path.
- Strict change validation, shell syntax, AST, diff checks and independent Spec
  and Standards reviews pass. Generated evidence is retained with no auto cleanup.
- Tested archive SHA-256:
  `7266812d8057fe302cfd495b0a4ac38defbf54dcb9add5044fde3cf1b8fa1c35`.

Next: commit the tested source, package the exact commit, run authorized consumer
acceptance using that package and push the existing PR branch. No release/archive.
