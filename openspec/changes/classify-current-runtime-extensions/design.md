## Contract

Baseline 59cfe33. The requester approved precise optional classification plus
core-path validation and continuing the existing PR with isolated verification.
The primary agent owns parity source, its tests and public schema fixtures,
README.md, this OpenSpec change, TASK_LEDGER.md, STATE.md and verification.
Reviewers are read-only. No live installation/profile/configuration or external
provider interaction is required. All native probes use temporary homes and,
where model responses are needed, an explicitly local deterministic provider.

## Research and Decisions

Current observed versions: official 0.155.0-alpha.9.2, candidate 0.155.0.
Seven current feature errors are analytics_plan_history, guardian_ext,
personality, realtime_conversation, send_message_to_user_async, use_xaa and
worktrees. The first, fifth and sixth are absent internally and default off
on the reference. guardian_ext has equal disabled behavior with stage drift.
The remaining three are presentation/realtime/worktree extensions outside the
local core coding acceptance contract. Their exact default/effective-state
pairs are reviewed for that contract; unexpected state changes or observed
core dependence remain unhealthy. No user configuration is silently aligned.

The five request differences (thread/queue/add, thread/queue/update,
thread/resume, turn/start, turn/steer) share one extension: image fileId/file_id
references in addition to URL references. Restricting only those schema
branches to URL form makes every common method schema compatible. Add exact
pair records naming image_file_reference. As additional evidence, coverage
construction verifies that the bounded schema projection leaves a compatible
common contract. This is schema evidence only: production payloads are neither
rewritten nor stripped, and file-ID image support is not claimed.

thread/rollback exists only as a candidate client request. Classify that exact
direction/method/schema as a backend extension absent from the supported
reference contract, retain a deterministic warning, and reject changed schemas,
other directions or an observed dependence. Do not broadly permit unknown
backend messages or candidate-only methods.

Existing original-design optional extensions establish that unsupported media
outside the supported core path need not be emulated. A new versioned core
acceptance contract records the tested text/local coding methods and required
multi_agent_v2 dependency. Both eligibility and final evaluation consume it;
new observed optional feature/extension use must fail. Policy version changes
invalidate prior receipts. Actual transformations and their existing evidence
remain untouched; optional classification never substitutes an adapter digest.

## Completion Criteria and Sequence

1. Capture sanitized current normalized schema/feature fixtures and reproduce
   the thirteen failures through compare/build/evaluate public interfaces.
2. Implement exact image-extension coverage and prove common URL compatibility;
   test observed extension, unrelated schema change and missing coverage failure.
3. Implement exact feature and candidate-only request classification; test
   changed activation/stage/default, observed dependencies and wrong direction.
4. Validate the refreshed core contract in isolated native runtime tests; retain
   required core_protocol and typed_subagent_v2 promotion guards. Deterministic
   provider-boundary fixtures do not certify a live provider or Desktop UI.
5. Run full parity and relevant profile/transaction regressions, strict OpenSpec,
   Python compatibility, clean package/installed-module checks and two independent
   review axes. Commit/push to PR #1 and refresh its verified local test package.

Public test seams: existing inventory comparison, build_method_coverage,
evaluate_parity_policy, preparation/probes, receipt serialization and CLI
entrypoints. Native fixture provenance uses version/schema digests, never local
credentials, private paths or business metadata. Primary owns generated logs
under .planning/devflow/verification/local/current-extensions-* and each test's
new TemporaryDirectory root; preserve other artifacts.

## Risks and Boundaries

A passing text/core contract does not establish file-ID image, realtime,
worktree-specific or presentation-feature support. Optional warnings must say
what remains outside the proven contract. A stale historical empty trace is
not the evidence for the refreshed contract: record the current isolated
request/behavior evidence. No blanket method allowlist, raw hash-only core
exception, fabricated image URL, removed image data or skip-check flag.
If a required core probe fails, investigate and repair within this contract;
do not turn its absence into success. Additional behavioral changes must be
recorded with RED/GREEN evidence before proceeding.

Native core verification exposed an existing transport defect: the probe wrote
all requests and immediately closed stdin; the current app-server then exited
after initialize before replying to the remaining requests. The bounded repair
keeps stdin open and waits for each request's matching response under the same
overall timeout/output/process-group bounds. A public run_parity_probes test
with an asynchronous server must fail before this repair and pass after it.

Native typed verification also proves that exec JSON omits the role/source
metadata assumed by the previous synthetic probe fixtures. Replace that probe
with an app-server conversation, followed by thread/read for the spawned child.
Require exactly one v2 subAgentActivity, matching thread-spawn parent/path and
explorer role, child completion before parent completion, and both exact text
markers. Define the probe explorer description only via process-local config;
use the v2 task name and fork_turns contract explicitly. Keep the same overall
timeout, output and candidate fingerprint bounds. No synthetic exec success
format is accepted as typed proof. Capture the actual native event shape as a
sanitized fixture and verify failure mutations before replacing the evaluator.

## Skill Routing Ledger

- artifact-status: final; workflow: Full OpenSpec.
- capability-research: used; current native inventories and original parity design.
- decision-resolution: used; requester approved exact optional classification.
- decision-grilling: skipped; accepted scope has no unresolved product choice.
- implementation-planning: used; this design and dependency-ordered tasks.
- architecture-guidance: used; optional evidence remains distinct from adapters.
- domain-language-modeling: skipped; existing parity vocabulary suffices.
- openspec-routing: used; this change owns execution.
- test-first-execution: used; existing approved public seams.
