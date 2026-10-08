# Existing repair commit verification, 2026-10-08

Implementation commit: `5a6d9fc3a628d01a75045765c5100f241eaca0bf`, policy 8.
The requester authorized committing existing changes, testing exact committed
source and diagnosing current failures. This verification adds no production
behavior or new compatibility classification. No live configuration, account,
Desktop activation, release or archive mutation occurred.

## Fresh verification

- Complete-source runtime-config/runtime-binding/setup-parity/parity/protocol-config/staged-update regression: 279 tests passed (224.278 seconds).
- Exact installed package: 19 focused/native runtime tests passed (13.952 seconds).
- Isolated installation upgraded policy 6 to policy 8. Release manifests validate before and after tests, with no Python bytecode residue.
- Both active OpenSpec changes pass strict validation. Shell/Python syntax and diff checks pass.
- Raw generic runtime logs are in `commit-retest-20261008/` next to this report.

The first broad test invocation used the release-source subset, which omits
repository-only `testdata`. Seven fixture-dependent cases failed (six errors and
one failure). The complete Git archive fixes that test invocation: all 279 cases
then pass. No production source or assertion was changed. A preliminary install
check also used a nonexistent validator name; the corrected check calls the
existing `validate_release_outputs` API and passes. These attempts are not
counted as passing runs.

## Current Desktop findings

Installed Desktop CLI: `0.158.0-alpha.2.1`; internal CLI: `0.155.0`.
`discover_desktop_hosts` still requires `Contents/Resources/codex`, but the App
now has a shell entry at `Contents/Resources/codex-cli/bin/codex` delegating to
`codex-cli/CodexCLI.app/Contents/MacOS/codex`. Discovery returns no current host
and `desktop.current.cli_invalid`. Presence, validity and missing-host reporting
need to remain distinct in a future repair.

Direct offline schema/feature collection uses the original App shell entry and
isolated configuration homes, with no model requests. The inventory contains 150
official and 142 internal features. Current policy leaves these differences
unclassified:

- Features: `agent_message_board`, `daemon_auto_start`, `guardian_reuse_parent_compaction`, `guardianv2.thread_context`, `prefer_mxc`, `transcript_v2`.
- Client requests: `account/gatewayOAuth/cancel`, `account/gatewayOAuth/login`, `account/gatewayOAuth/read`.
- Server notification: `account/gatewayOAuth/changed`.
- Server request: `mcpServer/elicitation/request`.

The inventory-only evaluation also reports `multi_agent_v2` core drift because
it has no full preparation/probe evidence. This is not evidence of an additional
new incompatibility; the existing typed-v2 runtime tests pass. These offline
findings do not constitute a successful current-version apply or foreground UI
acceptance. New methods and changed feature stages require source/schema and
runtime analysis, not blanket optional classification.

Caller-specific orchestration, configuration migration and integration evidence
remain in the dependent repository. This final documentation commit changes no
release inputs; exact package source hashes can be compared with the tested
implementation before refreshing its dependent entrypoint.
