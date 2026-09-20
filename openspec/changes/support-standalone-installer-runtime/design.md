## Target State and Completion Contract

A staged update using either a legacy direct executable or a recognized
standalone package remains executable after scratch cleanup. The complete
runtime is validated before selection. Invalid assets, failed installation,
failed promotion, and interrupted recovery retain the last-known-good command.
No user configuration, credentials, shell profiles, installed runtime, or
Desktop process is used or changed by verification.

## Scope and Authority

The requester authorized this generic compatibility repair, isolated testing,
and a pull request. Source, tests, public documentation, this change, ledger,
and workflow evidence are in scope. Installing the result on a workstation,
release publication, unrelated cleanup, new dependencies, and broad workflow
changes are excluded. The approved test seams are the installer/materializer helper CLIs,
generated executable, and existing transactional promotion API. All tests use
synthetic credentials, local installer fixtures, and temporary stores.

## Capability Evidence

- authoritative_current: public SDGLBL/codex release install.sh, inspected
  2026-09-20; CODEX_NON_INTERACTIVE suppresses yes/no prompts; standalone
  packages contain bin/codex, bin/codex-code-mode-host, and codex-path/rg.
- local_scan: codex_env_setup isolates then removes installer scratch;
  candidate validation requires a regular executable. Existing update tests
  produce only direct-file candidates.
- comparison: disabling prompts alone leaves broken symlinks; copying only
  codex loses companion assets; retaining installer HOME retains private state.
- assumptions: only explicitly recognized layouts are imported. Unknown
  layouts fail with a phase-specific error rather than guessing.
- contract: delta scenarios and commands in tasks.md.

## Architecture Decisions

1. Keep private installer HOME/CODEX_HOME and cleanup on all exits. Track and
   reap both installer and materializer children on signals. Materialize
   runtime output before cleanup and set CODEX_NON_INTERACTIVE=1 for the child.
2. Add one Python helper for package validation, materialization, and launcher
   generation. Recognize bin-based standalone and legacy standalone layouts
   under the exact private releases directory. Reject foreign links and
   unexpected top-level content. Copy runtime files only, preserving layout.
3. Store immutable generations beside the bound command in a private
   `.codex-internal-runtimes/<digest>` directory. Register task-owned staging
   before writing it. Retain unselected generations; this repair does not
   garbage-collect old or unknown state.
4. Sign actual executable assets before fingerprinting. A regular launcher
   embeds the absolute generation path and complete manifest. Every invocation
   verifies file set, types, modes, sizes, and streaming SHA-256 before exec;
   arguments, exit behavior, HOME, and CODEX_HOME are inherited unchanged.
   Preserve the stable command as native argv[0] for process attestation. Flush
   generation files and directory entries before publishing a durable launcher.
5. Reuse the existing regular-file executable swap. Its digest binds the
   launcher and embedded package identity, and existing version/parity/final
   probes execute the validating launcher. This avoids introducing a second
   activation pointer or changing recovery marker schemas. Legacy binaries and
   their rollback records continue to work.
6. Never sign the generated launcher as though it were a native executable.
   Preserve the old signing path for legacy direct-file installers.

## Critical Path and Capability Slices

1. Reproduce standalone cleanup failure at the helper CLI; implement complete
   materialization and noninteractive installation.
2. Cover unsafe outputs, tampering, legacy behavior, argument/environment
   forwarding, and package reuse; repair findings within the selected seam.
3. Verify transactional selection and rollback through existing APIs, package
   inclusion, focused and broad regressions; review and submit the public diff.

## Skill Routing Ledger

- kind: bug-fix; workflow mode: Full OpenSpec; artifact-status: final.
- capability-research: used; public installer and source/test contracts.
- decision-resolution: used; approved generic package-preserving repair.
- decision-grilling: skipped; no unresolved product decision.
- implementation-planning: used; ai-native-tech-plan and change-plan.
- architecture-guidance: used; codebase-design, one materialization interface.
- test-first-execution / root-cause-diagnosis: used; TDD at the approved CLI seam.
- domain-language-modeling: skipped; existing package/update terminology.
- openspec-routing: used; this change is the execution source.
- implementation_readiness: not required; no external implementation provider.
- project-refresh-impact: not applicable; no DevFlow/skill behavior changes.

## Execution and Generated Artifact Strategy

Single implementation owner, no delegated production writes. Continue through
dependency-ready tasks, verification, review, commit, fork/push, and PR without
routine phase confirmation. No release or live install is authorized.
Disposable test output is owned by unittest fixture lifetimes beneath isolated
temporary roots; stdout reports are retained under ignored verification/local.
Each fixture registers its root at setUp before invoking commands and cleans
only that root after owner exit. Runtime materialization stages have similarly
bounded ownership; published generations are retained, not recursively purged.
Do not infer cleanup authority for pre-existing user state.

## Risks, Rollback, and Review

Whole-package validation adds streaming file reads to startup; correctness and
tamper detection take priority over unverified caches. The launcher depends on
the same available Python runtime already required by codex-switch. Package
bytes and credentials must never share a lifetime or directory. Test foreign
links, missing resources, and a changed generation before activation.
Existing executable-swap rollback selects the original command and package.
Review specifically for package completeness, credential leakage, path escape,
read/write races, and full versus CLI-only promotion behavior.

The incidental finding budget is one bounded repair within the selected seam;
unrelated defects are recorded without expansion. New public commands,
production dependencies, or destructive/live effects require renewed authority.
