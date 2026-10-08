from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from codex_switch_core import SwitchError, atomic_write
from codex_switch_protocol_adapter import capability_receipt_path_for_launcher
from codex_switch_store import Store
from codex_switch_runtime_binding import (
    binding_profile_home,
    manifest_has_internal_runtime_generation,
)


def managed_profile_app_cli_path(store: Store, name: str) -> Path:
    return store.bin_dir / f"codex-{name}-app"


def should_refresh_profile_app_wrapper(store: Store, name: str, app_cli_path: str) -> bool:
    path = Path(app_cli_path).expanduser()
    if not path.is_absolute():
        return False
    if path == managed_profile_app_cli_path(store, name):
        return True
    return name == "internal" and path == store.bin_dir / "codex-internal-app"


def profile_app_home(store: Store, name: str) -> Path:
    try:
        manifest = store.load_manifest(name)
    except Exception:
        manifest = {}
    if name in {"internal", "openai-official", "official"}:
        return binding_profile_home(store, name, manifest)
    raw_home = manifest.get("codex_home")
    if raw_home:
        return Path(str(raw_home)).expanduser()
    return store.managed_home(name)


def resolved_wrapper_python() -> Path:
    raw = os.environ.get("CODEX_SWITCH_PYTHON") or sys.executable
    resolved = shutil.which(raw) if "/" not in raw else raw
    if not resolved:
        raise SwitchError(
            f"Python 3.11+ interpreter does not exist: {raw}"
        )
    path = Path(resolved).expanduser().resolve()
    try:
        result = subprocess.run(
            [
                str(path),
                "-c",
                (
                    "import sys, tomllib; "
                    "raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
                ),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SwitchError(
            f"Unable to validate Python 3.11+ interpreter: {path}"
        ) from exc
    if result.returncode != 0:
        raise SwitchError(
            f"Python 3.11+ with tomllib is required: {path}"
        )
    return path


def write_profile_app_wrapper(
    store: Store,
    name: str,
    app_cli_path: str,
    codex_bin: str,
    switch_scripts: Path,
    capability_receipt_path: Path | None = None,
    schema_sha256: str = "",
    capability_receipt_sha256: str = "",
    manifest_override: Mapping[str, object] | None = None,
) -> str:
    if not codex_bin:
        raise SwitchError("Profile manifest has no codex_bin. Capture or edit the profile first.")
    bin_path = Path(codex_bin).expanduser()
    if not bin_path.exists():
        raise SwitchError(f"codex_bin does not exist: {bin_path}")
    wrapper_path = Path(app_cli_path).expanduser()
    if not wrapper_path.is_absolute():
        raise SwitchError(f"app_cli_path must be an absolute path: {app_cli_path}")

    if manifest_override is not None:
        manifest = dict(manifest_override)
    else:
        try:
            manifest = store.load_manifest(name)
        except Exception:
            manifest = {}
    if capability_receipt_path is None:
        raw_receipt_path = manifest.get("app_capability_receipt_path")
        if isinstance(raw_receipt_path, str) and raw_receipt_path:
            capability_receipt_path = Path(raw_receipt_path).expanduser()
        elif name == "internal":
            capability_receipt_path = capability_receipt_path_for_launcher(
                managed_profile_app_cli_path(store, name)
            )
    if not schema_sha256:
        raw_schema_sha256 = manifest.get("app_schema_sha256")
        if isinstance(raw_schema_sha256, str):
            schema_sha256 = raw_schema_sha256
    if not capability_receipt_sha256:
        raw_receipt_sha256 = manifest.get("app_capability_receipt_sha256")
        if isinstance(raw_receipt_sha256, str):
            capability_receipt_sha256 = raw_receipt_sha256

    app_home = profile_app_home(store, name)
    python_bin = resolved_wrapper_python()
    generation_validation = ""
    if (
        name == "internal"
        and manifest_has_internal_runtime_generation(manifest)
    ):
        generation_validation = r"""
"$PYTHON_BIN" -B \
  "$SWITCH_SCRIPTS/codex_switch_runtime_binding.py" \
  validate-internal-generation --refresh-config \
  --store-root "$STORE_ROOT" \
  --fallback-home "$APP_CODEX_HOME" \
  --official-home "$OFFICIAL_CODEX_HOME" \
  --launcher-path "$0" \
  --expected-backend "$CODEX_BIN" \
  --expected-home "$APP_CODEX_HOME" \
  --expected-capability-receipt-path "$CAPABILITY_RECEIPT" \
  --expected-schema-sha256 "$EXPECTED_SCHEMA_SHA256" \
  --expected-capability-receipt-sha256 "$EXPECTED_RECEIPT_SHA256"
"""
    runtime_sync_option = " --preserve-runtime-settings" if generation_validation else ""
    script = f"""#!/usr/bin/env sh
set -eu

STORE_ROOT={shlex.quote(str(store.root))}
LIVE_CODEX_HOME={shlex.quote(str(store.live_codex_home))}
OFFICIAL_CODEX_HOME={shlex.quote(str(store.official_codex_home))}
APP_CODEX_HOME={shlex.quote(str(app_home))}
CODEX_BIN={shlex.quote(str(bin_path))}
SWITCH_SCRIPTS={shlex.quote(str(switch_scripts))}
PYTHON_BIN={shlex.quote(str(python_bin))}
PROFILE_NAME={shlex.quote(name)}
PROFILE_CONFIG={shlex.quote(str(store.profile_dir(name) / "config.toml"))}
CAPABILITY_RECEIPT={shlex.quote(str(capability_receipt_path or ""))}
EXPECTED_SCHEMA_SHA256={shlex.quote(schema_sha256)}
EXPECTED_RECEIPT_SHA256={shlex.quote(capability_receipt_sha256)}

export CODEX_SWITCH_CAPABILITY_RECEIPT="$CAPABILITY_RECEIPT"
export CODEX_SWITCH_EXPECTED_SCHEMA_SHA256="$EXPECTED_SCHEMA_SHA256"
export CODEX_SWITCH_EXPECTED_RECEIPT_SHA256="$EXPECTED_RECEIPT_SHA256"

if [ "${{PYTHONPATH+x}}" = "x" ]; then
  export CODEX_SWITCH_PROXY_PYTHONPATH_WAS_SET=1
  export CODEX_SWITCH_PROXY_ORIGINAL_PYTHONPATH="$PYTHONPATH"
else
  export CODEX_SWITCH_PROXY_PYTHONPATH_WAS_SET=0
  export CODEX_SWITCH_PROXY_ORIGINAL_PYTHONPATH=
fi

if [ "${{CODEX_SWITCH_REBIND_SMOKE:-}}" = "1" ]; then
  SMOKE_CODEX_HOME="${{CODEX_SWITCH_REBIND_SMOKE_HOME:-$APP_CODEX_HOME}}"
  if [ -n "${{CODEX_SWITCH_REBIND_CAPABILITY_RECEIPT:-}}" ]; then
    export CODEX_SWITCH_CAPABILITY_RECEIPT="$CODEX_SWITCH_REBIND_CAPABILITY_RECEIPT"
  fi
  export CODEX_HOME="$SMOKE_CODEX_HOME"
  exec env PYTHONPATH="$SWITCH_SCRIPTS" "$PYTHON_BIN" -B \
    "$SWITCH_SCRIPTS/codex_switch_app_proxy.py" \
    "$CODEX_BIN" \
    "$SMOKE_CODEX_HOME/config.toml" \
    "$@"
fi

umask 077
{generation_validation}
env PYTHONPATH="$SWITCH_SCRIPTS" "$PYTHON_BIN" -B \
  "$SWITCH_SCRIPTS/codex_switch_home_sync.py" \
  prepare-launch \
  --live-home "$LIVE_CODEX_HOME" \
  --app-home "$APP_CODEX_HOME" \
  --profile-config "$PROFILE_CONFIG" \
  --profile-name "$PROFILE_NAME"{runtime_sync_option}
{generation_validation}

export CODEX_HOME="$APP_CODEX_HOME"
exec env PYTHONPATH="$SWITCH_SCRIPTS" "$PYTHON_BIN" -B \\
  "$SWITCH_SCRIPTS/codex_switch_app_proxy.py" \\
  "$CODEX_BIN" \\
  "$APP_CODEX_HOME/config.toml" \\
  "$@"
"""
    atomic_write(wrapper_path, script.encode(), mode=0o755)
    return str(wrapper_path)


def maybe_refresh_profile_app_wrapper(
    store: Store,
    name: str,
    manifest: dict[str, object],
    app_cli_path: str,
    switch_scripts: Path,
) -> str:
    if not should_refresh_profile_app_wrapper(store, name, app_cli_path):
        return app_cli_path
    return write_profile_app_wrapper(
        store=store,
        name=name,
        app_cli_path=app_cli_path,
        codex_bin=str(manifest.get("codex_bin") or ""),
        switch_scripts=switch_scripts,
    )
