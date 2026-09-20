# Standalone runtime compatibility verification

## Claim

The repair preserves recognized standalone packages after private installer
cleanup, retains the stable command and native process identity, and uses the
existing full/CLI-only transactions. All required isolated verification and
review checks passed. The repair is submitted for upstream review.

## Contract and environment

- Baseline: main at 04374d3, release 0.1.15.
- Execution: openspec/changes/support-standalone-installer-runtime/tasks.md.
- Seams: installer/materializer CLIs, generated executable, transaction API.
- Host: macOS Apple Silicon, Python 3.14; optional native test compiler present.
- All checks use owned temporary HOME, CODEX_HOME, stores, and output roots.
  Live application/package-manager actions are guarded in the suite harness.
- Workflow tooling is checkout-local; no global activation or live installation.
- Public installer metadata was read through authenticated GitHub transport
  after anonymous API access returned 403; native assets and checksums remain
  the public 0.155.0 release assets. No provider request or Desktop task ran.

## TDD and regression evidence

- RED: the original helper rejected the symlink candidate after deleting its
  package: `Internal candidate must be a non-empty regular executable`.
- GREEN: complete package remains executable after installer scratch removal;
  legacy isolation, signing order, wrong-version and failure-code tests pass.
- RED/GREEN: directory-valued package metadata was accepted, then rejected.
- Review RED/GREEN: nested private files were retained and materialization TERM
  returned 0; both now fail safely. Independent HUP/INT/TERM probes return
  129/130/143 and leave no scratch/staging directory.
- Native argv[0] mutation test: restoring generation-path identity causes all
  three absolute/relative/PATH assertions to fail. Correct code passes, including
  spaces and quotes. The macOS fixture compiles a tiny native program because
  protected Apple system executables cannot be relocated on every host.

## Commands and results

| Check | Result | Evidence |
|---|---|---|
| `python3 -B scripts/test_codex_update_release.py CodexStandaloneRuntimeTests` | PASS, 17 tests | `.planning/verification/local/standalone-final.log` |
| `python3 -B scripts/test_codex_update_release.py` | PASS, 196 tests | `.planning/verification/local/update-release-verified.log` |
| `python3 -B scripts/test_codex_transaction.py` | PASS, 258 tests, 1 existing skip | `.planning/verification/local/transaction.log` |
| `python3 -B scripts/test_codex_profile_switch.py` | PASS, 227 tests | `.planning/verification/local/profile-wrapper.log` |
| Public Codex 0.155.0 native installer smoke | PASS: install, post-cleanup version, app-server schema, rg | `.planning/verification/local/native-smoke.log` |
| Fresh release package build and validation | PASS; materializer included and runnable | `.planning/verification/local/package-final.log` |
| Shell syntax, all Python script compilation, diff whitespace | PASS | Fresh commands in this checkout |
| `openspec validate --all --strict --no-interactive` | PASS, 23 items | `.planning/verification/local/openspec.log` |
| Workflow state and routed dependency checks | PASS; existing guidance recommendation only | Checkout-local reports |

Disposable logs are intentionally ignored; results above are the durable record.
The transaction suite's initial outer temp path exceeded a Unix socket path
limit; shortening the isolated root resolved it. Initial concurrent update
validation hit one existing one-second smoke timeout and two stale bootstrap
hashes. The timeout passed on rerun; both trust hashes now bind the changed
packaging validator and their focused regressions pass. An intermediate native
fixture was corrected for macOS protected system binaries and canonical temp
paths; the fresh 196-test run passes with the final fixture.

## Review

Separate Standards and Spec reviews found three bounded issues: cancellation
handling, nested private-state retention, and native process identity. All were
fixed, tested, and closed by read-only re-review. Package files and directories
are flushed before the durable launcher is written. The review found no new
outstanding issue within the repair scope. Public additions were checked for
private identifiers, workstation paths, credentials, and unrelated workflows.
Only this repository's own planning conventions are included.

## Risks and limits

- Whole-package verification reads runtime bytes on each invocation.
- Published generations are retained; automatic garbage collection is excluded.
- Unknown layouts fail closed. Recognized package, raw, and legacy layouts
  have fixture coverage; native smoke covers the published macOS raw layout.
- Tests do not attest a live Desktop or make provider-backed requests.
- Runtime launcher uses the Python interpreter chosen during preparation.
- No release or workstation installation is part of this pull request.

## Knowledge and delivery

README documents the user-visible installer/launcher contract; no separate
knowledge asset is needed. The OpenSpec change stays active for PR review.
Delivery URL: https://github.com/cYz26/codex-switch/pull/1
Implementation commit: `b1ef95e`. Delivery-record updates do not change runtime code.
