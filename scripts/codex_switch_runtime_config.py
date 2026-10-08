"""Compatibility inputs and launch-only refresh of a materialized runtime."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import tempfile
import tomllib
from pathlib import Path

from codex_switch_constants import SwitchError


def runtime_compatibility_digest(payload: bytes) -> str:
    data = tomllib.loads(payload.decode("utf-8"))
    # These records do not select the backend/model/protocol capability. Native
    # loading still validates them; they are not overrides of runtime features.
    for key in ("marketplaces", "plugins", "skills", "tui", "history"):
        data.pop(key, None)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        hooks.pop("state", None)
        if not hooks:
            data.pop("hooks")
    projects = data.get("projects")
    if isinstance(projects, dict):
        for key, project in tuple(projects.items()):
            if isinstance(project, dict):
                project.pop("trust_level", None)
                if not project:
                    projects.pop(key)
        if not projects:
            data.pop("projects")

    def typed_value(value):
        if isinstance(value, dict):
            return ["table", [[key, typed_value(value[key])] for key in sorted(value)]]
        if isinstance(value, list):
            return ["array", [typed_value(item) for item in value]]
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return [type(value).__name__, value.isoformat()]
        return [type(value).__name__, value]

    encoded = json.dumps(["runtime-semantics-v1", typed_value(data)],
                         ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def ensure_internal_runtime_generation(*, official_home: Path | None = None, **validation_args):
    """Launch boundary only. Read-only validators must never call this function."""
    from codex_switch_runtime_binding import (
        RuntimeConfigurationChanged, validate_internal_runtime_generation,
        _canonical_generation_path, _read_generation_file,
        _MAX_INTERNAL_GENERATION_MANIFEST_BYTES,
    )

    root = _canonical_generation_path(validation_args["store_root"], label="store")
    manifest_path = root / "profiles/internal/manifest.json"
    def read_manifest():
        return _read_generation_file(manifest_path, label="manifest",
                                     max_bytes=_MAX_INTERNAL_GENERATION_MANIFEST_BYTES)
    manifest_payload = read_manifest()
    try:
        return validate_internal_runtime_generation(**validation_args)
    except RuntimeConfigurationChanged:
        # Do not turn damaged executables, receipts, catalogs or launchers into
        # self-repair. The first validator verified those before raising.
        generation = validate_internal_runtime_generation(**validation_args, validate_config=False)
    from codex_switch_bindings import PrivateRuntimePreparation, _private_auth_snapshot, cmd_set_bin
    from codex_switch_parity import MAX_PARITY_CONFIG_BYTES, _regular_file_snapshot

    if read_manifest() != manifest_payload:
        raise SwitchError("Runtime generation changed during launch validation; retry launch.")
    home = generation.codex_home
    source = _regular_file_snapshot(home / "config.toml", code="parity.config.source_unsafe",
                                   label="current runtime configuration", max_bytes=MAX_PARITY_CONFIG_BYTES, capture_payload=True)
    auth_payload, auth_snapshot = _private_auth_snapshot(home)

    def frozen_inputs():
        current = _regular_file_snapshot(home / "config.toml", code="parity.config.source_unsafe",
                                        label="current runtime configuration", max_bytes=MAX_PARITY_CONFIG_BYTES, capture_payload=True)
        if (current != source or read_manifest() != manifest_payload
                or _private_auth_snapshot(home) != (auth_payload, auth_snapshot)):
            raise SwitchError("Runtime configuration changed during compatibility preparation; retry launch.")

    print("Runtime configuration changed; checking current settings with the binding compatibility policy.", file=sys.stderr)
    # Preparation sources must be distinct from promotion targets. Otherwise
    # promotion itself invalidates the captured source inode and rolls back.
    with tempfile.TemporaryDirectory(prefix=".launch-config-", dir=root) as temporary:
        private_home = Path(temporary)
        (private_home / "config.toml").write_bytes(source.payload)
        (private_home / "config.toml").chmod(0o600)
        if auth_payload is not None:
            (private_home / "auth.json").write_bytes(auth_payload)
            (private_home / "auth.json").chmod(0o600)
        args = argparse.Namespace(
            store_dir=root, official_codex_home=official_home or Path.home() / ".codex",
            internal_codex_home=home, internal_codex_home_source="manifest",
            launch_agent_path=root / "runtime-config-refresh.plist", launch_agent_label="",
            name="internal", codex_bin=str(generation.backend_cli), preserve_app_cli=False,
            suppress_rebind_success_output=True,
            rebind_private_inputs=PrivateRuntimePreparation(
                candidate_manifest=json.loads(manifest_payload), source_home=private_home,
                update_id=None, input_fingerprint=None, frozen_input_validator=frozen_inputs),
        )
        # Same schema, feature, model, config and native probe pipeline as
        # initial binding, including atomic/frozen-input promotion.
        cmd_set_bin(args)
    return validate_internal_runtime_generation(**validation_args)
