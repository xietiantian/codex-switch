from __future__ import annotations

import shlex
from pathlib import Path

from codex_switch_constants import SwitchError
from codex_switch_io import atomic_write
from codex_switch_store import Store


def _paths_equal(first: Path, second: Path) -> bool:
    return first.expanduser().resolve(strict=False) == second.expanduser().resolve(
        strict=False
    )


def _shim_profile_name(
    store: Store,
    codex_bin: str,
    codex_home: Path | None,
    profile_name: str,
) -> str:
    if profile_name:
        return profile_name
    internal_home = (
        Path(store.internal_codex_home)
        if store.internal_codex_home is not None
        else store.managed_home("internal")
    )
    if codex_home is not None and _paths_equal(codex_home, internal_home):
        return "internal"
    try:
        manifest = store.load_manifest("internal")
    except SwitchError:
        return ""
    raw_backend = manifest.get("codex_bin")
    if isinstance(raw_backend, str) and raw_backend and _paths_equal(
        Path(raw_backend),
        Path(codex_bin),
    ):
        return "internal"
    return ""


def render_codex_shim_payload(
    store: Store,
    codex_bin: str,
    codex_home: Path,
    *,
    profile_name: str = "",
    switch_scripts: Path | None = None,
) -> bytes:
    selected_profile = _shim_profile_name(
        store,
        codex_bin,
        codex_home,
        profile_name,
    )
    if selected_profile != "internal":
        return (
            "#!/usr/bin/env sh\n"
            f'export CODEX_HOME="{codex_home}"\n'
            f'exec "{codex_bin}" "$@"\n'
        ).encode()

    from codex_switch_app_wrapper import resolved_wrapper_python

    scripts = switch_scripts or Path(__file__).resolve().parent
    python_bin = resolved_wrapper_python()
    script = f"""#!/usr/bin/env sh
set -eu

STORE_ROOT={shlex.quote(str(store.root))}
FALLBACK_CODEX_HOME={shlex.quote(str(codex_home))}
OFFICIAL_CODEX_HOME={shlex.quote(str(store.official_codex_home))}
FALLBACK_CODEX_BIN={shlex.quote(str(codex_bin))}
SWITCH_SCRIPTS={shlex.quote(str(scripts))}
PYTHON_BIN={shlex.quote(str(python_bin))}

exec env PYTHONPATH="$SWITCH_SCRIPTS" "$PYTHON_BIN" -B \\
  "$SWITCH_SCRIPTS/codex_switch_runtime_binding.py" \\
  exec-internal-shell \\
  --store-root "$STORE_ROOT" \\
  --fallback-home "$FALLBACK_CODEX_HOME" \\
  --official-home "$OFFICIAL_CODEX_HOME" \\
  --fallback-backend "$FALLBACK_CODEX_BIN" \\
  -- "$@"
"""
    return script.encode()


def write_codex_shim(
    store: Store,
    codex_bin: str,
    codex_home: Path | None = None,
    *,
    profile_name: str = "",
    switch_scripts: Path | None = None,
) -> Path:
    if not codex_bin:
        raise SwitchError("Profile manifest has no codex_bin. Capture or edit the profile first.")
    bin_path = Path(codex_bin).expanduser()
    if not bin_path.exists():
        raise SwitchError(f"codex_bin does not exist: {bin_path}")

    shim_path = store.bin_dir / "codex"
    home = codex_home or store.live_codex_home
    payload = render_codex_shim_payload(
        store,
        str(bin_path),
        home,
        profile_name=profile_name,
        switch_scripts=switch_scripts,
    )
    atomic_write(shim_path, payload, mode=0o755)
    return shim_path
