from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from codex_switch_constants import SwitchError
from codex_switch_shared_configuration import (
    preflight_internal_shared_configuration,
)
from codex_switch_update_policy import parse_semantic_version


CURRENT_CHATGPT_BUNDLE_ID = "com.openai.codex"
_MAX_INTERNAL_GENERATION_MANIFEST_BYTES = 1024 * 1024
_MAX_INTERNAL_GENERATION_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_INTERNAL_GENERATION_EXECUTABLE_BYTES = 2 * 1024 * 1024 * 1024
_INTERNAL_PARITY_MANIFEST_PREFIX = "parity_"
_INFORMATIONAL_INTERNAL_ARGS = frozenset(
    {"-h", "--help", "-V", "--version"}
)


@dataclass(frozen=True)
class BindingFinding:
    code: str
    severity: str
    message: str
    expected: str = ""
    observed: str = ""


@dataclass(frozen=True)
class DesktopRoots:
    chatgpt: Path = Path("/Applications/ChatGPT.app")
    legacy_codex: Path = Path("/Applications/Codex.app")
    chatgpt_classic: Path = Path("/Applications/ChatGPT Classic.app")


DEFAULT_DESKTOP_ROOTS = DesktopRoots()


@dataclass(frozen=True)
class DesktopHost:
    kind: str
    bundle_root: Path
    bundle_id: str
    main_executable: Path
    bundled_cli: Path
    healthy: bool
    migration_only: bool = False


@dataclass(frozen=True)
class ChatGPTDesktopHost(DesktopHost):
    pass


@dataclass(frozen=True)
class LegacyCodexDesktopHost(DesktopHost):
    pass


@dataclass(frozen=True)
class DesktopInventory:
    current: ChatGPTDesktopHost | None
    legacy: tuple[LegacyCodexDesktopHost, ...] = ()
    excluded: tuple[DesktopHost, ...] = ()
    findings: tuple[BindingFinding, ...] = ()


@dataclass(frozen=True)
class RuntimeBindingContext:
    profile: str
    manifest: Mapping[str, object]
    store_root: Path
    bin_dir: Path
    profile_home: Path
    path_codex: Path | None = None
    active_record: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeBinding:
    profile: str
    shell_cli: Path
    desktop_cli: Path
    backend_cli: Path
    codex_home: Path
    desktop_host: DesktopHost | None
    requires_proxy: bool
    findings: tuple[BindingFinding, ...] = ()
    launcher_fingerprint: str = ""


@dataclass(frozen=True)
class RuntimeObservation:
    processes: tuple[object, ...] = ()
    gui_app_cli: str = ""
    launch_agent_cli: str = ""
    managed_launcher_fingerprint: str = ""


@dataclass(frozen=True)
class RuntimeAttestation:
    binding: RuntimeBinding
    observation: RuntimeObservation
    healthy: bool
    findings: tuple[BindingFinding, ...] = ()


@dataclass(frozen=True)
class InternalRuntimeGeneration:
    backend_cli: Path
    codex_home: Path
    capability_receipt_path: Path
    capability_receipt_sha256: str
    schema_sha256: str
    parity_receipt_path: Path
    parity_receipt_sha256: str
    overlay_path: Path
    overlay_sha256: str


@dataclass(frozen=True)
class InternalCliRuntimeGeneration:
    backend_cli: Path
    codex_home: Path
    backend_sha256: str
    backend_version: str


