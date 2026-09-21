## Target State and Authority

The requester approved first-install support on the existing PR branch,
isolated verification, and updating that PR. The public update-internal CLI is
the test seam: local fake installers, temporary HOME/store/install paths, and
synthetic credentials. This is the approved command behavior under test.
An isolated Python interpreter shim injects the post-link signal and concurrent
hard-link collision through that same public CLI without a production test hook.
Existing update tests protect upgrade behavior. No live installation is allowed.

## Decisions

1. A first install requires both the internal profile directory and target
   executable to be absent. Dangling links, partial profiles, old backups,
   invalid paths, or a profile with a missing executable are not first installs.
   An existing unregistered executable requires capture rather than overwrite.
2. Resolve the explicit target version or a valid, unblocked latest version
   before installation. Reuse the blocked-release fallback policy for a fresh
   install. Missing or ambiguous release metadata fails before publication.
3. Reuse the candidate helper with a private first-install option. In that
   mode its before/after bound identity is absence; an unexpected path appearing
   is an error. The installer still gets private HOME/CODEX_HOME, noninteractive
   mode, complete package materialization, signing, and exact version checks.
4. A focused first-install module owns validation and no-replace publication.
   Hold the existing store lock during final checks and publication. Recheck
   the target/profile and candidate identity, fsync candidate bytes, atomically
   hard-link the regular candidate into the absent target, verify the installed
   version, and fsync the directory. A failed final check removes only our own
   unchanged link; an unknown replacement is preserved. Candidate assets remain
   available for diagnosis. No old binary or rollback generation is invented.
5. First-install success means an installed CLI. It does not create a profile,
   import installer scratch configuration, claim Desktop parity, or switch an
   active runtime. Output names the required configuration/capture next step.
6. Dry-run resolves and validates the plan without creating directories,
   invoking installers, capturing profiles, or publishing executables.

## Completion Contract and Validation

Cover fresh legacy and standalone installs, absent parent directories,
explicit/latest versions, failure and wrong version, a target appearing during
preparation, partial/dangling state, existing-profile preservation, dry-run,
and failure at the final installed-path check. Run focused tests first, then
the update/release and profile regression suites, package smoke, syntax,
strict OpenSpec validation, and diff review. All tests use isolated paths.

## Skill Routing Ledger

- artifact-status: final; workflow: Full OpenSpec.
- capability-research: used; public CLI/helper source and historical commit.
- decision-resolution: used; requester approved first-install restoration.
- decision-grilling: skipped; no unresolved product choice.
- implementation-planning: used; this design and dependency-ordered tasks.
- architecture-guidance: used; separate first publication from replacement.
- domain-language-modeling: skipped; existing install/profile terms suffice.
- openspec-routing: used; this change is the execution source.
- test-first-execution: used; local tdd skill at the authorized CLI seam.

## Risks, Recovery, and Artifact Ownership

Absence is local state, not proof that a machine has never run Codex. Never
infer absence from a failed version probe. Publication must not overwrite a
concurrent installation. Interruption before publication leaves no command;
interruption after durable publication leaves a fully validated command.
No Desktop health claim is made until normal profile activation succeeds.
The public first-install command execs its orchestration process so a signal to
the CLI PID cancels and reaps the installer. Publication is inside the owned-link
cleanup boundary; a failed link syscall never grants cleanup ownership. New
ancestor directory entries are synchronized before a durable success is reported.
Fixtures own only their newly allocated temporary roots. Verification output
is retained under .planning/devflow/verification/local. No pre-existing user
files or unrelated research artifacts are modified or committed.
