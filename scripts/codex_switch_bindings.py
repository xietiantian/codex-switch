from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import shlex
import subprocess
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Callable, Mapping

from codex_switch_config import config_uses_file_auth
from codex_switch_core import (
    SwitchError,
    atomic_write,
    copy_file_atomic,
    ensure_private_dir,
    make_store,
    now_stamp,
    resolve_codex_bin,
    resolve_internal_codex_bin,
    resolve_path,
    write_json,
)
from codex_switch_launch import validate_executable_path
from codex_switch_home_select import resolve_runtime_homes
from codex_switch_app_wrapper import write_profile_app_wrapper
from codex_switch_parity import (
    ConfigInputs,
    ParityBundle,
    catalog_provenance_for_bundle,
    ParityCandidate,
    ParityTimeouts,
    prepare_parity_bundle,
    project_parity_runtime_config_payload,
    revalidate_parity_bundle_immutable_inputs,
    revalidate_parity_bundle_inputs,
)
from codex_switch_protocol_adapter import (
    capability_receipt_path_for_launcher,
    prepare_capability_receipt_artifact,
    protocol_adapter_rule_set_digest,
)
from codex_switch_running_app import managed_launcher_fingerprint
from codex_switch_selection import require_active_profile_selection_payload
from codex_switch_shim import render_codex_shim_payload
from codex_switch_runtime_binding import (
    DesktopInventory,
    RuntimeObservation,
    attest_runtime_binding,
    resolve_store_runtime_binding,
    validate_internal_cli_runtime_generation,
)
from codex_switch_transaction import (
    RuntimeBindingExecutableSwap,
    RuntimeBindingTextArtifact,
    commit_runtime_binding_bundle,
    locked_store_mutation,
)
from codex_switch_update_policy import extract_semantic_version
from codex_switch_verify import run_app_server_smoke, run_bounded_process, sanitize_external_text


