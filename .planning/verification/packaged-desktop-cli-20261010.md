# Packaged Desktop CLI verification (2026-10-10)

Baseline: `42af1e820db9`; approved change: `support-packaged-desktop-cli`.
No caller-specific workflow, configuration or business content is added.

## Runtime evidence

The installed Desktop 26.924.22138 / CLI 0.158.0-alpha.2.1 uses
`Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`; the `bin/codex`
entry is a shell trampoline. Internal CLI is 0.155.0. The legacy layout remains
supported. Missing/linked/non-executable packaged members are errors, never a
fallback to the obsolete sibling. Stage freezes both native executable and entry.

Feature and schema exports are freshly collected from the actual executables.
Official schema SHA-256: d7e52e79d204911a48a75f5b13804feeea9c0eed0b9ec4b384ed07a7210a3b10.
Internal schema SHA-256: a4ae6ff849a41784bfcc954b209637d79369e09f306ad32580b81d4abd40134d.
Source research uses openai/codex tag rust-v0.158.0-alpha.2.1, commit
9cf62076c17c6a46b7d05dcdb7fe3ecc3c7a9c6f. The exact feature pairs, gateway method
hashes, rationale and reject conditions are recorded in the change design.

Policy 9 permits only reviewed unused differences and corrects open-object
intersection comparison. Explicit feature dependence, observed gateway use,
schema drift, new required elicitation metadata and unknown differences fail.

## RED / GREEN and checks

- Packaged discovery initially returned no current host. Discovery/reference,
  invalid-host diagnostics and entry/backend drift regressions now pass.
- Fresh current inventories initially failed six feature and five protocol
  differences. Exact policy/elicitation positive and negative fixtures pass.
- Full `python3 -B -m unittest discover -s scripts -p 'test_codex_*.py'`:
  1,294 cases, 14 conditional skips, two baseline failures. Both failures were
  reproduced at unchanged 42af: old-Python import ordering and an obsolete
  private-seed fixture expectation. The bounded corrections are in the ledger.
- Final focused rerun: 98 tests pass across config document, bundled sources,
  model routing, packaged layout and packaged parity modules (45.830 s).
- Explicit `python3 -B scripts/test_codex_profile_switch.py`: 227 pass (390.461 s).
- Native parity and staged update with real internal / legacy official binaries:
  12 pass (195.783 s). No global App binary is modified by these fixtures.
- Native lifecycle with current actual Desktop and loopback provider: passes.
- Explicit Python 3.9 transaction execution passes. These native/interpreter
  runs execute all 14 cases conditionally skipped by the discovery invocation.
- Isolated prior-policy-8 to candidate-policy-9 install succeeds, manifests
  validate, and installed check-official resolves the actual packaged CLI.
- Strict OpenSpec and git diff checks pass. Exact committed package validation
  and publication readback remain delivery follow-through after source commit.

The full discovery invocation is not described as green: its two failures are
closed by the fresh 98-case rerun. Logs and generated packages are retained
locally under the matching timestamped verification root; they are not source.

## Standards review

Independent Standards reviewer: no blocking violations or scope leakage.
Conservative schema merge constraints and exact gateway hashes were checked.
Wrapper permission/symlink negative tests were added. Suggested additional
closed-object/conflicting-property tests are optional; existing conservative
branches were inspected and manually probed. Follow-up review approved the
old-Python guard and stronger private-seed test without new findings.

## Spec review

Independent Spec reviewer: no production defect or missing behavior. One
wording correction clarified that wrapper identity is frozen for staged
preparation; native CLI identity governs direct binding. Applied to the spec.

Neither release publication, archive nor foreground Desktop UI acceptance is
claimed by these source and native handshake checks.
