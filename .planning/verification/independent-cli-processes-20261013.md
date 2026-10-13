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
  mismatch. The normal setup path subsequently regenerated the installed shim and passed
  installed doctor, verify and status (all exit 0).
- Strict change validation, shell syntax, AST, diff checks and independent Spec
  and Standards reviews pass. Generated evidence is retained with no auto cleanup.
- Tested archive SHA-256:
  `7266812d8057fe302cfd495b0a4ac38defbf54dcb9add5044fde3cf1b8fa1c35`.

## Exact-commit acceptance and delivery

Implementation `937bb05f3ff23c2a031934c060d8b8705cabc3f2` is committed and
pushed to the existing `fix/standalone-installer-runtime` PR fork branch.
Packaging the exact commit reproduced the tested archive digest above.

The original consumer setup entry was updated to this exact package and run in
full with only `--non-interactive`: 62.7 seconds, exit 0. The update journal is
complete with runtime ownership handed off. The actual managed App launcher
handshake and provider-backed `codex exec` smoke passed. Internal CLI remains
0.162.1; no CLI version lock was added. The installed Desktop binary was unchanged
and existing terminal CLI daemon processes were not stopped. Fresh installed
`verify internal --repair=none`, `doctor`, and `status` all exit 0. Status still
reports the pre-existing caller PATH order advisory; it is not an attestation
failure. Caller-specific orchestration, private backups and logs stay outside
this repository.

The broader OpenSpec sweep initially found an existing default-difference
requirement paragraph extraction error (SHALL on a continuation line). A
format-only sentence reflow fixes it without changing policy. All 31 items now
pass strict validation. This is the only incidental scope addition.

Final tracked follow-up records this evidence and the paragraph format correction;
runtime/package content remains identical. No release publication or archive.
