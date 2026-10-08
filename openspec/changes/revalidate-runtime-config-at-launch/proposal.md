## Why

A healthy internal generation currently pins the entire runtime config byte-for-byte. Desktop trust and plugin bookkeeping can therefore prevent its backend from reaching initialize, even though compatibility inputs have not changed. Launch-time legacy home merging can also resurrect settings omitted from the prepared runtime.

## What Changes

- Retain immutable binary, launcher, capability receipt and catalog verification.
- Bind reusable compatibility evidence to parsed runtime settings, excluding explicitly reviewed mutable UI and integration state.
- Revalidate changed compatibility settings through the same preparation policy used by initial binding; publish only a healthy, frozen-input generation.
- Treat the materialized internal runtime as the launch configuration authority, without restoring legacy fallback settings.
- Provide an explicit public verification option for the actual launch path, separate from isolated backend smoke testing.

## Impact

Generic runtime launch, receipts, home synchronization, verification CLI and their independent tests. No dependent repository orchestration, identity/login UX or caller-specific evidence is included. Source repair and isolated tests are approved; live installation and Git publication are outside this authorization.
