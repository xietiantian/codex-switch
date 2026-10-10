## Why

Runtime updates can reject supported user choices or optional runtime capabilities solely because they differ between the reference and candidate. This change implements the explicitly approved exceptions while preserving core and unknown-difference guards.

## What Changes

- Allow independent apps, guardian_approval and prevent_idle_sleep values when both runtimes support matching stage/default metadata.
- Allow missing nonfatal_clock_read_errors independent of the reference setting, except when requested by candidate configuration or required by the core contract.
- Report a missing client_request rollout/compress as optional maintenance without calling it.
- Expose bounded, nonsecret diagnostics through apply/status/replay; preserve transaction outcome and recovery semantics.
- Permit supported default-only feature differences generically while preserving specialized compatibility rules.
- Advance the current policy 9 to policy 10; earlier policy 7 work remains historical.

## Capabilities

### New Capabilities

- `setup-parity-choices`: optional runtime choices and durable update diagnostics.

### Modified Capabilities

None outside setup-parity-choices, including the newly approved default-only classification.

## Impact

Runtime parity policy, candidate preparation, update JSON diagnostics, independent Python tests and CLI documentation. Caller-specific installation workflows, configuration generation, authentication guidance and UI wording are outside this repository's contract.

The reviewed system_proxy_fallback absence is now approved as optional when official reports stable/default=true and internal lacks it, independently of the official effective setting. Candidate requests and core dependencies remain blocking. Two protocol-test startup budgets are increased without changing production timeouts. Existing enable_fanout/removed behavior remains unchanged. Unknown differences outside the supported default-only rule remain blocking. The current follow-up includes verified package refresh and the already authorized existing PR push; release publication and archive remain excluded.
