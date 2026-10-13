# setup-parity-choices Specification

## Purpose

Allow independently configured optional capabilities during a runtime update, while retaining real configuration, core compatibility and transactional failure protection.

## Requirements

### Requirement: Supported user feature choices
The update SHALL preserve and permit differing enabled values of apps, guardian_approval and prevent_idle_sleep when both runtimes support the same stage and default. Explicit core requirements and actual configuration failures SHALL remain blocking.

#### Scenario: Independent supported settings
- **WHEN** these supported features differ only in effective boolean values
- **THEN** the update reports the choices and continues remaining checks without rewriting them

#### Scenario: Unsupported or required capability
- **WHEN** a supported-choice feature is absent, changes stage, changes default without meeting the supported default-only rule, or is explicitly required by the core contract
- **THEN** its incompatible difference remains blocking with a named diagnostic

### Requirement: Optional clock capability
Absence of nonfatal_clock_read_errors SHALL be nonblocking regardless of official enabled state unless the candidate itself enables the unsupported feature or the core contract requires it.
When both runtimes support this feature with matching stage/default, independent enabled values SHALL also be permitted subject to the same core requirements.

#### Scenario: Both runtimes support the optional clock behavior
- **WHEN** both runtimes support nonfatal_clock_read_errors with matching stage/default and different enabled values
- **THEN** update preserves each configuration and reports the choice without blocking

#### Scenario: Official enables a feature the candidate does not request
- **WHEN** official enables nonfatal_clock_read_errors and internal lacks it without requesting it
- **THEN** the difference is reported and remaining checks continue

#### Scenario: Candidate requests unsupported behavior
- **WHEN** the final candidate configuration enables nonfatal_clock_read_errors but the candidate runtime lacks it
- **THEN** update fails with the unsupported feature named and no publication

### Requirement: Optional maintenance request
Missing rollout/compress on internal SHALL produce a warning about unavailable explicit old-session compression and SHALL NOT alone fail the runtime update. Other core/protocol checks SHALL continue.

#### Scenario: Missing maintenance trigger
- **WHEN** official has client_request rollout/compress and internal lacks it without a core dependency
- **THEN** update reports the optional missing maintenance interface and proceeds without calling it

#### Scenario: Core or unknown incompatibility
- **WHEN** another unknown method is absent, a core method is incompatible, or rollout/compress becomes an explicit core dependency
- **THEN** update remains blocking

### Requirement: Concrete durable update diagnostics
Apply and subsequent status/replay SHALL expose bounded nonsecret diagnostic details for optional findings and errors, including names and relevant states. Diagnostics SHALL NOT change the confirmed transaction outcome.

#### Scenario: Successful update with warnings
- **WHEN** optional differences exist and all remaining checks pass
- **THEN** apply returns applied with diagnostics and status retains them

#### Scenario: Failed preparation
- **WHEN** preparation fails
- **THEN** the result identifies the affected feature or method and compatibility reason while retaining existing safe_to_restore semantics

### Requirement: Preserve excluded behavior
The existing enable_fanout and removed-feature rules SHALL remain unchanged; unknown differences and native-probe failures SHALL NOT be globally downgraded.

#### Scenario: Removed flag still differs
- **WHEN** enable_fanout is removed on both sides but effective values differ
- **THEN** its existing blocking result remains in force

### Requirement: Optional system-proxy retry capability
When official reports system_proxy_fallback as stable with default true and internal lacks it, the update SHALL report the absent optional connection-retry capability and continue regardless of the official effective boolean. An explicit candidate request, core dependency, other stage/default or reversed absence SHALL retain blocking classification. Actual connection/configuration failures SHALL remain blocking.

#### Scenario: Reviewed missing retry
- **WHEN** official reports the reviewed definition and internal lacks the unrequested optional feature
- **THEN** diagnostics explain unavailable system-proxy retry and all remaining checks run

#### Scenario: Required or changed retry definition
- **WHEN** candidate enables the absent feature, the core contract requires it, or its definition/absence direction differs
- **THEN** the difference remains blocking

### Requirement: Supported default differences are nonblocking

Parity SHALL report a nonblocking default-difference warning for a feature
without a specialized capability/evidence rule when both runtimes support it at
the same active stage, defaults differ, and each effective value equals its own
default. Active stages are stable, experimental and under development. The
rule SHALL apply independently of the feature name, without rewriting either
configuration or requesting synchronization of the accepted defaults. Independent
supported-choice features may use this rule when their values follow defaults;
features governed by exact reviewed pairs SHALL NOT use it when their pair changes.

#### Scenario: Instant interruption uses different defaults
- **WHEN** instant_interrupt is under development on both runtimes, official
  defaults to and uses false, internal defaults to and uses true, and no core
  dependency or explicit internal request requires it
- **THEN** the difference is a warning and all other compatibility checks continue

#### Scenario: Another feature reverses the defaults
- **WHEN** another supported feature satisfies the same conditions with official
  true and internal false
- **THEN** it receives the same nonblocking classification without a name allowlist

### Requirement: Existing compatibility boundaries remain enforced
The new default rule SHALL NOT override specialized feature policies, observed
dependencies, explicit internal feature requests, missing features, stage
changes, removed or deprecated features, or effective values differing from
their defaults. Protocol/configuration errors and native probe failures SHALL
retain their existing blocking semantics.

#### Scenario: Default-looking core dependency
- **WHEN** a differing feature is core, observed, explicitly requested, or has
  item-ID requirements governed by the specialized policy
- **THEN** its existing dependency/evidence validation determines acceptance

#### Scenario: Difference is not solely supported defaults
- **WHEN** a runtime lacks the feature, stages differ or are inactive, or an
  effective value differs from that runtime's isolated default
- **THEN** the new default rule does not accept the difference

#### Scenario: Protocol fails alongside a default warning
- **WHEN** supported defaults differ but a core protocol method is incompatible
- **THEN** parity reports the warning and still rejects the runtime update

### Requirement: Diagnostics and receipts reflect the policy
The warning SHALL identify the feature, development stage and both default and
effective values. Policy identity SHALL change so old receipts cannot establish
health without fresh validation under the new rule.

#### Scenario: Fresh preparation after upgrade
- **WHEN** an older-policy receipt exists and defaults now differ
- **THEN** preparation uses the new policy and records the concrete warning