class RuntimeBindingError(SwitchError):
    def __init__(
        self,
        code: str,
        message: str,
        findings: tuple[BindingFinding, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.findings = findings


class RuntimeConfigurationChanged(RuntimeBindingError):
    """Intact generation whose runtime inputs need fresh compatibility evidence."""

    def __init__(self, message: str) -> None:
        super().__init__("runtime.config.changed", message)


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    expected: object = "",
    observed: object = "",
) -> BindingFinding:
    return BindingFinding(
        code=code,
        severity=severity,
        message=message,
        expected=str(expected) if expected != "" else "",
        observed=str(observed) if observed != "" else "",
    )


def _read_bundle_id(bundle_root: Path) -> tuple[str, BindingFinding | None]:
    plist_path = bundle_root / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return "", _finding(
            "desktop.bundle.plist_invalid",
            "error",
            "Desktop bundle Info.plist is missing or invalid.",
            observed=plist_path,
        )
    bundle_id = payload.get("CFBundleIdentifier") if isinstance(payload, dict) else None
    if not isinstance(bundle_id, str) or not bundle_id:
        return "", _finding(
            "desktop.bundle.plist_invalid",
            "error",
            "Desktop bundle has no valid CFBundleIdentifier.",
            observed=plist_path,
        )
    return bundle_id, None


def _is_regular_executable(path: Path, *, allow_symlink: bool = False) -> bool:
    try:
        lexical_mode = path.lstat().st_mode
    except OSError:
        return False
    if stat.S_ISLNK(lexical_mode) and not allow_symlink:
        return False
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and os.access(str(path), os.X_OK)


def _generation_error(message: str) -> RuntimeBindingError:
    return RuntimeBindingError(
        "binding.internal.generation_invalid",
        f"Internal runtime generation invalid: {message}",
    )


def _cli_generation_error(message: str) -> RuntimeBindingError:
    return RuntimeBindingError(
        "binding.internal.cli_generation_invalid",
        f"Internal CLI generation invalid: {message}",
    )


def require_internal_app_readiness(
    manifest: Mapping[str, object],
) -> None:
    readiness = manifest.get("internal_app_readiness")
    has_cli_generation = "internal_cli_generation" in manifest
    if readiness is None and not has_cli_generation:
        return
    if readiness == "unverified":
        raise RuntimeBindingError(
            "internal.app_readiness.unverified",
            "internal.app_readiness.unverified: the current internal backend "
            "was promoted for CLI-only use and is not verified for Codex App",
        )
    raise RuntimeBindingError(
        "internal.app_readiness.invalid",
        "internal.app_readiness.invalid: internal App readiness metadata is "
        "missing or invalid",
    )


def _canonical_generation_path(
    value: object,
    *,
    label: str,
) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise _generation_error(f"{label} path is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _generation_error(f"{label} path is not absolute")
    return path.resolve(strict=False)


def _read_generation_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise _generation_error(f"{label} is missing or unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _generation_error(
            f"{label} must be a regular non-symlink file"
        )
    if before.st_size > max_bytes:
        raise _generation_error(f"{label} exceeds the size limit")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise _generation_error(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
            or opened.st_size > max_bytes
        ):
            raise _generation_error(f"{label} identity changed before read")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _generation_error(f"{label} exceeds the size limit")
            chunks.append(chunk)
        completed = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as exc:
            raise _generation_error(f"{label} changed during read") from exc
        if (
            (completed.st_dev, completed.st_ino, completed.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or (after.st_dev, after.st_ino, after.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
        ):
            raise _generation_error(f"{label} changed during read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _reject_generation_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key}")
        result[key] = value
    return result


def _load_generation_json(
    payload: bytes,
    *,
    label: str,
) -> Mapping[str, object]:
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_reject_generation_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"unsupported constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _generation_error(f"{label} JSON is invalid") from exc
    if not isinstance(document, Mapping):
        raise _generation_error(f"{label} root must be an object")
    return document


def _required_generation_digest(
    value: object,
    *,
    label: str,
) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise _generation_error(f"{label} digest is invalid")
    return value


def _required_runtime_config_digests(
    internal_fingerprint: Mapping[str, object],
) -> dict[str, str]:
    raw_entries = internal_fingerprint.get("config_sha256s")
    if not isinstance(raw_entries, list):
        raise _generation_error(
            "parity receipt config digests are invalid"
        )
    expected_names = {"profile", "shared", "runtime"}
    digests: dict[str, str] = {}
    for raw_entry in raw_entries:
        if (
            not isinstance(raw_entry, Mapping)
            or set(raw_entry) != {"name", "sha256"}
        ):
            raise _generation_error(
                "parity receipt config digest entry is invalid"
            )
        name = raw_entry.get("name")
        if (
            not isinstance(name, str)
            or name not in expected_names | {"runtime_semantics_v1"}
            or name in digests
        ):
            raise _generation_error(
                "parity receipt config digest name is invalid"
            )
        digests[name] = _required_generation_digest(
            raw_entry.get("sha256"),
            label=f"parity {name} config",
        )
    if set(digests) - {"runtime_semantics_v1"} != expected_names:
        raise _generation_error(
            "parity receipt config digests are incomplete"
        )
    return digests


def _generation_file_digest(
    path: Path,
    *,
    label: str,
    max_bytes: int = _MAX_INTERNAL_GENERATION_ARTIFACT_BYTES,
) -> tuple[bytes, str]:
    payload = _read_generation_file(
        path,
        label=label,
        max_bytes=max_bytes,
    )
    return payload, hashlib.sha256(payload).hexdigest()


def _generation_executable_digest(
    path: Path,
    *,
    label: str,
    max_bytes: int = _MAX_INTERNAL_GENERATION_EXECUTABLE_BYTES,
) -> tuple[int, str]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise _generation_error(f"{label} is missing or unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _generation_error(
            f"{label} must be a regular non-symlink executable"
        )
    if before.st_mode & 0o111 == 0:
        raise _generation_error(f"{label} is not executable")
    if before.st_size > max_bytes:
        raise _generation_error(
            f"{label} exceeds the executable size limit"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise _generation_error(f"{label} cannot be opened safely") from exc
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
            or opened.st_size > max_bytes
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
            raise _generation_error(
                f"{label} identity changed before executable read"
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _generation_error(
                    f"{label} exceeds the executable size limit"
                )
            digest.update(chunk)
        completed = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as exc:
            raise _generation_error(
                f"{label} changed during executable read"
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
            or total != opened.st_size
        ):
            raise _generation_error(
                f"{label} changed during executable read"
            )
        return total, digest.hexdigest()
    finally:
        os.close(descriptor)


def _require_generation_path(
    observed: object,
    expected: Path,
    *,
    label: str,
) -> Path:
    path = _canonical_generation_path(observed, label=label)
    if path != expected.resolve(strict=False):
        raise _generation_error(f"{label} path does not match the manifest")
    return path


def _require_generation_artifact(
    manifest: Mapping[str, object],
    *,
    path_key: str,
    digest_key: str,
    expected_path: Path,
    label: str,
) -> tuple[Path, bytes, str]:
    path = _require_generation_path(
        manifest.get(path_key),
        expected_path,
        label=label,
    )
    expected_digest = _required_generation_digest(
        manifest.get(digest_key),
        label=label,
    )
    payload, observed_digest = _generation_file_digest(
        path,
        label=label,
    )
    if observed_digest != expected_digest:
        raise _generation_error(f"{label} digest does not match the manifest")
    return path, payload, expected_digest


def _require_expected_generation_value(
    observed: Path | str,
    expected: Path | str | None,
    *,
    label: str,
) -> None:
    if expected is None or not str(expected):
        return
    if isinstance(observed, Path) or isinstance(expected, Path):
        observed_value = Path(observed).expanduser().resolve(strict=False)
        expected_value = Path(expected).expanduser().resolve(strict=False)
    else:
        observed_value = observed
        expected_value = expected
    if observed_value != expected_value:
        raise _generation_error(
            f"{label} does not match the launcher generation"
        )


def manifest_has_internal_runtime_generation(
    manifest: Mapping[str, object],
) -> bool:
    return any(
        isinstance(key, str)
        and key.startswith(_INTERNAL_PARITY_MANIFEST_PREFIX)
        for key in manifest
    )


def manifest_has_internal_cli_generation(
    manifest: Mapping[str, object],
) -> bool:
    return "internal_cli_generation" in manifest


def validate_internal_cli_runtime_generation(
    *,
    manifest: Mapping[str, object],
    fallback_home: Path,
    fallback_backend: Path,
) -> InternalCliRuntimeGeneration:
    raw_generation = manifest.get("internal_cli_generation")
    expected_fields = {
        "schema_version",
        "scope",
        "backend_sha256",
        "backend_version",
    }
    if (
        not isinstance(raw_generation, Mapping)
        or set(raw_generation) != expected_fields
    ):
        raise _cli_generation_error("metadata fields are invalid")
    if raw_generation.get("schema_version") != 1:
        raise _cli_generation_error("schema version is invalid")
    if raw_generation.get("scope") != "cli-only":
        raise _cli_generation_error("scope is invalid")
    if manifest.get("internal_app_readiness") != "unverified":
        raise _cli_generation_error("App readiness is not unverified")
    expected_digest = raw_generation.get("backend_sha256")
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        raise _cli_generation_error("backend digest is invalid")
    backend_version = raw_generation.get("backend_version")
    if (
        not isinstance(backend_version, str)
        or parse_semantic_version(backend_version) is None
    ):
        raise _cli_generation_error("backend version is invalid")
    backend = _canonical_generation_path(
        manifest.get("codex_bin"),
        label="CLI backend",
    )
    expected_backend = _canonical_generation_path(
        fallback_backend,
        label="fallback CLI backend",
    )
    if backend != expected_backend:
        raise _cli_generation_error(
            "backend path does not match the managed shell fallback"
        )
    if not _is_regular_executable(backend, allow_symlink=False):
        raise _cli_generation_error(
            "backend is not a regular non-symlink executable"
        )
    _backend_size, observed_digest = _generation_executable_digest(
        backend,
        label="CLI backend",
    )
    if observed_digest != expected_digest:
        raise _cli_generation_error(
            "backend digest does not match the manifest"
        )
    raw_home = manifest.get("codex_home")
    home = _canonical_generation_path(
        raw_home if isinstance(raw_home, str) and raw_home else fallback_home,
        label="CLI CODEX_HOME",
    )
    return InternalCliRuntimeGeneration(
        backend_cli=backend,
        codex_home=home,
        backend_sha256=expected_digest,
        backend_version=backend_version,
    )


def validate_internal_runtime_generation(
    *,
    store_root: Path,
    fallback_home: Path,
    launcher_path: Path | None = None,
    expected_backend: Path | None = None,
    expected_home: Path | None = None,
    expected_capability_receipt_path: Path | None = None,
    expected_schema_sha256: str = "",
    expected_capability_receipt_sha256: str = "",
    validate_config: bool = True,
) -> InternalRuntimeGeneration:
    canonical_store = _canonical_generation_path(
        store_root,
        label="store",
    )
    manifest_path = (
        canonical_store / "profiles" / "internal" / "manifest.json"
    )
    manifest = _load_generation_json(
        _read_generation_file(
            manifest_path,
            label="manifest",
            max_bytes=_MAX_INTERNAL_GENERATION_MANIFEST_BYTES,
        ),
        label="manifest",
    )
    require_internal_app_readiness(manifest)

    expected_launcher = (
        canonical_store / "bin" / "codex-internal-app"
    ).resolve(strict=False)
    manifest_launcher = _require_generation_path(
        manifest.get("app_cli_path"),
        expected_launcher,
        label="launcher",
    )
    actual_launcher = (
        _canonical_generation_path(
            launcher_path,
            label="launcher",
        )
        if launcher_path is not None
        else manifest_launcher
    )
    if actual_launcher != manifest_launcher:
        raise _generation_error(
            "launcher path does not match the active manifest"
        )
    expected_launcher_digest = _required_generation_digest(
        manifest.get("app_launcher_sha256"),
        label="launcher",
    )
    _launcher_payload, launcher_digest = _generation_file_digest(
        actual_launcher,
        label="launcher",
    )
    if launcher_digest != expected_launcher_digest:
        raise _generation_error(
            "launcher digest does not match the active manifest"
        )

    backend = _canonical_generation_path(
        manifest.get("codex_bin"),
        label="backend",
    )
    if not _is_regular_executable(backend, allow_symlink=True):
        raise _generation_error("backend is not a regular executable")
    try:
        backend = backend.resolve(strict=True)
    except OSError as exc:
        raise _generation_error("backend cannot be resolved") from exc
    _require_expected_generation_value(
        backend,
        expected_backend,
        label="backend",
    )

    raw_home = manifest.get("codex_home")
    home = _canonical_generation_path(
        raw_home if isinstance(raw_home, str) and raw_home else fallback_home,
        label="CODEX_HOME",
    )
    _require_expected_generation_value(
        home,
        expected_home,
        label="CODEX_HOME",
    )

    capability_path = (
        canonical_store
        / "bin"
        / "codex-internal-app.capabilities.json"
    ).resolve(strict=False)
    (
        capability_path,
        capability_payload,
        capability_digest,
    ) = _require_generation_artifact(
        manifest,
        path_key="app_capability_receipt_path",
        digest_key="app_capability_receipt_sha256",
        expected_path=capability_path,
        label="capability receipt",
    )
    capability_environment_path = Path(
        str(manifest["app_capability_receipt_path"])
    ).expanduser()
    schema_digest = _required_generation_digest(
        manifest.get("app_schema_sha256"),
        label="capability schema",
    )
    capability_receipt = _load_generation_json(
        capability_payload,
        label="capability receipt",
    )
    if capability_receipt.get("schema_sha256") != schema_digest:
        raise _generation_error(
            "capability receipt schema digest does not match the manifest"
        )
    backend_digest = _required_generation_digest(
        capability_receipt.get("backend_sha256"),
        label="capability backend",
    )
    _backend_size, observed_backend_digest = _generation_executable_digest(
        backend,
        label="backend",
    )
    if observed_backend_digest != backend_digest:
        raise _generation_error(
            "capability receipt backend digest does not match the backend"
        )
    _require_expected_generation_value(
        capability_path,
        expected_capability_receipt_path,
        label="capability receipt",
    )
    _require_expected_generation_value(
        schema_digest,
        expected_schema_sha256 or None,
        label="capability schema digest",
    )
    _require_expected_generation_value(
        capability_digest,
        expected_capability_receipt_sha256 or None,
        label="capability receipt digest",
    )

    parity_dir = (
        canonical_store / "profiles" / "internal" / "parity"
    ).resolve(strict=False)
    parity_receipt_path, parity_payload, parity_digest = (
        _require_generation_artifact(
            manifest,
            path_key="parity_receipt_path",
            digest_key="parity_receipt_sha256",
            expected_path=parity_dir / "receipt.json",
            label="parity receipt",
        )
    )
    overlay_path, overlay_payload, overlay_digest = (
        _require_generation_artifact(
            manifest,
            path_key="parity_overlay_path",
            digest_key="parity_overlay_sha256",
            expected_path=parity_dir / "model-catalog.json",
            label="parity overlay",
        )
    )
    parity_receipt = _load_generation_json(
        parity_payload,
        label="parity receipt",
    )
    if parity_receipt.get("healthy") is not True:
        raise _generation_error("parity receipt is not healthy")
    receipt_schema = parity_receipt.get("schema_version")
    if (
        type(receipt_schema) is not int
        or receipt_schema
        != manifest.get("parity_receipt_schema_version")
    ):
        raise _generation_error(
            "parity receipt schema does not match the manifest"
        )
    receipt_overlay = parity_receipt.get("overlay")
    if not isinstance(receipt_overlay, Mapping):
        raise _generation_error(
            "parity receipt has no overlay generation binding"
        )
    if (
        _canonical_generation_path(
            receipt_overlay.get("path"),
            label="parity receipt overlay",
        )
        != overlay_path
        or receipt_overlay.get("sha256") != overlay_digest
    ):
        raise _generation_error(
            "parity receipt does not match the overlay generation"
        )
    receipt_internal = parity_receipt.get("internal_fingerprint")
    if not isinstance(receipt_internal, Mapping):
        raise _generation_error(
            "parity receipt has no internal generation binding"
        )
    expected_config_digests = _required_runtime_config_digests(
        receipt_internal
    )
    receipt_capability_digest = _required_generation_digest(
        receipt_internal.get("capability_receipt_sha256"),
        label="parity capability receipt",
    )
    manifest_parity_capability_digest = _required_generation_digest(
        manifest.get("parity_capability_receipt_sha256"),
        label="manifest parity capability receipt",
    )
    if (
        receipt_capability_digest != capability_digest
        or manifest_parity_capability_digest != capability_digest
    ):
        raise _generation_error(
            "parity receipt does not match the capability receipt generation"
        )
    _load_generation_json(
        overlay_payload,
        label="parity overlay",
    )

    if validate_config:
        from codex_switch_runtime_config import runtime_compatibility_digest
        config_payload = _read_generation_file(
            home / "config.toml", label="projected config",
            max_bytes=_MAX_INTERNAL_GENERATION_ARTIFACT_BYTES,
        )
        if hashlib.sha256(config_payload).hexdigest() != expected_config_digests["runtime"]:
            try:
                reusable = (expected_config_digests.get("runtime_semantics_v1")
                            == runtime_compatibility_digest(config_payload))
            except (UnicodeDecodeError, ValueError) as exc:
                raise _generation_error("projected config TOML is invalid") from exc
            if not reusable:
                raise RuntimeConfigurationChanged(
                    "Internal runtime configuration changed; current settings require compatibility preparation."
                )

        import tomllib
        try:
            config = tomllib.loads(config_payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _generation_error("projected config TOML is invalid") from exc
        configured_overlay = config.get("model_catalog_json")
        if not isinstance(configured_overlay, str) or _canonical_generation_path(configured_overlay, label="projected config overlay") != overlay_path:
            raise _generation_error("projected config does not select the parity overlay generation")
        features = config.get("features")
        if not isinstance(features, Mapping) or features.get("multi_agent_v2") is not True:
            raise _generation_error("projected config does not enable the v2 generation")

    return InternalRuntimeGeneration(
        backend_cli=backend,
        codex_home=home,
        capability_receipt_path=capability_environment_path,
        capability_receipt_sha256=capability_digest,
        schema_sha256=schema_digest,
        parity_receipt_path=parity_receipt_path,
        parity_receipt_sha256=parity_digest,
        overlay_path=overlay_path,
        overlay_sha256=overlay_digest,
    )


def _observed_host(
    *,
    kind: str,
    bundle_root: Path,
    main_name: str,
    healthy: bool,
    migration_only: bool,
) -> DesktopHost:
    bundle_id, _ = _read_bundle_id(bundle_root)
    return DesktopHost(
        kind=kind,
        bundle_root=bundle_root,
        bundle_id=bundle_id,
        main_executable=bundle_root / "Contents" / "MacOS" / main_name,
        bundled_cli=bundle_root / "Contents" / "Resources" / "codex",
        healthy=healthy,
        migration_only=migration_only,
    )


def discover_desktop_hosts(
    roots: DesktopRoots = DEFAULT_DESKTOP_ROOTS,
) -> DesktopInventory:
    findings: list[BindingFinding] = []
    current: ChatGPTDesktopHost | None = None
    legacy: list[LegacyCodexDesktopHost] = []
    excluded: list[DesktopHost] = []

    current_root = roots.chatgpt
    if current_root.exists():
        bundle_id, plist_finding = _read_bundle_id(current_root)
        if plist_finding is not None:
            findings.append(
                _finding(
                    "desktop.current.plist_invalid",
                    "error",
                    plist_finding.message,
                    observed=plist_finding.observed,
                )
            )
        elif bundle_id != CURRENT_CHATGPT_BUNDLE_ID:
            findings.append(
                _finding(
                    "desktop.current.bundle_id_mismatch",
                    "error",
                    "ChatGPT Desktop bundle identity does not match the current Codex host.",
                    expected=CURRENT_CHATGPT_BUNDLE_ID,
                    observed=bundle_id,
                )
            )
        else:
            main = current_root / "Contents" / "MacOS" / "ChatGPT"
            cli = current_root / "Contents" / "Resources" / "codex"
            if not _is_regular_executable(main):
                findings.append(
                    _finding(
                        "desktop.current.main_invalid",
                        "error",
                        "ChatGPT Desktop main executable is missing, non-regular, or not executable.",
                        observed=main,
                    )
                )
            if not _is_regular_executable(cli):
                findings.append(
                    _finding(
                        "desktop.current.cli_invalid",
                        "error",
                        "ChatGPT Desktop bundled Codex CLI is missing, non-regular, or not executable.",
                        observed=cli,
                    )
                )
            if _is_regular_executable(main) and _is_regular_executable(cli):
                current = ChatGPTDesktopHost(
                    kind="chatgpt",
                    bundle_root=current_root,
                    bundle_id=bundle_id,
                    main_executable=main,
                    bundled_cli=cli,
                    healthy=True,
                    migration_only=False,
                )

    legacy_root = roots.legacy_codex
    if legacy_root.exists():
        observed = _observed_host(
            kind="legacy-codex",
            bundle_root=legacy_root,
            main_name="Codex",
            healthy=False,
            migration_only=True,
        )
        legacy.append(LegacyCodexDesktopHost(**observed.__dict__))
        findings.append(
            _finding(
                "desktop.legacy.migration_only",
                "warning",
                "Codex.app was observed but is migration-only; install or update ChatGPT.app.",
                observed=legacy_root,
            )
        )

    classic_root = roots.chatgpt_classic
    if classic_root.exists():
        excluded.append(
            _observed_host(
                kind="chatgpt-classic",
                bundle_root=classic_root,
                main_name="ChatGPT",
                healthy=False,
                migration_only=False,
            )
        )
        findings.append(
            _finding(
                "desktop.classic.excluded",
                "info",
                "ChatGPT Classic is not a Codex Desktop host candidate.",
                observed=classic_root,
            )
        )

    return DesktopInventory(
        current=current,
        legacy=tuple(legacy),
        excluded=tuple(excluded),
        findings=tuple(findings),
    )


def _canonical(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _is_within(path: Path, parent: Path) -> bool:
    canonical_path = _canonical(path)
    canonical_parent = _canonical(parent)
    return canonical_path == canonical_parent or canonical_parent in canonical_path.parents


def _path_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _paths_equal(left: Path, right: Path) -> bool:
    return _canonical(left) == _canonical(right)


def _active_record_findings(
    active_record: Mapping[str, object],
    *,
    expected_profile: str,
    expected_shell: Path,
    expected_desktop: Path,
) -> tuple[BindingFinding, ...]:
    if not active_record:
        return ()
    observed_profile = _path_text(active_record.get("profile"))
    observed_shell = _path_text(active_record.get("shell_cli_path"))
    observed_desktop = _path_text(active_record.get("app_cli_path"))
    stale = bool(observed_profile and observed_profile != expected_profile)
    if observed_shell:
        stale = stale or not _paths_equal(Path(observed_shell).expanduser(), expected_shell)
    if observed_desktop:
        stale = stale or not _paths_equal(Path(observed_desktop).expanduser(), expected_desktop)
    if not stale:
        return ()
    return (
        _finding(
            "binding.observation.active_stale",
            "warning",
            "The active record differs from manifest-derived runtime intent.",
        ),
    )


def _normalize_profile(profile: str) -> str:
    if profile == "official":
        return "openai-official"
    if profile in {"openai-official", "internal"}:
        return profile
    raise RuntimeBindingError(
        "binding.profile.unsupported",
        f"Unsupported product profile for canonical runtime binding: {profile}",
    )


def _official_binding(
    context: RuntimeBindingContext,
    inventory: DesktopInventory,
) -> RuntimeBinding:
    host = inventory.current
    if host is None:
        path_codex = context.path_codex
        if path_codex is not None and _is_within(path_codex, context.bin_dir):
            raise RuntimeBindingError(
                "binding.official.managed_shim_rejected",
                "The codex-switch managed shim cannot certify the official ChatGPT Desktop binding.",
                inventory.findings,
            )
        raise RuntimeBindingError(
            "binding.official.current_host_unavailable",
            "No verified current ChatGPT.app Codex Desktop host is available.",
            inventory.findings,
        )
    cli = host.bundled_cli
    findings_list = list(inventory.findings)
    raw_shell = _path_text(context.manifest.get("codex_bin"))
    raw_desktop = _path_text(context.manifest.get("app_cli_path"))
    if (
        (raw_shell and not _paths_equal(Path(raw_shell).expanduser(), cli))
        or (raw_desktop and not _paths_equal(Path(raw_desktop).expanduser(), cli))
    ):
        findings_list.append(
            _finding(
                "binding.official.manifest_drift",
                "warning",
                "The official manifest differs from the canonical ChatGPT bundled CLI.",
                expected=cli,
                observed=raw_desktop or raw_shell,
            )
        )
    findings_list.extend(
        _active_record_findings(
            context.active_record,
            expected_profile="openai-official",
            expected_shell=cli,
            expected_desktop=cli,
        )
    )
    findings = tuple(findings_list)
    return RuntimeBinding(
        profile="openai-official",
        shell_cli=cli,
        desktop_cli=cli,
        backend_cli=cli,
        codex_home=context.profile_home,
        desktop_host=host,
        requires_proxy=False,
        findings=findings,
    )


def _internal_backend(context: RuntimeBindingContext) -> Path:
    raw_backend = _path_text(context.manifest.get("codex_bin"))
    backend = Path(raw_backend).expanduser()
    if (
        not raw_backend
        or not backend.is_absolute()
        or not _is_regular_executable(backend, allow_symlink=True)
    ):
        raise RuntimeBindingError(
            "binding.internal.backend_invalid",
            "The internal profile codex_bin must be an absolute regular executable.",
        )
    try:
        canonical_backend = backend.resolve(strict=True)
    except OSError as exc:
        raise RuntimeBindingError(
            "binding.internal.backend_invalid",
            "The internal profile codex_bin must resolve to a regular executable.",
        ) from exc
    if _is_within(canonical_backend, context.bin_dir):
        raise RuntimeBindingError(
            "binding.internal.recursive_backend",
            "The internal backend cannot resolve to a codex-switch managed shim or launcher.",
        )
    return canonical_backend


def _internal_binding(context: RuntimeBindingContext) -> RuntimeBinding:
    backend = _internal_backend(context)
    launcher = internal_managed_launcher_path(context)
    launcher_fingerprint = _path_text(
        context.manifest.get("app_launcher_sha256")
    )
    if launcher_fingerprint and re.fullmatch(
        r"[0-9a-f]{64}", launcher_fingerprint
    ) is None:
        raise RuntimeBindingError(
            "binding.internal.launcher_fingerprint_invalid",
            "The internal profile app_launcher_sha256 must be a lowercase SHA-256 digest.",
        )
    findings: list[BindingFinding] = []
    raw_app_cli = _path_text(context.manifest.get("app_cli_path"))
    if raw_app_cli:
        observed_app_cli = Path(raw_app_cli).expanduser()
        if _paths_equal(observed_app_cli, backend):
            findings.append(
                _finding(
                    "binding.internal.raw_app_cli_migration_drift",
                    "warning",
                    "The internal manifest still records its raw backend as Desktop intent.",
                    expected=launcher,
                    observed=observed_app_cli,
                )
            )
        elif not _paths_equal(observed_app_cli, launcher):
            findings.append(
                _finding(
                    "binding.internal.app_cli_drift",
                    "warning",
                    "The internal manifest Desktop path differs from the managed launcher.",
                    expected=launcher,
                    observed=observed_app_cli,
                )
            )
    findings.extend(
        _active_record_findings(
            context.active_record,
            expected_profile="internal",
            expected_shell=backend,
            expected_desktop=launcher,
        )
    )
    return RuntimeBinding(
        profile="internal",
        shell_cli=backend,
        desktop_cli=launcher,
        backend_cli=backend,
        codex_home=context.profile_home,
        desktop_host=None,
        requires_proxy=True,
        findings=tuple(findings),
        launcher_fingerprint=launcher_fingerprint,
    )


def internal_managed_launcher_path(store_or_context: object) -> Path:
    return Path(getattr(store_or_context, "bin_dir")) / "codex-internal-app"


def resolve_runtime_binding(
    context: RuntimeBindingContext,
    inventory: DesktopInventory,
) -> RuntimeBinding:
    profile = _normalize_profile(context.profile)
    if profile == "openai-official":
        return _official_binding(context, inventory)
    return _internal_binding(context)


def binding_profile_home(
    store: object,
    profile: str,
    manifest: Mapping[str, object],
) -> Path:
    raw_home = _path_text(manifest.get("codex_home"))
    if raw_home:
        candidate = Path(raw_home).expanduser()
        if candidate.is_absolute():
            return candidate
    normalized = _normalize_profile(profile)
    if normalized == "openai-official":
        return Path(getattr(store, "official_codex_home"))
    explicit_internal = getattr(store, "internal_codex_home", None)
    if explicit_internal is not None:
        return Path(explicit_internal)
    managed_home = getattr(store, "managed_home")
    return Path(managed_home("internal"))


def runtime_binding_context_from_store(
    store: object,
    profile: str,
    manifest: Mapping[str, object],
    *,
    path_codex: Path | None = None,
    active_record: Mapping[str, object] | None = None,
) -> RuntimeBindingContext:
    return RuntimeBindingContext(
        profile=profile,
        manifest=manifest,
        store_root=Path(getattr(store, "root")),
        bin_dir=Path(getattr(store, "bin_dir")),
        profile_home=binding_profile_home(store, profile, manifest),
        path_codex=path_codex,
        active_record=active_record or {},
    )


def resolve_store_runtime_binding(
    store: object,
    profile: str,
    *,
    manifest: Mapping[str, object] | None = None,
    inventory: DesktopInventory | None = None,
    path_codex: Path | None = None,
    active_record: Mapping[str, object] | None = None,
) -> RuntimeBinding:
    selected_manifest = (
        manifest
        if manifest is not None
        else getattr(store, "load_manifest")(profile)
    )
    selected_inventory = inventory or discover_desktop_hosts()
    return resolve_runtime_binding(
        runtime_binding_context_from_store(
            store,
            profile,
            selected_manifest,
            path_codex=path_codex,
            active_record=active_record,
        ),
        selected_inventory,
    )


def manifest_uses_canonical_binding(
    profile: str,
    manifest: Mapping[str, object],
    inventory: DesktopInventory | None = None,
) -> bool:
    normalized = _normalize_profile(profile)
    if normalized == "internal":
        return True
    if manifest.get("runtime_binding") == "canonical":
        return True
    selected_inventory = inventory or discover_desktop_hosts()
    current = selected_inventory.current
    if current is None:
        return False
    raw_shell = _path_text(manifest.get("codex_bin"))
    raw_desktop = _path_text(manifest.get("app_cli_path"))
    return bool(
        raw_shell
        and raw_desktop
        and _paths_equal(Path(raw_shell).expanduser(), current.bundled_cli)
        and _paths_equal(Path(raw_desktop).expanduser(), current.bundled_cli)
    )


def attest_runtime_binding(
    binding: RuntimeBinding,
    observation: RuntimeObservation,
) -> RuntimeAttestation:
    findings = list(binding.findings)

    if observation.launch_agent_cli and not _paths_equal(
        Path(observation.launch_agent_cli).expanduser(), binding.desktop_cli
    ):
        findings.append(
            _finding(
                "attestation.launch_agent.cli_mismatch",
                "error",
                "LaunchAgent Desktop CLI differs from the canonical binding.",
                expected=binding.desktop_cli,
                observed=observation.launch_agent_cli,
            )
        )

    desktop_processes = [
        process
        for process in observation.processes
        if getattr(process, "kind", "") == "desktop"
    ]
    app_server_processes = [
        process
        for process in observation.processes
        if getattr(process, "kind", "") == "app-server"
    ]

    for process in desktop_processes:
        host_kind = str(getattr(process, "host_kind", ""))
        command_path = str(getattr(process, "command_path", ""))
        if host_kind == "legacy-codex":
            findings.append(
                _finding(
                    "attestation.desktop.legacy_running",
                    "error",
                    "A legacy Codex.app Desktop host is still running.",
                    observed=command_path,
                )
            )
        elif binding.desktop_host is not None and command_path and not _paths_equal(
            Path(command_path).expanduser(), binding.desktop_host.main_executable
        ):
            findings.append(
                _finding(
                    "attestation.desktop.host_mismatch",
                    "error",
                    "The running Desktop host differs from the canonical ChatGPT host.",
                    expected=binding.desktop_host.main_executable,
                    observed=command_path,
                )
            )

        process_app_cli = str(getattr(process, "app_cli_env", ""))
        observed_gui_cli = process_app_cli or observation.gui_app_cli
        if not observed_gui_cli:
            findings.append(
                _finding(
                    "attestation.gui_env.unset",
                    "error",
                    "The running Desktop host has no CODEX_CLI_PATH observation.",
                    expected=binding.desktop_cli,
                )
            )
        elif not _paths_equal(
            Path(observed_gui_cli).expanduser(), binding.desktop_cli
        ):
            findings.append(
                _finding(
                    "attestation.gui_env.cli_mismatch",
                    "error",
                    "The running Desktop CODEX_CLI_PATH differs from the canonical binding.",
                    expected=binding.desktop_cli,
                    observed=observed_gui_cli,
                )
            )

    if observation.gui_app_cli and not _paths_equal(
        Path(observation.gui_app_cli).expanduser(), binding.desktop_cli
    ):
        findings.append(
            _finding(
                "attestation.gui_env.cli_mismatch",
                "error",
                "The GUI CODEX_CLI_PATH differs from the canonical binding.",
                expected=binding.desktop_cli,
                observed=observation.gui_app_cli,
            )
        )

    for process in app_server_processes:
        command_path = str(getattr(process, "command_path", ""))
        if not command_path or not _paths_equal(
            Path(command_path).expanduser(), binding.backend_cli
        ):
            findings.append(
                _finding(
                    "attestation.app_server.backend_mismatch",
                    "error",
                    "The running app-server child differs from the canonical backend.",
                    expected=binding.backend_cli,
                    observed=command_path,
                )
            )
        if binding.requires_proxy:
            parent_command = str(getattr(process, "parent_command", ""))
            if "codex_switch_app_proxy.py" not in parent_command:
                findings.append(
                    _finding(
                        "attestation.internal.proxy_bypass",
                        "error",
                        "The internal app-server backend is not owned by the managed proxy.",
                        observed=parent_command,
                    )
                )

    if binding.requires_proxy and binding.launcher_fingerprint:
        if (
            observation.managed_launcher_fingerprint
            != binding.launcher_fingerprint
        ):
            findings.append(
                _finding(
                    "attestation.internal.launcher_fingerprint_mismatch",
                    "error",
                    "The managed launcher bytes differ from the attested binding.",
                    expected=binding.launcher_fingerprint,
                    observed=observation.managed_launcher_fingerprint,
                )
            )

    unique_findings: list[BindingFinding] = []
    seen = set()
    for finding in findings:
        key = (finding.code, finding.expected, finding.observed)
        if key in seen:
            continue
        seen.add(key)
        unique_findings.append(finding)
    final_findings = tuple(unique_findings)
    return RuntimeAttestation(
        binding=binding,
        observation=observation,
        healthy=not any(finding.severity == "error" for finding in final_findings),
        findings=final_findings,
    )


def _internal_generation_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one promoted internal runtime generation."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-internal-generation")
    validate.add_argument("--store-root", type=Path, required=True)
    validate.add_argument("--fallback-home", type=Path, required=True)
    validate.add_argument("--launcher-path", type=Path, required=True)
    validate.add_argument("--expected-backend", type=Path)
    validate.add_argument("--expected-home", type=Path)
    validate.add_argument("--expected-capability-receipt-path", type=Path)
    validate.add_argument("--official-home", type=Path)
    validate.add_argument("--refresh-config", action="store_true")
    validate.add_argument("--expected-schema-sha256", default="")
    validate.add_argument("--expected-capability-receipt-sha256", default="")

    shell = commands.add_parser("exec-internal-shell")
    shell.add_argument("--store-root", type=Path, required=True)
    shell.add_argument("--fallback-home", type=Path, required=True)
    shell.add_argument("--official-home", type=Path)
    shell.add_argument("--fallback-backend", type=Path, required=True)
    shell.add_argument("backend_args", nargs=argparse.REMAINDER)
    return parser


def _is_informational_internal_invocation(
    backend_args: Sequence[str],
) -> bool:
    return bool(backend_args) and all(
        argument in _INFORMATIONAL_INTERNAL_ARGS
        for argument in backend_args
    )


def _require_cli_ready_shared_configuration(receipt: object) -> None:
    if getattr(receipt, "cli_ready", False):
        return
    codes = tuple(
        str(getattr(finding, "code", "")).strip()
        for finding in getattr(receipt, "findings", ())
        if str(getattr(finding, "code", "")).strip()
    )
    detail = ", ".join(codes) if codes else str(
        getattr(receipt, "status", "not-ready")
    )
    raise SwitchError(
        "shared_configuration.not_cli_ready: internal CLI preflight "
        f"did not produce a ready generation ({detail})"
    )


def _run_internal_generation_command(
    argv: Sequence[str] | None = None,
) -> int:
    args = _internal_generation_parser().parse_args(argv)
    if args.command == "validate-internal-generation":
        from codex_switch_runtime_config import ensure_internal_runtime_generation
        validator = ensure_internal_runtime_generation if args.refresh_config else validate_internal_runtime_generation
        options = {"official_home": args.official_home} if args.refresh_config else {}
        validator(
            **options,
            store_root=args.store_root,
            fallback_home=args.fallback_home,
            launcher_path=args.launcher_path,
            expected_backend=args.expected_backend,
            expected_home=args.expected_home,
            expected_capability_receipt_path=(
                args.expected_capability_receipt_path
            ),
            expected_schema_sha256=args.expected_schema_sha256,
            expected_capability_receipt_sha256=(
                args.expected_capability_receipt_sha256
            ),
        )
        return 0

    if args.command == "exec-internal-shell":
        canonical_store = _canonical_generation_path(
            args.store_root,
            label="store",
        )
        manifest = _load_generation_json(
            _read_generation_file(
                (
                    canonical_store
                    / "profiles"
                    / "internal"
                    / "manifest.json"
                ),
                label="manifest",
                max_bytes=_MAX_INTERNAL_GENERATION_MANIFEST_BYTES,
            ),
            label="manifest",
        )
        cli_generation = (
            validate_internal_cli_runtime_generation(
                manifest=manifest,
                fallback_home=args.fallback_home,
                fallback_backend=args.fallback_backend,
            )
            if manifest_has_internal_cli_generation(manifest)
            else None
        )
        from codex_switch_runtime_config import ensure_internal_runtime_generation
        app_generation = (
            ensure_internal_runtime_generation(
                official_home=args.official_home,
                store_root=canonical_store,
                fallback_home=args.fallback_home,
            )
            if cli_generation is None
            and manifest_has_internal_runtime_generation(manifest)
            else None
        )
        backend_args = list(args.backend_args)
        if backend_args and backend_args[0] == "--":
            backend_args = backend_args[1:]
        environment = os.environ.copy()
        if cli_generation is not None:
            backend = cli_generation.backend_cli
            environment["CODEX_HOME"] = str(cli_generation.codex_home)
            for name in (
                "CODEX_SWITCH_CAPABILITY_RECEIPT",
                "CODEX_SWITCH_EXPECTED_SCHEMA_SHA256",
                "CODEX_SWITCH_EXPECTED_RECEIPT_SHA256",
            ):
                environment.pop(name, None)
        elif app_generation is None:
            backend = _canonical_generation_path(
                args.fallback_backend,
                label="legacy backend",
            )
            if not _is_regular_executable(backend, allow_symlink=True):
                raise _generation_error(
                    "legacy backend is not a regular executable"
                )
            backend = backend.resolve(strict=True)
            environment["CODEX_HOME"] = str(args.fallback_home)
            for name in (
                "CODEX_SWITCH_CAPABILITY_RECEIPT",
                "CODEX_SWITCH_EXPECTED_SCHEMA_SHA256",
                "CODEX_SWITCH_EXPECTED_RECEIPT_SHA256",
            ):
                environment.pop(name, None)
        else:
            backend = app_generation.backend_cli
            environment["CODEX_HOME"] = str(app_generation.codex_home)
            environment["CODEX_SWITCH_CAPABILITY_RECEIPT"] = str(
                app_generation.capability_receipt_path
            )
            environment["CODEX_SWITCH_EXPECTED_SCHEMA_SHA256"] = (
                app_generation.schema_sha256
            )
            environment["CODEX_SWITCH_EXPECTED_RECEIPT_SHA256"] = (
                app_generation.capability_receipt_sha256
            )
        if not _is_informational_internal_invocation(backend_args):
            shared_receipt = preflight_internal_shared_configuration(
                store_root=canonical_store,
                internal_home=Path(environment["CODEX_HOME"]),
                backend_args=tuple(backend_args),
            )
            _require_cli_ready_shared_configuration(shared_receipt)
        try:
            os.execve(
                backend,
                [str(backend), *backend_args],
                environment,
            )
        except OSError as exc:
            raise _generation_error(
                f"backend execution failed: {backend}"
            ) from exc
    raise _generation_error("generation command is unsupported")


if __name__ == "__main__":
    try:
        raise SystemExit(_run_internal_generation_command())
    except SwitchError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
