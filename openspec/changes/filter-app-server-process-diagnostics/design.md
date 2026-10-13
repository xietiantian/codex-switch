# Design

## Target State

`running_codex_processes()` reports only actual Codex app-server processes. A textual mention of `codex app-server` inside another process argument is ignored, preventing status/doctor output from leaking long JSON payloads.

## Approach

Tighten `app_server_command_path()` from substring splitting to a regex anchored at the start of the `ps` args line. The accepted command path must end in a Codex-like executable basename (`codex` or `codex-*`) and be immediately followed by `app-server` as the next token.

## Non-goals

- Do not change Desktop wrapper launch behavior.
- Do not suppress legitimate stale app-server warnings.

## Independent CLI ancestry follow-up (2026-10-13)

The earlier substring fix is complete. Current parsing already accepts global
CLI options. The remaining failure is attribution after parsing: a terminal CLI
can spawn a separately packaged daemon with the same analytics flags as Desktop.
Path, version, inherited CODEX_CLI_PATH, and flags alone cannot establish ownership.

Walk ancestors in the same `ps pid,ppid,args` snapshot. A recognized Desktop host
or managed proxy in the chain preserves the candidate, even if a Codex ancestor
is also present. A complete chain to the process root containing a non-app-server
Codex invocation proves an independent CLI child and excludes it. Missing parents,
cycles, malformed ancestry and otherwise unknown ownership remain candidates.
Keep attestation's backend and proxy checks unchanged. No process is stopped.

Positive CLI attribution accepts the `codex` executable's non-app-server forms
after recognized global options, including the documented initial TUI prompt
(`codex [OPTIONS] [PROMPT]`). Unknown aliases, unknown options or incomplete
options retain diagnostics. Proxy recognition checks
the executable/script argv position rather than mentions in prompts or payloads.
Unknown Python invocation syntax and code/module modes retain diagnostics too.

The common observation collector supplies status, doctor and verification, so one
filter fixes all consumers. Do not blacklist release paths, versions, PIDs, or
daemon subcommands: Desktop can legitimately use the same executables.

### Execution and completion contract

- Owner: main; baseline d7efb37724e1778551668c592ee4f655f50e9916.
- Scope: running-process collector, public observation/attestation regression
  tests, README and this change's execution/evidence records.
- User approved the diagnosed repair, tests, commit and existing PR push. The
  agreed test seams are process snapshot -> observation -> attestation, and
  public verify/status/doctor commands. Test system inputs, not private helpers.
- Required: RED on terminal daemons; GREEN while real stale Desktop/backend,
  proxy-bypass and ambiguous-ancestry diagnostics survive; focused and broader
  affected suites; exact package and read-only current-machine verification;
  source/spec review; remote branch readback.
- Preserve unrelated untracked research. No caller-specific content, installer
  policy change, release publication, archive, App restart, or runtime replacement.
- Rollback: revert the bounded source commit; no persisted schema migration.

### Skill Routing Ledger

- artifact-status: final
- capability-research: used — current process tree and installed/source comparison
- decision-resolution: used — requester approved the diagnosed boundary
- decision-grilling: skipped — no unresolved product decision
- implementation-planning: used — this follow-up and tasks 4–7
- architecture-guidance: skipped — retain the common collector/attestation boundary
- domain-language-modeling: skipped — no new domain or persistence model
- openspec-routing: used — reuse filter-app-server-process-diagnostics
