# Filter App Server Process Diagnostics

## Why

`codex-switch status` and `doctor` inspect running processes to warn when Codex Desktop is still using an old app CLI. The current scan matches any `" app-server"` substring in `ps` args, so unrelated helper processes whose JSON/text arguments mention `codex app-server` are misreported and can dump large conversation payloads into CLI output.

## What Changes

- Treat a process as a Codex app-server only when its command line starts with a Codex executable path followed by `app-server` as the invoked subcommand.
- Ignore unrelated process arguments that merely contain `codex app-server` in payload text.

## Impact

- Affects status/doctor/verify process diagnostics and post-switch verification.
- No profile store, config, auth, or switching mutation behavior changes.

## Independent CLI daemon follow-up (2026-10-13)

An independent terminal CLI now starts its own `app-server` and `app-server
daemon` processes. Their real command lines pass the existing parser, but they
do not belong to Desktop and must not be required to use its backend or proxy.
Use the process ancestry snapshot to exclude confirmed independent CLI children.
Desktop/proxy ancestry takes precedence; incomplete or unknown ancestry retains
the existing diagnostics. Reuse this unarchived change for the authorized repair
and delivery to the existing PR branch.
