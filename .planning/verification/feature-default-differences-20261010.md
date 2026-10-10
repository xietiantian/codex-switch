# Supported default differences verification (2026-10-10)

Existing change: allow-optional-setup-parity-differences, tasks 9–13.
Baseline: c0ebda44dea68ee6428b09009964f0da180cdc25.

Policy 10 permits supported default-only differences with explicit diagnostics,
without writing configuration or synchronizing accepted defaults. Known exact
reviewed pairs, core/item-ID rules, explicit requests, observed dependencies,
inactive/stage/missing/override differences and protocol failures retain their
existing checks.

RED: instant_interrupt official default/effective false versus internal true
was unclassified. GREEN: initial setup suite 9, expanded setup/current 20 pass.
Spec review found a reviewed-pair fallback gap; existing current-parity tests
reproduced 3 failures. Excluding reviewed names and adding negative cases fixed
it. Spec and Standards rechecks have zero remaining findings.

The original native fixture fails after copying the signed packaged Desktop
CLI outside its bundle (exit -9 during version probing, before policy). It now
re-signs only the owned temporary copy and retains original runtime/App state
checks. This is a test fixture correction, not a production signing change.

Final discovery, profile, native and exact-package verification are pending.
The superseded discovery run, which includes the 3 fixed failures, is retained
as partial historical evidence. Its completion is not claimed.
