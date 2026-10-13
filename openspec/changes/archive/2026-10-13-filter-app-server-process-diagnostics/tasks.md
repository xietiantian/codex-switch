# Tasks

## Capability Slices

- [x] Add regression coverage for payload text that mentions `codex app-server`.
- [x] Tighten process detection to ignore payload mentions while keeping real app-server detection.
- [x] Run focused unit tests and syntax/diff checks.

## Validation Commands

### Independent CLI daemon follow-up (2026-10-13)

- [x] 4. Reproduce the terminal CLI daemon failure through public observation and attestation; implement conservative ancestry filtering.
- [x] 5. Cover Desktop/proxy precedence, inherited environment, nested children, incomplete/cyclic/unknown ancestry and unchanged real failure checks; document diagnostics.
- [x] 6. Run affected regression suites, package/current-machine verification and source/spec reviews; record exact evidence.
- [x] 7. Commit and push the existing PR branch and verify its remote head.

### Original validation commands

- `python3 scripts/test_codex_profile_switch.py CodexProfileSwitchTests.test_app_server_command_path_ignores_payload_mentions -v`
- `python3 scripts/test_codex_profile_switch.py CodexProfileSwitchTests.test_app_server_command_path_accepts_codex_executables -v`
- `python3 scripts/test_codex_profile_switch.py`
- `python3 -m py_compile scripts/codex_switch_running_app.py scripts/test_codex_profile_switch.py`
- `bash -n scripts/codex-switch`
- `git diff --check`
- `openspec validate filter-app-server-process-diagnostics --strict`
- `openspec validate --all --strict --no-interactive`

## Verification Evidence

- 2026-06-18: Focused app-server command path tests passed.
- 2026-06-18: Full `scripts/test_codex_profile_switch.py` suite passed: 63 tests.
- 2026-06-18: Python compile, shell syntax, diff whitespace, focused OpenSpec, and all strict OpenSpec checks passed.