def login_config_uses_file_auth(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    return config_uses_file_auth(config_path.read_text())


def run_file_auth_login(
    command: list[str],
    base_env: dict[str, str],
    profile_dir: Path,
) -> int:
    ensure_private_dir(profile_dir)
    with tempfile.TemporaryDirectory(prefix="codex-switch-login-") as tmp:
        login_home = Path(tmp)
        atomic_write(
            login_home / "config.toml",
            b'cli_auth_credentials_store = "file"\n',
            mode=0o600,
        )
        env = dict(base_env)
        env["CODEX_HOME"] = str(login_home)
        result = subprocess.call(command, env=env)
        if result != 0:
            return result
        auth_path = login_home / "auth.json"
        if not auth_path.exists():
            raise SwitchError(f"Login completed but did not create auth.json in {login_home}.")
        copy_file_atomic(auth_path, profile_dir / "auth.json", mode=0o600)
    return 0


def _parity_error_codes(bundle: object) -> tuple[str, ...]:
    codes: set[str] = set()
    sources = (
        getattr(bundle, "findings", ()),
        getattr(getattr(bundle, "receipt", None), "findings", ()),
        getattr(getattr(bundle, "config_projection", None), "findings", ()),
    )
    for findings in sources:
        try:
            entries = tuple(findings)
        except TypeError:
            continue
        for finding in entries:
            if getattr(finding, "severity", "") != "error":
                continue
            code = getattr(finding, "code", "")
            if isinstance(code, str) and code:
                codes.add(code)
    return tuple(sorted(codes))


def _require_healthy_parity_bundle(bundle: object) -> None:
    receipt = getattr(bundle, "receipt", None)
    projection = getattr(bundle, "config_projection", None)
    if (
        getattr(bundle, "healthy", None) is not True
        or getattr(receipt, "healthy", None) is not True
        or getattr(projection, "healthy", None) is not True
    ):
        codes = _parity_error_codes(bundle)
        suffix = f": {', '.join(codes)}" if codes else ""
        raise SwitchError(f"Internal parity preparation is unhealthy{suffix}")
    overlay = getattr(bundle, "overlay", None)
    receipt_payload = getattr(bundle, "receipt_payload", None)
    overlay_payload = getattr(overlay, "overlay_payload", None)
    manifest_metadata = getattr(bundle, "manifest_metadata", None)
    staged_runtime_payload = getattr(
        bundle,
        "staged_runtime_config_payload",
        None,
    )
    if (
        not isinstance(receipt_payload, bytes)
        or not receipt_payload
        or not isinstance(overlay_payload, bytes)
        or not overlay_payload
        or not isinstance(manifest_metadata, Mapping)
        or not manifest_metadata
        or not isinstance(staged_runtime_payload, bytes)
        or not staged_runtime_payload
    ):
        raise SwitchError("Internal parity preparation bundle is incomplete")


def _parity_projection_payloads(bundle: object) -> dict[Path, bytes]:
    projection = getattr(bundle, "config_projection", None)
    try:
        entries = tuple(getattr(projection, "payloads"))
    except (AttributeError, TypeError) as exc:
        raise SwitchError(
            "Internal parity config projection is incomplete"
        ) from exc
    payloads: dict[Path, bytes] = {}
    for entry in entries:
        if (
            not isinstance(entry, tuple)
            or len(entry) != 2
            or not isinstance(entry[0], Path)
            or not isinstance(entry[1], bytes)
            or entry[0] in payloads
        ):
            raise SwitchError(
                "Internal parity config projection is invalid"
            )
        payloads[entry[0]] = entry[1]
    return payloads


def _read_stable_active_record(active_path: Path) -> dict[str, object] | None:
    try:
        before = active_path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SwitchError(
            f"Active profile record is not a regular file: {active_path}"
        )
    max_bytes = 64 * 1024
    if before.st_size > max_bytes:
        raise SwitchError(f"Active profile record is oversized: {active_path}")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise SwitchError(
            f"Active profile record cannot be opened safely: {active_path}"
        )
    try:
        descriptor = os.open(active_path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise SwitchError(
            f"Active profile record cannot be opened safely: {active_path}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mode,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity
            != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mode,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
        ):
            raise SwitchError(
                f"Active profile record changed before reading: {active_path}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SwitchError(
                    f"Active profile record is oversized: {active_path}"
                )
            chunks.append(chunk)
        read_complete = os.fstat(descriptor)
        try:
            after = active_path.lstat()
        except OSError as exc:
            raise SwitchError(
                f"Active profile record changed while reading: {active_path}"
            ) from exc
        if (
            (
                read_complete.st_dev,
                read_complete.st_ino,
                read_complete.st_size,
                read_complete.st_mode,
                read_complete.st_mtime_ns,
                read_complete.st_ctime_ns,
            )
            != identity
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mode,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != identity
        ):
            raise SwitchError(
                f"Active profile record changed while reading: {active_path}"
            )
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        active = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwitchError(f"Invalid JSON: {active_path}: {exc}") from exc
    if not isinstance(active, dict):
        raise SwitchError(f"Active profile record must be an object: {active_path}")
    return active


def _materialized_active_internal_config(
    store: object,
    internal_binding: object,
) -> Path | None:
    active_path = Path(getattr(store, "active_path"))
    active = _read_stable_active_record(active_path)
    if active is None:
        return None
    profile = active.get("profile")
    if not isinstance(profile, str) or not profile:
        raise SwitchError(
            f"Active profile record has an invalid profile: {active_path}"
        )
    raw_home = active.get("codex_home") or active.get("live_codex_home")
    if not isinstance(raw_home, str) or not raw_home:
        raise SwitchError(
            f"Active profile record has an invalid home: {active_path}"
        )
    active_home = Path(raw_home).expanduser()
    if not active_home.is_absolute():
        raise SwitchError(
            f"Active profile record home is not absolute: {active_path}"
        )
    active_home = Path(os.path.normpath(str(active_home)))
    if profile != "internal":
        return None
    binding_home = Path(getattr(internal_binding, "codex_home"))
    if active_home != binding_home:
        raise SwitchError(
            "Active internal home does not match the internal Runtime Binding"
        )
    config_path = binding_home / "config.toml"
    try:
        config_info = config_path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(config_info.st_mode) or not stat.S_ISREG(
        config_info.st_mode
    ):
        raise SwitchError(
            f"Active internal config is not a regular file: {config_path}"
        )
    return config_path


@dataclass(frozen=True)
class PrivateRuntimePreparation:
    """Validated private apply context, never populated by public CLI arguments."""
    candidate_manifest: Mapping[str, object]
    source_home: Path | None
    update_id: str
    input_fingerprint: str
    frozen_input_validator: Callable[[], None]
    allow_absent_profile: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_manifest, Mapping) or not callable(self.frozen_input_validator) or type(self.allow_absent_profile) is not bool:
            raise SwitchError("Private runtime preparation contract is invalid")
        if self.source_home is not None and (not isinstance(self.source_home, Path) or not self.source_home.is_absolute()):
            raise SwitchError("Private runtime source home must be absolute")
        object.__setattr__(self, "candidate_manifest", MappingProxyType(dict(self.candidate_manifest)))


def _private_auth_snapshot(source_home: Path) -> tuple[bytes | None, object]:
    from codex_switch_parity import _regular_file_snapshot, ParityValidationError
    path = source_home / "auth.json"
    try:
        snapshot = _regular_file_snapshot(path, code="parity.auth.source_unsafe", label="private authentication input", max_bytes=1024 * 1024, capture_payload=True)
    except ParityValidationError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
        return None, None
    try:
        document = json.loads(snapshot.payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SwitchError("Private authentication input is malformed") from exc
    if not isinstance(document, dict):
        raise SwitchError("Private authentication input must be an object")
    return snapshot.payload, snapshot


@dataclass(frozen=True)
class _InternalRuntimeRebindResult:
    binding: object
    manifest: Mapping[str, object]
    capability_receipt: object
    parity_bundle: object
    artifacts: tuple[RuntimeBindingTextArtifact, ...]
    terminal_receipt: Mapping[str, object] | None = None


def _stable_executable_identity(
    path: Path,
    *,
    label: str,
) -> tuple[Path, int, str]:
    canonical = Path(os.path.normpath(str(path.expanduser())))
    if not canonical.is_absolute() or not canonical.name:
        raise SwitchError(f"{label} path must be absolute: {path}")
    try:
        before = canonical.lstat()
    except OSError as exc:
        raise SwitchError(f"{label} is unavailable: {canonical}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_mode & 0o111 == 0
    ):
        raise SwitchError(
            f"{label} must be a regular executable: {canonical}"
        )
    try:
        descriptor = os.open(
            canonical,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SwitchError(f"{label} cannot be opened safely: {canonical}") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mode,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_mode & 0o111 == 0
            or identity
            != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mode,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
        ):
            raise SwitchError(f"{label} changed before reading: {canonical}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        completed = os.fstat(descriptor)
        try:
            after = canonical.lstat()
        except OSError as exc:
            raise SwitchError(
                f"{label} changed while reading: {canonical}"
            ) from exc
        if (
            (
                completed.st_dev,
                completed.st_ino,
                completed.st_size,
                completed.st_mode,
                completed.st_mtime_ns,
                completed.st_ctime_ns,
            )
            != identity
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mode,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != identity
        ):
            raise SwitchError(f"{label} changed while reading: {canonical}")
        try:
            resolved = canonical.resolve(strict=True)
        except OSError as exc:
            raise SwitchError(
                f"{label} cannot be resolved after reading: {canonical}"
            ) from exc
        return (
            resolved,
            stat.S_IMODE(completed.st_mode),
            digest.hexdigest(),
        )
    finally:
        os.close(descriptor)


def _exact_executable_version(
    path: Path,
    *,
    expected_version: str,
    label: str,
) -> str:
    try:
        with tempfile.TemporaryDirectory(prefix="codex-binding-version-") as temp:
            result = run_bounded_process(
                [str(path), "--version"], kind="runtime version",
                env={"PATH": os.environ.get("PATH", os.defpath), "HOME": temp,
                     "CODEX_HOME": temp, "PYTHONDONTWRITEBYTECODE": "1"},
                cwd=temp, timeout_seconds=5,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SwitchError(f"{label} version probe failed: {path}") from exc
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or result.timed_out:
        raise SwitchError(
            f"{label} version probe failed (exit {result.returncode})"
        )
    observed = extract_semantic_version(output)
    if observed != expected_version:
        raise SwitchError(
            f"{label} version mismatch: expected {expected_version} but "
            f"observed {observed or '<unparseable>'}"
        )
    return observed


def cmd_login(args: argparse.Namespace) -> None:
    store = make_store(args)
    manifest = store.load_manifest(args.name)
    codex_bin = str(manifest.get("codex_bin", ""))
    if args.codex_bin:
        codex_bin = resolve_codex_bin(args.codex_bin)
    if not codex_bin:
        raise SwitchError("No codex_bin configured for this profile.")
    profile_dir = store.profile_dir(args.name)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(profile_dir)
    command = [codex_bin, "login"]
    if args.with_api_key:
        command.append("--with-api-key")
    print(f"Running login for {args.name} with CODEX_HOME={profile_dir}")
    if login_config_uses_file_auth(profile_dir / "config.toml"):
        print("File auth profile detected; using a clean temporary CODEX_HOME for Codex login.")
        raise SystemExit(run_file_auth_login(command, env, profile_dir))
    raise SystemExit(subprocess.call(command, env=env))


def cmd_set_bin(
    args: argparse.Namespace,
) -> _InternalRuntimeRebindResult | None:
    store = make_store(args)
    if args.name == "openai-official":
        raise SwitchError(
            "openai-official is owned by the verified ChatGPT.app bundled CLI; "
            "update ChatGPT.app or rerun init instead of set-bin"
        )
    private_inputs = getattr(args, "rebind_private_inputs", None)
    if private_inputs is not None and (args.name != "internal" or not isinstance(private_inputs, PrivateRuntimePreparation)):
        raise SwitchError("Private runtime preparation contract is invalid")
    manifest_path = store.manifest_path(args.name)
    if private_inputs is not None and private_inputs.allow_absent_profile and not os.path.lexists(manifest_path):
        if os.path.lexists(store.profile_dir(args.name)):
            raise SwitchError("Absent profile publication found a partial profile")
        manifest = None
    else:
        manifest = store.load_manifest(args.name)
    requested_swap = getattr(args, "rebind_executable_swap", None)
    absent_target = (
        private_inputs is not None
        and isinstance(requested_swap, RuntimeBindingExecutableSwap)
        and requested_swap.old_sha256 is None
        and requested_swap.old_mode is None
        and requested_swap.bound_path == Path(args.codex_bin).expanduser()
        and not os.path.lexists(requested_swap.bound_path)
    )
    codex_bin = (
        str(requested_swap.bound_path) if absent_target else (
            resolve_internal_codex_bin(args.codex_bin)
            if args.name == "internal"
            else resolve_codex_bin(args.codex_bin)
        )
    )
    if not codex_bin:
        raise SwitchError("No codex binary path provided and none found on PATH.")
    bin_path = Path(codex_bin).expanduser()
    if not bin_path.exists() and not (
        private_inputs is not None
        and isinstance(getattr(args, "rebind_executable_swap", None), RuntimeBindingExecutableSwap)
        and args.rebind_executable_swap.bound_path == bin_path
        and args.rebind_executable_swap.old_sha256 is None
    ):
        raise SwitchError(f"codex_bin does not exist: {bin_path}")

    if args.name != "internal":
        manifest["codex_bin"] = str(bin_path)
        if not args.preserve_app_cli:
            manifest["app_cli_path"] = str(bin_path)
            manifest["app_cli_binding"] = "launchagent"
        manifest["updated_at"] = now_stamp()
        write_json(store.manifest_path(args.name), manifest)
        print(f"Updated {args.name} codex_bin: {bin_path}")
        if not args.preserve_app_cli:
            print(f"Updated {args.name} app_cli_path: {bin_path}")
        return

    if args.preserve_app_cli:
        raise SwitchError(
            "internal always binds ChatGPT Desktop through its managed launcher; "
            "--preserve-app-cli is not supported"
        )
    raw_execution_bin = getattr(
        args,
        "rebind_execution_codex_bin",
        str(bin_path),
    )
    execution_bin_path = Path(
        resolve_internal_codex_bin(str(raw_execution_bin))
    ).expanduser()
    executable_swap = getattr(args, "rebind_executable_swap", None)
    if executable_swap is not None and not isinstance(
        executable_swap,
        RuntimeBindingExecutableSwap,
    ):
        raise SwitchError("Internal executable swap contract is invalid")
    official_manifest_path = store.manifest_path("openai-official")
    try:
        original_official_manifest_payload = official_manifest_path.read_bytes()
    except FileNotFoundError:
        original_official_manifest_payload = None
    home_plan = resolve_runtime_homes(store)
    try:
        selected_official_manifest_payload = official_manifest_path.read_bytes()
    except FileNotFoundError:
        selected_official_manifest_payload = None
    if selected_official_manifest_payload != original_official_manifest_payload:
        raise SwitchError("Official home binding changed during runtime selection")
    official_manifest_update = home_plan.manifest_updates.get("openai-official")
    if home_plan.official.source == "default" and home_plan.official.path == store.official_codex_home:
        # A default home needs no new binding record just to refresh evidence.
        official_manifest_update = None
    official_manifest_payload = (
        (json.dumps(official_manifest_update, indent=2, sort_keys=True) + "\n").encode()
        if official_manifest_update is not None else None
    )
    # Keep the legacy capture source separate from the resolved runtime homes.
    store.official_codex_home = home_plan.official.path
    store.internal_codex_home = home_plan.internal.path
    candidate_manifest = dict(private_inputs.candidate_manifest if private_inputs is not None else home_plan.manifest_updates.get("internal", manifest))
    if private_inputs is not None:
        candidate_manifest["codex_home"] = str(home_plan.internal.path)
        candidate_manifest["home_selection_confirmed"] = True
        candidate_manifest.setdefault("name", "internal")
        candidate_manifest.setdefault("managed_files", ["config.toml", "auth.json"])
    candidate_manifest.pop("internal_cli_generation", None)
    candidate_manifest.pop("internal_app_readiness", None)
    candidate_manifest["codex_bin"] = str(bin_path)
    candidate_manifest["app_cli_path"] = str(store.bin_dir / "codex-internal-app")
    candidate_manifest["app_cli_binding"] = "launchagent"
    candidate_manifest["runtime_binding"] = "canonical"
    candidate_manifest["updated_at"] = now_stamp()
    execution_manifest = dict(candidate_manifest)
    execution_manifest["codex_bin"] = str(execution_bin_path)
    execution_binding = resolve_store_runtime_binding(
        store, "internal", manifest=execution_manifest,
        inventory=DesktopInventory(current=None),
    )
    candidate_binding = (
        replace(execution_binding, shell_cli=bin_path, backend_cli=bin_path)
        if private_inputs is not None and not bin_path.exists()
        else resolve_store_runtime_binding(store, "internal", manifest=candidate_manifest,
            inventory=DesktopInventory(current=None))
    )

    preparation_lock = (
        locked_store_mutation(store, operation="runtime rebind")
        if executable_swap is None and private_inputs is None
        else nullcontext(None)
    )
    with preparation_lock as locked_store:
        if locked_store is not None:
            locked_store.revalidate()
        if hasattr(args, "expected_active_selection_payload"):
            expected_active_payload = getattr(
                args,
                "expected_active_selection_payload",
            )
            if expected_active_payload is not None and not isinstance(
                expected_active_payload,
                bytes,
            ):
                raise SwitchError(
                    "Expected active selection snapshot is invalid"
                )
            require_active_profile_selection_payload(
                store.active_path,
                expected_active_payload,
            )
        manifest_path = store.manifest_path("internal")
        current_manifest = store.load_manifest("internal") if os.path.lexists(manifest_path) else None
        if current_manifest != manifest:
            raise SwitchError("Internal manifest changed before runtime rebind")
        original_manifest_payload = manifest_path.read_bytes() if current_manifest is not None else None
        with tempfile.TemporaryDirectory(
            prefix=".runtime-rebind-",
            dir=store.root,
        ) as temp_dir:
            stage_root = Path(temp_dir)
            staged_launcher = stage_root / "codex-internal-app"
            smoke_launcher = stage_root / "codex-internal-app-smoke"
            smoke_home = stage_root / "smoke-home"
            smoke_home.mkdir(mode=0o700)
            child_receipt_path = stage_root / "proxy-child.json"
            final_receipt_path = capability_receipt_path_for_launcher(
                candidate_binding.desktop_cli
            )
            raw_receipt_path = (current_manifest or {}).get(
                "app_capability_receipt_path"
            )
            receipt_path_matches = (
                isinstance(raw_receipt_path, str)
                and Path(raw_receipt_path).expanduser() == final_receipt_path
            )
            receipt_artifact = prepare_capability_receipt_artifact(
                execution_binding.backend_cli,
                receipt_path=(
                    final_receipt_path if receipt_path_matches else None
                ),
                expected_payload_sha256=(
                    str(
                        (current_manifest or {}).get(
                            "app_capability_receipt_sha256"
                        )
                        or ""
                    )
                    if receipt_path_matches
                    else ""
                ),
                expected_schema_sha256=(
                    str((current_manifest or {}).get("app_schema_sha256") or "")
                    if receipt_path_matches
                    else ""
                ),
                schema_timeout_seconds=5.0,
                probe_timeout_seconds=5.0,
            )
            capability_receipt = receipt_artifact.receipt
            receipt_payload = receipt_artifact.payload
            receipt_sha256 = receipt_artifact.payload_sha256
            candidate_manifest["app_capability_receipt_path"] = str(
                final_receipt_path
            )
            candidate_manifest["app_capability_receipt_sha256"] = receipt_sha256
            candidate_manifest["app_schema_sha256"] = (
                capability_receipt.schema_sha256
            )
            desktop_inventory = getattr(
                args,
                "rebind_desktop_inventory",
                None,
            )
            if (
                desktop_inventory is not None
                and not isinstance(desktop_inventory, DesktopInventory)
            ):
                raise SwitchError(
                    "Internal parity Desktop inventory is invalid"
                )
            official_binding = resolve_store_runtime_binding(
                store,
                "openai-official",
                manifest={},
                inventory=desktop_inventory,
            )
            profile_config_path = (
                store.profile_dir("internal") / "config.toml"
            )
            shared_config_path = store.official_codex_home / "config.toml"
            source_home = private_inputs.source_home if private_inputs is not None else None
            source_config_path = source_home / "config.toml" if source_home is not None else profile_config_path
            config_inputs = ConfigInputs.capture(
                profile_config=profile_config_path, profile_source=source_config_path,
                source_paths=(source_config_path, shared_config_path),
                optional_sources=(shared_config_path,),
            )
            auth_payload, auth_snapshot = _private_auth_snapshot(source_home) if source_home is not None else (None, None)
            if source_home is not None and auth_payload is None:
                from codex_switch_config_document import ConfigDocument
                config_data = ConfigDocument.parse(source_config_path.read_text(), "private runtime config").data
                providers = config_data.get("model_providers")
                provider = providers.get(config_data.get("model_provider"), {}) if isinstance(providers, Mapping) else {}
                if isinstance(provider, Mapping) and provider.get("requires_openai_auth") is True:
                    raise SwitchError("Private runtime input requires auth.json")
            active_runtime_config_path = _materialized_active_internal_config(
                store,
                candidate_binding,
            )
            if source_home is not None:
                active_runtime_config_path = candidate_binding.codex_home / "config.toml"
            parity_bundle = prepare_parity_bundle(
                ParityCandidate(
                    official_binding=official_binding,
                    internal_binding=execution_binding,
                    internal_manifest=candidate_manifest,
                    capability_receipt=receipt_artifact,
                    source_config=config_inputs,
                    auth_payload=auth_payload,
                    adapter_rule_set_sha256=(
                        protocol_adapter_rule_set_digest()
                    ),
                    active_runtime_config_path=active_runtime_config_path,
                    canonical_internal_binding=candidate_binding,
                ),
                work_root=stage_root,
                timeouts=ParityTimeouts(),
            )
            _require_healthy_parity_bundle(parity_bundle)
            candidate_manifest.update(
                dict(parity_bundle.manifest_metadata)
            )
            if isinstance(parity_bundle, ParityBundle):
                candidate_manifest["parity_catalog_provenance"] = catalog_provenance_for_bundle(parity_bundle)
            staged_runtime_payload = (
                parity_bundle.staged_runtime_config_payload
            )
            atomic_write(
                smoke_home / "config.toml",
                staged_runtime_payload,
                mode=0o600,
            )
            if auth_payload is not None:
                atomic_write(smoke_home / "auth.json", auth_payload, mode=0o600)
            parity_staged_receipt = getattr(
                parity_bundle,
                "staged_capability_receipt_path",
                None,
            )
            parity_staged_receipt_payload = getattr(
                parity_bundle,
                "staged_capability_receipt_payload",
                None,
            )
            if (
                not isinstance(parity_staged_receipt, Path)
                or parity_staged_receipt_payload != receipt_payload
                or parity_staged_receipt.is_symlink()
                or not parity_staged_receipt.is_file()
                or parity_staged_receipt.read_bytes() != receipt_payload
            ):
                raise SwitchError(
                    "Internal parity staged capability receipt is invalid"
                )
            write_profile_app_wrapper(
                store=store,
                name="internal",
                app_cli_path=str(smoke_launcher),
                codex_bin=str(execution_binding.backend_cli),
                switch_scripts=Path(__file__).resolve().parent,
                capability_receipt_path=final_receipt_path,
                schema_sha256=capability_receipt.schema_sha256,
                capability_receipt_sha256=receipt_sha256,
                manifest_override=candidate_manifest,
            )
            smoke_launcher_payload = smoke_launcher.read_bytes()
            smoke_launcher_payload = smoke_launcher_payload.replace(
                b"set -eu\n", ("set -eu\ncd " + shlex.quote(str(smoke_home)) + "\n").encode(), 1,
            )
            atomic_write(smoke_launcher, smoke_launcher_payload, mode=0o755)
            if (
                b"codex_switch_app_proxy.py" not in smoke_launcher_payload
                or str(execution_binding.backend_cli).encode()
                not in smoke_launcher_payload
            ):
                raise SwitchError(
                    "Staged internal smoke launcher does not retain the "
                    "candidate proxy/backend chain"
                )
            write_profile_app_wrapper(
                store=store,
                name="internal",
                app_cli_path=str(staged_launcher),
                codex_bin=str(execution_binding.backend_cli if not bin_path.exists() else candidate_binding.backend_cli),
                switch_scripts=Path(__file__).resolve().parent,
                capability_receipt_path=final_receipt_path,
                schema_sha256=capability_receipt.schema_sha256,
                capability_receipt_sha256=receipt_sha256,
                manifest_override=candidate_manifest,
            )
            staged_payload = staged_launcher.read_bytes()
            if not bin_path.exists():
                previous_assignment = ("CODEX_BIN=" + shlex.quote(str(execution_binding.backend_cli)) + "\n").encode()
                final_assignment = ("CODEX_BIN=" + shlex.quote(str(candidate_binding.backend_cli)) + "\n").encode()
                if staged_payload.count(previous_assignment) != 1:
                    raise SwitchError("Candidate wrapper backend assignment is ambiguous")
                staged_payload = staged_payload.replace(previous_assignment, final_assignment, 1)
                atomic_write(staged_launcher, staged_payload, mode=0o755)
            if (
                b"codex_switch_app_proxy.py" not in staged_payload
                or str(candidate_binding.backend_cli).encode() not in staged_payload
            ):
                raise SwitchError(
                    "Staged internal launcher does not retain the requested proxy/backend chain"
                )
            expected_fingerprint = hashlib.sha256(staged_payload).hexdigest()
            candidate_manifest["app_launcher_sha256"] = expected_fingerprint
            candidate_binding = replace(
                candidate_binding,
                launcher_fingerprint=expected_fingerprint,
            )
            code, smoke_output = run_app_server_smoke(
                str(smoke_launcher),
                smoke_home,
                isolate_home=True,
                extra_env={
                    "HOME": str(smoke_home),
                    "XDG_CONFIG_HOME": str(smoke_home / ".config"),
                    "CODEX_SWITCH_REBIND_SMOKE": "1",
                    "CODEX_SWITCH_REBIND_SMOKE_HOME": str(smoke_home),
                    "CODEX_SWITCH_REBIND_CAPABILITY_RECEIPT": str(
                        parity_staged_receipt
                    ),
                    "CODEX_SWITCH_PROXY_CHILD_RECEIPT": str(child_receipt_path),
                },
            )
            if code != 0:
                raise SwitchError(
                    "Internal rebind compatibility smoke failed: "
                    f"{sanitize_external_text(smoke_output)}"
                )
            try:
                child_receipt = json.loads(child_receipt_path.read_text())
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise SwitchError(
                    "Internal rebind smoke produced no valid proxy child receipt"
                ) from exc
            observed_backend = child_receipt.get("codex_bin")
            observed_args = child_receipt.get("args")
            if (
                not isinstance(observed_backend, str)
                or Path(observed_backend).resolve()
                != execution_binding.backend_cli.resolve()
                or not isinstance(observed_args, list)
                or not observed_args
                or observed_args[0] != "app-server"
                or child_receipt.get("capability_receipt_path")
                != str(parity_staged_receipt)
                or child_receipt.get("expected_schema_sha256")
                != capability_receipt.schema_sha256
                or child_receipt.get("expected_receipt_sha256")
                != receipt_sha256
                or child_receipt.get("config_write_proven") is not True
            ):
                raise SwitchError(
                    "Internal rebind smoke child does not match the requested backend"
                )
            smoke_binding = replace(
                execution_binding,
                desktop_cli=smoke_launcher,
                launcher_fingerprint=managed_launcher_fingerprint(
                    smoke_launcher
                ),
            )
            attestation = attest_runtime_binding(
                smoke_binding,
                RuntimeObservation(
                    processes=(
                        SimpleNamespace(
                            pid=child_receipt.get("pid", 0),
                            ppid=0,
                            kind="app-server",
                            command_path=observed_backend,
                            app_cli_env=str(smoke_binding.desktop_cli),
                            parent_command="codex_switch_app_proxy.py",
                            host_kind="",
                        ),
                    ),
                    managed_launcher_fingerprint=managed_launcher_fingerprint(
                        smoke_launcher
                    ),
                ),
            )
            if not attestation.healthy:
                codes = ", ".join(
                    finding.code
                    for finding in attestation.findings
                    if finding.severity == "error"
                )
                raise SwitchError(
                    f"Internal rebind child attestation failed: {codes}"
                )
            if not capability_receipt.matches_backend_and_schema_digest(
                execution_binding.backend_cli,
                capability_receipt.schema_sha256,
            ):
                raise SwitchError(
                    "Capability backend changed during capability receipt generation"
                )
            manifest_payload = (
                json.dumps(candidate_manifest, indent=2, sort_keys=True).encode()
                + b"\n"
            )

            def validate_rebind_inputs() -> None:
                marker_present = (
                    store.root / ".runtime-binding-rebind.json"
                ).exists()
                if marker_present:
                    revalidate_parity_bundle_immutable_inputs(
                        parity_bundle
                    )
                else:
                    revalidate_parity_bundle_inputs(parity_bundle)
                if not marker_present and manifest is None and os.path.lexists(store.profile_dir("internal")):
                    raise SwitchError("Absent internal profile changed before runtime publication")
                observed_manifest = store.load_manifest("internal") if os.path.lexists(manifest_path) else None
                if not marker_present:
                    manifest_matches = (
                        observed_manifest == manifest
                        and (manifest_path.read_bytes() if observed_manifest is not None else None)
                        == original_manifest_payload
                    )
                elif observed_manifest == manifest:
                    manifest_matches = (
                        (manifest_path.read_bytes() if observed_manifest is not None else None)
                        == original_manifest_payload
                    )
                else:
                    manifest_matches = (
                        observed_manifest == candidate_manifest
                        and manifest_path.read_bytes() == manifest_payload
                    )
                if not manifest_matches:
                    raise SwitchError(
                        "Internal manifest changed before runtime rebind"
                    )
                try:
                    observed_official = official_manifest_path.read_bytes()
                except FileNotFoundError:
                    observed_official = None
                allowed_official = {original_official_manifest_payload}
                if marker_present and official_manifest_payload is not None:
                    allowed_official.add(official_manifest_payload)
                if observed_official not in allowed_official:
                    raise SwitchError("Official home binding changed before runtime rebind")
                if source_home is None and (
                    _materialized_active_internal_config(
                        store,
                        candidate_binding,
                    )
                    != active_runtime_config_path
                ):
                    raise SwitchError(
                        "Active internal runtime config changed before rebind"
                    )

                if source_home is not None and _private_auth_snapshot(source_home) != (auth_payload, auth_snapshot):
                    raise SwitchError("Private authentication input changed before runtime publication")

            validate_rebind_inputs()
            projection_payloads = _parity_projection_payloads(parity_bundle)
            try:
                profile_config_payload = projection_payloads[
                    profile_config_path
                ]
            except KeyError as exc:
                raise SwitchError(
                    "Internal parity profile config projection is missing"
                ) from exc
            changed_paths = set(
                getattr(
                    parity_bundle.config_projection,
                    "changed_paths",
                    (),
                )
            )
            artifacts = [
                *(RuntimeBindingTextArtifact(
                    role=f"parity_{side}_model_source", path=path, payload=payload, mode=0o600,
                ) for side, path, payload in getattr(parity_bundle, "model_source_artifacts", ())),
                RuntimeBindingTextArtifact(
                    role="parity_overlay",
                    path=(
                        store.profile_dir("internal")
                        / "parity"
                        / "model-catalog.json"
                    ),
                    payload=parity_bundle.overlay.overlay_payload,
                    mode=0o600,
                ),
                RuntimeBindingTextArtifact(
                    role="capability_receipt",
                    path=final_receipt_path,
                    payload=receipt_payload,
                    mode=0o600,
                ),
                RuntimeBindingTextArtifact(
                    role="parity_receipt",
                    path=(
                        store.profile_dir("internal")
                        / "parity"
                        / "receipt.json"
                    ),
                    payload=parity_bundle.receipt_payload,
                    mode=0o600,
                ),
            ]
            if official_manifest_payload is not None:
                artifacts.append(RuntimeBindingTextArtifact(
                    role="official_manifest", path=official_manifest_path,
                    payload=official_manifest_payload, mode=0o600,
                ))
            if shared_config_path in changed_paths:
                try:
                    shared_config_payload = projection_payloads[
                        shared_config_path
                    ]
                except KeyError as exc:
                    raise SwitchError(
                        "Internal parity shared config projection is missing"
                    ) from exc
                artifacts.append(
                    RuntimeBindingTextArtifact(
                        role="shared_config",
                        path=shared_config_path,
                        payload=shared_config_payload,
                        mode=0o600,
                    )
                )
            artifacts.append(
                RuntimeBindingTextArtifact(
                    role="profile_config",
                    path=profile_config_path,
                    payload=profile_config_payload,
                    mode=0o600,
                )
            )
            active_runtime_path = getattr(
                parity_bundle,
                "active_runtime_config_path",
                None,
            )
            active_runtime_payload = getattr(
                parity_bundle,
                "active_runtime_config_payload",
                None,
            )
            if (
                active_runtime_path is not None
                or active_runtime_payload is not None
            ):
                if (
                    not isinstance(active_runtime_path, Path)
                    or not isinstance(active_runtime_payload, bytes)
                ):
                    raise SwitchError(
                        "Internal parity active runtime config is incomplete"
                    )
                artifacts.append(
                    RuntimeBindingTextArtifact(
                        role="active_runtime_config",
                        path=active_runtime_path,
                        payload=active_runtime_payload,
                        mode=0o600,
                    )
                )
            if source_home is not None:
                artifacts.extend((
                    RuntimeBindingTextArtifact("profile_auth", store.profile_dir("internal") / "auth.json", auth_payload, 0o600),
                    RuntimeBindingTextArtifact("active_runtime_auth", candidate_binding.codex_home / "auth.json", auth_payload, 0o600),
                ))
            artifacts.extend(
                (
                    RuntimeBindingTextArtifact(
                        role="launcher",
                        path=candidate_binding.desktop_cli,
                        payload=staged_payload,
                        mode=0o755,
                    ),
                    RuntimeBindingTextArtifact(
                        role="manifest",
                        path=store.manifest_path("internal"),
                        payload=manifest_payload,
                        mode=0o600,
                    ),
                )
            )
            artifact_tuple = tuple(artifacts)
            rebind_result = _InternalRuntimeRebindResult(
                binding=candidate_binding,
                manifest=MappingProxyType(dict(candidate_manifest)),
                capability_receipt=receipt_artifact,
                parity_bundle=parity_bundle,
                artifacts=artifact_tuple,
            )
            prepared_validator = getattr(
                args,
                "rebind_prepared_validator",
                None,
            )
            if prepared_validator is not None and not callable(
                prepared_validator
            ):
                raise SwitchError(
                    "Internal runtime prepared validator is invalid"
                )
            retire_executable_backup = getattr(
                args,
                "rebind_retire_executable_backup",
                False,
            )
            if type(retire_executable_backup) is not bool:
                raise SwitchError(
                    "Internal runtime backup retirement flag is invalid"
                )
            if retire_executable_backup and (
                executable_swap is None
                or prepared_validator is None
            ):
                raise SwitchError(
                    "Internal runtime backup retirement requires the "
                    "post-promotion handshake"
                )
            commit_options: dict[str, object] = {
                "artifacts": artifact_tuple,
                "executable_swap": executable_swap,
                "input_validator": validate_rebind_inputs,
                "fault_hook": getattr(
                    args,
                    "rebind_commit_fault_hook",
                    None,
                ),
            }
            if private_inputs is not None:
                commit_options.update(update_id=private_inputs.update_id,
                    input_fingerprint=private_inputs.input_fingerprint,
                    allow_absent_profile=private_inputs.allow_absent_profile)
            if prepared_validator is not None:
                commit_options["prepared_validator"] = lambda: (
                    prepared_validator(rebind_result)
                )
            if retire_executable_backup:
                commit_options["retire_executable_backup"] = True
            commit_lock = (
                nullcontext(locked_store)
                if locked_store is not None
                else locked_store_mutation(
                    store,
                    operation="runtime update promotion",
                )
            )
            with commit_lock as active_lock:
                if active_lock is None:
                    raise SwitchError(
                        "Internal runtime promotion lock is unavailable"
                    )
                if private_inputs is not None:
                    private_inputs.frozen_input_validator()
                terminal_receipt = commit_runtime_binding_bundle(
                    active_lock,
                    **commit_options,
                )
                rebind_result = replace(rebind_result, terminal_receipt=terminal_receipt)

    if not getattr(args, "suppress_rebind_success_output", False):
        print(f"Updated internal codex_bin: {candidate_binding.backend_cli}")
        print(f"Updated internal app_cli_path: {candidate_binding.desktop_cli}")
        print(
            "Restart required: quit ChatGPT completely and reopen it to use the "
            "new internal backend."
        )
    return rebind_result


def _verify_internal_update_promotion(
    store: object,
    *,
    result: _InternalRuntimeRebindResult,
    executable_swap: RuntimeBindingExecutableSwap | None,
    target_version: str,
) -> None:
    expected_bound = executable_swap.bound_path if executable_swap is not None else result.binding.backend_cli
    bound_path, bound_mode, bound_sha256 = _stable_executable_identity(
        expected_bound, label="Promoted internal binary",
    )
    if executable_swap is not None:
        if (bound_path != executable_swap.bound_path or bound_mode != executable_swap.new_mode
            or bound_sha256 != executable_swap.new_sha256):
            raise SwitchError("Promoted internal binary does not match the staged candidate")
        if executable_swap.old_sha256 is None:
            if os.path.lexists(executable_swap.backup_path):
                raise SwitchError("First runtime publication has an unexpected backup")
        else:
            _backup_path, backup_mode, backup_sha256 = _stable_executable_identity(
                executable_swap.backup_path, label="Last-known-good internal backup",
            )
            if backup_mode != executable_swap.old_mode or backup_sha256 != executable_swap.old_sha256:
                raise SwitchError("Last-known-good internal backup does not match the prior binary")
        if os.path.lexists(executable_swap.candidate_path):
            raise SwitchError("Promoted internal candidate path was not retired by the swap")
    _exact_executable_version(
        bound_path,
        expected_version=target_version,
        label="Promoted internal binary",
    )

    manifest = store.load_manifest("internal")
    expected_manifest = dict(result.manifest)
    if manifest != expected_manifest:
        raise SwitchError(
            "Internal manifest does not match the promoted runtime generation"
        )
    binding = resolve_store_runtime_binding(
        store,
        "internal",
        manifest=manifest,
        inventory=DesktopInventory(current=None),
    )
    if (
        binding.backend_cli != bound_path
        or binding.desktop_cli != result.binding.desktop_cli
        or binding.launcher_fingerprint
        != result.binding.launcher_fingerprint
    ):
        raise SwitchError(
            "Canonical Runtime Binding does not match the promoted binary"
        )

    for artifact in result.artifacts:
        if artifact.payload is None:
            if os.path.lexists(artifact.path):
                raise SwitchError(f"Promoted runtime auth absence is invalid: {artifact.role}")
            continue
        try:
            info = artifact.path.lstat()
        except OSError as exc:
            raise SwitchError(
                f"Promoted runtime artifact is unavailable: {artifact.role}"
            ) from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != artifact.mode
            or artifact.path.read_bytes() != artifact.payload
        ):
            raise SwitchError(
                f"Promoted runtime artifact is invalid: {artifact.role}"
            )

    capability_artifact = result.capability_receipt
    capability_receipt = getattr(capability_artifact, "receipt", None)
    if (
        capability_receipt is None
        or not capability_receipt.matches_backend_and_schema_digest(
            bound_path,
            capability_receipt.schema_sha256,
        )
    ):
        raise SwitchError(
            "Promoted capability receipt does not match the bound binary"
        )
    parity_bundle = result.parity_bundle
    parity_receipt = getattr(parity_bundle, "receipt", None)
    internal_fingerprint = getattr(
        parity_receipt,
        "internal_fingerprint",
        None,
    )
    if (
        getattr(parity_receipt, "healthy", None) is not True
        or getattr(internal_fingerprint, "backend_cli", None) != bound_path
        or getattr(internal_fingerprint, "binary_sha256", None)
        != bound_sha256
    ):
        raise SwitchError(
            "Promoted parity receipt does not match the canonical binding"
        )
    revalidate_parity_bundle_immutable_inputs(parity_bundle)

    final_receipt_path = capability_receipt_path_for_launcher(
        binding.desktop_cli
    )
    final_overlay_path = Path(
        str(manifest.get("parity_overlay_path") or "")
    ).expanduser()
    if not final_overlay_path.is_absolute():
        raise SwitchError("Promoted parity overlay path is invalid")
    staged_runtime_payload = getattr(
        parity_bundle,
        "staged_runtime_config_payload",
        None,
    )
    if not isinstance(staged_runtime_payload, bytes):
        raise SwitchError(
            "Promoted parity runtime config evidence is incomplete"
        )
    smoke_runtime_payload = project_parity_runtime_config_payload(
        staged_runtime_payload,
        overlay_path=final_overlay_path,
    )
    with tempfile.TemporaryDirectory(
        prefix=".internal-update-handshake-",
        dir=store.root,
    ) as temp_dir:
        smoke_root = Path(temp_dir)
        smoke_home = smoke_root / "home"
        smoke_home.mkdir(mode=0o700)
        atomic_write(
            smoke_home / "config.toml",
            smoke_runtime_payload,
            mode=0o600,
        )
        child_receipt_path = smoke_root / "proxy-child.json"
        code, smoke_output = run_app_server_smoke(
            str(binding.desktop_cli),
            smoke_home,
            isolate_home=True,
            extra_env={
                "HOME": str(smoke_home),
                "XDG_CONFIG_HOME": str(smoke_home / ".config"),
                "CODEX_SWITCH_REBIND_SMOKE": "1",
                "CODEX_SWITCH_REBIND_SMOKE_HOME": str(smoke_home),
                "CODEX_SWITCH_REBIND_CAPABILITY_RECEIPT": str(
                    final_receipt_path
                ),
                "CODEX_SWITCH_PROXY_CHILD_RECEIPT": str(
                    child_receipt_path
                ),
            },
        )
        if code != 0:
            raise SwitchError(
                "Promoted internal app-server smoke failed: "
                f"{sanitize_external_text(smoke_output)}"
            )
        try:
            child_receipt = json.loads(child_receipt_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise SwitchError(
                "Promoted internal smoke produced no valid child receipt"
            ) from exc
        observed_backend = child_receipt.get("codex_bin")
        observed_args = child_receipt.get("args")
        if (
            not isinstance(observed_backend, str)
            or Path(observed_backend).resolve() != bound_path
            or not isinstance(observed_args, list)
            or not observed_args
            or observed_args[0] != "app-server"
            or child_receipt.get("capability_receipt_path")
            != str(final_receipt_path)
            or child_receipt.get("expected_schema_sha256")
            != capability_receipt.schema_sha256
            or child_receipt.get("expected_receipt_sha256")
            != capability_artifact.payload_sha256
            or child_receipt.get("config_write_proven") is not True
        ):
            raise SwitchError(
                "Promoted internal smoke child does not match the "
                "canonical binding"
            )
        attestation = attest_runtime_binding(
            binding,
            RuntimeObservation(
                processes=(
                    SimpleNamespace(
                        pid=child_receipt.get("pid", 0),
                        ppid=0,
                        kind="app-server",
                        command_path=observed_backend,
                        app_cli_env=str(binding.desktop_cli),
                        parent_command="codex_switch_app_proxy.py",
                        host_kind="",
                    ),
                ),
                managed_launcher_fingerprint=managed_launcher_fingerprint(
                    binding.desktop_cli
                ),
            ),
        )
        if not attestation.healthy:
            codes = ", ".join(
                finding.code
                for finding in attestation.findings
                if finding.severity == "error"
            )
            raise SwitchError(
                f"Promoted internal child attestation failed: {codes}"
            )


def _verify_internal_cli_update_promotion(
    store: object,
    *,
    expected_manifest: Mapping[str, object],
    executable_swap: RuntimeBindingExecutableSwap,
    target_version: str,
) -> None:
    bound_path, bound_mode, bound_sha256 = _stable_executable_identity(
        executable_swap.bound_path,
        label="Promoted internal CLI",
    )
    if (
        bound_path != executable_swap.bound_path
        or bound_mode != executable_swap.new_mode
        or bound_sha256 != executable_swap.new_sha256
    ):
        raise SwitchError(
            "Promoted internal CLI does not match the staged candidate"
        )
    _backup_path, backup_mode, backup_sha256 = _stable_executable_identity(
        executable_swap.backup_path,
        label="Last-known-good internal CLI backup",
    )
    if (
        backup_mode != executable_swap.old_mode
        or backup_sha256 != executable_swap.old_sha256
    ):
        raise SwitchError(
            "Last-known-good internal CLI backup does not match the prior binary"
        )
    if os.path.lexists(executable_swap.candidate_path):
        raise SwitchError(
            "Promoted internal CLI candidate path was not retired by the swap"
        )
    _exact_executable_version(
        bound_path,
        expected_version=target_version,
        label="Promoted internal CLI",
    )
    manifest = store.load_manifest("internal")
    if manifest != dict(expected_manifest):
        raise SwitchError(
            "Internal manifest does not match the promoted CLI generation"
        )
    try:
        manifest_backend = Path(
            str(manifest.get("codex_bin") or "")
        ).expanduser().resolve(strict=True)
    except OSError as exc:
        raise SwitchError(
            "Promoted internal CLI manifest backend is unavailable"
        ) from exc
    if manifest_backend != bound_path:
        raise SwitchError(
            "Promoted internal CLI manifest does not select the bound binary"
        )
    expected_generation = {
        "schema_version": 1,
        "scope": "cli-only",
        "backend_sha256": executable_swap.new_sha256,
        "backend_version": target_version,
    }
    if manifest.get("internal_cli_generation") != expected_generation:
        raise SwitchError(
            "Promoted internal CLI generation metadata is invalid"
        )
    if manifest.get("internal_app_readiness") != "unverified":
        raise SwitchError(
            "Promoted internal App readiness metadata is invalid"
        )
    internal_home = getattr(store, "internal_codex_home", None)
    if internal_home is None:
        internal_home = store.managed_home("internal")
    generation = validate_internal_cli_runtime_generation(
        manifest=manifest,
        fallback_home=Path(internal_home),
        fallback_backend=bound_path,
    )
    with tempfile.TemporaryDirectory(
        prefix="codex-switch-cli-promotion-shim-"
    ) as temp_dir:
        managed_shim = Path(temp_dir) / "codex"
        atomic_write(
            managed_shim,
            render_codex_shim_payload(
                store,
                str(generation.backend_cli),
                generation.codex_home,
                profile_name="internal",
            ),
            mode=0o755,
        )
        _exact_executable_version(
            managed_shim,
            expected_version=target_version,
            label="Managed internal CLI shell",
        )


def _promote_internal_cli_update(
    store: object,
    *,
    manifest: Mapping[str, object],
    executable_swap: RuntimeBindingExecutableSwap,
    target_version: str,
    update_id: str | None = None,
    input_fingerprint: str | None = None,
    frozen_input_validator: Callable[[], None] | None = None,
) -> dict[str, object] | None:
    if frozen_input_validator is not None and not callable(frozen_input_validator):
        raise SwitchError("Internal CLI-only frozen input validator is invalid")
    manifest_path = store.manifest_path("internal")
    original_manifest_payload = manifest_path.read_bytes()
    candidate_manifest = dict(manifest)
    candidate_manifest["internal_cli_generation"] = {
        "schema_version": 1,
        "scope": "cli-only",
        "backend_sha256": executable_swap.new_sha256,
        "backend_version": target_version,
    }
    candidate_manifest["internal_app_readiness"] = "unverified"
    candidate_manifest["updated_at"] = now_stamp()
    manifest_payload = (
        json.dumps(candidate_manifest, indent=2, sort_keys=True).encode()
        + b"\n"
    )

    def validate_inputs() -> None:
        marker_present = (
            store.root / ".runtime-binding-rebind.json"
        ).exists()
        observed_manifest = store.load_manifest("internal")
        observed_payload = manifest_path.read_bytes()
        if not marker_present:
            matches = (
                observed_manifest == dict(manifest)
                and observed_payload == original_manifest_payload
            )
        else:
            matches = (
                observed_manifest == dict(manifest)
                and observed_payload == original_manifest_payload
            ) or (
                observed_manifest == candidate_manifest
                and observed_payload == manifest_payload
            )
        if not matches:
            raise SwitchError(
                "Internal manifest changed before CLI-only promotion"
            )

    def validate_promoted_generation() -> None:
        _verify_internal_cli_update_promotion(
            store,
            expected_manifest=candidate_manifest,
            executable_swap=executable_swap,
            target_version=target_version,
        )

    artifact = RuntimeBindingTextArtifact(
        role="manifest",
        path=manifest_path,
        payload=manifest_payload,
        mode=0o600,
    )
    with locked_store_mutation(
        store,
        operation="internal CLI-only update promotion",
    ) as locked_store:
        if frozen_input_validator is not None:
            frozen_input_validator()
        terminal_receipt = commit_runtime_binding_bundle(
            locked_store,
            artifacts=(artifact,),
            executable_swap=executable_swap,
            input_validator=validate_inputs,
            prepared_validator=validate_promoted_generation,
            retire_executable_backup=True,
            bundle_scope="cli-only",
            update_id=update_id,
            input_fingerprint=input_fingerprint,
        )

    print(f"update-internal: verified CLI version {target_version}.")
    print("CLI-only promotion: passed")
    print(
        "Internal App readiness: unverified; split keeps Codex App on the "
        "official bundle."
    )
    return terminal_receipt


def cmd_promote_internal_update(args: argparse.Namespace) -> None:
    store = make_store(args)
    manifest = store.load_manifest("internal")
    bound_path, old_mode, old_sha256 = _stable_executable_identity(
        Path(args.bound_bin),
        label="Bound internal binary",
    )
    candidate_path, new_mode, new_sha256 = _stable_executable_identity(
        Path(args.candidate_bin),
        label="Staged internal candidate",
    )
    raw_backup_path = Path(
        os.path.normpath(str(Path(args.backup_bin).expanduser()))
    )
    if (
        not raw_backup_path.is_absolute()
        or not raw_backup_path.name
        or os.path.lexists(raw_backup_path)
    ):
        raise SwitchError(
            "Internal update backup path must be an unused bound-binary sibling"
        )
    try:
        backup_parent = raw_backup_path.parent.resolve(strict=True)
    except OSError as exc:
        raise SwitchError(
            "Internal update backup directory is unavailable"
        ) from exc
    if backup_parent != bound_path.parent:
        raise SwitchError(
            "Internal update backup path must be an unused bound-binary sibling"
        )
    backup_path = bound_path.parent / raw_backup_path.name
    if os.path.lexists(backup_path):
        raise SwitchError(
            "Internal update backup path must be an unused bound-binary sibling"
        )
    candidate_parent = candidate_path.parent
    try:
        candidate_parent_info = candidate_parent.lstat()
    except OSError as exc:
        raise SwitchError(
            "Internal update candidate directory is unavailable"
        ) from exc
    if (
        candidate_path.name != "codex"
        or candidate_parent.parent != bound_path.parent
        or not candidate_parent.name.startswith(
            ".codex-internal-update-"
        )
        or stat.S_ISLNK(candidate_parent_info.st_mode)
        or not stat.S_ISDIR(candidate_parent_info.st_mode)
        or stat.S_IMODE(candidate_parent_info.st_mode) != 0o700
    ):
        raise SwitchError(
            "Internal update candidate is not in a private sibling directory"
        )
    manifest_backend = Path(
        str(manifest.get("codex_bin") or "")
    ).expanduser()
    try:
        manifest_backend = manifest_backend.resolve(strict=True)
    except OSError as exc:
        raise SwitchError(
            "Internal manifest bound binary is unavailable"
        ) from exc
    if manifest_backend != bound_path:
        raise SwitchError(
            "Internal manifest changed before staged update promotion"
        )
    _exact_executable_version(
        candidate_path,
        expected_version=args.target_version,
        label="Staged internal candidate",
    )

    executable_swap = RuntimeBindingExecutableSwap(
        bound_path=bound_path,
        candidate_path=candidate_path,
        backup_path=backup_path,
        old_mode=old_mode,
        old_sha256=old_sha256,
        new_mode=new_mode,
        new_sha256=new_sha256,
    )

    if getattr(args, "cli_only", False):
        _promote_internal_cli_update(
            store,
            manifest=manifest,
            executable_swap=executable_swap,
            target_version=args.target_version,
        )
        return

    def promotion_validator(
        result: _InternalRuntimeRebindResult,
    ) -> None:
        _verify_internal_update_promotion(
            store,
            result=result,
            executable_swap=executable_swap,
            target_version=args.target_version,
        )

    rebind_args = SimpleNamespace(
        **vars(args),
        name="internal",
        codex_bin=str(bound_path),
        preserve_app_cli=False,
        rebind_execution_codex_bin=str(candidate_path),
        rebind_executable_swap=executable_swap,
        rebind_prepared_validator=promotion_validator,
        rebind_retire_executable_backup=True,
        suppress_rebind_success_output=True,
    )
    result = cmd_set_bin(rebind_args)
    if not isinstance(result, _InternalRuntimeRebindResult):
        raise SwitchError("Internal update promotion returned no runtime receipt")
    print(
        f"update-internal: verified installed version "
        f"{args.target_version}."
    )
    print("App-server smoke: passed")
    print("update-internal: capability and parity receipts verified.")
    print(
        "Restart required: quit ChatGPT completely and reopen it to use the "
        "new internal backend."
    )


def cmd_set_app_bin(args: argparse.Namespace) -> None:
    store = make_store(args)
    if args.name == "internal":
        raise SwitchError(
            "internal Desktop binding is the managed launcher; use `set-bin internal "
            "<backend>` instead of set-app-bin"
        )
    if args.name == "openai-official":
        raise SwitchError(
            "openai-official Desktop binding is owned by ChatGPT.app; rerun init "
            "after updating ChatGPT.app"
        )
    manifest = store.load_manifest(args.name)
    app_cli_path = resolve_path(args.app_cli_path)
    bin_path = validate_executable_path(app_cli_path, "app_cli_path")

    manifest["app_cli_path"] = str(bin_path)
    manifest["app_cli_binding"] = "launchagent"
    manifest["updated_at"] = now_stamp()
    write_json(store.manifest_path(args.name), manifest)
    print(f"Updated {args.name} app_cli_path: {bin_path}")


def cmd_shim_env(args: argparse.Namespace) -> None:
    store = make_store(args)
    print(f'export PATH="{store.bin_dir}:$PATH"')
    print("hash -r 2>/dev/null || true")
