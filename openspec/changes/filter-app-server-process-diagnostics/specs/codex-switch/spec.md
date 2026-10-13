## ADDED Requirements

### Requirement: Running app-server diagnostics are bounded to Codex processes

`codex-switch` SHALL report running app-server diagnostics only for actual Codex app-server command invocations, not arbitrary process arguments that contain matching words in payload text.

#### Scenario: Payload text mentions app-server

- GIVEN a non-Codex helper process command line includes JSON or text containing `codex app-server`
- WHEN `codex-switch status` or `codex-switch doctor` scans running processes
- THEN the helper process is ignored as an app-server candidate
- AND its payload text is not printed as a stale app-server path.

#### Scenario: Codex app-server process is running

- GIVEN a command line starts with a Codex executable path followed by `app-server`
- WHEN `codex-switch status` or `codex-switch doctor` scans running processes
- THEN that command line is reported as a running Codex app-server candidate unless its complete ancestry proves it belongs to an independent CLI.

### Requirement: Independent CLI daemons do not fail Desktop verification

`codex-switch` SHALL exclude confirmed independent CLI children from Desktop
process observations consumed by status, doctor and verification. Desktop or
managed proxy ancestry SHALL take precedence over an independent CLI ancestor.
Unknown, incomplete, malformed or cyclic ancestry SHALL retain diagnostics.

#### Scenario: Terminal CLI starts background app-server processes

- GIVEN a terminal Codex CLI starts an app-server and an app-server daemon
- AND their complete ancestry contains no recognized Desktop host or managed proxy
- WHEN Desktop runtime verification collects processes
- THEN these children do not cause backend mismatch or proxy bypass findings
- AND inherited CODEX_CLI_PATH and Desktop-like analytics flags do not change ownership.

#### Scenario: Desktop or proxy owns a nested backend

- GIVEN an app-server has a recognized Desktop or managed proxy ancestor
- AND another Codex process occurs in the same ancestor chain
- WHEN runtime verification collects processes
- THEN the app-server remains a candidate
- AND a wrong backend or missing required proxy still fails verification.

#### Scenario: Ownership cannot be established

- GIVEN an app-server's ancestry is missing, malformed, cyclic or contains no identifiable CLI owner
- WHEN runtime verification collects processes
- THEN the app-server remains eligible for existing diagnostics.
