from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import plistlib
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from codex_switch_config_document import ConfigDocument
from codex_switch_constants import SwitchError
from codex_switch_protocol_adapter import (
    CapabilityReceiptArtifact,
    ProtocolAdapterRule,
    THREAD_RESUME_HISTORY_RULE_ID,
    generate_app_server_schema,
    protocol_adapter_rule_manifest,
    protocol_adapter_rule_set_digest,
)
from codex_switch_runtime_binding import (
    CURRENT_CHATGPT_BUNDLE_ID,
    ChatGPTDesktopHost,
    RuntimeBinding,
)


PARITY_POLICY_VERSION = "3"
PARITY_RECEIPT_SCHEMA_VERSION = 2
MAX_PARITY_RECEIPT_BYTES = 256 * 1024
MAX_PARITY_CATALOG_BYTES = 16 * 1024 * 1024
MAX_PARITY_CONFIG_BYTES = 4 * 1024 * 1024
MAX_PARITY_PROBE_OUTPUT_BYTES = 256 * 1024
REFERENCE_AUTHORITY = "chatgpt-bundle"
_UNKNOWN_PARITY_SHA256 = "0" * 64
ALLOWED_IDENTITY_DIFFERENCES = frozenset(
    {"binary", "model", "endpoint", "provider", "auth"}
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_POLICY_VERSION_RE = re.compile(r"[1-9][0-9]*\Z")
_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*\Z")
_FEATURE_NAME_PATTERN = r"[a-z0-9][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)*"
_FEATURE_NAME_RE = re.compile(_FEATURE_NAME_PATTERN + r"\Z")
_MODEL_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_EXTENSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_PROBE_SENSITIVE_HEADER = re.compile(
    r"(?im)(?P<prefix>\b(?:authorization|proxy-authorization|cookie|"
    r"set-cookie|x-api-key|api[-_]?key)\b[\"']?\s*:\s*)[^\r\n]*"
)
_PROBE_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>\b(?:[A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret)|password)\b[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\r\n]+)"
)
_PROBE_BEARER_TOKEN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+"
)
_PROBE_SIGNED_QUERY_VALUE = re.compile(
    r"(?i)(?P<prefix>[?&](?:sig|signature|x-amz-signature|"
    r"x-goog-signature|access_token|api_key|apikey|credential)=)"
    r"[^&#\s]+"
)
_PROBE_URL_USERINFO = re.compile(
    r"(?i)(?P<scheme>https?://)[^/\s:@]+:[^@\s/]+@"
)
_FEATURE_LINE_RE = re.compile(
    rf"(?P<name>{_FEATURE_NAME_PATTERN})\s{{2,}}"
    r"(?P<stage>under development|stable|experimental|deprecated|removed)"
    r"\s{2,}(?P<state>true|false)\Z"
)
_FEATURE_STAGES = frozenset(
    {"under development", "stable", "experimental", "deprecated", "removed"}
)
PROTOCOL_DIRECTIONS = (
    "client_request",
    "client_notification",
    "server_request",
    "server_notification",
)
_PROTOCOL_DIRECTION_INDEX = {
    direction: index
    for index, direction in enumerate(PROTOCOL_DIRECTIONS)
}
_PROTOCOL_ROOT_NAMES = {
    "client_request": "ClientRequest.json",
    "client_notification": "ClientNotification.json",
    "server_request": "ServerRequest.json",
    "server_notification": "ServerNotification.json",
}
_PROTOCOL_METHOD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*\Z")
_PROTOCOL_DOCUMENTATION_KEYS = frozenset(
    {"$comment", "$schema", "description", "examples", "title"}
)
_PROTOCOL_SCHEMA_KEYS = frozenset(
    {
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "default",
        "definitions",
        "enum",
        "format",
        "items",
        "minLength",
        "minimum",
        "oneOf",
        "properties",
        "required",
        "type",
    }
)
_JSON_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_PARITY_CLASSIFICATION_TABLES: Mapping[str, Mapping[str, object]] = (
    MappingProxyType(
        {
            PARITY_POLICY_VERSION: MappingProxyType(
                {
                    "core_protocol_exact": frozenset(
                        {
                            "applyPatchApproval",
                            "configWarning",
                            "execCommandApproval",
                            "initialize",
                            "initialized",
                        }
                    ),
                    "core_protocol_prefixes": (
                        "collaborationMode/",
                        "config/",
                        "configRequirements/",
                        "item/",
                        "mcpServer/tool/",
                        "model/",
                        "modelProvider/",
                        "thread/",
                        "turn/",
                    ),
                    "optional_protocol": frozenset(
                        {
                            ("client_request", "app/installed"),
                            ("client_request", "app/read"),
                            ("client_request", "environment/status"),
                            (
                                "client_request",
                                "thread/searchOccurrences",
                            ),
                            (
                                "server_notification",
                                "thread/environment/connected",
                            ),
                            (
                                "server_notification",
                                "thread/environment/disconnected",
                            ),
                        }
                    ),
                    "core_features": frozenset({"multi_agent_v2"}),
                    "optional_unless_observed_features": frozenset(
                        {"skill_search"}
                    ),
                    "optional_features": frozenset(
                        {
                            "code_mode_buffered_exec",
                            "executor_capability_discovery",
                            "external_agent_memory_import",
                            "mcp_2026_07_28",
                        }
                    ),
                    "metadata_optional_features": frozenset(
                        {"enable_fanout", "item_ids", "memories"}
                    ),
                }
            )
        }
    )
)


class ParityValidationError(SwitchError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_text(value: str, *, code: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ParityValidationError(
            code,
            f"{field_name} must be a non-empty canonical string.",
        )


def _require_sha256(
    value: str,
    *,
    code: str,
    field_name: str,
) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ParityValidationError(
            code,
            f"{field_name} must be a lowercase SHA-256 digest.",
        )


def _canonical_path(path: Path, *, code: str, field_name: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ParityValidationError(
            code,
            f"{field_name} must be an absolute path.",
        )
    normalized = Path(os.path.normpath(str(path)))
    if normalized != path:
        raise ParityValidationError(
            code,
            f"{field_name} must not contain redundant path segments.",
        )
    return normalized


@dataclass(frozen=True)
class _RegularFileSnapshot:
    path: Path
    payload: bytes | None
    sha256: str
    mode: int
    device: int
    inode: int


def _regular_file_snapshot(
    path: Path,
    *,
    code: str,
    label: str,
    max_bytes: int | None = None,
    capture_payload: bool = False,
    executable: bool = False,
) -> _RegularFileSnapshot:
    canonical = _canonical_path(path, code=code, field_name=label)
    try:
        before = canonical.lstat()
    except OSError as exc:
        raise ParityValidationError(
            code,
            f"{label} cannot be inspected safely.",
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or (executable and not os.access(str(canonical), os.X_OK))
        or (max_bytes is not None and before.st_size > max_bytes)
    ):
        raise ParityValidationError(
            code,
            f"{label} is not a supported regular file.",
        )
    try:
        descriptor = os.open(
            canonical,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ParityValidationError(
            code,
            f"{label} cannot be opened safely.",
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
            raise ParityValidationError(
                code,
                f"{label} changed before reading.",
            )
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ParityValidationError(
                    code,
                    f"{label} exceeds its size limit.",
                )
            digest.update(chunk)
            if capture_payload:
                chunks.append(chunk)
        read_complete = os.fstat(descriptor)
        try:
            after = canonical.lstat()
        except OSError as exc:
            raise ParityValidationError(
                code,
                f"{label} changed while reading.",
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
            raise ParityValidationError(
                code,
                f"{label} changed while reading.",
            )
        return _RegularFileSnapshot(
            path=canonical,
            payload=b"".join(chunks) if capture_payload else None,
            sha256=digest.hexdigest(),
            mode=stat.S_IMODE(opened.st_mode),
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    finally:
        os.close(descriptor)


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        _plain_value(payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _payload_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _matches_sha256(expected: str, observed: str) -> bool:
    return (
        isinstance(observed, str)
        and _SHA256_RE.fullmatch(observed) is not None
        and hmac.compare_digest(expected, observed)
    )


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_value(child)
                for key, child in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(child) for child in value)
    return value


def _plain_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _plain_value(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_value(child) for child in value]
    return value


class _BoundedCapture:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.buffer = bytearray()
        self.discarded = 0
        self.lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self.lock:
            remaining = self.max_bytes - len(self.buffer)
            if remaining > 0:
                self.buffer.extend(chunk[:remaining])
            self.discarded += max(0, len(chunk) - max(0, remaining))

    def render(self) -> tuple[str, bool]:
        with self.lock:
            payload = bytes(self.buffer)
            truncated = self.discarded > 0
        return payload.decode("utf-8", errors="replace"), truncated


def _read_bounded_stream(stream: object, capture: _BoundedCapture) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)  # type: ignore[union-attr]
            if not chunk:
                return
            capture.append(chunk)
    except (OSError, ValueError):
        return


def _terminate_feature_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


@dataclass(frozen=True)
class ParityPolicyVersion:
    value: str = PARITY_POLICY_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or _POLICY_VERSION_RE.fullmatch(self.value) is None
        ):
            raise ParityValidationError(
                "parity.policy.version_invalid",
                "Parity policy version must be a positive canonical integer.",
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ParityTimeouts:
    command_seconds: float = 5.0
    probe_seconds: float = 30.0
    feature_output_bytes: int = 128 * 1024
    probe_output_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for field_name, value in (
            ("command_seconds", self.command_seconds),
            ("probe_seconds", self.probe_seconds),
        ):
            if not isinstance(value, (int, float)) or value <= 0:
                raise ParityValidationError(
                    "parity.preparation.timeouts_invalid",
                    f"Parity preparation {field_name} must be positive.",
                )
        for field_name, value, maximum in (
            (
                "feature_output_bytes",
                self.feature_output_bytes,
                MAX_PARITY_PROBE_OUTPUT_BYTES,
            ),
            (
                "probe_output_bytes",
                self.probe_output_bytes,
                MAX_PARITY_PROBE_OUTPUT_BYTES,
            ),
        ):
            if (
                not isinstance(value, int)
                or value <= 0
                or value > maximum
            ):
                raise ParityValidationError(
                    "parity.preparation.timeouts_invalid",
                    f"Parity preparation {field_name} is invalid.",
                )


@dataclass(frozen=True)
class OfficialReference:
    authority: str
    bundle_root: Path
    bundle_id: str
    bundle_version: str
    bundled_cli: Path
    cli_version: str
    binary_sha256: str
    schema_sha256: str
    feature_inventory_sha256: str

    def __post_init__(self) -> None:
        if self.authority != REFERENCE_AUTHORITY:
            raise ParityValidationError(
                "parity.reference.authority_invalid",
                "Official parity reference authority must be the verified ChatGPT bundle.",
            )
        if self.bundle_id != CURRENT_CHATGPT_BUNDLE_ID:
            raise ParityValidationError(
                "parity.reference.bundle_id_invalid",
                "Official parity reference bundle id is not the current ChatGPT host.",
            )
        bundle_root = _canonical_path(
            self.bundle_root,
            code="parity.reference.path_invalid",
            field_name="bundle_root",
        )
        bundled_cli = _canonical_path(
            self.bundled_cli,
            code="parity.reference.path_invalid",
            field_name="bundled_cli",
        )
        expected_cli = bundle_root / "Contents" / "Resources" / "codex"
        if bundled_cli != expected_cli:
            raise ParityValidationError(
                "parity.reference.path_invalid",
                "Official parity reference CLI must be the verified bundle CLI.",
            )
        _require_text(
            self.bundle_version,
            code="parity.reference.version_invalid",
            field_name="bundle_version",
        )
        _require_text(
            self.cli_version,
            code="parity.reference.version_invalid",
            field_name="cli_version",
        )
        for field_name, value in (
            ("binary_sha256", self.binary_sha256),
            ("schema_sha256", self.schema_sha256),
            ("feature_inventory_sha256", self.feature_inventory_sha256),
        ):
            _require_sha256(
                value,
                code="parity.reference.digest_invalid",
                field_name=field_name,
            )

    @classmethod
    def from_runtime_binding(
        cls,
        binding: RuntimeBinding,
        *,
        bundle_version: str,
        cli_version: str,
        binary_sha256: str,
        schema_sha256: str,
        feature_inventory_sha256: str,
    ) -> OfficialReference:
        host = binding.desktop_host
        if (
            binding.profile != "openai-official"
            or not isinstance(host, ChatGPTDesktopHost)
            or not host.healthy
            or host.migration_only
            or host.bundle_id != CURRENT_CHATGPT_BUNDLE_ID
            or binding.requires_proxy
            or binding.shell_cli != host.bundled_cli
            or binding.desktop_cli != host.bundled_cli
            or binding.backend_cli != host.bundled_cli
        ):
            raise ParityValidationError(
                "parity.reference.binding_invalid",
                "Official parity reference requires the canonical verified ChatGPT binding.",
            )
        return cls(
            authority=REFERENCE_AUTHORITY,
            bundle_root=host.bundle_root,
            bundle_id=host.bundle_id,
            bundle_version=bundle_version,
            bundled_cli=host.bundled_cli,
            cli_version=cli_version,
            binary_sha256=binary_sha256,
            schema_sha256=schema_sha256,
            feature_inventory_sha256=feature_inventory_sha256,
        )

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "authority": self.authority,
                "binary_sha256": self.binary_sha256,
                "bundle_id": self.bundle_id,
                "bundle_root": str(self.bundle_root),
                "bundle_version": self.bundle_version,
                "bundled_cli": str(self.bundled_cli),
                "cli_version": self.cli_version,
                "feature_inventory_sha256": self.feature_inventory_sha256,
                "schema_sha256": self.schema_sha256,
            }
        )

    @property
    def fingerprint_sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())

    def matches_fingerprint(self, fingerprint_sha256: str) -> bool:
        return _matches_sha256(self.fingerprint_sha256, fingerprint_sha256)

    def is_stale(self, fingerprint_sha256: str) -> bool:
        return not self.matches_fingerprint(fingerprint_sha256)


@dataclass(frozen=True)
class FeatureCommandRequest:
    phase: str
    command: tuple[str, ...]
    codex_home: Path
    timeout_seconds: float
    max_output_bytes: int

    def __post_init__(self) -> None:
        if self.phase not in {"isolated", "effective"}:
            raise ParityValidationError(
                "parity.feature.request_invalid",
                "Feature-list request phase is unsupported.",
            )
        if (
            len(self.command) != 3
            or self.command[1:] != ("features", "list")
            or not self.command[0]
        ):
            raise ParityValidationError(
                "parity.feature.request_invalid",
                "Feature-list request command is invalid.",
            )
        _canonical_path(
            self.codex_home,
            code="parity.feature.request_invalid",
            field_name="feature-list CODEX_HOME",
        )
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ParityValidationError(
                "parity.feature.request_invalid",
                "Feature-list bounds must be positive.",
            )


@dataclass(frozen=True)
class FeatureCommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


FeatureRunner = Callable[[FeatureCommandRequest], FeatureCommandResult]


def _run_feature_command(
    request: FeatureCommandRequest,
) -> FeatureCommandResult:
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(request.codex_home)
    try:
        process = subprocess.Popen(
            request.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=environment,
            start_new_session=True,
        )
    except OSError as exc:
        return FeatureCommandResult(
            returncode=None,
            stdout="",
            stderr=str(exc),
        )

    stdout_capture = _BoundedCapture(request.max_output_bytes)
    stderr_capture = _BoundedCapture(request.max_output_bytes)
    threads = (
        threading.Thread(
            target=_read_bounded_stream,
            args=(process.stdout, stdout_capture),
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded_stream,
            args=(process.stderr, stderr_capture),
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=request.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if timed_out:
            _terminate_feature_process(process)

    deadline = time.monotonic() + 0.5
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        timed_out = True
        _terminate_feature_process(process)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    stdout, stdout_truncated = stdout_capture.render()
    stderr, stderr_truncated = stderr_capture.render()
    return FeatureCommandResult(
        returncode=process.poll(),
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


@dataclass(frozen=True)
class FeatureRecord:
    name: str
    stage: str
    isolated_default: bool
    effective_state: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _FEATURE_NAME_RE.fullmatch(self.name) is None
            or self.stage not in _FEATURE_STAGES
            or type(self.isolated_default) is not bool
            or type(self.effective_state) is not bool
        ):
            raise ParityValidationError(
                "parity.feature.output_invalid",
                "Feature record is invalid.",
            )

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "effective_state": self.effective_state,
                "isolated_default": self.isolated_default,
                "name": self.name,
                "stage": self.stage,
            }
        )


@dataclass(frozen=True)
class FeatureInventory:
    side: str
    features: tuple[FeatureRecord, ...]

    def __post_init__(self) -> None:
        if self.side not in {"official", "internal"}:
            raise ParityValidationError(
                "parity.feature.inventory_invalid",
                "Feature inventory side is unsupported.",
            )
        normalized = tuple(sorted(self.features, key=lambda item: item.name))
        names = [item.name for item in normalized]
        if len(names) != len(set(names)):
            raise ParityValidationError(
                "parity.feature.output_invalid",
                "Feature inventory contains duplicate names.",
            )
        object.__setattr__(self, "features", normalized)

    def feature(self, name: str) -> FeatureRecord:
        for feature in self.features:
            if feature.name == name:
                return feature
        raise KeyError(name)

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "features": [
                    dict(feature.canonical_payload())
                    for feature in self.features
                ],
                "side": self.side,
            }
        )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_payload())

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True)
class FeatureInventoryComparisonEntry:
    name: str
    official: FeatureRecord | None
    internal: FeatureRecord | None

    def __post_init__(self) -> None:
        if (
            _FEATURE_NAME_RE.fullmatch(self.name) is None
            or (self.official is None and self.internal is None)
            or (
                self.official is not None
                and self.official.name != self.name
            )
            or (
                self.internal is not None
                and self.internal.name != self.name
            )
        ):
            raise ParityValidationError(
                "parity.feature.inventory_invalid",
                "Feature comparison entry is invalid.",
            )


@dataclass(frozen=True)
class FeatureInventoryComparison:
    entries: tuple[FeatureInventoryComparisonEntry, ...]

    def __post_init__(self) -> None:
        normalized = tuple(sorted(self.entries, key=lambda item: item.name))
        names = [item.name for item in normalized]
        if len(names) != len(set(names)):
            raise ParityValidationError(
                "parity.feature.inventory_invalid",
                "Feature comparison contains duplicate names.",
            )
        object.__setattr__(self, "entries", normalized)

    def entry(self, name: str) -> FeatureInventoryComparisonEntry:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise KeyError(name)


def _parse_feature_output(output: str) -> dict[str, tuple[str, bool]]:
    if not isinstance(output, str) or not output:
        raise ParityValidationError(
            "parity.feature.output_invalid",
            "Feature-list output is empty or invalid.",
        )
    parsed: dict[str, tuple[str, bool]] = {}
    for raw_line in output.splitlines():
        match = _FEATURE_LINE_RE.fullmatch(raw_line)
        if match is None:
            raise ParityValidationError(
                "parity.feature.output_invalid",
                "Feature-list output contains a malformed row.",
            )
        name = match.group("name")
        if name in parsed:
            raise ParityValidationError(
                "parity.feature.output_invalid",
                "Feature-list output contains a duplicate feature.",
            )
        parsed[name] = (
            match.group("stage"),
            match.group("state") == "true",
        )
    if not parsed:
        raise ParityValidationError(
            "parity.feature.output_invalid",
            "Feature-list output contains no features.",
        )
    return parsed


def _feature_result_output(result: FeatureCommandResult) -> str:
    if (
        result.returncode != 0
        or result.timed_out
        or result.stdout_truncated
        or result.stderr_truncated
    ):
        raise ParityValidationError(
            "parity.feature.command_failed",
            "Feature-list command failed or exceeded its bounds.",
        )
    return result.stdout


def collect_feature_inventory(
    *,
    side: str,
    cli_path: Path,
    isolated_home: Path,
    effective_home: Path,
    runner: FeatureRunner | None = None,
    timeout_seconds: float = 5.0,
    max_output_bytes: int = 128 * 1024,
) -> FeatureInventory:
    canonical_cli = _canonical_path(
        cli_path,
        code="parity.feature.request_invalid",
        field_name="feature-list CLI",
    )
    canonical_isolated_home = _canonical_path(
        isolated_home,
        code="parity.feature.request_invalid",
        field_name="isolated CODEX_HOME",
    )
    canonical_effective_home = _canonical_path(
        effective_home,
        code="parity.feature.request_invalid",
        field_name="effective CODEX_HOME",
    )
    if canonical_isolated_home == canonical_effective_home:
        raise ParityValidationError(
            "parity.feature.request_invalid",
            "Isolated and effective feature homes must be distinct.",
        )
    selected_runner = runner or _run_feature_command
    command = (str(canonical_cli), "features", "list")
    isolated_result = selected_runner(
        FeatureCommandRequest(
            phase="isolated",
            command=command,
            codex_home=canonical_isolated_home,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
    )
    isolated = _parse_feature_output(
        _feature_result_output(isolated_result)
    )
    effective_result = selected_runner(
        FeatureCommandRequest(
            phase="effective",
            command=command,
            codex_home=canonical_effective_home,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
    )
    effective = _parse_feature_output(
        _feature_result_output(effective_result)
    )
    if set(isolated) != set(effective):
        raise ParityValidationError(
            "parity.feature.output_invalid",
            "Feature names changed between isolated and effective inventory.",
        )
    features: list[FeatureRecord] = []
    for name in sorted(isolated):
        isolated_stage, isolated_default = isolated[name]
        effective_stage, effective_state = effective[name]
        if effective_stage != isolated_stage:
            raise ParityValidationError(
                "parity.feature.output_invalid",
                "Feature stage changed between isolated and effective inventory.",
            )
        features.append(
            FeatureRecord(
                name=name,
                stage=isolated_stage,
                isolated_default=isolated_default,
                effective_state=effective_state,
            )
        )
    return FeatureInventory(side=side, features=tuple(features))


def compare_feature_inventories(
    official: FeatureInventory,
    internal: FeatureInventory,
) -> FeatureInventoryComparison:
    if official.side != "official" or internal.side != "internal":
        raise ParityValidationError(
            "parity.feature.inventory_invalid",
            "Feature comparison requires official then internal inventories.",
        )
    official_by_name = {
        feature.name: feature
        for feature in official.features
    }
    internal_by_name = {
        feature.name: feature
        for feature in internal.features
    }
    return FeatureInventoryComparison(
        entries=tuple(
            FeatureInventoryComparisonEntry(
                name=name,
                official=official_by_name.get(name),
                internal=internal_by_name.get(name),
            )
            for name in sorted(set(official_by_name) | set(internal_by_name))
        )
    )


def _protocol_sort_key(direction: str, method: str) -> tuple[int, str]:
    return _PROTOCOL_DIRECTION_INDEX[direction], method


def _protocol_schema_error(code: str, message: str) -> None:
    raise ParityValidationError(code, message)


def _canonicalize_json_value(value: object) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, list):
        return [_canonicalize_json_value(child) for child in value]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                _protocol_schema_error(
                    "parity.protocol.schema_invalid",
                    "Protocol schema JSON object keys must be strings.",
                )
            normalized[key] = _canonicalize_json_value(value[key])
        return normalized
    _protocol_schema_error(
        "parity.protocol.schema_invalid",
        "Protocol schema contains a non-JSON value.",
    )
    raise AssertionError("unreachable")


def _protocol_ref_name(reference: object) -> str:
    prefix = "#/definitions/"
    if (
        not isinstance(reference, str)
        or not reference.startswith(prefix)
        or len(reference) == len(prefix)
        or "/" in reference[len(prefix):]
    ):
        _protocol_schema_error(
            "parity.protocol.schema_unsupported",
            "Protocol schema references must target one local definition.",
        )
    encoded = reference[len(prefix):]
    name = encoded.replace("~1", "/").replace("~0", "~")
    if not name:
        _protocol_schema_error(
            "parity.protocol.schema_invalid",
            "Protocol schema reference name is empty.",
        )
    return name


def _normalized_schema_sort_key(schema: object) -> bytes:
    return json.dumps(
        _plain_value(schema),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _simple_schema_type_union(
    branches: list[object],
) -> list[str] | None:
    union: set[str] = set()
    for branch in branches:
        if not isinstance(branch, Mapping) or set(branch) != {"type"}:
            return None
        branch_type = branch["type"]
        if isinstance(branch_type, str):
            branch_types = (branch_type,)
        elif isinstance(branch_type, (list, tuple)):
            branch_types = tuple(branch_type)
        else:
            return None
        if (
            not branch_types
            or any(
                not isinstance(entry, str)
                or entry not in _JSON_SCHEMA_TYPES
                for entry in branch_types
            )
        ):
            return None
        union.update(branch_types)
    return sorted(union) if union else None


def _normalize_protocol_schema(
    schema: object,
    *,
    definitions: Mapping[str, object],
    active_references: tuple[str, ...] = (),
    reference_cache: dict[str, object] | None = None,
) -> object:
    if type(schema) is bool:
        return schema
    if not isinstance(schema, Mapping):
        _protocol_schema_error(
            "parity.protocol.schema_invalid",
            "Protocol schema node must be an object.",
        )
    unknown = set(schema).difference(
        _PROTOCOL_SCHEMA_KEYS | _PROTOCOL_DOCUMENTATION_KEYS
    )
    if unknown:
        _protocol_schema_error(
            "parity.protocol.schema_unsupported",
            "Protocol schema contains unsupported constructs: "
            + ", ".join(sorted(str(key) for key in unknown)),
        )

    cache = reference_cache if reference_cache is not None else {}
    if "$ref" in schema:
        siblings = set(schema).difference(
            _PROTOCOL_DOCUMENTATION_KEYS | {"$ref"}
        )
        if siblings:
            _protocol_schema_error(
                "parity.protocol.schema_unsupported",
                "Protocol schema reference siblings are unsupported.",
            )
        reference_name = _protocol_ref_name(schema["$ref"])
        if reference_name in active_references:
            _protocol_schema_error(
                "parity.protocol.reference_cycle",
                "Protocol schema contains a local reference cycle.",
            )
        cached = cache.get(reference_name)
        if cached is not None:
            return cached
        target = definitions.get(reference_name)
        if target is None:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema references a missing local definition.",
            )
        normalized_target = _normalize_protocol_schema(
            target,
            definitions=definitions,
            active_references=(*active_references, reference_name),
            reference_cache=cache,
        )
        cache[reference_name] = normalized_target
        return normalized_target

    result: dict[str, object] = {}
    schema_type = schema.get("type")
    if schema_type is not None:
        if isinstance(schema_type, str):
            if schema_type not in _JSON_SCHEMA_TYPES:
                _protocol_schema_error(
                    "parity.protocol.schema_unsupported",
                    "Protocol schema type is unsupported.",
                )
            result["type"] = schema_type
        elif isinstance(schema_type, list):
            if (
                not schema_type
                or any(
                    not isinstance(entry, str)
                    or entry not in _JSON_SCHEMA_TYPES
                    for entry in schema_type
                )
                or len(schema_type) != len(set(schema_type))
            ):
                _protocol_schema_error(
                    "parity.protocol.schema_invalid",
                    "Protocol schema type union is invalid.",
                )
            result["type"] = sorted(schema_type)
        else:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema type must be a string or string array.",
            )

    enum_values = schema.get("enum")
    if enum_values is not None:
        if not isinstance(enum_values, list) or not enum_values:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema enum must be a non-empty array.",
            )
        normalized_values = [
            _canonicalize_json_value(value)
            for value in enum_values
        ]
        by_bytes: dict[bytes, object] = {}
        for value in normalized_values:
            encoded = json.dumps(
                value,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            by_bytes[encoded] = value
        if len(by_bytes) != len(normalized_values):
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema enum contains duplicate values.",
            )
        result["enum"] = [
            by_bytes[key]
            for key in sorted(by_bytes)
        ]

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema properties must be an object.",
            )
        normalized_properties: dict[str, object] = {}
        for name in sorted(properties):
            if not isinstance(name, str) or not name:
                _protocol_schema_error(
                    "parity.protocol.schema_invalid",
                    "Protocol schema property names must be non-empty strings.",
                )
            normalized_properties[name] = _normalize_protocol_schema(
                properties[name],
                definitions=definitions,
                active_references=active_references,
                reference_cache=cache,
            )
        result["properties"] = normalized_properties

    required = schema.get("required")
    if required is not None:
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) or not name for name in required)
            or len(required) != len(set(required))
        ):
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema required fields are invalid.",
            )
        result["required"] = sorted(required)

    additional_properties = schema.get("additionalProperties")
    if additional_properties is not None:
        if type(additional_properties) is bool:
            result["additionalProperties"] = additional_properties
        elif isinstance(additional_properties, Mapping):
            result["additionalProperties"] = _normalize_protocol_schema(
                additional_properties,
                definitions=definitions,
                active_references=active_references,
                reference_cache=cache,
            )
        else:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema additionalProperties is invalid.",
            )

    items = schema.get("items")
    if items is not None:
        if isinstance(items, list):
            _protocol_schema_error(
                "parity.protocol.schema_unsupported",
                "Tuple-form protocol array items are unsupported.",
            )
        if type(items) is not bool and not isinstance(items, Mapping):
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema array items are invalid.",
            )
        result["items"] = _normalize_protocol_schema(
            items,
            definitions=definitions,
            active_references=active_references,
            reference_cache=cache,
        )

    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list) or not branches:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                f"Protocol schema {keyword} must be a non-empty array.",
            )
        normalized_branches = [
            _normalize_protocol_schema(
                branch,
                definitions=definitions,
                active_references=active_references,
                reference_cache=cache,
            )
            for branch in branches
        ]
        normalized_branches.sort(key=_normalized_schema_sort_key)
        if keyword == "anyOf" and "type" not in result:
            type_union = _simple_schema_type_union(normalized_branches)
            if type_union is not None:
                result["type"] = type_union
                continue
        if len(normalized_branches) == 1 and not result:
            return normalized_branches[0]
        result[keyword] = normalized_branches

    default = schema.get("default")
    if "default" in schema:
        result["default"] = _canonicalize_json_value(default)

    schema_format = schema.get("format")
    if schema_format is not None:
        if not isinstance(schema_format, str) or not schema_format:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema format must be a non-empty string.",
            )
        result["format"] = schema_format

    min_length = schema.get("minLength")
    if min_length is not None:
        if type(min_length) is not int or min_length < 0:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema minLength must be a non-negative integer.",
            )
        result["minLength"] = min_length

    minimum = schema.get("minimum")
    if minimum is not None:
        if type(minimum) not in {int, float}:
            _protocol_schema_error(
                "parity.protocol.schema_invalid",
                "Protocol schema minimum must be numeric.",
            )
        result["minimum"] = minimum

    return result


def _protocol_method_from_variant(variant: object) -> str:
    if not isinstance(variant, Mapping):
        _protocol_schema_error(
            "parity.protocol.schema_invalid",
            "Protocol root variants must be objects.",
        )
    properties = variant.get("properties")
    if not isinstance(properties, Mapping):
        _protocol_schema_error(
            "parity.protocol.schema_invalid",
            "Protocol root variant properties are missing.",
        )
    method_schema = properties.get("method")
    if not isinstance(method_schema, Mapping):
        _protocol_schema_error(
            "parity.protocol.schema_invalid",
            "Protocol root variant method schema is missing.",
        )
    method_values = method_schema.get("enum")
    if (
        not isinstance(method_values, list)
        or len(method_values) != 1
        or not isinstance(method_values[0], str)
        or _PROTOCOL_METHOD_RE.fullmatch(method_values[0]) is None
    ):
        _protocol_schema_error(
            "parity.protocol.schema_invalid",
            "Protocol method must use one canonical string enum value.",
        )
    return method_values[0]


class ProtocolMethodRecord:
    __slots__ = ("direction", "method", "_schema")

    def __init__(
        self,
        *,
        direction: str,
        method: str,
        schema: Mapping[str, object],
    ) -> None:
        if direction not in _PROTOCOL_DIRECTION_INDEX:
            raise ParityValidationError(
                "parity.protocol.inventory_invalid",
                "Protocol method direction is unsupported.",
            )
        if (
            not isinstance(method, str)
            or _PROTOCOL_METHOD_RE.fullmatch(method) is None
        ):
            raise ParityValidationError(
                "parity.protocol.inventory_invalid",
                "Protocol method name is invalid.",
            )
        if not isinstance(schema, Mapping):
            raise ParityValidationError(
                "parity.protocol.inventory_invalid",
                "Protocol method schema is invalid.",
            )
        self.direction = direction
        self.method = method
        self._schema = _freeze_value(schema)

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise FrozenInstanceError(
                f"cannot assign to field '{name}'"
            )
        object.__setattr__(self, name, value)

    @property
    def schema(self) -> Mapping[str, object]:
        plain = _plain_value(self._schema)
        assert isinstance(plain, dict)
        return MappingProxyType(plain)

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "direction": self.direction,
                "method": self.method,
                "schema": _plain_value(self._schema),
            }
        )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_payload())

    @property
    def schema_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True)
class ProtocolInventory:
    side: str
    methods: tuple[ProtocolMethodRecord, ...]

    def __post_init__(self) -> None:
        if self.side not in {"official", "internal"}:
            raise ParityValidationError(
                "parity.protocol.inventory_invalid",
                "Protocol inventory side is unsupported.",
            )
        normalized = tuple(
            sorted(
                self.methods,
                key=lambda record: _protocol_sort_key(
                    record.direction,
                    record.method,
                ),
            )
        )
        keys = [
            (record.direction, record.method)
            for record in normalized
        ]
        if len(keys) != len(set(keys)):
            raise ParityValidationError(
                "parity.protocol.schema_invalid",
                "Protocol inventory contains duplicate methods.",
            )
        object.__setattr__(self, "methods", normalized)

    def method(
        self,
        direction: str,
        method: str,
    ) -> ProtocolMethodRecord:
        for record in self.methods:
            if record.direction == direction and record.method == method:
                return record
        raise KeyError((direction, method))

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "methods": [
                    dict(record.canonical_payload())
                    for record in self.methods
                ],
                "side": self.side,
            }
        )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_payload())

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def collect_protocol_inventory(
    *,
    side: str,
    schema_documents: Mapping[str, object],
) -> ProtocolInventory:
    if side not in {"official", "internal"}:
        raise ParityValidationError(
            "parity.protocol.inventory_invalid",
            "Protocol inventory side is unsupported.",
        )
    if not isinstance(schema_documents, Mapping):
        raise ParityValidationError(
            "parity.protocol.schema_invalid",
            "Protocol schema documents must be a mapping.",
        )
    records: list[ProtocolMethodRecord] = []
    for direction in PROTOCOL_DIRECTIONS:
        root_name = _PROTOCOL_ROOT_NAMES[direction]
        matches = [
            document
            for path, document in schema_documents.items()
            if isinstance(path, str) and Path(path).name == root_name
        ]
        if len(matches) != 1 or not isinstance(matches[0], Mapping):
            raise ParityValidationError(
                "parity.protocol.schema_invalid",
                f"Protocol schema bundle must contain one {root_name}.",
            )
        root = matches[0]
        definitions = root.get("definitions", {})
        if not isinstance(definitions, Mapping):
            raise ParityValidationError(
                "parity.protocol.schema_invalid",
                f"{root_name} definitions must be an object.",
            )
        variants = root.get("oneOf")
        if not isinstance(variants, list) or not variants:
            raise ParityValidationError(
                "parity.protocol.schema_invalid",
                f"{root_name} must contain method variants.",
            )
        reference_cache: dict[str, object] = {}
        for variant in variants:
            method = _protocol_method_from_variant(variant)
            normalized_schema = _normalize_protocol_schema(
                variant,
                definitions=definitions,
                reference_cache=reference_cache,
            )
            if not isinstance(normalized_schema, Mapping):
                raise ParityValidationError(
                    "parity.protocol.schema_invalid",
                    "Protocol method envelope must normalize to an object.",
                )
            records.append(
                ProtocolMethodRecord(
                    direction=direction,
                    method=method,
                    schema=normalized_schema,
                )
            )
    return ProtocolInventory(side=side, methods=tuple(records))


def _schema_types(schema: Mapping[str, object]) -> set[str] | None:
    value = schema.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple)):
        return {
            entry
            for entry in value
            if isinstance(entry, str)
        }
    return None


def _type_is_accepted(
    producer_type: str,
    consumer_types: set[str],
) -> bool:
    return (
        producer_type in consumer_types
        or (
            producer_type == "integer"
            and "number" in consumer_types
        )
    )


def _enum_keys(values: object) -> set[bytes] | None:
    if not isinstance(values, (list, tuple)):
        return None
    return {
        json.dumps(
            _plain_value(value),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        for value in values
    }


def _schema_without(
    schema: Mapping[str, object],
    *keys: str,
) -> Mapping[str, object]:
    return {
        key: value
        for key, value in schema.items()
        if key not in keys
    }


def _schema_intersection(
    base: Mapping[str, object],
    branch: object,
) -> object:
    if branch is False:
        return False
    if branch is True:
        return base if base else True
    if not isinstance(branch, Mapping):
        return False
    if not base:
        return branch
    return {"allOf": (base, branch)}


def _union_branches(
    schema: Mapping[str, object],
) -> tuple[object, ...] | None:
    for keyword in ("anyOf", "oneOf"):
        raw_branches = schema.get(keyword)
        if not isinstance(raw_branches, (list, tuple)):
            continue
        base = _schema_without(schema, keyword)
        branches: list[object] = []
        for raw_branch in raw_branches:
            branches.append(_schema_intersection(base, raw_branch))
        return tuple(branches)
    return None


def _compare_all_of_producer(
    producer: Mapping[str, object],
    consumer: Mapping[str, object],
) -> set[str] | None:
    branches = producer.get("allOf")
    if not isinstance(branches, (list, tuple)):
        return None
    base = _schema_without(producer, "allOf")
    best: set[str] | None = None
    for branch in branches:
        candidate = _schema_intersection(base, branch)
        reasons = _protocol_schema_subset_reasons(candidate, consumer)
        if not reasons:
            return set()
        if best is None or len(reasons) < len(best):
            best = reasons
    return best or {"parity.protocol.schema_incompatible"}


def _compare_all_of_consumer(
    producer: Mapping[str, object],
    consumer: Mapping[str, object],
) -> set[str] | None:
    branches = consumer.get("allOf")
    if not isinstance(branches, (list, tuple)):
        return None
    reasons: set[str] = set()
    base = _schema_without(consumer, "allOf")
    if base:
        reasons.update(
            _protocol_schema_subset_reasons(producer, base)
        )
    for branch in branches:
        reasons.update(
            _protocol_schema_subset_reasons(producer, branch)
        )
    return reasons


def _protocol_schema_subset_reasons(
    producer: object,
    consumer: object,
) -> set[str]:
    if _canonical_json_bytes(producer) == _canonical_json_bytes(consumer):
        return set()
    if producer is False:
        return set()
    if consumer is False:
        return {"parity.protocol.schema_incompatible"}
    if producer is True:
        producer = {}
    if consumer is True:
        consumer = {}
    if not isinstance(producer, Mapping) or not isinstance(consumer, Mapping):
        return {"parity.protocol.schema_incompatible"}

    producer_union = _union_branches(producer)
    consumer_union = _union_branches(consumer)
    if producer_union is not None or consumer_union is not None:
        producer_branches = producer_union or (producer,)
        consumer_branches = consumer_union or (consumer,)
        reasons: set[str] = set()
        for producer_branch in producer_branches:
            branch_results = [
                _protocol_schema_subset_reasons(
                    producer_branch,
                    consumer_branch,
                )
                for consumer_branch in consumer_branches
            ]
            if any(not branch_reasons for branch_reasons in branch_results):
                continue
            reasons.update(
                min(branch_results, key=lambda item: (len(item), sorted(item)))
            )
        return reasons

    producer_all_of = _compare_all_of_producer(producer, consumer)
    if producer_all_of is not None:
        return producer_all_of
    consumer_all_of = _compare_all_of_consumer(producer, consumer)
    if consumer_all_of is not None:
        return consumer_all_of

    reasons: set[str] = set()
    producer_types = _schema_types(producer)
    consumer_types = _schema_types(consumer)
    if consumer_types is not None:
        if producer_types is None or any(
            not _type_is_accepted(producer_type, consumer_types)
            for producer_type in producer_types
        ):
            reasons.add("parity.protocol.type_incompatible")

    producer_enum = _enum_keys(producer.get("enum"))
    consumer_enum = _enum_keys(consumer.get("enum"))
    if consumer_enum is not None and (
        producer_enum is None
        or not producer_enum.issubset(consumer_enum)
    ):
        reasons.add("parity.protocol.enum_incompatible")

    producer_required = set(producer.get("required", ()))
    consumer_required = set(consumer.get("required", ()))
    if not consumer_required.issubset(producer_required):
        reasons.add("parity.protocol.required_field_incompatible")

    producer_properties = producer.get("properties", {})
    consumer_properties = consumer.get("properties", {})
    if isinstance(producer_properties, Mapping) and isinstance(
        consumer_properties,
        Mapping,
    ):
        for name in sorted(
            set(producer_properties).intersection(consumer_properties)
        ):
            producer_child = producer_properties[name]
            consumer_child = consumer_properties[name]
            reasons.update(
                _protocol_schema_subset_reasons(
                    producer_child,
                    consumer_child,
                )
            )
        consumer_additional = consumer.get("additionalProperties")
        for name in set(producer_properties).difference(consumer_properties):
            producer_child = producer_properties[name]
            additional_reasons = _protocol_schema_subset_reasons(
                producer_child,
                consumer_additional
                if consumer_additional is not None
                else True,
            )
            if additional_reasons:
                reasons.add(
                    "parity.protocol.additional_property_incompatible"
                )
                reasons.update(additional_reasons)

    producer_additional = producer.get("additionalProperties", True)
    consumer_additional = consumer.get("additionalProperties", True)
    additional_reasons = _protocol_schema_subset_reasons(
        producer_additional,
        consumer_additional,
    )
    if additional_reasons:
        reasons.add("parity.protocol.additional_property_incompatible")
        reasons.update(additional_reasons)

    producer_items = producer.get("items")
    consumer_items = consumer.get("items")
    if consumer_items is not None:
        if producer_items is None:
            reasons.add("parity.protocol.items_incompatible")
        else:
            item_reasons = _protocol_schema_subset_reasons(
                producer_items,
                consumer_items,
            )
            if item_reasons:
                reasons.add("parity.protocol.items_incompatible")
                reasons.update(
                    item_reasons
                )

    consumer_format = consumer.get("format")
    producer_format = producer.get("format")
    if (
        isinstance(consumer_format, str)
        and producer_format != consumer_format
    ):
        reasons.add("parity.protocol.format_incompatible")

    consumer_min_length = consumer.get("minLength")
    producer_min_length = producer.get("minLength")
    if isinstance(consumer_min_length, int) and (
        not isinstance(producer_min_length, int)
        or producer_min_length < consumer_min_length
    ):
        reasons.add("parity.protocol.constraint_incompatible")

    consumer_minimum = consumer.get("minimum")
    producer_minimum = producer.get("minimum")
    if type(consumer_minimum) in {int, float} and (
        type(producer_minimum) not in {int, float}
        or producer_minimum < consumer_minimum
    ):
        reasons.add("parity.protocol.constraint_incompatible")

    return reasons


@dataclass(frozen=True)
class ProtocolInventoryComparisonEntry:
    direction: str
    method: str
    official: ProtocolMethodRecord | None
    internal: ProtocolMethodRecord | None
    producer_side: str
    consumer_side: str
    compatible: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.direction not in _PROTOCOL_DIRECTION_INDEX
            or _PROTOCOL_METHOD_RE.fullmatch(self.method) is None
            or self.producer_side not in {"official", "internal"}
            or self.consumer_side not in {"official", "internal"}
            or self.producer_side == self.consumer_side
            or type(self.compatible) is not bool
        ):
            raise ParityValidationError(
                "parity.protocol.inventory_invalid",
                "Protocol comparison entry is invalid.",
            )
        object.__setattr__(
            self,
            "reason_codes",
            tuple(sorted(set(self.reason_codes))),
        )


@dataclass(frozen=True)
class ProtocolInventoryComparison:
    entries: tuple[ProtocolInventoryComparisonEntry, ...]

    def __post_init__(self) -> None:
        normalized = tuple(
            sorted(
                self.entries,
                key=lambda entry: _protocol_sort_key(
                    entry.direction,
                    entry.method,
                ),
            )
        )
        keys = [
            (entry.direction, entry.method)
            for entry in normalized
        ]
        if len(keys) != len(set(keys)):
            raise ParityValidationError(
                "parity.protocol.inventory_invalid",
                "Protocol comparison contains duplicate methods.",
            )
        object.__setattr__(self, "entries", normalized)

    def entry(
        self,
        direction: str,
        method: str,
    ) -> ProtocolInventoryComparisonEntry:
        for entry in self.entries:
            if entry.direction == direction and entry.method == method:
                return entry
        raise KeyError((direction, method))


def compare_protocol_inventories(
    official: ProtocolInventory,
    internal: ProtocolInventory,
) -> ProtocolInventoryComparison:
    if official.side != "official" or internal.side != "internal":
        raise ParityValidationError(
            "parity.protocol.inventory_invalid",
            "Protocol comparison requires official then internal inventories.",
        )
    official_by_key = {
        (record.direction, record.method): record
        for record in official.methods
    }
    internal_by_key = {
        (record.direction, record.method): record
        for record in internal.methods
    }
    entries: list[ProtocolInventoryComparisonEntry] = []
    for direction, method in sorted(
        set(official_by_key) | set(internal_by_key),
        key=lambda item: _protocol_sort_key(item[0], item[1]),
    ):
        official_record = official_by_key.get((direction, method))
        internal_record = internal_by_key.get((direction, method))
        if direction.startswith("client_"):
            producer_side = "official"
            consumer_side = "internal"
            producer = official_record
            consumer = internal_record
        else:
            producer_side = "internal"
            consumer_side = "official"
            producer = internal_record
            consumer = official_record
        if producer is None:
            compatible = True
            reason_codes: tuple[str, ...] = ()
        elif consumer is None:
            compatible = False
            reason_codes = ("parity.protocol.method_missing",)
        else:
            reasons = _protocol_schema_subset_reasons(
                producer._schema,
                consumer._schema,
            )
            compatible = not reasons
            reason_codes = tuple(sorted(reasons))
        entries.append(
            ProtocolInventoryComparisonEntry(
                direction=direction,
                method=method,
                official=official_record,
                internal=internal_record,
                producer_side=producer_side,
                consumer_side=consumer_side,
                compatible=compatible,
                reason_codes=reason_codes,
            )
        )
    return ProtocolInventoryComparison(entries=tuple(entries))


_METHOD_COVERAGE_DISPOSITIONS = frozenset(
    {
        "adapter_transformed",
        "native_equivalent",
        "optional_extension",
        "uncovered",
    }
)


@dataclass(frozen=True)
class MethodCoverageRecord:
    direction: str
    method: str
    official_schema_sha256: str
    internal_schema_sha256: str
    reason_codes: tuple[str, ...]
    disposition: str
    adapter_rule_digests: tuple[tuple[str, str], ...] = ()
    optional_extension_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.direction not in _PROTOCOL_DIRECTION_INDEX
            or not isinstance(self.method, str)
            or _PROTOCOL_METHOD_RE.fullmatch(self.method) is None
            or self.disposition not in _METHOD_COVERAGE_DISPOSITIONS
        ):
            raise ParityValidationError(
                "parity.protocol.coverage_invalid",
                "Method coverage identity or disposition is invalid.",
            )
        _require_sha256(
            self.official_schema_sha256,
            code="parity.protocol.coverage_invalid",
            field_name="official method schema digest",
        )
        _require_sha256(
            self.internal_schema_sha256,
            code="parity.protocol.coverage_invalid",
            field_name="internal method schema digest",
        )
        reasons = tuple(sorted(set(self.reason_codes)))
        if (
            len(reasons) != len(self.reason_codes)
            or any(
                not isinstance(code, str)
                or not code.startswith("parity.protocol.")
                or _CODE_RE.fullmatch(code) is None
                for code in reasons
            )
        ):
            raise ParityValidationError(
                "parity.protocol.coverage_invalid",
                "Method coverage reason codes are invalid.",
            )
        adapter_bindings = tuple(
            sorted(
                self.adapter_rule_digests,
                key=lambda item: item[0],
            )
        )
        if (
            len(adapter_bindings)
            != len({rule_id for rule_id, _digest in adapter_bindings})
            or any(
                not isinstance(rule_id, str)
                or not rule_id
                or rule_id != rule_id.strip()
                or not isinstance(digest, str)
                or _SHA256_RE.fullmatch(digest) is None
                for rule_id, digest in adapter_bindings
            )
        ):
            raise ParityValidationError(
                "parity.protocol.coverage_invalid",
                "Method coverage adapter bindings are invalid.",
            )
        extension_ids = tuple(sorted(set(self.optional_extension_ids)))
        if (
            len(extension_ids) != len(self.optional_extension_ids)
            or any(
                not isinstance(extension_id, str)
                or _EXTENSION_ID_RE.fullmatch(extension_id) is None
                for extension_id in extension_ids
            )
        ):
            raise ParityValidationError(
                "parity.protocol.coverage_invalid",
                "Method coverage extension identifiers are invalid.",
            )
        if self.disposition == "native_equivalent":
            valid_shape = (
                self.official_schema_sha256
                == self.internal_schema_sha256
                and not reasons
                and not adapter_bindings
                and not extension_ids
            )
        elif self.disposition == "adapter_transformed":
            valid_shape = bool(reasons and adapter_bindings)
        elif self.disposition == "optional_extension":
            valid_shape = bool(
                reasons and extension_ids and not adapter_bindings
            )
        else:
            valid_shape = bool(
                reasons and not adapter_bindings and not extension_ids
            )
        if not valid_shape:
            raise ParityValidationError(
                "parity.protocol.coverage_invalid",
                "Method coverage proof does not match its disposition.",
            )
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(
            self,
            "adapter_rule_digests",
            adapter_bindings,
        )
        object.__setattr__(
            self,
            "optional_extension_ids",
            extension_ids,
        )

    @property
    def sort_key(self) -> tuple[int, str]:
        return _protocol_sort_key(self.direction, self.method)

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "adapter_rule_digests": [
                    {
                        "rule_id": rule_id,
                        "sha256": digest,
                    }
                    for rule_id, digest in self.adapter_rule_digests
                ],
                "direction": self.direction,
                "disposition": self.disposition,
                "internal_schema_sha256": (
                    self.internal_schema_sha256
                ),
                "method": self.method,
                "official_schema_sha256": (
                    self.official_schema_sha256
                ),
                "optional_extension_ids": list(
                    self.optional_extension_ids
                ),
                "reason_codes": list(self.reason_codes),
            }
        )


@dataclass(frozen=True)
class _ExactMethodCoverageRule:
    direction: str
    method: str
    official_schema_sha256: str
    internal_schema_sha256: str
    reason_codes: tuple[str, ...]
    disposition: str
    optional_extension_ids: tuple[str, ...]
    adapter_rule_ids: tuple[str, ...] = ()

    @property
    def lookup_key(
        self,
    ) -> tuple[str, str, str, str, tuple[str, ...]]:
        return (
            self.direction,
            self.method,
            self.official_schema_sha256,
            self.internal_schema_sha256,
            self.reason_codes,
        )


_EXACT_METHOD_COVERAGE_RULES = (
    _ExactMethodCoverageRule(
        direction="client_request",
        method="account/login/start",
        official_schema_sha256=(
            "9ee1c1845c95aead79d8576ad148dae40812ef7504c01f3e9dbab0db06e76e94"
        ),
        internal_schema_sha256=(
            "c7bb889ffce831bda9988b0ff8cd21d26307a0640ce232f3cd88c2aad0780350"
        ),
        reason_codes=("parity.protocol.enum_incompatible",),
        disposition="optional_extension",
        optional_extension_ids=("amazon_bedrock",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="externalAgentConfig/import",
        official_schema_sha256=(
            "fefc184359f5e62b08965f02947e01289c10f22547d7caf1f29ec07ff032fb04"
        ),
        internal_schema_sha256=(
            "fed545a509c5373f6b20d637e6fecc4e0159ba593a4c41dcc2861e2b522da983"
        ),
        reason_codes=(
            "parity.protocol.enum_incompatible",
            "parity.protocol.items_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("memory_provider_migration",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="plugin/share/updateTargets",
        official_schema_sha256=(
            "ed52c627514626c94a30ce7a8150d39344dd5bb7be64dcc45417134e2e2248a1"
        ),
        internal_schema_sha256=(
            "62854e4d78b8b1650b57f5a5d5bd02b8b45019267d9e8638e34107aee229e85e"
        ),
        reason_codes=("parity.protocol.enum_incompatible",),
        disposition="optional_extension",
        optional_extension_ids=("listed_discoverability",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="thread/realtime/start",
        official_schema_sha256=(
            "e1d4742bc1fede7aa522dad547ebc86e655a77bc5a305fb7a1d65c6f8228cdee"
        ),
        internal_schema_sha256=(
            "cc1e41a2dc2e3199ce5ddb002110f4aba7bd474f474d2f0f0ccfdd11c2739206"
        ),
        reason_codes=("parity.protocol.enum_incompatible",),
        disposition="optional_extension",
        optional_extension_ids=(
            "realtime_v3_handoff_initial_items",
        ),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="thread/resume",
        official_schema_sha256=(
            "c757c1b83790de16ab05fb06972dd72fa834c271edca72857fc3d49908fe5067"
        ),
        internal_schema_sha256=(
            "5cdfe4d5e82e4883bd34e51e4d648110f4cb2bbffc6843b7da51f98a9a10d4fd"
        ),
        reason_codes=(
            "parity.protocol.enum_incompatible",
            "parity.protocol.items_incompatible",
        ),
        disposition="adapter_transformed",
        adapter_rule_ids=(THREAD_RESUME_HISTORY_RULE_ID,),
        optional_extension_ids=("input_audio",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="turn/start",
        official_schema_sha256=(
            "cc050a440131cf50e45858727b6888a6a8569091a560e22fc6da9b52dbb17d29"
        ),
        internal_schema_sha256=(
            "3df948965f5ff576f76d423fabb6ece4828575fc54b5b0179e7e277918e11cb2"
        ),
        reason_codes=(
            "parity.protocol.enum_incompatible",
            "parity.protocol.items_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("local_audio_and_audio",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="turn/steer",
        official_schema_sha256=(
            "97fcf150a9e85f0d99ef0df1c86139d0f29d48d7bf3e2ad1b16437f8c7c871dd"
        ),
        internal_schema_sha256=(
            "4106041861e355b3aee80bcffa88377d2c1b8a6dbd0d4f10a03157a397563c65"
        ),
        reason_codes=(
            "parity.protocol.enum_incompatible",
            "parity.protocol.items_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("local_audio_and_audio",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="thread/queue/add",
        official_schema_sha256=(
            "52b07c5d01bdc0e58d2efce15cb7c8404ad9c11b6a154adda956001fa7f7ba8d"
        ),
        internal_schema_sha256=(
            "1bf66b604b6b284d334b367462945791e55c9edf186a9ad252ccba5ab06a5820"
        ),
        reason_codes=(
            "parity.protocol.items_incompatible",
            "parity.protocol.required_field_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("image_file_reference",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="thread/queue/update",
        official_schema_sha256=(
            "3ed4dcef71d50edc84c8e3912e60009f2158d9138901d8ddd7817ccfdecf4747"
        ),
        internal_schema_sha256=(
            "679732c5c7c7bafaf4a1baff5bec2ee219325244eda62b6cbd0b94cab6522184"
        ),
        reason_codes=(
            "parity.protocol.items_incompatible",
            "parity.protocol.required_field_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("image_file_reference",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="thread/resume",
        official_schema_sha256=(
            "e9035c909d29ecd7e074061c1a3fdbca27cdc91b53644e829fd4d5aa3f2bd5cc"
        ),
        internal_schema_sha256=(
            "5d56e720d9df741def766f4d63562ddd4b3e46aed010512d1bedd4ea6c05f2df"
        ),
        reason_codes=(
            "parity.protocol.enum_incompatible",
            "parity.protocol.items_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("image_file_reference",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="turn/start",
        official_schema_sha256=(
            "d754c4242ca539c4c0b062c8fd1fdb95867b992dee2ad73d47a36b881db30a9d"
        ),
        internal_schema_sha256=(
            "8cf451912811592ce01af647c516ec903ffda8d50fa97a5e9c8497f1c57804b1"
        ),
        reason_codes=(
            "parity.protocol.items_incompatible",
            "parity.protocol.required_field_incompatible",
            "parity.protocol.type_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("image_file_reference",),
    ),
    _ExactMethodCoverageRule(
        direction="client_request",
        method="turn/steer",
        official_schema_sha256=(
            "bed2e44f53ebda862af149f27a311dc9b16e1ff4297d6b198e06d89c45ad4e79"
        ),
        internal_schema_sha256=(
            "97fcf150a9e85f0d99ef0df1c86139d0f29d48d7bf3e2ad1b16437f8c7c871dd"
        ),
        reason_codes=(
            "parity.protocol.items_incompatible",
            "parity.protocol.required_field_incompatible",
        ),
        disposition="optional_extension",
        optional_extension_ids=("image_file_reference",),
    ),
)
_EXACT_METHOD_COVERAGE_BY_KEY = MappingProxyType(
    {
        rule.lookup_key: rule
        for rule in _EXACT_METHOD_COVERAGE_RULES
    }
)
_THREAD_RESUME_ADAPTER_CONTRACT = MappingProxyType(
    {
        "capability_predicate": "always",
        "direction": "client_request",
        "method": "thread/resume",
        "paths": (("params", "history"),),
        "variants": (
            "drop_nonportable_opaque_reasoning",
            "remove_top_level_item_id",
        ),
    }
)


def _thread_resume_rule_matches_coverage_contract(
    rule: ProtocolAdapterRule,
) -> bool:
    if rule.rule_id != THREAD_RESUME_HISTORY_RULE_ID:
        return False
    return (
        rule.direction == _THREAD_RESUME_ADAPTER_CONTRACT["direction"]
        and rule.method == _THREAD_RESUME_ADAPTER_CONTRACT["method"]
        and rule.paths == _THREAD_RESUME_ADAPTER_CONTRACT["paths"]
        and rule.variants == _THREAD_RESUME_ADAPTER_CONTRACT["variants"]
        and rule.capability_predicate
        == _THREAD_RESUME_ADAPTER_CONTRACT["capability_predicate"]
    )


def _image_reference_common_contract(
    entry: ProtocolInventoryComparisonEntry,
) -> bool:
    """Prove the URL-only schema contract; never rewrite runtime messages."""
    if entry.official is None or entry.internal is None:
        return False
    projected = 0

    def url_contract(node: object) -> object:
        nonlocal projected
        if isinstance(node, (list, tuple)):
            return [url_contract(child) for child in node]
        if not isinstance(node, Mapping):
            return node
        result = {key: url_contract(value) for key, value in node.items()}
        properties = result.get("properties")
        tag = properties.get("type") if isinstance(properties, dict) else None
        kind = tag.get("enum") if isinstance(tag, dict) else None
        if kind == ["image"]:
            reference, url = "fileId", "url"
        elif kind == ["input_image"]:
            reference, url = "file_id", "image_url"
        else:
            return result
        branches = result.get("anyOf")
        expected = [
            {"type": "object", "properties": {key: {"type": "string"}},
             "required": [key]}
            for key in (reference, url)
        ]
        if not isinstance(branches, list) or len(branches) != 2 or any(
            branch not in branches for branch in expected
        ):
            return result
        del result["anyOf"]
        properties[url] = {"type": "string"}
        result["required"] = sorted(set(result.get("required", ())) | {url})
        projected += 1
        return result

    common = url_contract(entry.official.schema)
    return projected > 0 and not _protocol_schema_subset_reasons(
        common, entry.internal.schema
    )


def build_method_coverage(
    protocol_comparison: ProtocolInventoryComparison,
    *,
    adapter_rules: tuple[ProtocolAdapterRule, ...] | None = None,
) -> tuple[MethodCoverageRecord, ...]:
    if not isinstance(protocol_comparison, ProtocolInventoryComparison):
        raise ParityValidationError(
            "parity.protocol.coverage_invalid",
            "Method coverage requires a protocol comparison.",
        )
    manifest = tuple(
        protocol_adapter_rule_manifest()
        if adapter_rules is None
        else adapter_rules
    )
    if any(
        not isinstance(rule, ProtocolAdapterRule)
        for rule in manifest
    ):
        raise ParityValidationError(
            "parity.protocol.coverage_invalid",
            "Method coverage adapter manifest is invalid.",
        )
    manifest_by_id = {
        rule.rule_id: rule
        for rule in manifest
    }
    if len(manifest_by_id) != len(manifest):
        raise ParityValidationError(
            "parity.protocol.coverage_invalid",
            "Method coverage adapter manifest has duplicate rules.",
        )

    records: list[MethodCoverageRecord] = []
    for entry in protocol_comparison.entries:
        if (
            entry.compatible
            or entry.official is None
            or entry.internal is None
        ):
            continue
        lookup_key = (
            entry.direction,
            entry.method,
            entry.official.schema_sha256,
            entry.internal.schema_sha256,
            entry.reason_codes,
        )
        exact = _EXACT_METHOD_COVERAGE_BY_KEY.get(lookup_key)
        if exact is None or (
            "image_file_reference" in exact.optional_extension_ids
            and not _image_reference_common_contract(entry)
        ):
            records.append(
                MethodCoverageRecord(
                    direction=entry.direction,
                    method=entry.method,
                    official_schema_sha256=(
                        entry.official.schema_sha256
                    ),
                    internal_schema_sha256=(
                        entry.internal.schema_sha256
                    ),
                    reason_codes=entry.reason_codes,
                    disposition="uncovered",
                )
            )
            continue
        adapter_bindings: list[tuple[str, str]] = []
        adapter_contract_valid = True
        for rule_id in exact.adapter_rule_ids:
            adapter_rule = manifest_by_id.get(rule_id)
            if (
                adapter_rule is None
                or not _thread_resume_rule_matches_coverage_contract(
                    adapter_rule
                )
            ):
                adapter_contract_valid = False
                break
            adapter_bindings.append(
                (adapter_rule.rule_id, adapter_rule.sha256)
            )
        if not adapter_contract_valid:
            records.append(
                MethodCoverageRecord(
                    direction=entry.direction,
                    method=entry.method,
                    official_schema_sha256=(
                        entry.official.schema_sha256
                    ),
                    internal_schema_sha256=(
                        entry.internal.schema_sha256
                    ),
                    reason_codes=entry.reason_codes,
                    disposition="uncovered",
                )
            )
            continue
        records.append(
            MethodCoverageRecord(
                direction=exact.direction,
                method=exact.method,
                official_schema_sha256=(
                    exact.official_schema_sha256
                ),
                internal_schema_sha256=(
                    exact.internal_schema_sha256
                ),
                reason_codes=exact.reason_codes,
                disposition=exact.disposition,
                adapter_rule_digests=tuple(adapter_bindings),
                optional_extension_ids=(
                    exact.optional_extension_ids
                ),
            )
        )
    return tuple(sorted(records, key=lambda record: record.sort_key))


@dataclass(frozen=True)
class InternalFingerprint:
    backend_cli: Path
    cli_version: str
    binary_sha256: str
    active_model: str
    provider_id: str
    wire_api: str
    endpoint_sha256: str
    auth_source_kind: str
    capability_receipt_sha256: str
    source_catalog: Path
    source_catalog_sha256: str
    config_sha256s: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _canonical_path(
            self.backend_cli,
            code="parity.internal.path_invalid",
            field_name="backend_cli",
        )
        _canonical_path(
            self.source_catalog,
            code="parity.internal.path_invalid",
            field_name="source_catalog",
        )
        for field_name, value in (
            ("cli_version", self.cli_version),
            ("active_model", self.active_model),
            ("provider_id", self.provider_id),
            ("wire_api", self.wire_api),
            ("auth_source_kind", self.auth_source_kind),
        ):
            _require_text(
                value,
                code="parity.internal.value_invalid",
                field_name=field_name,
            )
        for field_name, value in (
            ("binary_sha256", self.binary_sha256),
            ("endpoint_sha256", self.endpoint_sha256),
            (
                "capability_receipt_sha256",
                self.capability_receipt_sha256,
            ),
            ("source_catalog_sha256", self.source_catalog_sha256),
        ):
            _require_sha256(
                value,
                code="parity.internal.digest_invalid",
                field_name=field_name,
            )
        normalized_config: list[tuple[str, str]] = []
        seen_names: set[str] = set()
        for name, digest in self.config_sha256s:
            _require_text(
                name,
                code="parity.internal.config_digest_invalid",
                field_name="config digest name",
            )
            _require_sha256(
                digest,
                code="parity.internal.config_digest_invalid",
                field_name=f"config digest {name}",
            )
            if name in seen_names:
                raise ParityValidationError(
                    "parity.internal.config_digest_invalid",
                    "Internal config digest names must be unique.",
                )
            seen_names.add(name)
            normalized_config.append((name, digest))
        object.__setattr__(
            self,
            "config_sha256s",
            tuple(sorted(normalized_config)),
        )

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "active_model": self.active_model,
                "auth_source_kind": self.auth_source_kind,
                "backend_cli": str(self.backend_cli),
                "binary_sha256": self.binary_sha256,
                "capability_receipt_sha256": self.capability_receipt_sha256,
                "cli_version": self.cli_version,
                "config_sha256s": [
                    {"name": name, "sha256": digest}
                    for name, digest in self.config_sha256s
                ],
                "endpoint_sha256": self.endpoint_sha256,
                "provider_id": self.provider_id,
                "source_catalog": str(self.source_catalog),
                "source_catalog_sha256": self.source_catalog_sha256,
                "wire_api": self.wire_api,
            }
        )

    @property
    def fingerprint_sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())

    def matches_fingerprint(self, fingerprint_sha256: str) -> bool:
        return _matches_sha256(self.fingerprint_sha256, fingerprint_sha256)

    def is_stale(self, fingerprint_sha256: str) -> bool:
        return not self.matches_fingerprint(fingerprint_sha256)


@dataclass(frozen=True)
class ParityCandidate:
    official_binding: RuntimeBinding
    internal_binding: RuntimeBinding
    internal_manifest: Mapping[str, object]
    capability_receipt: object
    source_config: object
    adapter_rule_set_sha256: str
    active_runtime_config_path: Path | None = None
    canonical_internal_binding: RuntimeBinding | None = None

    def __post_init__(self) -> None:
        if self.official_binding.profile != "openai-official":
            raise ParityValidationError(
                "parity.candidate.official_binding_invalid",
                "Parity candidate requires an official Runtime Binding.",
            )
        if self.internal_binding.profile != "internal":
            raise ParityValidationError(
                "parity.candidate.internal_binding_invalid",
                "Parity candidate requires an internal Runtime Binding.",
            )
        canonical_internal_binding = (
            self.canonical_internal_binding or self.internal_binding
        )
        if (
            canonical_internal_binding.profile != "internal"
            or canonical_internal_binding.codex_home
            != self.internal_binding.codex_home
            or canonical_internal_binding.desktop_cli
            != self.internal_binding.desktop_cli
            or canonical_internal_binding.requires_proxy
            != self.internal_binding.requires_proxy
        ):
            raise ParityValidationError(
                "parity.candidate.canonical_binding_invalid",
                "Parity candidate canonical Runtime Binding is invalid.",
            )
        object.__setattr__(
            self,
            "canonical_internal_binding",
            canonical_internal_binding,
        )
        if not isinstance(self.internal_manifest, Mapping):
            raise ParityValidationError(
                "parity.candidate.manifest_invalid",
                "Parity candidate internal manifest must be a mapping.",
            )
        object.__setattr__(
            self,
            "internal_manifest",
            _freeze_value(dict(self.internal_manifest)),
        )
        _require_sha256(
            self.adapter_rule_set_sha256,
            code="parity.candidate.adapter_rule_set_digest_invalid",
            field_name="Parity candidate adapter rule-set digest",
        )
        active_runtime_config_path = self.active_runtime_config_path
        if active_runtime_config_path is not None:
            active_runtime_config_path = _canonical_path(
                active_runtime_config_path,
                code="parity.candidate.active_config_invalid",
                field_name="Parity candidate active runtime config",
            )
            expected_active_runtime_config = _canonical_path(
                self.internal_binding.codex_home / "config.toml",
                code="parity.candidate.active_config_invalid",
                field_name="Parity candidate internal runtime config",
            )
            if active_runtime_config_path != expected_active_runtime_config:
                raise ParityValidationError(
                    "parity.candidate.active_config_invalid",
                    "Parity candidate active runtime config does not match the "
                    "internal Runtime Binding.",
                )
            object.__setattr__(
                self,
                "active_runtime_config_path",
                active_runtime_config_path,
            )


@dataclass(frozen=True)
class ParityFinding:
    category: str
    code: str
    severity: str
    message: str
    expected: str = ""
    observed: str = ""

    def __post_init__(self) -> None:
        _require_text(
            self.category,
            code="parity.finding.invalid",
            field_name="finding category",
        )
        if not isinstance(self.code, str) or _CODE_RE.fullmatch(self.code) is None:
            raise ParityValidationError(
                "parity.finding.invalid",
                "Parity finding code must be canonical.",
            )
        if self.severity not in {"error", "warning", "info"}:
            raise ParityValidationError(
                "parity.finding.invalid",
                "Parity finding severity is unsupported.",
            )
        _require_text(
            self.message,
            code="parity.finding.invalid",
            field_name="finding message",
        )

    @property
    def sort_key(self) -> tuple[str, str]:
        return self.category, self.code


@dataclass(frozen=True)
class ParityQueueItem:
    category: str
    identifier: str
    finding_code: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("queue category", self.category),
            ("queue identifier", self.identifier),
        ):
            _require_text(
                value,
                code="parity.queue.invalid",
                field_name=field_name,
            )
        if (
            not isinstance(self.finding_code, str)
            or _CODE_RE.fullmatch(self.finding_code) is None
        ):
            raise ParityValidationError(
                "parity.queue.invalid",
                "Parity queue finding code must be canonical.",
            )

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return self.category, self.identifier, self.finding_code


_ITEM_IDS_RESUME_DEPENDENCY = "thread/resume.params.history"


@dataclass(frozen=True)
class ParityAcceptanceTrace:
    schema_version: int
    trace_id: str
    observed_protocol_methods: tuple[tuple[str, str], ...]
    observed_features: tuple[str, ...]
    item_ids_observed_dependencies: tuple[str, ...]
    observed_protocol_extensions: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not isinstance(self.trace_id, str)
            or _CODE_RE.fullmatch(self.trace_id) is None
        ):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace identity is invalid.",
            )
        try:
            raw_protocol_methods = tuple(
                self.observed_protocol_methods
            )
        except TypeError as exc:
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace protocol methods are invalid.",
            ) from exc
        if (
            any(
                not isinstance(item, tuple)
                or len(item) != 2
                or item[0] not in _PROTOCOL_DIRECTION_INDEX
                or not isinstance(item[1], str)
                or _PROTOCOL_METHOD_RE.fullmatch(item[1]) is None
                for item in raw_protocol_methods
            )
        ):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace protocol methods are invalid.",
            )
        protocol_methods = tuple(
            sorted(
                raw_protocol_methods,
                key=lambda item: _protocol_sort_key(
                    item[0],
                    item[1],
                ),
            )
        )
        if len(protocol_methods) != len(set(protocol_methods)):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace protocol methods are invalid.",
            )
        try:
            raw_observed_features = tuple(self.observed_features)
        except TypeError as exc:
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace features are invalid.",
            ) from exc
        if (
            any(
                not isinstance(name, str)
                or _FEATURE_NAME_RE.fullmatch(name) is None
                for name in raw_observed_features
            )
        ):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace features are invalid.",
            )
        observed_features = tuple(sorted(raw_observed_features))
        if len(observed_features) != len(set(observed_features)):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace features are invalid.",
            )
        try:
            raw_item_dependencies = tuple(
                self.item_ids_observed_dependencies
            )
        except TypeError as exc:
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace item-ID dependencies are invalid.",
            ) from exc
        if (
            any(
                not isinstance(dependency, str)
                or _PROTOCOL_METHOD_RE.fullmatch(dependency) is None
                for dependency in raw_item_dependencies
            )
        ):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace item-ID dependencies are invalid.",
            )
        item_dependencies = tuple(sorted(raw_item_dependencies))
        if len(item_dependencies) != len(set(item_dependencies)):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace item-ID dependencies are invalid.",
            )
        try:
            raw_protocol_extensions = tuple(
                self.observed_protocol_extensions
            )
        except TypeError as exc:
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace protocol extensions are invalid.",
            ) from exc
        if (
            any(
                not isinstance(item, tuple)
                or len(item) != 3
                or item[0] not in _PROTOCOL_DIRECTION_INDEX
                or not isinstance(item[1], str)
                or _PROTOCOL_METHOD_RE.fullmatch(item[1]) is None
                or not isinstance(item[2], str)
                or _EXTENSION_ID_RE.fullmatch(item[2]) is None
                for item in raw_protocol_extensions
            )
        ):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace protocol extensions are invalid.",
            )
        protocol_extensions = tuple(
            sorted(
                raw_protocol_extensions,
                key=lambda item: (
                    _PROTOCOL_DIRECTION_INDEX[item[0]],
                    item[1],
                    item[2],
                ),
            )
        )
        if len(protocol_extensions) != len(set(protocol_extensions)):
            raise ParityValidationError(
                "parity.acceptance_trace.invalid",
                "Parity acceptance trace protocol extensions are invalid.",
            )
        object.__setattr__(
            self,
            "observed_protocol_methods",
            protocol_methods,
        )
        object.__setattr__(
            self,
            "observed_features",
            observed_features,
        )
        object.__setattr__(
            self,
            "item_ids_observed_dependencies",
            item_dependencies,
        )
        object.__setattr__(
            self,
            "observed_protocol_extensions",
            protocol_extensions,
        )

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "item_ids_observed_dependencies": list(
                    self.item_ids_observed_dependencies
                ),
                "observed_features": list(self.observed_features),
                "observed_protocol_extensions": [
                    {
                        "direction": direction,
                        "extension_id": extension_id,
                        "method": method,
                    }
                    for direction, method, extension_id
                    in self.observed_protocol_extensions
                ],
                "observed_protocol_methods": [
                    {
                        "direction": direction,
                        "method": method,
                    }
                    for direction, method in self.observed_protocol_methods
                ],
                "schema_version": self.schema_version,
                "trace_id": self.trace_id,
            }
        )

    @property
    def payload_sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())


_CURRENT_PARITY_ACCEPTANCE_TRACE = ParityAcceptanceTrace(
    schema_version=1,
    trace_id="official-desktop-core-v2",
    observed_protocol_methods=(
        ("client_request", "collaborationMode/list"),
        ("client_request", "initialize"),
        ("client_request", "thread/read"),
        ("client_request", "thread/resume"),
        ("client_request", "thread/start"),
        ("client_request", "turn/start"),
    ),
    observed_features=("multi_agent_v2",),
    item_ids_observed_dependencies=(
        _ITEM_IDS_RESUME_DEPENDENCY,
    ),
    observed_protocol_extensions=(),
)


def current_parity_acceptance_trace() -> ParityAcceptanceTrace:
    return _CURRENT_PARITY_ACCEPTANCE_TRACE


@dataclass(frozen=True)
class MultiAgentV2Evidence:
    overlay_projected: bool
    config_projected: bool
    typed_probe_result: str | None = None
    fingerprints_revalidated: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.overlay_projected) is not bool
            or type(self.config_projected) is not bool
            or type(self.fingerprints_revalidated) is not bool
            or (
                self.typed_probe_result is not None
                and (
                    not isinstance(self.typed_probe_result, str)
                    or not self.typed_probe_result
                    or self.typed_probe_result
                    != self.typed_probe_result.strip()
                )
            )
        ):
            raise ParityValidationError(
                "parity.policy.evidence_invalid",
                "Multi-agent v2 evidence is invalid.",
            )

    @property
    def eligibility_ready(self) -> bool:
        return self.overlay_projected and self.config_projected

    @property
    def final_ready(self) -> bool:
        return (
            self.eligibility_ready
            and self.typed_probe_result == "passed"
            and self.fingerprints_revalidated
        )


@dataclass(frozen=True)
class ParityPolicyEvaluation:
    healthy: bool
    policy_version: ParityPolicyVersion
    findings: tuple[ParityFinding, ...] = ()
    synchronization_queue: tuple[ParityQueueItem, ...] = ()
    evaluation_stage: str = "final"
    method_coverage: tuple[MethodCoverageRecord, ...] = ()
    provisional_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.healthy) is not bool:
            raise ParityValidationError(
                "parity.policy.result_invalid",
                "Parity policy health must be boolean.",
            )
        if not isinstance(self.policy_version, ParityPolicyVersion):
            raise ParityValidationError(
                "parity.policy.result_invalid",
                "Parity policy version is invalid.",
            )
        if self.evaluation_stage not in {"eligibility", "final"}:
            raise ParityValidationError(
                "parity.policy.result_invalid",
                "Parity policy evaluation stage is invalid.",
            )
        findings = tuple(
            sorted(
                self.findings,
                key=lambda finding: (
                    finding.category,
                    finding.code,
                    finding.message,
                    finding.expected,
                    finding.observed,
                ),
            )
        )
        queue = tuple(
            sorted(
                self.synchronization_queue,
                key=lambda item: item.sort_key,
            )
        )
        method_coverage = tuple(
            sorted(
                self.method_coverage,
                key=lambda record: record.sort_key,
            )
        )
        if (
            any(
                not isinstance(record, MethodCoverageRecord)
                for record in method_coverage
            )
            or len(method_coverage)
            != len(
                {
                    (record.direction, record.method)
                    for record in method_coverage
                }
            )
        ):
            raise ParityValidationError(
                "parity.policy.result_invalid",
                "Parity policy method coverage is invalid.",
            )
        if self.healthy and any(
            record.disposition == "uncovered"
            for record in method_coverage
        ):
            raise ParityValidationError(
                "parity.policy.result_invalid",
                "Healthy parity policy cannot contain uncovered methods.",
            )
        provisional = tuple(sorted(set(self.provisional_capabilities)))
        if (
            len(provisional) != len(self.provisional_capabilities)
            or any(
                _FEATURE_NAME_RE.fullmatch(name) is None
                for name in provisional
            )
            or (
                self.evaluation_stage == "final"
                and provisional
            )
        ):
            raise ParityValidationError(
                "parity.policy.result_invalid",
                "Parity policy provisional capabilities are invalid.",
            )
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "synchronization_queue", queue)
        object.__setattr__(
            self,
            "method_coverage",
            method_coverage,
        )
        object.__setattr__(
            self,
            "provisional_capabilities",
            provisional,
        )

    @property
    def probe_eligible(self) -> bool:
        return (
            self.evaluation_stage == "eligibility"
            and not any(
                finding.severity == "error"
                for finding in self.findings
            )
        )

    def canonical_payload(self) -> Mapping[str, object]:
        findings: list[dict[str, str]] = []
        for finding in self.findings:
            _validate_policy_persisted_category(finding.category)
            _validate_policy_persisted_code(finding.code)
            findings.append(
                {
                    "category": finding.category,
                    "code": finding.code,
                    "severity": finding.severity,
                }
            )
        queue: list[dict[str, str]] = []
        for item in self.synchronization_queue:
            _validate_policy_persisted_category(item.category)
            _validate_policy_persisted_code(item.finding_code)
            _validate_policy_queue_identifier(
                category=item.category,
                identifier=item.identifier,
            )
            queue.append(
                {
                    "category": item.category,
                    "finding_code": item.finding_code,
                    "identifier": item.identifier,
                }
            )
        return MappingProxyType(
            {
                "evaluation_stage": self.evaluation_stage,
                "findings": findings,
                "healthy": self.healthy,
                "method_coverage": [
                    dict(record.canonical_payload())
                    for record in self.method_coverage
                ],
                "policy_version": str(self.policy_version),
                "provisional_capabilities": list(
                    self.provisional_capabilities
                ),
                "synchronization_queue": queue,
            }
        )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_payload())

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True)
class ConfigInputs:
    profile_config: Path
    sources: tuple[tuple[Path, str], ...]
    source_states: tuple[tuple[Path, int, int, int], ...] = ()

    @classmethod
    def capture(
        cls,
        *,
        profile_config: Path,
        source_paths: tuple[Path, ...],
    ) -> ConfigInputs:
        snapshots = tuple(
            _regular_file_snapshot(
                path,
                code="parity.config.source_unsafe",
                label="parity config source",
                max_bytes=MAX_PARITY_CONFIG_BYTES,
            )
            for path in source_paths
        )
        return cls(
            profile_config=profile_config,
            sources=tuple(
                (snapshot.path, snapshot.sha256)
                for snapshot in snapshots
            ),
            source_states=tuple(
                (
                    snapshot.path,
                    snapshot.device,
                    snapshot.inode,
                    snapshot.mode,
                )
                for snapshot in snapshots
            ),
        )

    def __post_init__(self) -> None:
        profile_config = _canonical_path(
            self.profile_config,
            code="parity.config.inputs_invalid",
            field_name="internal profile config",
        )
        if (
            profile_config.name != "config.toml"
            or profile_config.parent.name != "internal"
            or profile_config.parent.parent.name != "profiles"
        ):
            raise ParityValidationError(
                "parity.config.inputs_invalid",
                "Parity config inputs require the internal profile config.",
            )
        try:
            entries = tuple(self.sources)
        except TypeError as exc:
            raise ParityValidationError(
                "parity.config.inputs_invalid",
                "Parity config sources must be a path/digest sequence.",
            ) from exc
        normalized: list[tuple[Path, str]] = []
        seen_paths: set[Path] = set()
        for entry in entries:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ParityValidationError(
                    "parity.config.inputs_invalid",
                    "Parity config source entries must contain path and digest.",
                )
            path, digest = entry
            canonical_path = _canonical_path(
                path,
                code="parity.config.inputs_invalid",
                field_name="parity config source",
            )
            _require_sha256(
                digest,
                code="parity.config.inputs_invalid",
                field_name=f"parity config source digest {canonical_path}",
            )
            if canonical_path in seen_paths:
                raise ParityValidationError(
                    "parity.config.inputs_invalid",
                    "Parity config source paths must be unique.",
                )
            seen_paths.add(canonical_path)
            normalized.append((canonical_path, digest))
        if profile_config not in seen_paths:
            raise ParityValidationError(
                "parity.config.inputs_invalid",
                "Internal profile config must be one parity config source.",
            )
        try:
            state_entries = tuple(self.source_states)
        except TypeError as exc:
            raise ParityValidationError(
                "parity.config.inputs_invalid",
                "Parity config source states are invalid.",
            ) from exc
        normalized_states: list[tuple[Path, int, int, int]] = []
        state_paths: set[Path] = set()
        for entry in state_entries:
            if not isinstance(entry, tuple) or len(entry) != 4:
                raise ParityValidationError(
                    "parity.config.inputs_invalid",
                    "Parity config source states are invalid.",
                )
            path, device, inode, mode = entry
            canonical_path = _canonical_path(
                path,
                code="parity.config.inputs_invalid",
                field_name="parity config source state",
            )
            if (
                canonical_path not in seen_paths
                or canonical_path in state_paths
                or type(device) is not int
                or device < 0
                or type(inode) is not int
                or inode < 0
                or type(mode) is not int
                or mode < 0
                or mode > 0o7777
            ):
                raise ParityValidationError(
                    "parity.config.inputs_invalid",
                    "Parity config source states are invalid.",
                )
            state_paths.add(canonical_path)
            normalized_states.append(
                (canonical_path, device, inode, mode)
            )
        if state_paths and state_paths != seen_paths:
            raise ParityValidationError(
                "parity.config.inputs_invalid",
                "Parity config source states are incomplete.",
            )
        object.__setattr__(self, "profile_config", profile_config)
        object.__setattr__(
            self,
            "sources",
            tuple(sorted(normalized, key=lambda entry: str(entry[0]))),
        )
        object.__setattr__(
            self,
            "source_states",
            tuple(
                sorted(normalized_states, key=lambda entry: str(entry[0]))
            ),
        )

    def expected_sha256(self, path: Path) -> str:
        canonical_path = _canonical_path(
            path,
            code="parity.config.inputs_invalid",
            field_name="parity config source lookup",
        )
        for source_path, digest in self.sources:
            if source_path == canonical_path:
                return digest
        raise KeyError(canonical_path)

    def expected_state(self, path: Path) -> tuple[int, int, int]:
        canonical_path = _canonical_path(
            path,
            code="parity.config.inputs_invalid",
            field_name="parity config source state lookup",
        )
        for source_path, device, inode, mode in self.source_states:
            if source_path == canonical_path:
                return device, inode, mode
        raise KeyError(canonical_path)


@dataclass(frozen=True)
class ConfigProjection:
    config_inputs: ConfigInputs
    overlay_path: Path
    payloads: tuple[tuple[Path, bytes], ...]
    healthy: bool
    findings: tuple[ParityFinding, ...] = ()
    changed_paths: tuple[Path, ...] = ()
    max_threads_source: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config_inputs, ConfigInputs):
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Config projection inputs are invalid.",
            )
        overlay_path = _canonical_path(
            self.overlay_path,
            code="parity.config.projection_invalid",
            field_name="managed parity overlay",
        )
        expected_overlay = (
            self.config_inputs.profile_config.parent
            / "parity"
            / "model-catalog.json"
        )
        if overlay_path != expected_overlay:
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Config projection overlay path is not profile-local.",
            )
        if type(self.healthy) is not bool:
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Config projection health must be boolean.",
            )
        try:
            raw_findings = tuple(self.findings)
        except TypeError as exc:
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Config projection findings are invalid.",
            ) from exc
        if any(
            not isinstance(finding, ParityFinding)
            for finding in raw_findings
        ):
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Config projection findings are invalid.",
            )
        findings = tuple(
            sorted(
                raw_findings,
                key=lambda finding: (
                    finding.category,
                    finding.code,
                    finding.message,
                ),
            )
        )
        expected_health = not any(
            finding.severity == "error" for finding in findings
        )
        if self.healthy != expected_health:
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Config projection health disagrees with its findings.",
            )
        source_paths = {path for path, _digest in self.config_inputs.sources}
        normalized_payloads: list[tuple[Path, bytes]] = []
        payload_paths: set[Path] = set()
        for entry in self.payloads:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ParityValidationError(
                    "parity.config.projection_invalid",
                    "Config projection payload entries are invalid.",
                )
            path, payload = entry
            canonical_path = _canonical_path(
                path,
                code="parity.config.projection_invalid",
                field_name="projected config payload path",
            )
            if (
                canonical_path not in source_paths
                or canonical_path in payload_paths
                or not isinstance(payload, bytes)
                or len(payload) > MAX_PARITY_CONFIG_BYTES
            ):
                raise ParityValidationError(
                    "parity.config.projection_invalid",
                    "Config projection payload is invalid.",
                )
            payload_paths.add(canonical_path)
            normalized_payloads.append((canonical_path, payload))
        if self.healthy and payload_paths != source_paths:
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Healthy config projection must cover every source.",
            )
        if not self.healthy and normalized_payloads:
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Unhealthy config projection must not expose payloads.",
            )
        normalized_changed: list[Path] = []
        seen_changed: set[Path] = set()
        for path in self.changed_paths:
            canonical_path = _canonical_path(
                path,
                code="parity.config.projection_invalid",
                field_name="changed config path",
            )
            if (
                canonical_path not in payload_paths
                or canonical_path in seen_changed
            ):
                raise ParityValidationError(
                    "parity.config.projection_invalid",
                    "Config projection changed paths are invalid.",
                )
            seen_changed.add(canonical_path)
            normalized_changed.append(canonical_path)
        max_threads_source = self.max_threads_source
        if max_threads_source is not None:
            max_threads_source = _canonical_path(
                max_threads_source,
                code="parity.config.projection_invalid",
                field_name="max threads source",
            )
            if (
                max_threads_source not in source_paths
                or max_threads_source not in seen_changed
                or not self.healthy
            ):
                raise ParityValidationError(
                    "parity.config.projection_invalid",
                    "Config projection max-threads source is invalid.",
                )
        if not self.healthy and (normalized_changed or max_threads_source):
            raise ParityValidationError(
                "parity.config.projection_invalid",
                "Unhealthy config projection cannot promote changes.",
            )
        object.__setattr__(self, "overlay_path", overlay_path)
        object.__setattr__(self, "findings", findings)
        object.__setattr__(
            self,
            "payloads",
            tuple(
                sorted(normalized_payloads, key=lambda entry: str(entry[0]))
            ),
        )
        object.__setattr__(
            self,
            "changed_paths",
            tuple(sorted(normalized_changed, key=str)),
        )
        object.__setattr__(
            self,
            "max_threads_source",
            max_threads_source,
        )

    def payload_for(self, path: Path) -> bytes:
        canonical_path = _canonical_path(
            path,
            code="parity.config.projection_invalid",
            field_name="projected config payload lookup",
        )
        for payload_path, payload in self.payloads:
            if payload_path == canonical_path:
                return payload
        raise KeyError(canonical_path)


@dataclass(frozen=True)
class _ParityConfigSource:
    path: Path
    payload: bytes
    payload_sha256: str
    identity: tuple[int, int, int, int, int, int]


def _read_parity_config_source(
    *,
    path: Path,
    expected_sha256: str,
    _source_observer: Callable[[str, Path], None] | None,
) -> _ParityConfigSource:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ParityValidationError(
            "parity.config.source_unsafe",
            "Config source cannot be inspected safely.",
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise ParityValidationError(
            "parity.config.source_unsafe",
            "Config source must be a regular non-symlink file.",
        )
    if before.st_size > MAX_PARITY_CONFIG_BYTES:
        raise ParityValidationError(
            "parity.config.source_oversized",
            "Config source exceeds the size limit.",
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ParityValidationError(
            "parity.config.source_unsafe",
            "Config source cannot be opened safely.",
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
            raise ParityValidationError(
                "parity.config.source_stale",
                "Config source identity changed before reading.",
            )
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PARITY_CONFIG_BYTES:
                raise ParityValidationError(
                    "parity.config.source_oversized",
                    "Config source exceeds the size limit.",
                )
            chunks.append(chunk)
            digest.update(chunk)
        if _source_observer is not None:
            _source_observer("after_read", path)
        read_complete = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as exc:
            raise ParityValidationError(
                "parity.config.source_stale",
                "Config source identity changed while reading.",
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
            raise ParityValidationError(
                "parity.config.source_stale",
                "Config source identity changed while reading.",
            )
        observed_sha256 = digest.hexdigest()
        if not hmac.compare_digest(observed_sha256, expected_sha256):
            raise ParityValidationError(
                "parity.config.source_stale",
                "Config source digest no longer matches the candidate.",
            )
        return _ParityConfigSource(
            path=path,
            payload=b"".join(chunks),
            payload_sha256=observed_sha256,
            identity=identity,
        )
    finally:
        os.close(descriptor)


_AGENTS_TABLE_LINE_RE = re.compile(
    r"^[ \t]*\[[ \t]*agents[ \t]*\][ \t]*(?:#.*)?$"
)
_TABLE_LINE_RE = re.compile(r"^[ \t]*\[\[?.*\]?\][ \t]*(?:#.*)?$")
_MAX_THREADS_LINE_RE = re.compile(r"^[ \t]*max_threads[ \t]*=")


def _exact_max_threads_assignment_count(text: str) -> int:
    in_agents_table = False
    count = 0
    for line in text.splitlines():
        if _AGENTS_TABLE_LINE_RE.fullmatch(line):
            in_agents_table = True
            continue
        if _TABLE_LINE_RE.fullmatch(line):
            in_agents_table = False
            continue
        if in_agents_table and _MAX_THREADS_LINE_RE.match(line):
            count += 1
    return count


def _parse_parity_config_source(source: _ParityConfigSource) -> ConfigDocument:
    try:
        text = source.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParityValidationError(
            "parity.config.source_invalid",
            "Config source is not valid UTF-8.",
        ) from exc
    try:
        return ConfigDocument.parse(text, f"parity config source {source.path}")
    except SwitchError as exc:
        code = (
            "parity.config.max_threads_ambiguous"
            if _exact_max_threads_assignment_count(text) > 1
            else "parity.config.source_invalid"
        )
        raise ParityValidationError(
            code,
            "Config source TOML is invalid or ambiguous.",
        ) from exc


def _config_projection_failure(
    *,
    config_inputs: ConfigInputs,
    overlay_path: Path,
    code: str,
    message: str,
) -> ConfigProjection:
    return ConfigProjection(
        config_inputs=config_inputs,
        overlay_path=overlay_path,
        payloads=(),
        healthy=False,
        findings=(
            ParityFinding(
                category="config",
                code=code,
                severity="error",
                message=message,
            ),
        ),
        changed_paths=(),
        max_threads_source=None,
    )


def _project_internal_profile_config(
    document: ConfigDocument,
    *,
    overlay_path: Path,
) -> ConfigDocument:
    overlay = ConfigDocument.parse(
        (
            "model_catalog_json = "
            f"{json.dumps(str(overlay_path), ensure_ascii=True)}\n"
            "\n"
            "[features]\n"
            "multi_agent_v2 = true\n"
        ),
        "parity internal profile projection",
    )
    replaced = document.replace_values_from(overlay)
    return replaced.recover_missing_from(
        overlay,
        protected_paths=frozenset(),
    ).document


def _config_document_contains_path(
    document: ConfigDocument,
    path: tuple[str, ...],
) -> bool:
    if any(assignment.path == path for assignment in document.assignments):
        return True
    current: object = document.data
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return True


def prepare_parity_config_projection(
    *,
    config_inputs: ConfigInputs,
    overlay_path: Path,
    _source_observer: Callable[[str, Path], None] | None = None,
) -> ConfigProjection:
    if not isinstance(config_inputs, ConfigInputs):
        raise ParityValidationError(
            "parity.config.inputs_invalid",
            "Parity config projection requires ConfigInputs.",
        )
    canonical_overlay = _canonical_path(
        overlay_path,
        code="parity.config.inputs_invalid",
        field_name="managed parity overlay",
    )
    expected_overlay = (
        config_inputs.profile_config.parent
        / "parity"
        / "model-catalog.json"
    )
    if canonical_overlay != expected_overlay:
        raise ParityValidationError(
            "parity.config.inputs_invalid",
            "Managed parity overlay path is not profile-local.",
        )
    if _source_observer is not None and not callable(_source_observer):
        raise ParityValidationError(
            "parity.config.inputs_invalid",
            "Config source observer is invalid.",
        )
    sources: dict[Path, _ParityConfigSource] = {}
    documents: dict[Path, ConfigDocument] = {}
    for path, expected_sha256 in config_inputs.sources:
        try:
            source = _read_parity_config_source(
                path=path,
                expected_sha256=expected_sha256,
                _source_observer=_source_observer,
            )
            document = _parse_parity_config_source(source)
        except ParityValidationError as exc:
            return _config_projection_failure(
                config_inputs=config_inputs,
                overlay_path=canonical_overlay,
                code=exc.code,
                message=str(exc),
            )
        sources[path] = source
        documents[path] = document

    target_path = ("agents", "max_threads")
    target_sources = [
        path
        for path, document in documents.items()
        if _config_document_contains_path(document, target_path)
    ]
    if len(target_sources) > 1:
        return _config_projection_failure(
            config_inputs=config_inputs,
            overlay_path=canonical_overlay,
            code="parity.config.max_threads_ambiguous",
            message="agents.max_threads is supplied more than once.",
        )

    max_threads_source: Path | None = None
    if target_sources:
        max_threads_source = target_sources[0]
        try:
            documents[max_threads_source] = documents[
                max_threads_source
            ].remove_exact_scalar_assignment(
                path=target_path,
                table_path=("agents",),
                label=(
                    f"parity config without agents.max_threads: "
                    f"{max_threads_source}"
                ),
            )
        except SwitchError:
            return _config_projection_failure(
                config_inputs=config_inputs,
                overlay_path=canonical_overlay,
                code="parity.config.max_threads_ambiguous",
                message=(
                    "agents.max_threads is not one exact scalar assignment."
                ),
            )

    try:
        documents[config_inputs.profile_config] = (
            _project_internal_profile_config(
                documents[config_inputs.profile_config],
                overlay_path=canonical_overlay,
            )
        )
        payloads = tuple(
            (
                path,
                documents[path].text.encode("utf-8"),
            )
            for path, _digest in config_inputs.sources
        )
        for path, payload in payloads:
            ConfigDocument.parse(
                payload.decode("utf-8"),
                f"projected parity config {path}",
            )
    except (KeyError, UnicodeDecodeError, SwitchError) as exc:
        return _config_projection_failure(
            config_inputs=config_inputs,
            overlay_path=canonical_overlay,
            code="parity.config.source_invalid",
            message="Config projection could not be reparsed.",
        )

    for path, source in sources.items():
        try:
            revalidated = _read_parity_config_source(
                path=path,
                expected_sha256=source.payload_sha256,
                _source_observer=None,
            )
        except ParityValidationError as exc:
            return _config_projection_failure(
                config_inputs=config_inputs,
                overlay_path=canonical_overlay,
                code=exc.code,
                message=str(exc),
            )
        if revalidated.identity != source.identity:
            return _config_projection_failure(
                config_inputs=config_inputs,
                overlay_path=canonical_overlay,
                code="parity.config.source_stale",
                message="Config source identity changed during projection.",
            )

    changed_paths = tuple(
        path
        for path, payload in payloads
        if payload != sources[path].payload
    )
    return ConfigProjection(
        config_inputs=config_inputs,
        overlay_path=canonical_overlay,
        payloads=payloads,
        healthy=True,
        findings=(),
        changed_paths=changed_paths,
        max_threads_source=max_threads_source,
    )


@dataclass(frozen=True)
class ParityProbeInputs:
    backend_cli: Path
    backend_sha256: str
    codex_home: Path
    workspace: Path
    config_path: Path
    config_sha256: str
    overlay_path: Path
    overlay_sha256: str
    capability_receipt_path: Path
    capability_receipt_sha256: str

    def __post_init__(self) -> None:
        code = "parity.probe.inputs_invalid"
        backend_cli = _canonical_path(
            self.backend_cli,
            code=code,
            field_name="probe backend",
        )
        codex_home = _canonical_path(
            self.codex_home,
            code=code,
            field_name="probe CODEX_HOME",
        )
        workspace = _canonical_path(
            self.workspace,
            code=code,
            field_name="probe workspace",
        )
        config_path = _canonical_path(
            self.config_path,
            code=code,
            field_name="probe config",
        )
        overlay_path = _canonical_path(
            self.overlay_path,
            code=code,
            field_name="probe overlay",
        )
        capability_receipt_path = _canonical_path(
            self.capability_receipt_path,
            code=code,
            field_name="probe capability receipt",
        )
        if config_path != codex_home / "config.toml":
            raise ParityValidationError(
                code,
                "Probe config must be the isolated candidate home config.",
            )
        parity_root = codex_home / "parity"
        if (
            overlay_path != parity_root / "model-catalog.json"
            or capability_receipt_path
            != parity_root / "capability-receipt.json"
        ):
            raise ParityValidationError(
                code,
                "Probe evidence paths must be isolated candidate artifacts.",
            )
        for field_name, digest in (
            ("probe backend digest", self.backend_sha256),
            ("probe config digest", self.config_sha256),
            ("probe overlay digest", self.overlay_sha256),
            (
                "probe capability receipt digest",
                self.capability_receipt_sha256,
            ),
        ):
            _require_sha256(
                digest,
                code=code,
                field_name=field_name,
            )
        object.__setattr__(self, "backend_cli", backend_cli)
        object.__setattr__(self, "codex_home", codex_home)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "config_path", config_path)
        object.__setattr__(self, "overlay_path", overlay_path)
        object.__setattr__(
            self,
            "capability_receipt_path",
            capability_receipt_path,
        )


@dataclass(frozen=True)
class ParityProbeRequest:
    name: str
    command: tuple[str, ...]
    codex_home: Path
    stdin_messages: tuple[Mapping[str, object], ...]
    timeout_seconds: float
    max_output_bytes: int

    def __post_init__(self) -> None:
        if self.name not in {"core_protocol", "typed_subagent_v2"}:
            raise ParityValidationError(
                "parity.probe.request_invalid",
                "Parity probe request name is unsupported.",
            )
        command = tuple(self.command)
        if (
            not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            raise ParityValidationError(
                "parity.probe.request_invalid",
                "Parity probe command is invalid.",
            )
        codex_home = _canonical_path(
            self.codex_home,
            code="parity.probe.request_invalid",
            field_name="probe request CODEX_HOME",
        )
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
            or not isinstance(self.max_output_bytes, int)
            or self.max_output_bytes <= 0
            or self.max_output_bytes > MAX_PARITY_PROBE_OUTPUT_BYTES
        ):
            raise ParityValidationError(
                "parity.probe.request_invalid",
                "Parity probe request bounds are invalid.",
            )
        messages: list[Mapping[str, object]] = []
        for message in self.stdin_messages:
            if not isinstance(message, Mapping):
                raise ParityValidationError(
                    "parity.probe.request_invalid",
                    "Parity probe stdin messages are invalid.",
                )
            messages.append(_freeze_value(dict(message)))
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "codex_home", codex_home)
        object.__setattr__(self, "stdin_messages", tuple(messages))


@dataclass(frozen=True)
class ParityProbeCommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    process_group_terminated: bool = False

    def __post_init__(self) -> None:
        if self.returncode is not None and not isinstance(self.returncode, int):
            raise ParityValidationError(
                "parity.probe.result_invalid",
                "Parity probe return code is invalid.",
            )
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ParityValidationError(
                "parity.probe.result_invalid",
                "Parity probe output is invalid.",
            )
        for value in (
            self.timed_out,
            self.stdout_truncated,
            self.stderr_truncated,
            self.process_group_terminated,
        ):
            if type(value) is not bool:
                raise ParityValidationError(
                    "parity.probe.result_invalid",
                    "Parity probe result flags are invalid.",
                )


ParityProbeRunner = Callable[
    [ParityProbeRequest],
    ParityProbeCommandResult,
]


@dataclass(frozen=True)
class ParityProbeResult:
    name: str
    result_code: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if (
            self.name not in {"core_protocol", "typed_subagent_v2"}
            or not isinstance(self.result_code, str)
            or _CODE_RE.fullmatch(self.result_code) is None
        ):
            raise ParityValidationError(
                "parity.probe.result_invalid",
                "Parity probe result identity is invalid.",
            )
        _require_sha256(
            self.evidence_sha256,
            code="parity.probe.result_invalid",
            field_name="probe evidence digest",
        )


@dataclass(frozen=True)
class ParityProbeReport:
    healthy: bool
    results: tuple[ParityProbeResult, ...]
    findings: tuple[ParityFinding, ...] = ()

    def __post_init__(self) -> None:
        if type(self.healthy) is not bool:
            raise ParityValidationError(
                "parity.probe.report_invalid",
                "Parity probe report health is invalid.",
            )
        results = tuple(self.results)
        if (
            any(not isinstance(result, ParityProbeResult) for result in results)
            or len({result.name for result in results}) != len(results)
        ):
            raise ParityValidationError(
                "parity.probe.report_invalid",
                "Parity probe report results are invalid.",
            )
        findings = tuple(
            sorted(
                self.findings,
                key=lambda finding: (
                    finding.category,
                    finding.code,
                    finding.message,
                ),
            )
        )
        if any(
            not isinstance(finding, ParityFinding)
            or finding.category != "probe"
            for finding in findings
        ):
            raise ParityValidationError(
                "parity.probe.report_invalid",
                "Parity probe report findings are invalid.",
            )
        expected_health = bool(results) and not any(
            finding.severity == "error" for finding in findings
        )
        if self.healthy != expected_health:
            raise ParityValidationError(
                "parity.probe.report_invalid",
                "Parity probe report health contradicts its findings.",
            )
        object.__setattr__(self, "results", results)
        object.__setattr__(self, "findings", findings)

    @property
    def receipt_results(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (
                result.name,
                result.result_code,
                result.evidence_sha256,
            )
            for result in self.results
        )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(
            MappingProxyType(
                {
                    "findings": [
                        {
                            "category": finding.category,
                            "code": finding.code,
                            "severity": finding.severity,
                        }
                        for finding in self.findings
                    ],
                    "healthy": self.healthy,
                    "results": [
                        {
                            "evidence_sha256": result.evidence_sha256,
                            "name": result.name,
                            "result_code": result.result_code,
                        }
                        for result in self.results
                    ],
                }
            )
        )


@dataclass(frozen=True)
class _ParityProbeArtifactState:
    name: str
    path: Path
    identity: tuple[int, int, int, int, int, int]
    sha256: str


def _probe_file_state(
    *,
    name: str,
    path: Path,
    expected_sha256: str,
    executable: bool,
) -> _ParityProbeArtifactState:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ParityValidationError(
            "parity.probe.candidate_stale",
            "Parity probe candidate input is unavailable.",
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or (executable and before.st_mode & 0o111 == 0)
    ):
        raise ParityValidationError(
            "parity.probe.candidate_stale",
            "Parity probe candidate input is unsafe.",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ParityValidationError(
            "parity.probe.candidate_stale",
            "Parity probe candidate input cannot be read safely.",
        ) from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ParityValidationError(
                "parity.probe.candidate_stale",
                "Parity probe candidate identity changed.",
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as exc:
        raise ParityValidationError(
            "parity.probe.candidate_stale",
            "Parity probe candidate identity changed.",
        ) from exc
    identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_mode != before.st_mode
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ctime_ns != before.st_ctime_ns
        or not hmac.compare_digest(digest.hexdigest(), expected_sha256)
    ):
        raise ParityValidationError(
            "parity.probe.candidate_stale",
            "Parity probe candidate input changed.",
        )
    return _ParityProbeArtifactState(
        name=name,
        path=path,
        identity=identity,
        sha256=digest.hexdigest(),
    )


def _probe_directory_identity(path: Path) -> tuple[int, int, int]:
    try:
        state = path.lstat()
    except OSError as exc:
        raise ParityValidationError(
            "parity.probe.candidate_stale",
            "Parity probe candidate directory is unavailable.",
        ) from exc
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISDIR(state.st_mode):
        raise ParityValidationError(
            "parity.probe.candidate_stale",
            "Parity probe candidate directory is unsafe.",
        )
    return state.st_dev, state.st_ino, state.st_mode


def _probe_candidate_snapshot(
    inputs: ParityProbeInputs,
) -> tuple[
    tuple[int, int, int],
    tuple[int, int, int],
    tuple[_ParityProbeArtifactState, ...],
]:
    return (
        _probe_directory_identity(inputs.codex_home),
        _probe_directory_identity(inputs.workspace),
        (
            _probe_file_state(
                name="backend",
                path=inputs.backend_cli,
                expected_sha256=inputs.backend_sha256,
                executable=True,
            ),
            _probe_file_state(
                name="config",
                path=inputs.config_path,
                expected_sha256=inputs.config_sha256,
                executable=False,
            ),
            _probe_file_state(
                name="overlay",
                path=inputs.overlay_path,
                expected_sha256=inputs.overlay_sha256,
                executable=False,
            ),
            _probe_file_state(
                name="capability_receipt",
                path=inputs.capability_receipt_path,
                expected_sha256=inputs.capability_receipt_sha256,
                executable=False,
            ),
        ),
    )


def _probe_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_probe_process_group(
    process_group: int,
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _probe_process_group_exists(process_group):
            return True
        time.sleep(0.01)
    return not _probe_process_group_exists(process_group)


def _terminate_parity_probe_process_group(
    process: subprocess.Popen[bytes],
) -> bool:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    if not _wait_for_probe_process_group(
        process.pid,
        timeout_seconds=0.2,
    ):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        _wait_for_probe_process_group(
            process.pid,
            timeout_seconds=0.5,
        )
    if process.poll() is None:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    return not _probe_process_group_exists(process.pid)


def _captured_probe_messages(
    capture: _BoundedCapture,
) -> tuple[Mapping[str, object], ...] | None:
    output, truncated = capture.render()
    if truncated:
        return None
    return _probe_json_lines(output.rpartition("\n")[0])


def _wait_for_probe_message(
    process: subprocess.Popen[bytes],
    capture: _BoundedCapture,
    matches: Callable[[Mapping[str, object]], bool],
    deadline: float,
) -> Mapping[str, object] | None:
    while True:
        messages = _captured_probe_messages(capture)
        if messages is None:
            return None
        for message in messages:
            if matches(message):
                return message
        if process.poll() is not None:
            return None
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, 0)
        time.sleep(0.01)


def _send_probe_message(
    process: subprocess.Popen[bytes],
    capture: _BoundedCapture,
    message: Mapping[str, object],
    deadline: float,
) -> Mapping[str, object] | None:
    assert process.stdin is not None
    descriptor = process.stdin.fileno()
    os.set_blocking(descriptor, False)
    payload = _canonical_json_bytes(message) + b"\n"
    offset = 0
    while offset < len(payload):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, 0)
        try:
            written = os.write(descriptor, payload[offset:offset + 4096])
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if written == 0:
            raise BrokenPipeError("Parity probe input closed")
        offset += written
    if "id" not in message:
        return None
    return _wait_for_probe_message(
        process, capture, lambda reply: reply.get("id") == message["id"], deadline
    )


def _probe_mapping(value: object, *keys: str) -> Mapping[str, object]:
    for key in keys:
        if not isinstance(value, Mapping):
            return {}
        value = value.get(key)
    return value if isinstance(value, Mapping) else {}


def _complete_typed_probe(
    process: subprocess.Popen[bytes],
    capture: _BoundedCapture,
    deadline: float,
) -> None:
    messages = _captured_probe_messages(capture) or ()
    thread_reply = next(
        (m for m in messages if m.get("id") == _PARITY_PROBE_CORE_IDS[2]), {}
    )
    parent = _probe_mapping(thread_reply, "result", "thread").get("id")
    if not isinstance(parent, str) or not parent:
        return
    turn_reply = _send_probe_message(
        process, capture,
        {
            "id": "parity-probe-turn",
            "method": "turn/start",
            "params": {
                "threadId": parent,
                "input": [{"type": "text", "text": _PARITY_PROBE_PROMPT}],
            },
        },
        deadline,
    )
    if turn_reply is None or "error" in turn_reply:
        return
    completed = _wait_for_probe_message(
        process, capture,
        lambda m: (
            m.get("method") == "turn/completed"
            and _probe_mapping(m, "params").get("threadId") == parent
        ),
        deadline,
    )
    if completed is None:
        return
    messages = _captured_probe_messages(capture) or ()
    children = []
    for message in messages:
        params = _probe_mapping(message, "params")
        item = _probe_mapping(params, "item")
        if (
            message.get("method") == "item/completed"
            and params.get("threadId") == parent
            and item.get("type") == "subAgentActivity"
            and item.get("kind") == "started"
        ):
            children.append(item.get("agentThreadId"))
    if len(children) == 1 and isinstance(children[0], str) and children[0]:
        _send_probe_message(
            process, capture,
            {
                "id": "parity-probe-child",
                "method": "thread/read",
                "params": {"threadId": children[0], "includeTurns": False},
            },
            deadline,
        )


def _run_parity_probe_command(
    request: ParityProbeRequest,
) -> ParityProbeCommandResult:
    command_deadline = time.monotonic() + request.timeout_seconds
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(request.codex_home)
    stdin = subprocess.PIPE if request.stdin_messages else subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            request.command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=environment,
            start_new_session=True,
        )
    except OSError:
        return ParityProbeCommandResult(
            returncode=None,
            stdout="",
            stderr="",
        )
    stdout_capture = _BoundedCapture(request.max_output_bytes)
    stderr_capture = _BoundedCapture(request.max_output_bytes)
    threads = (
        threading.Thread(
            target=_read_bounded_stream,
            args=(process.stdout, stdout_capture),
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded_stream,
            args=(process.stderr, stderr_capture),
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()
    timed_out = False
    process_group_terminated = False
    try:
        if request.stdin_messages and process.stdin is not None:
            for message in request.stdin_messages:
                # EOF shuts down current asynchronous app-server dispatch.
                reply = _send_probe_message(process, stdout_capture, message, command_deadline)
                if "id" in message and (reply is None or "error" in reply):
                    break
            else:
                if request.name == "typed_subagent_v2":
                    _complete_typed_probe(process, stdout_capture, command_deadline)
            process.stdin.close()
        process.wait(timeout=max(0.0, command_deadline - time.monotonic()))
    except (BrokenPipeError, OSError):
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        # Reap a peer that closed its input without escaping the same bound.
        try:
            process.wait(timeout=max(0.0, command_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            process_group_terminated = _terminate_parity_probe_process_group(process)
    except subprocess.TimeoutExpired:
        timed_out = True
        process_group_terminated = _terminate_parity_probe_process_group(
            process
        )
    deadline = time.monotonic() + 0.5
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in threads):
        timed_out = True
        process_group_terminated = _terminate_parity_probe_process_group(
            process
        )
        deadline = time.monotonic() + 0.5
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    stdout, stdout_truncated = stdout_capture.render()
    stderr, stderr_truncated = stderr_capture.render()
    return ParityProbeCommandResult(
        returncode=process.poll(),
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        process_group_terminated=process_group_terminated,
    )


_PARITY_PROBE_PARENT_MARKER = "parity-parent-ok"
_PARITY_PROBE_CHILD_MARKER = "parity-subagent-ok"
_PARITY_PROBE_PROMPT = (
    "Use the v2 collaboration tool to spawn exactly one explorer subagent "
    "named parity_probe with fork_turns=none. "
    "Ask the child to return exactly parity-subagent-ok. After the child "
    "completes, return exactly parity-parent-ok."
)
_PARITY_PROBE_CORE_IDS = (
    "parity-probe-initialize",
    "parity-probe-collaboration",
    "parity-probe-thread",
)


def _probe_requests(
    inputs: ParityProbeInputs,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[ParityProbeRequest, ParityProbeRequest]:
    core_messages = (
        {
            "id": _PARITY_PROBE_CORE_IDS[0],
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codex-switch-parity",
                    "version": PARITY_POLICY_VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized"},
        {
            "id": _PARITY_PROBE_CORE_IDS[1],
            "method": "collaborationMode/list",
            "params": {},
        },
        {
            "id": _PARITY_PROBE_CORE_IDS[2],
            "method": "thread/start",
            "params": {"cwd": str(inputs.workspace), "ephemeral": True,
                       "approvalPolicy": "never", "sandbox": "read-only"},
        },
    )
    return (
        ParityProbeRequest(
            name="core_protocol",
            command=(
                str(inputs.backend_cli),
                "app-server",
                "--analytics-default-enabled",
            ),
            codex_home=inputs.codex_home,
            stdin_messages=core_messages,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        ),
        ParityProbeRequest(
            name="typed_subagent_v2",
            command=(
                str(inputs.backend_cli),
                "app-server",
                "-c",
                'agents.explorer.description="Read-only parity probe explorer"',
            ),
            codex_home=inputs.codex_home,
            stdin_messages=core_messages,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        ),
    )


def _sanitize_parity_probe_text(text: str, *, max_bytes: int) -> str:
    sanitized = _PROBE_URL_USERINFO.sub(
        lambda match: f"{match.group('scheme')}[REDACTED]@",
        text,
    )
    sanitized = _PROBE_SENSITIVE_HEADER.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        sanitized,
    )
    sanitized = _PROBE_SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        sanitized,
    )
    sanitized = _PROBE_BEARER_TOKEN.sub("Bearer [REDACTED]", sanitized)
    sanitized = _PROBE_SIGNED_QUERY_VALUE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        sanitized,
    )
    payload = sanitized.encode("utf-8", errors="replace")
    if len(payload) <= max_bytes:
        return sanitized
    return payload[:max_bytes].decode("utf-8", errors="ignore")


def _probe_json_lines(stdout: str) -> tuple[Mapping[str, object], ...] | None:
    messages: list[Mapping[str, object]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(message, dict):
            return None
        if any(
            key in message and not isinstance(message[key], str)
            for key in ("method", "type")
        ) or ("id" in message and type(message["id"]) not in (str, int)):
            return None
        for item in (
            _probe_mapping(message, "item"),
            _probe_mapping(message, "params", "item"),
        ):
            if "type" in item and not isinstance(item["type"], str):
                return None
        messages.append(message)
    return tuple(messages)


def _probe_common_failure_code(
    command_result: ParityProbeCommandResult,
    *,
    max_output_bytes: int,
) -> str | None:
    if command_result.timed_out:
        return (
            "timeout"
            if command_result.process_group_terminated
            else "termination_failed"
        )
    if (
        command_result.stdout_truncated
        or command_result.stderr_truncated
        or len(command_result.stdout.encode("utf-8")) > max_output_bytes
        or len(command_result.stderr.encode("utf-8")) > max_output_bytes
    ):
        return "output_oversized"
    if command_result.returncode != 0:
        return "early_exit"
    return None


def _evaluate_core_probe(
    command_result: ParityProbeCommandResult,
    *,
    max_output_bytes: int,
) -> tuple[str, Mapping[str, object]]:
    common = _probe_common_failure_code(
        command_result,
        max_output_bytes=max_output_bytes,
    )
    if common is not None:
        return common, MappingProxyType({})
    messages = _probe_json_lines(command_result.stdout)
    if messages is None:
        return "malformed_output", MappingProxyType({})
    observed_ids = tuple(
        message.get("id")
        for message in messages
        if message.get("id") in _PARITY_PROBE_CORE_IDS
    )
    if len(observed_ids) < len(_PARITY_PROBE_CORE_IDS):
        return "missing_response", MappingProxyType({})
    if observed_ids != _PARITY_PROBE_CORE_IDS:
        return "protocol_order", MappingProxyType({})
    by_id = {
        message.get("id"): message
        for message in messages
        if message.get("id") in _PARITY_PROBE_CORE_IDS
    }
    if any(
        "result" not in by_id[request_id]
        or "error" in by_id[request_id]
        for request_id in _PARITY_PROBE_CORE_IDS
    ):
        return "response_error", MappingProxyType({})
    return (
        "passed",
        MappingProxyType(
            {
                "response_ids": list(_PARITY_PROBE_CORE_IDS),
            }
        ),
    )


def _evaluate_typed_probe(
    command_result: ParityProbeCommandResult,
    *,
    max_output_bytes: int,
) -> tuple[str, Mapping[str, object]]:
    common = _probe_common_failure_code(
        command_result,
        max_output_bytes=max_output_bytes,
    )
    if common is not None:
        return common, MappingProxyType({})
    messages = _probe_json_lines(command_result.stdout)
    if messages is None:
        return "malformed_output", MappingProxyType({})
    core_code, _semantic = _evaluate_core_probe(
        command_result, max_output_bytes=max_output_bytes
    )
    if core_code != "passed":
        return core_code, MappingProxyType({})
    # exec JSON loses role/source information. Only native app-server evidence
    # bound to this parent/child conversation may establish typed-v2 success.
    if any(
        _probe_mapping(m, "item").get("multi_agent_version") == "v1"
        for m in messages
    ):
        return "v1_fallback", MappingProxyType({})
    if any(
        m.get("type") in {"item.completed", "turn.completed"}
        for m in messages
    ):
        return "typed_subagent_missing", MappingProxyType({})
    replies = {m.get("id"): m for m in messages if "id" in m}
    if any(
        sum(m.get("id") == request_id for m in messages) != 1
        for request_id in ("parity-probe-turn", "parity-probe-child")
    ):
        return "typed_subagent_missing", MappingProxyType({})
    parent = _probe_mapping(
        replies.get(_PARITY_PROBE_CORE_IDS[2]), "result", "thread"
    ).get("id")
    child_record = _probe_mapping(
        replies.get("parity-probe-child"), "result", "thread"
    )
    child = child_record.get("id")
    source = _probe_mapping(child_record, "source", "subAgent", "thread_spawn")
    if (
        not all(isinstance(value, str) and value for value in (parent, child))
        or parent == child
    ):
        return "typed_subagent_missing", MappingProxyType({})
    if source and not source.get("agent_path"):
        return "v1_fallback", MappingProxyType({})
    if (
        child_record.get("parentThreadId") != parent
        or child_record.get("agentRole") != "explorer"
        or source.get("parent_thread_id") != parent
        or source.get("agent_role") != "explorer"
        or type(source.get("depth")) is not int
        or source.get("depth") != 1
        or source.get("agent_path") != "/root/parity_probe"
    ):
        return "typed_subagent_missing", MappingProxyType({})
    spawns: list[int] = []
    child_messages: list[int] = []
    child_turns: list[int] = []
    parent_messages: list[int] = []
    parent_turns: list[int] = []
    parent_turn_id = _probe_mapping(
        replies.get("parity-probe-turn"), "result", "turn"
    ).get("id")
    child_turn_id = next((
        _probe_mapping(m, "params", "turn").get("id") for m in messages
        if m.get("method") == "turn/completed"
        and _probe_mapping(m, "params").get("threadId") == child
    ), None)
    if not all(
        isinstance(value, str) and value
        for value in (parent_turn_id, child_turn_id)
    ):
        return "typed_subagent_missing", MappingProxyType({})
    for index, message in enumerate(messages):
        params = _probe_mapping(message, "params")
        item = _probe_mapping(params, "item")
        thread_id = params.get("threadId")
        if "error" in message or message.get("method") == "error":
            return "response_error", MappingProxyType({})
        if message.get("method") == "item/completed":
            if (
                item.get("type") == "collabAgentToolCall"
                and item.get("tool") == "spawnAgent"
            ):
                return "v1_fallback", MappingProxyType({})
            if (
                item.get("type") in {"subAgentActivity", "agentMessage"}
                and thread_id in (parent, child)
                and params.get("turnId") != (
                    parent_turn_id if thread_id == parent else child_turn_id
                )
            ):
                return "typed_subagent_missing", MappingProxyType({})
            if (
                item.get("type") == "subAgentActivity"
                and item.get("kind") == "started"
            ):
                if (
                    thread_id != parent or item.get("agentThreadId") != child
                    or item.get("agentPath") != "/root/parity_probe"
                ):
                    return "typed_subagent_missing", MappingProxyType({})
                spawns.append(index)
            if item.get("type") == "agentMessage":
                if (
                    thread_id == child
                    and item.get("text") == _PARITY_PROBE_CHILD_MARKER
                ):
                    child_messages.append(index)
                if (
                    thread_id == parent
                    and item.get("text") == _PARITY_PROBE_PARENT_MARKER
                ):
                    parent_messages.append(index)
        if (
            message.get("method") == "turn/completed"
            and thread_id in (parent, child)
        ):
            turn = _probe_mapping(params, "turn")
            if turn.get("status") != "completed" or turn.get("error") is not None:
                return "response_error", MappingProxyType({})
            if thread_id == parent:
                if turn.get("id") != parent_turn_id:
                    return "typed_subagent_missing", MappingProxyType({})
                parent_turns.append(index)
            else:
                child_turns.append(index)
    evidence = (spawns, child_messages, child_turns, parent_messages, parent_turns)
    if any(len(items) != 1 for items in evidence) or not (
        spawns[0] < child_messages[0] < child_turns[0]
        < parent_messages[0] < parent_turns[0]
    ):
        return "typed_subagent_missing", MappingProxyType({})
    return (
        "passed",
        MappingProxyType(
            {
                "agent_role": "explorer",
                "child_marker": True,
                "contract": "v2",
                "parent_marker": True,
                "source": "thread_spawn",
            }
        ),
    )


def _probe_evidence_sha256(
    *,
    name: str,
    result_code: str,
    semantic_evidence: Mapping[str, object],
    stderr: str,
    max_output_bytes: int,
) -> str:
    return _payload_sha256(
        MappingProxyType(
            {
                "diagnostic": _sanitize_parity_probe_text(
                    stderr,
                    max_bytes=max_output_bytes,
                ),
                "name": name,
                "result_code": result_code,
                "semantic": semantic_evidence,
            }
        )
    )


def _probe_finding(name: str, result_code: str) -> ParityFinding:
    return ParityFinding(
        category="probe",
        code=f"parity.probe.{result_code}",
        severity="error",
        message=f"{name} parity probe did not pass.",
    )


def _candidate_stale_report(name: str) -> ParityProbeReport:
    result_code = "candidate_stale"
    result = ParityProbeResult(
        name=name,
        result_code=result_code,
        evidence_sha256=_probe_evidence_sha256(
            name=name,
            result_code=result_code,
            semantic_evidence=MappingProxyType({}),
            stderr="",
            max_output_bytes=1024,
        ),
    )
    return ParityProbeReport(
        healthy=False,
        results=(result,),
        findings=(_probe_finding(name, result_code),),
    )


def run_parity_probes(
    *,
    inputs: ParityProbeInputs,
    runner: ParityProbeRunner | None = None,
    timeout_seconds: float = 30.0,
    max_output_bytes: int = 64 * 1024,
) -> ParityProbeReport:
    if not isinstance(inputs, ParityProbeInputs):
        raise ParityValidationError(
            "parity.probe.inputs_invalid",
            "Parity probe inputs are invalid.",
        )
    if (
        not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
        or max_output_bytes > MAX_PARITY_PROBE_OUTPUT_BYTES
    ):
        raise ParityValidationError(
            "parity.probe.inputs_invalid",
            "Parity probe bounds are invalid.",
        )
    selected_runner = _run_parity_probe_command if runner is None else runner
    if not callable(selected_runner):
        raise ParityValidationError(
            "parity.probe.inputs_invalid",
            "Parity probe runner is invalid.",
        )
    try:
        expected_snapshot = _probe_candidate_snapshot(inputs)
    except ParityValidationError:
        return _candidate_stale_report("core_protocol")
    results: list[ParityProbeResult] = []
    findings: list[ParityFinding] = []
    for request in _probe_requests(
        inputs,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    ):
        try:
            command_result = selected_runner(request)
        except Exception:
            command_result = ParityProbeCommandResult(
                returncode=None,
                stdout="",
                stderr="",
            )
        if not isinstance(command_result, ParityProbeCommandResult):
            command_result = ParityProbeCommandResult(
                returncode=None,
                stdout="",
                stderr="",
            )
        if request.name == "core_protocol":
            result_code, semantic = _evaluate_core_probe(
                command_result,
                max_output_bytes=max_output_bytes,
            )
        else:
            result_code, semantic = _evaluate_typed_probe(
                command_result,
                max_output_bytes=max_output_bytes,
            )
        try:
            observed_snapshot = _probe_candidate_snapshot(inputs)
        except ParityValidationError:
            observed_snapshot = None
        if observed_snapshot != expected_snapshot:
            result_code = "candidate_stale"
            semantic = MappingProxyType({})
        result = ParityProbeResult(
            name=request.name,
            result_code=result_code,
            evidence_sha256=_probe_evidence_sha256(
                name=request.name,
                result_code=result_code,
                semantic_evidence=semantic,
                stderr=command_result.stderr,
                max_output_bytes=max_output_bytes,
            ),
        )
        results.append(result)
        if result_code != "passed":
            findings.append(_probe_finding(request.name, result_code))
            break
    return ParityProbeReport(
        healthy=not findings,
        results=tuple(results),
        findings=tuple(findings),
    )


def _validate_policy_persisted_category(category: str) -> None:
    if category not in {"feature", "model_metadata", "protocol"}:
        raise ParityValidationError(
            "parity.policy.serialization_invalid",
            "Parity policy payload category is unsupported.",
        )


def _validate_policy_persisted_code(code: str) -> None:
    if (
        not isinstance(code, str)
        or len(code) > 128
        or not code.startswith("parity.")
        or _CODE_RE.fullmatch(code) is None
    ):
        raise ParityValidationError(
            "parity.policy.serialization_invalid",
            "Parity policy payload code is unsafe.",
        )


def _validate_policy_queue_identifier(
    *,
    category: str,
    identifier: str,
) -> None:
    if not isinstance(identifier, str) or len(identifier) > 256:
        raise ParityValidationError(
            "parity.policy.serialization_invalid",
            "Parity policy queue identifier is unsafe.",
        )
    if category == "feature":
        valid = _FEATURE_NAME_RE.fullmatch(identifier) is not None
    elif category == "model_metadata":
        valid = identifier in {"multi_agent_version", "tool_mode"}
    else:
        method_identifier, extension_separator, extension_id = (
            identifier.partition("#")
        )
        direction, separator, method = method_identifier.partition(":")
        valid = (
            separator == ":"
            and direction in _PROTOCOL_DIRECTION_INDEX
            and _PROTOCOL_METHOD_RE.fullmatch(method) is not None
            and (
                not extension_separator
                or _EXTENSION_ID_RE.fullmatch(extension_id) is not None
            )
        )
    if not valid:
        raise ParityValidationError(
            "parity.policy.serialization_invalid",
            "Parity policy queue identifier is unsafe.",
        )


_PARITY_RECEIPT_KEYS = frozenset(
    {
        "acceptance_trace",
        "adapter_rule_set_sha256",
        "evaluation_stage",
        "feature_inventory_sha256s",
        "findings",
        "healthy",
        "internal_fingerprint",
        "internal_fingerprint_sha256",
        "method_coverage",
        "official_reference",
        "official_reference_sha256",
        "overlay",
        "policy_version",
        "probe_results",
        "provisional_capabilities",
        "protocol_inventory_sha256s",
        "schema_version",
        "synchronization_queue",
    }
)
_OFFICIAL_REFERENCE_KEYS = frozenset(
    {
        "authority",
        "binary_sha256",
        "bundle_id",
        "bundle_root",
        "bundle_version",
        "bundled_cli",
        "cli_version",
        "feature_inventory_sha256",
        "schema_sha256",
    }
)
_INTERNAL_FINGERPRINT_KEYS = frozenset(
    {
        "active_model",
        "auth_source_kind",
        "backend_cli",
        "binary_sha256",
        "capability_receipt_sha256",
        "cli_version",
        "config_sha256s",
        "endpoint_sha256",
        "provider_id",
        "source_catalog",
        "source_catalog_sha256",
        "wire_api",
    }
)


def _receipt_malformed(message: str) -> None:
    raise ParityValidationError("parity.receipt.malformed", message)


def _receipt_mapping(
    value: object,
    *,
    keys: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _receipt_malformed(f"{label} must be an object.")
    normalized = dict(value)
    if any(not isinstance(key, str) for key in normalized):
        _receipt_malformed(f"{label} keys must be strings.")
    if frozenset(normalized) != keys:
        _receipt_malformed(f"{label} fields are incomplete or unsupported.")
    return normalized


def _receipt_list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        _receipt_malformed(f"{label} must be an array.")
    return value


def _receipt_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _receipt_malformed(f"{label} must be a canonical string.")
    return value


def _receipt_digest(value: object, *, label: str) -> str:
    digest = _receipt_string(value, label=label)
    if _SHA256_RE.fullmatch(digest) is None:
        _receipt_malformed(f"{label} must be a lowercase SHA-256 digest.")
    return digest


def _receipt_json_value(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, (list, tuple)):
        return [_receipt_json_value(child) for child in value]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key in sorted(value):
            if not isinstance(key, str) or not key:
                _receipt_malformed(
                    "Parity receipt JSON object keys must be non-empty strings."
                )
            result[key] = _receipt_json_value(value[key])
        return result
    _receipt_malformed("Parity receipt contains a non-JSON value.")
    raise AssertionError("unreachable")


def _normalize_receipt_digest_pairs(
    values: tuple[tuple[str, str], ...],
    *,
    label: str,
) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        entries = tuple(values)
    except TypeError as exc:
        raise ParityValidationError(
            "parity.receipt.invalid",
            f"{label} must be a pair sequence.",
        ) from exc
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ParityValidationError(
                "parity.receipt.invalid",
                f"{label} entries must contain side and digest.",
            )
        side, digest = entry
        if side not in {"official", "internal"} or side in seen:
            raise ParityValidationError(
                "parity.receipt.invalid",
                f"{label} sides must be unique official/internal entries.",
            )
        _require_sha256(
            digest,
            code="parity.receipt.invalid",
            field_name=f"{label} {side}",
        )
        seen.add(side)
        normalized.append((side, digest))
    if seen != {"official", "internal"}:
        raise ParityValidationError(
            "parity.receipt.invalid",
            f"{label} must contain official and internal digests.",
        )
    return tuple(sorted(normalized))


def _normalize_receipt_overlay_changes(
    changes: tuple[Mapping[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    normalized: list[Mapping[str, object]] = []
    try:
        entries = tuple(changes)
    except TypeError as exc:
        raise ParityValidationError(
            "parity.receipt.invalid",
            "Parity overlay changes must be a sequence.",
        ) from exc
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity overlay change must be an object.",
            )
        raw = dict(entry)
        if set(raw) != {"op", "path", "value"}:
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity overlay change fields are incomplete or unsupported.",
            )
        operation = raw["op"]
        pointer = raw["path"]
        if (
            operation not in {"add", "replace"}
            or not isinstance(pointer, str)
            or not pointer.startswith("/")
            or len(pointer) > 512
            or "\x00" in pointer
        ):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity overlay change operation or path is invalid.",
            )
        normalized.append(
            MappingProxyType(
                {
                    "op": operation,
                    "path": pointer,
                    "value": _freeze_value(
                        _receipt_json_value(raw["value"])
                    ),
                }
            )
        )
    return tuple(
        sorted(
            normalized,
            key=lambda item: _canonical_json_bytes(item),
        )
    )


def _normalize_receipt_probe_results(
    results: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str, str], ...]:
    required_probe_names = {
        "core_protocol",
        "typed_subagent_v2",
    }
    normalized: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    try:
        entries = tuple(results)
    except TypeError as exc:
        raise ParityValidationError(
            "parity.receipt.invalid",
            "Parity probe results must be a sequence.",
        ) from exc
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 3:
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity probe result must contain name, code, and digest.",
            )
        name, result_code, digest = entry
        if (
            not isinstance(name, str)
            or _CODE_RE.fullmatch(name) is None
            or name in seen
            or not isinstance(result_code, str)
            or _CODE_RE.fullmatch(result_code) is None
        ):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity probe name or result code is invalid.",
            )
        _require_sha256(
            digest,
            code="parity.receipt.invalid",
            field_name=f"probe result {name}",
        )
        seen.add(name)
        normalized.append((name, result_code, digest))
    if (
        seen != required_probe_names
        or any(
            result_code != "passed"
            for _name, result_code, _digest in normalized
        )
    ):
        raise ParityValidationError(
            "parity.receipt.invalid",
            "Parity receipt requires both successful bounded probes.",
        )
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class ParityArtifactPaths:
    profile_dir: Path
    directory: Path
    receipt_path: Path
    overlay_path: Path


def resolve_parity_artifact_paths(
    *,
    profile_dir: Path,
) -> ParityArtifactPaths:
    canonical_profile = _canonical_path(
        profile_dir,
        code="parity.receipt.path_invalid",
        field_name="internal profile directory",
    )
    if (
        canonical_profile.name != "internal"
        or canonical_profile.parent.name != "profiles"
    ):
        raise ParityValidationError(
            "parity.receipt.path_invalid",
            "Parity artifacts must belong to the internal profile directory.",
        )
    directory = canonical_profile / "parity"
    return ParityArtifactPaths(
        profile_dir=canonical_profile,
        directory=directory,
        receipt_path=directory / "receipt.json",
        overlay_path=directory / "model-catalog.json",
    )


def _overlay_source_invalid(message: str) -> None:
    raise ParityValidationError("parity.overlay.source_invalid", message)


def _reject_overlay_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate parity overlay key")
        result[key] = value
    return result


def _reject_overlay_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant {value}")


def _validate_active_model_slug(
    active_model_slug: str,
    *,
    code: str = "parity.overlay.expectation_invalid",
) -> None:
    if (
        not isinstance(active_model_slug, str)
        or _MODEL_SLUG_RE.fullmatch(active_model_slug) is None
    ):
        raise ParityValidationError(
            code,
            "Active model slug is invalid.",
        )


def _catalog_models(
    document: object,
    *,
    source: bool,
) -> list[Mapping[str, object]]:
    if not isinstance(document, Mapping):
        code = (
            "parity.overlay.source_invalid"
            if source
            else "parity.overlay.mutation_forbidden"
        )
        raise ParityValidationError(
            code,
            "Model catalog root must be an object.",
        )
    models = document.get("models")
    if not isinstance(models, list):
        code = (
            "parity.overlay.source_invalid"
            if source
            else "parity.overlay.mutation_forbidden"
        )
        raise ParityValidationError(
            code,
            "Model catalog models must be an array.",
        )
    normalized: list[Mapping[str, object]] = []
    for model in models:
        if not isinstance(model, Mapping):
            code = (
                "parity.overlay.source_invalid"
                if source
                else "parity.overlay.mutation_forbidden"
            )
            raise ParityValidationError(
                code,
                "Every model catalog entry must be an object.",
            )
        slug = model.get("slug")
        if (
            not isinstance(slug, str)
            or _MODEL_SLUG_RE.fullmatch(slug) is None
        ):
            code = (
                "parity.overlay.source_invalid"
                if source
                else "parity.overlay.mutation_forbidden"
            )
            raise ParityValidationError(
                code,
                "Every model catalog entry must have a valid slug.",
            )
        normalized.append(model)
    return normalized


def _active_model_index(
    document: object,
    *,
    active_model_slug: str,
    source: bool,
) -> int:
    models = _catalog_models(document, source=source)
    matches = [
        index
        for index, model in enumerate(models)
        if model.get("slug") == active_model_slug
    ]
    if not matches:
        code = (
            "parity.overlay.active_model_missing"
            if source
            else "parity.overlay.mutation_forbidden"
        )
        raise ParityValidationError(
            code,
            "Active model is missing from the model catalog.",
        )
    if len(matches) != 1:
        code = (
            "parity.overlay.active_model_ambiguous"
            if source
            else "parity.overlay.mutation_forbidden"
        )
        raise ParityValidationError(
            code,
            "Active model is ambiguous in the model catalog.",
        )
    index = matches[0]
    if source:
        active = models[index]
        if (
            "multi_agent_version" in active
            and active["multi_agent_version"] != "v2"
        ):
            _overlay_source_invalid(
                "Active model multi_agent_version must be absent or v2."
            )
    return index


def _json_pointer_segment(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _json_pointer_path(parent: str, segment: object) -> str:
    return f"{parent}/{_json_pointer_segment(segment)}"


def _json_changes(
    source: object,
    overlay: object,
    *,
    path: str = "",
) -> list[Mapping[str, object]]:
    if isinstance(source, Mapping) and isinstance(overlay, Mapping):
        changes: list[Mapping[str, object]] = []
        source_keys = set(source)
        overlay_keys = set(overlay)
        for key in sorted(source_keys | overlay_keys):
            pointer = _json_pointer_path(path, key)
            if key not in source:
                changes.append(
                    {
                        "op": "add",
                        "path": pointer,
                        "value": copy.deepcopy(overlay[key]),
                    }
                )
            elif key not in overlay:
                changes.append(
                    {
                        "op": "remove",
                        "path": pointer,
                    }
                )
            else:
                changes.extend(
                    _json_changes(
                        source[key],
                        overlay[key],
                        path=pointer,
                    )
                )
        return changes
    if isinstance(source, list) and isinstance(overlay, list):
        if len(source) != len(overlay):
            return [
                {
                    "op": "replace",
                    "path": path or "/",
                    "value": copy.deepcopy(overlay),
                }
            ]
        changes = []
        for index, (source_item, overlay_item) in enumerate(
            zip(source, overlay)
        ):
            changes.extend(
                _json_changes(
                    source_item,
                    overlay_item,
                    path=_json_pointer_path(path, index),
                )
            )
        return changes
    if type(source) is not type(overlay) or source != overlay:
        return [
            {
                "op": "replace",
                "path": path or "/",
                "value": copy.deepcopy(overlay),
            }
        ]
    return []


def validate_parity_overlay(
    *,
    source_document: object,
    overlay_document: object,
    active_model_slug: str,
) -> tuple[Mapping[str, object], ...]:
    _validate_active_model_slug(active_model_slug)
    active_index = _active_model_index(
        source_document,
        active_model_slug=active_model_slug,
        source=True,
    )
    _active_model_index(
        overlay_document,
        active_model_slug=active_model_slug,
        source=False,
    )
    source_models = _catalog_models(source_document, source=True)
    source_has_version = (
        "multi_agent_version" in source_models[active_index]
    )
    changes = _json_changes(source_document, overlay_document)
    expected_changes: list[Mapping[str, object]]
    if not source_has_version:
        expected_changes = [
            {
                "op": "add",
                "path": (
                    f"/models/{active_index}/multi_agent_version"
                ),
                "value": "v2",
            }
        ]
    else:
        expected_changes = []
    if changes != expected_changes:
        raise ParityValidationError(
            "parity.overlay.mutation_forbidden",
            "Managed model overlay contains a non-approved mutation.",
        )
    return tuple(
        MappingProxyType(
            {
                "op": change["op"],
                "path": change["path"],
                "value": _freeze_value(change["value"]),
            }
        )
        for change in changes
    )


@dataclass(frozen=True)
class _ParityCatalogSource:
    path: Path
    payload: bytes
    payload_sha256: str
    mode: int
    device: int
    inode: int


def _read_parity_catalog_source(
    *,
    source_catalog: Path,
    expected_source_sha256: str,
    _source_observer: Callable[[str, Path], None] | None,
) -> _ParityCatalogSource:
    canonical_path = _canonical_path(
        source_catalog,
        code="parity.overlay.source_invalid",
        field_name="source model catalog",
    )
    _require_sha256(
        expected_source_sha256,
        code="parity.overlay.expectation_invalid",
        field_name="expected source model catalog digest",
    )
    if _source_observer is not None and not callable(_source_observer):
        raise ParityValidationError(
            "parity.overlay.expectation_invalid",
            "Source catalog observer is invalid.",
        )
    try:
        before = canonical_path.lstat()
    except OSError as exc:
        raise ParityValidationError(
            "parity.overlay.source_unsafe",
            "Source model catalog cannot be inspected safely.",
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise ParityValidationError(
            "parity.overlay.source_unsafe",
            "Source model catalog must be a regular non-symlink file.",
        )
    if before.st_size > MAX_PARITY_CATALOG_BYTES:
        raise ParityValidationError(
            "parity.overlay.source_oversized",
            "Source model catalog exceeds the size limit.",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(canonical_path, flags)
    except OSError as exc:
        raise ParityValidationError(
            "parity.overlay.source_unsafe",
            "Source model catalog cannot be opened safely.",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or opened.st_ctime_ns != before.st_ctime_ns
        ):
            raise ParityValidationError(
                "parity.overlay.source_stale",
                "Source model catalog identity changed before reading.",
            )
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PARITY_CATALOG_BYTES:
                raise ParityValidationError(
                    "parity.overlay.source_oversized",
                    "Source model catalog exceeds the size limit.",
                )
            chunks.append(chunk)
            digest.update(chunk)
        if _source_observer is not None:
            _source_observer("after_read", canonical_path)
        read_complete = os.fstat(descriptor)
        try:
            after = canonical_path.lstat()
        except OSError as exc:
            raise ParityValidationError(
                "parity.overlay.source_stale",
                "Source model catalog identity changed while reading.",
            ) from exc
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mode,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
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
            raise ParityValidationError(
                "parity.overlay.source_stale",
                "Source model catalog identity changed while reading.",
            )
        observed_digest = digest.hexdigest()
        if not hmac.compare_digest(
            observed_digest,
            expected_source_sha256,
        ):
            raise ParityValidationError(
                "parity.overlay.source_stale",
                "Source model catalog digest no longer matches the candidate.",
            )
        return _ParityCatalogSource(
            path=canonical_path,
            payload=b"".join(chunks),
            payload_sha256=observed_digest,
            mode=stat.S_IMODE(opened.st_mode),
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    finally:
        os.close(descriptor)


def _parse_parity_catalog_source(payload: bytes) -> Mapping[str, object]:
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_overlay_duplicate_keys,
            parse_constant=_reject_overlay_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ParityValidationError(
            "parity.overlay.source_invalid",
            "Source model catalog JSON is malformed.",
        ) from exc
    _catalog_models(document, source=True)
    if not isinstance(document, Mapping):
        raise AssertionError("catalog validation must return an object")
    return document


@dataclass(frozen=True)
class ParityOverlayArtifact:
    source_catalog: Path
    source_catalog_sha256: str
    source_mode: int
    source_device: int
    source_inode: int
    active_model_slug: str
    active_model_index: int
    overlay_payload: bytes
    overlay_sha256: str
    changes: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        canonical_source = _canonical_path(
            self.source_catalog,
            code="parity.overlay.artifact_invalid",
            field_name="overlay source model catalog",
        )
        _require_sha256(
            self.source_catalog_sha256,
            code="parity.overlay.artifact_invalid",
            field_name="overlay source model catalog digest",
        )
        if (
            type(self.source_mode) is not int
            or self.source_mode < 0
            or self.source_mode > 0o7777
        ):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay source mode is invalid.",
            )
        if (
            type(self.source_device) is not int
            or self.source_device < 0
            or type(self.source_inode) is not int
            or self.source_inode < 0
        ):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay source identity is invalid.",
            )
        _validate_active_model_slug(
            self.active_model_slug,
            code="parity.overlay.artifact_invalid",
        )
        if (
            type(self.active_model_index) is not int
            or self.active_model_index < 0
        ):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay active model index is invalid.",
            )
        if not isinstance(self.overlay_payload, bytes):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay payload must be bytes.",
            )
        if len(self.overlay_payload) > MAX_PARITY_CATALOG_BYTES:
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay payload exceeds the size limit.",
            )
        _require_sha256(
            self.overlay_sha256,
            code="parity.overlay.artifact_invalid",
            field_name="overlay payload digest",
        )
        if not hmac.compare_digest(
            self.overlay_sha256,
            hashlib.sha256(self.overlay_payload).hexdigest(),
        ):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay payload digest does not match its bytes.",
            )
        try:
            overlay_document = json.loads(
                self.overlay_payload.decode("utf-8"),
                object_pairs_hook=_reject_overlay_duplicate_keys,
                parse_constant=_reject_overlay_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay payload JSON is malformed.",
            ) from exc
        if (
            not isinstance(overlay_document, Mapping)
            or _canonical_json_bytes(overlay_document)
            != self.overlay_payload
        ):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay payload is not canonical JSON.",
            )
        try:
            overlay_models = _catalog_models(
                overlay_document,
                source=False,
            )
            observed_index = _active_model_index(
                overlay_document,
                active_model_slug=self.active_model_slug,
                source=False,
            )
        except ParityValidationError as exc:
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay payload model identity is invalid.",
            ) from exc
        if (
            observed_index != self.active_model_index
            or overlay_models[observed_index].get(
                "multi_agent_version"
            )
            != "v2"
        ):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay payload does not bind the active model v2 metadata.",
            )
        try:
            normalized_changes = _normalize_receipt_overlay_changes(
                self.changes
            )
        except ParityValidationError as exc:
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay change evidence is invalid.",
            ) from exc
        allowed_add = (
            MappingProxyType(
                {
                    "op": "add",
                    "path": (
                        f"/models/{self.active_model_index}"
                        "/multi_agent_version"
                    ),
                    "value": "v2",
                }
            ),
        )
        if normalized_changes not in ((), allowed_add):
            raise ParityValidationError(
                "parity.overlay.artifact_invalid",
                "Overlay change evidence contains a non-approved mutation.",
            )
        object.__setattr__(self, "source_catalog", canonical_source)
        object.__setattr__(self, "changes", normalized_changes)


def prepare_parity_overlay(
    *,
    source_catalog: Path,
    expected_source_sha256: str,
    active_model_slug: str,
    _source_observer: Callable[[str, Path], None] | None = None,
) -> ParityOverlayArtifact:
    _validate_active_model_slug(active_model_slug)
    source = _read_parity_catalog_source(
        source_catalog=source_catalog,
        expected_source_sha256=expected_source_sha256,
        _source_observer=_source_observer,
    )
    source_document = _parse_parity_catalog_source(source.payload)
    active_index = _active_model_index(
        source_document,
        active_model_slug=active_model_slug,
        source=True,
    )
    overlay_document = copy.deepcopy(source_document)
    overlay_models = _catalog_models(overlay_document, source=False)
    if "multi_agent_version" not in overlay_models[active_index]:
        overlay_models[active_index]["multi_agent_version"] = "v2"
    changes = validate_parity_overlay(
        source_document=source_document,
        overlay_document=overlay_document,
        active_model_slug=active_model_slug,
    )
    overlay_payload = _canonical_json_bytes(overlay_document)
    if len(overlay_payload) > MAX_PARITY_CATALOG_BYTES:
        raise ParityValidationError(
            "parity.overlay.source_oversized",
            "Managed model overlay exceeds the size limit.",
        )
    return ParityOverlayArtifact(
        source_catalog=source.path,
        source_catalog_sha256=source.payload_sha256,
        source_mode=source.mode,
        source_device=source.device,
        source_inode=source.inode,
        active_model_slug=active_model_slug,
        active_model_index=active_index,
        overlay_payload=overlay_payload,
        overlay_sha256=hashlib.sha256(overlay_payload).hexdigest(),
        changes=changes,
    )


@dataclass(frozen=True)
class ParityReceipt:
    schema_version: int
    official_reference: OfficialReference
    internal_fingerprint: InternalFingerprint
    feature_inventory_sha256s: tuple[tuple[str, str], ...]
    protocol_inventory_sha256s: tuple[tuple[str, str], ...]
    adapter_rule_set_sha256: str
    acceptance_trace: ParityAcceptanceTrace
    overlay_path: Path
    overlay_sha256: str
    overlay_changes: tuple[Mapping[str, object], ...]
    probe_results: tuple[tuple[str, str, str], ...]
    policy_evaluation: ParityPolicyEvaluation

    def __post_init__(self) -> None:
        if self.schema_version != PARITY_RECEIPT_SCHEMA_VERSION:
            raise ParityValidationError(
                "parity.receipt.schema_unsupported",
                "Parity receipt schema version is unsupported.",
            )
        if not isinstance(self.official_reference, OfficialReference):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity receipt official reference is invalid.",
            )
        if not isinstance(self.internal_fingerprint, InternalFingerprint):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity receipt internal fingerprint is invalid.",
            )
        if not isinstance(self.policy_evaluation, ParityPolicyEvaluation):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity receipt policy evaluation is invalid.",
            )
        if (
            not isinstance(
                self.acceptance_trace,
                ParityAcceptanceTrace,
            )
            or self.acceptance_trace
            != current_parity_acceptance_trace()
        ):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity receipt acceptance trace is stale or invalid.",
            )
        if str(self.policy_evaluation.policy_version) != PARITY_POLICY_VERSION:
            raise ParityValidationError(
                "parity.receipt.policy_unsupported",
                "Parity receipt policy version is unsupported.",
            )
        if (
            self.policy_evaluation.evaluation_stage != "final"
            or self.policy_evaluation.provisional_capabilities
        ):
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity receipt requires one final non-provisional policy.",
            )
        expected_health = not any(
            finding.severity == "error"
            for finding in self.policy_evaluation.findings
        )
        if self.policy_evaluation.healthy != expected_health:
            raise ParityValidationError(
                "parity.receipt.invalid",
                "Parity receipt health contradicts its findings.",
            )
        object.__setattr__(
            self,
            "feature_inventory_sha256s",
            _normalize_receipt_digest_pairs(
                self.feature_inventory_sha256s,
                label="feature inventory digests",
            ),
        )
        object.__setattr__(
            self,
            "protocol_inventory_sha256s",
            _normalize_receipt_digest_pairs(
                self.protocol_inventory_sha256s,
                label="protocol inventory digests",
            ),
        )
        _require_sha256(
            self.adapter_rule_set_sha256,
            code="parity.receipt.invalid",
            field_name="adapter rule-set digest",
        )
        canonical_overlay = _canonical_path(
            self.overlay_path,
            code="parity.receipt.path_invalid",
            field_name="managed model overlay",
        )
        expected_overlay = resolve_parity_artifact_paths(
            profile_dir=canonical_overlay.parent.parent
        ).overlay_path
        if canonical_overlay != expected_overlay:
            raise ParityValidationError(
                "parity.receipt.path_invalid",
                "Parity receipt overlay path is not profile-local.",
            )
        _require_sha256(
            self.overlay_sha256,
            code="parity.receipt.invalid",
            field_name="managed model overlay digest",
        )
        object.__setattr__(self, "overlay_path", canonical_overlay)
        object.__setattr__(
            self,
            "overlay_changes",
            _normalize_receipt_overlay_changes(self.overlay_changes),
        )
        object.__setattr__(
            self,
            "probe_results",
            _normalize_receipt_probe_results(self.probe_results),
        )

    @property
    def healthy(self) -> bool:
        return self.policy_evaluation.healthy

    @property
    def policy_version(self) -> ParityPolicyVersion:
        return self.policy_evaluation.policy_version

    @property
    def findings(self) -> tuple[ParityFinding, ...]:
        return self.policy_evaluation.findings

    @property
    def synchronization_queue(self) -> tuple[ParityQueueItem, ...]:
        return self.policy_evaluation.synchronization_queue

    @property
    def method_coverage(self) -> tuple[MethodCoverageRecord, ...]:
        return self.policy_evaluation.method_coverage

    def canonical_payload(self) -> Mapping[str, object]:
        policy = self.policy_evaluation.canonical_payload()
        return MappingProxyType(
            {
                "acceptance_trace": dict(
                    self.acceptance_trace.canonical_payload()
                ),
                "adapter_rule_set_sha256": self.adapter_rule_set_sha256,
                "evaluation_stage": policy["evaluation_stage"],
                "feature_inventory_sha256s": [
                    {"sha256": digest, "side": side}
                    for side, digest in self.feature_inventory_sha256s
                ],
                "findings": policy["findings"],
                "healthy": self.healthy,
                "internal_fingerprint": dict(
                    self.internal_fingerprint.canonical_payload()
                ),
                "internal_fingerprint_sha256": (
                    self.internal_fingerprint.fingerprint_sha256
                ),
                "method_coverage": policy["method_coverage"],
                "official_reference": dict(
                    self.official_reference.canonical_payload()
                ),
                "official_reference_sha256": (
                    self.official_reference.fingerprint_sha256
                ),
                "overlay": {
                    "changes": [
                        _plain_value(change)
                        for change in self.overlay_changes
                    ],
                    "path": str(self.overlay_path),
                    "sha256": self.overlay_sha256,
                },
                "policy_version": str(self.policy_version),
                "probe_results": [
                    {
                        "evidence_sha256": digest,
                        "name": name,
                        "result_code": result_code,
                    }
                    for name, result_code, digest in self.probe_results
                ],
                "provisional_capabilities": policy[
                    "provisional_capabilities"
                ],
                "protocol_inventory_sha256s": [
                    {"sha256": digest, "side": side}
                    for side, digest in self.protocol_inventory_sha256s
                ],
                "schema_version": self.schema_version,
                "synchronization_queue": policy[
                    "synchronization_queue"
                ],
            }
        )

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.canonical_payload())

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ParityReceipt:
        try:
            return _parse_parity_receipt(payload)
        except ParityValidationError as exc:
            if exc.code in {
                "parity.receipt.malformed",
                "parity.receipt.policy_unsupported",
                "parity.receipt.schema_unsupported",
            }:
                raise
            raise ParityValidationError(
                "parity.receipt.malformed",
                "Parity receipt payload is invalid.",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ParityValidationError(
                "parity.receipt.malformed",
                "Parity receipt payload is invalid.",
            ) from exc


def _parse_receipt_reference(value: object) -> OfficialReference:
    raw = _receipt_mapping(
        value,
        keys=_OFFICIAL_REFERENCE_KEYS,
        label="official reference",
    )
    return OfficialReference(
        authority=_receipt_string(
            raw["authority"],
            label="official reference authority",
        ),
        bundle_root=Path(
            _receipt_string(
                raw["bundle_root"],
                label="official bundle root",
            )
        ),
        bundle_id=_receipt_string(
            raw["bundle_id"],
            label="official bundle id",
        ),
        bundle_version=_receipt_string(
            raw["bundle_version"],
            label="official bundle version",
        ),
        bundled_cli=Path(
            _receipt_string(
                raw["bundled_cli"],
                label="official bundled CLI",
            )
        ),
        cli_version=_receipt_string(
            raw["cli_version"],
            label="official CLI version",
        ),
        binary_sha256=_receipt_digest(
            raw["binary_sha256"],
            label="official binary digest",
        ),
        schema_sha256=_receipt_digest(
            raw["schema_sha256"],
            label="official schema digest",
        ),
        feature_inventory_sha256=_receipt_digest(
            raw["feature_inventory_sha256"],
            label="official feature inventory digest",
        ),
    )


def _parse_receipt_internal_fingerprint(
    value: object,
) -> InternalFingerprint:
    raw = _receipt_mapping(
        value,
        keys=_INTERNAL_FINGERPRINT_KEYS,
        label="internal fingerprint",
    )
    config_entries = _receipt_list(
        raw["config_sha256s"],
        label="internal config digests",
    )
    config_sha256s: list[tuple[str, str]] = []
    for entry in config_entries:
        item = _receipt_mapping(
            entry,
            keys=frozenset({"name", "sha256"}),
            label="internal config digest",
        )
        config_sha256s.append(
            (
                _receipt_string(
                    item["name"],
                    label="internal config digest name",
                ),
                _receipt_digest(
                    item["sha256"],
                    label="internal config digest",
                ),
            )
        )
    return InternalFingerprint(
        backend_cli=Path(
            _receipt_string(
                raw["backend_cli"],
                label="internal backend CLI",
            )
        ),
        cli_version=_receipt_string(
            raw["cli_version"],
            label="internal CLI version",
        ),
        binary_sha256=_receipt_digest(
            raw["binary_sha256"],
            label="internal binary digest",
        ),
        active_model=_receipt_string(
            raw["active_model"],
            label="internal active model",
        ),
        provider_id=_receipt_string(
            raw["provider_id"],
            label="internal provider id",
        ),
        wire_api=_receipt_string(
            raw["wire_api"],
            label="internal wire API",
        ),
        endpoint_sha256=_receipt_digest(
            raw["endpoint_sha256"],
            label="internal endpoint digest",
        ),
        auth_source_kind=_receipt_string(
            raw["auth_source_kind"],
            label="internal auth source kind",
        ),
        capability_receipt_sha256=_receipt_digest(
            raw["capability_receipt_sha256"],
            label="capability receipt digest",
        ),
        source_catalog=Path(
            _receipt_string(
                raw["source_catalog"],
                label="source catalog path",
            )
        ),
        source_catalog_sha256=_receipt_digest(
            raw["source_catalog_sha256"],
            label="source catalog digest",
        ),
        config_sha256s=tuple(config_sha256s),
    )


def _parse_receipt_digest_pairs(
    value: object,
    *,
    label: str,
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for entry in _receipt_list(value, label=label):
        item = _receipt_mapping(
            entry,
            keys=frozenset({"side", "sha256"}),
            label=label,
        )
        result.append(
            (
                _receipt_string(item["side"], label=f"{label} side"),
                _receipt_digest(
                    item["sha256"],
                    label=f"{label} digest",
                ),
            )
        )
    return tuple(result)


def _parse_receipt_method_coverage(
    value: object,
) -> tuple[MethodCoverageRecord, ...]:
    records: list[MethodCoverageRecord] = []
    for entry in _receipt_list(
        value,
        label="parity method coverage",
    ):
        item = _receipt_mapping(
            entry,
            keys=frozenset(
                {
                    "adapter_rule_digests",
                    "direction",
                    "disposition",
                    "internal_schema_sha256",
                    "method",
                    "official_schema_sha256",
                    "optional_extension_ids",
                    "reason_codes",
                }
            ),
            label="parity method coverage record",
        )
        adapter_bindings: list[tuple[str, str]] = []
        for binding in _receipt_list(
            item["adapter_rule_digests"],
            label="parity adapter rule bindings",
        ):
            raw_binding = _receipt_mapping(
                binding,
                keys=frozenset({"rule_id", "sha256"}),
                label="parity adapter rule binding",
            )
            adapter_bindings.append(
                (
                    _receipt_string(
                        raw_binding["rule_id"],
                        label="adapter rule id",
                    ),
                    _receipt_digest(
                        raw_binding["sha256"],
                        label="adapter rule digest",
                    ),
                )
            )
        reason_codes = tuple(
            _receipt_string(
                reason,
                label="method coverage reason",
            )
            for reason in _receipt_list(
                item["reason_codes"],
                label="method coverage reasons",
            )
        )
        extension_ids = tuple(
            _receipt_string(
                extension,
                label="method coverage extension",
            )
            for extension in _receipt_list(
                item["optional_extension_ids"],
                label="method coverage extensions",
            )
        )
        records.append(
            MethodCoverageRecord(
                direction=_receipt_string(
                    item["direction"],
                    label="method coverage direction",
                ),
                method=_receipt_string(
                    item["method"],
                    label="method coverage method",
                ),
                official_schema_sha256=_receipt_digest(
                    item["official_schema_sha256"],
                    label="official method schema digest",
                ),
                internal_schema_sha256=_receipt_digest(
                    item["internal_schema_sha256"],
                    label="internal method schema digest",
                ),
                reason_codes=reason_codes,
                disposition=_receipt_string(
                    item["disposition"],
                    label="method coverage disposition",
                ),
                adapter_rule_digests=tuple(adapter_bindings),
                optional_extension_ids=extension_ids,
            )
        )
    return tuple(records)


def _parse_receipt_acceptance_trace(
    value: object,
) -> ParityAcceptanceTrace:
    raw = _receipt_mapping(
        value,
        keys=frozenset(
            {
                "item_ids_observed_dependencies",
                "observed_features",
                "observed_protocol_extensions",
                "observed_protocol_methods",
                "schema_version",
                "trace_id",
            }
        ),
        label="parity acceptance trace",
    )
    schema_version = raw["schema_version"]
    if type(schema_version) is not int:
        _receipt_malformed(
            "Parity acceptance trace schema version must be an integer."
        )
    protocol_methods: list[tuple[str, str]] = []
    for entry in _receipt_list(
        raw["observed_protocol_methods"],
        label="observed protocol methods",
    ):
        item = _receipt_mapping(
            entry,
            keys=frozenset({"direction", "method"}),
            label="observed protocol method",
        )
        protocol_methods.append(
            (
                _receipt_string(
                    item["direction"],
                    label="observed protocol direction",
                ),
                _receipt_string(
                    item["method"],
                    label="observed protocol method",
                ),
            )
        )
    protocol_extensions: list[tuple[str, str, str]] = []
    for entry in _receipt_list(
        raw["observed_protocol_extensions"],
        label="observed protocol extensions",
    ):
        item = _receipt_mapping(
            entry,
            keys=frozenset(
                {"direction", "extension_id", "method"}
            ),
            label="observed protocol extension",
        )
        protocol_extensions.append(
            (
                _receipt_string(
                    item["direction"],
                    label="observed extension direction",
                ),
                _receipt_string(
                    item["method"],
                    label="observed extension method",
                ),
                _receipt_string(
                    item["extension_id"],
                    label="observed extension identifier",
                ),
            )
        )
    return ParityAcceptanceTrace(
        schema_version=schema_version,
        trace_id=_receipt_string(
            raw["trace_id"],
            label="acceptance trace id",
        ),
        observed_protocol_methods=tuple(protocol_methods),
        observed_features=tuple(
            _receipt_string(
                feature,
                label="observed feature",
            )
            for feature in _receipt_list(
                raw["observed_features"],
                label="observed features",
            )
        ),
        item_ids_observed_dependencies=tuple(
            _receipt_string(
                dependency,
                label="observed item-ID dependency",
            )
            for dependency in _receipt_list(
                raw["item_ids_observed_dependencies"],
                label="observed item-ID dependencies",
            )
        ),
        observed_protocol_extensions=tuple(protocol_extensions),
    )


def _parse_receipt_policy(
    raw: Mapping[str, object],
) -> ParityPolicyEvaluation:
    healthy = raw["healthy"]
    if type(healthy) is not bool:
        _receipt_malformed("Parity receipt health must be boolean.")
    policy_version = _receipt_string(
        raw["policy_version"],
        label="parity policy version",
    )
    if policy_version != PARITY_POLICY_VERSION:
        raise ParityValidationError(
            "parity.receipt.policy_unsupported",
            "Parity receipt policy version is unsupported.",
        )
    evaluation_stage = _receipt_string(
        raw["evaluation_stage"],
        label="parity evaluation stage",
    )
    provisional_capabilities = tuple(
        _receipt_string(
            capability,
            label="parity provisional capability",
        )
        for capability in _receipt_list(
            raw["provisional_capabilities"],
            label="parity provisional capabilities",
        )
    )
    findings: list[ParityFinding] = []
    for entry in _receipt_list(raw["findings"], label="parity findings"):
        item = _receipt_mapping(
            entry,
            keys=frozenset({"category", "code", "severity"}),
            label="parity finding",
        )
        category = _receipt_string(
            item["category"],
            label="parity finding category",
        )
        code = _receipt_string(
            item["code"],
            label="parity finding code",
        )
        findings.append(
            ParityFinding(
                category=category,
                code=code,
                severity=_receipt_string(
                    item["severity"],
                    label="parity finding severity",
                ),
                message=f"Persisted parity finding {code}.",
            )
        )
    queue: list[ParityQueueItem] = []
    for entry in _receipt_list(
        raw["synchronization_queue"],
        label="parity synchronization queue",
    ):
        item = _receipt_mapping(
            entry,
            keys=frozenset(
                {"category", "finding_code", "identifier"}
            ),
            label="parity synchronization queue item",
        )
        queue.append(
            ParityQueueItem(
                category=_receipt_string(
                    item["category"],
                    label="queue category",
                ),
                identifier=_receipt_string(
                    item["identifier"],
                    label="queue identifier",
                ),
                finding_code=_receipt_string(
                    item["finding_code"],
                    label="queue finding code",
                ),
            )
        )
    return ParityPolicyEvaluation(
        healthy=healthy,
        policy_version=ParityPolicyVersion(policy_version),
        findings=tuple(findings),
        synchronization_queue=tuple(queue),
        evaluation_stage=evaluation_stage,
        method_coverage=_parse_receipt_method_coverage(
            raw["method_coverage"]
        ),
        provisional_capabilities=provisional_capabilities,
    )


def _parse_parity_receipt(
    payload: Mapping[str, object],
) -> ParityReceipt:
    if (
        isinstance(payload, Mapping)
        and payload.get("schema_version") == 1
    ):
        raise ParityValidationError(
            "parity.receipt.schema_unsupported",
            "Parity receipt schema version is unsupported.",
        )
    raw = _receipt_mapping(
        payload,
        keys=_PARITY_RECEIPT_KEYS,
        label="parity receipt",
    )
    schema_version = raw["schema_version"]
    if type(schema_version) is not int:
        _receipt_malformed(
            "Parity receipt schema version must be an integer."
        )
    if schema_version != PARITY_RECEIPT_SCHEMA_VERSION:
        raise ParityValidationError(
            "parity.receipt.schema_unsupported",
            "Parity receipt schema version is unsupported.",
        )
    reference = _parse_receipt_reference(raw["official_reference"])
    internal = _parse_receipt_internal_fingerprint(
        raw["internal_fingerprint"]
    )
    if (
        _receipt_digest(
            raw["official_reference_sha256"],
            label="official reference fingerprint",
        )
        != reference.fingerprint_sha256
        or _receipt_digest(
            raw["internal_fingerprint_sha256"],
            label="internal fingerprint digest",
        )
        != internal.fingerprint_sha256
    ):
        _receipt_malformed(
            "Parity receipt fingerprint digests do not match their payloads."
        )
    overlay = _receipt_mapping(
        raw["overlay"],
        keys=frozenset({"changes", "path", "sha256"}),
        label="parity overlay",
    )
    changes: list[Mapping[str, object]] = []
    for entry in _receipt_list(
        overlay["changes"],
        label="parity overlay changes",
    ):
        changes.append(
            _receipt_mapping(
                entry,
                keys=frozenset({"op", "path", "value"}),
                label="parity overlay change",
            )
        )
    probes: list[tuple[str, str, str]] = []
    for entry in _receipt_list(
        raw["probe_results"],
        label="parity probe results",
    ):
        item = _receipt_mapping(
            entry,
            keys=frozenset(
                {"evidence_sha256", "name", "result_code"}
            ),
            label="parity probe result",
        )
        probes.append(
            (
                _receipt_string(
                    item["name"],
                    label="parity probe name",
                ),
                _receipt_string(
                    item["result_code"],
                    label="parity probe result code",
                ),
                _receipt_digest(
                    item["evidence_sha256"],
                    label="parity probe evidence digest",
                ),
            )
        )
    return ParityReceipt(
        schema_version=schema_version,
        official_reference=reference,
        internal_fingerprint=internal,
        feature_inventory_sha256s=_parse_receipt_digest_pairs(
            raw["feature_inventory_sha256s"],
            label="feature inventory digests",
        ),
        protocol_inventory_sha256s=_parse_receipt_digest_pairs(
            raw["protocol_inventory_sha256s"],
            label="protocol inventory digests",
        ),
        adapter_rule_set_sha256=_receipt_digest(
            raw["adapter_rule_set_sha256"],
            label="adapter rule-set digest",
        ),
        acceptance_trace=_parse_receipt_acceptance_trace(
            raw["acceptance_trace"]
        ),
        overlay_path=Path(
            _receipt_string(
                overlay["path"],
                label="parity overlay path",
            )
        ),
        overlay_sha256=_receipt_digest(
            overlay["sha256"],
            label="parity overlay digest",
        ),
        overlay_changes=tuple(changes),
        probe_results=tuple(probes),
        policy_evaluation=_parse_receipt_policy(raw),
    )


@dataclass(frozen=True)
class ParityReceiptArtifact:
    receipt: ParityReceipt
    receipt_path: Path
    payload: bytes
    payload_sha256: str
    reused: bool

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ParityReceipt):
            raise ParityValidationError(
                "parity.receipt.artifact_invalid",
                "Parity receipt artifact receipt is invalid.",
            )
        canonical_path = _canonical_path(
            self.receipt_path,
            code="parity.receipt.artifact_invalid",
            field_name="parity receipt artifact path",
        )
        if canonical_path.name != "receipt.json":
            raise ParityValidationError(
                "parity.receipt.artifact_invalid",
                "Parity receipt artifact path is invalid.",
            )
        if self.payload != self.receipt.canonical_bytes:
            raise ParityValidationError(
                "parity.receipt.artifact_invalid",
                "Parity receipt artifact payload is not canonical.",
            )
        _require_sha256(
            self.payload_sha256,
            code="parity.receipt.artifact_invalid",
            field_name="parity receipt artifact digest",
        )
        if not hmac.compare_digest(
            self.payload_sha256,
            hashlib.sha256(self.payload).hexdigest(),
        ):
            raise ParityValidationError(
                "parity.receipt.artifact_invalid",
                "Parity receipt artifact digest does not match its payload.",
            )
        if type(self.reused) is not bool:
            raise ParityValidationError(
                "parity.receipt.artifact_invalid",
                "Parity receipt artifact reuse state is invalid.",
            )
        object.__setattr__(self, "receipt_path", canonical_path)

    @property
    def manifest_metadata(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "parity_internal_fingerprint_sha256": (
                    self.receipt.internal_fingerprint.fingerprint_sha256
                ),
                "parity_official_reference_sha256": (
                    self.receipt.official_reference.fingerprint_sha256
                ),
                "parity_policy_version": str(
                    self.receipt.policy_version
                ),
                "parity_receipt_path": str(self.receipt_path),
                "parity_receipt_schema_version": (
                    self.receipt.schema_version
                ),
                "parity_receipt_sha256": self.payload_sha256,
            }
        )


_PARITY_BUNDLE_MANIFEST_KEYS = frozenset(
    {
        "parity_adapter_rule_set_sha256",
        "parity_capability_receipt_sha256",
        "parity_internal_fingerprint_sha256",
        "parity_official_reference_sha256",
        "parity_overlay_path",
        "parity_overlay_sha256",
        "parity_policy_version",
        "parity_receipt_path",
        "parity_receipt_schema_version",
        "parity_receipt_sha256",
        "parity_source_catalog_path",
        "parity_source_catalog_sha256",
    }
)


def _parity_bundle_target_paths(
    receipt: ParityReceipt,
) -> ParityArtifactPaths:
    return resolve_parity_artifact_paths(
        profile_dir=receipt.overlay_path.parent.parent
    )


def _validate_parity_bundle_artifacts(
    receipt: ParityReceipt,
    overlay: ParityOverlayArtifact,
) -> ParityArtifactPaths:
    if not isinstance(receipt, ParityReceipt):
        raise ParityValidationError(
            "parity.bundle.receipt_invalid",
            "Parity bundle receipt is invalid.",
        )
    if not isinstance(overlay, ParityOverlayArtifact):
        raise ParityValidationError(
            "parity.bundle.overlay_invalid",
            "Parity bundle overlay is invalid.",
        )
    paths = _parity_bundle_target_paths(receipt)
    internal = receipt.internal_fingerprint
    if (
        receipt.overlay_path != paths.overlay_path
        or receipt.overlay_sha256 != overlay.overlay_sha256
        or receipt.overlay_changes != overlay.changes
        or internal.source_catalog != overlay.source_catalog
        or internal.source_catalog_sha256
        != overlay.source_catalog_sha256
        or internal.active_model != overlay.active_model_slug
    ):
        raise ParityValidationError(
            "parity.bundle.artifact_mismatch",
            "Parity receipt and overlay evidence do not describe one candidate.",
        )
    return paths


def _parity_bundle_manifest_metadata(
    *,
    receipt: ParityReceipt,
    overlay: ParityOverlayArtifact,
    paths: ParityArtifactPaths,
) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "parity_adapter_rule_set_sha256": (
                receipt.adapter_rule_set_sha256
            ),
            "parity_capability_receipt_sha256": (
                receipt.internal_fingerprint.capability_receipt_sha256
            ),
            "parity_internal_fingerprint_sha256": (
                receipt.internal_fingerprint.fingerprint_sha256
            ),
            "parity_official_reference_sha256": (
                receipt.official_reference.fingerprint_sha256
            ),
            "parity_overlay_path": str(paths.overlay_path),
            "parity_overlay_sha256": overlay.overlay_sha256,
            "parity_policy_version": str(receipt.policy_version),
            "parity_receipt_path": str(paths.receipt_path),
            "parity_receipt_schema_version": receipt.schema_version,
            "parity_receipt_sha256": receipt.payload_sha256,
            "parity_source_catalog_path": str(overlay.source_catalog),
            "parity_source_catalog_sha256": (
                overlay.source_catalog_sha256
            ),
        }
    )


def _validate_private_staging_root(staging_root: Path) -> Path:
    canonical_root = _canonical_path(
        staging_root,
        code="parity.bundle.staging_invalid",
        field_name="parity bundle staging root",
    )
    try:
        root_state = canonical_root.lstat()
    except OSError as exc:
        raise ParityValidationError(
            "parity.bundle.staging_unsafe",
            "Parity bundle staging root cannot be inspected safely.",
        ) from exc
    if (
        not stat.S_ISDIR(root_state.st_mode)
        or stat.S_IMODE(root_state.st_mode) != 0o700
    ):
        raise ParityValidationError(
            "parity.bundle.staging_unsafe",
            "Parity bundle staging root must be a private directory.",
        )
    return canonical_root


def _read_private_staged_payload(
    *,
    staging_root: Path,
    staged_path: Path,
    expected_name: str,
    expected_payload: bytes,
) -> Path:
    canonical_path = _canonical_path(
        staged_path,
        code="parity.bundle.staging_invalid",
        field_name=f"staged {expected_name}",
    )
    if (
        canonical_path.parent != staging_root
        or canonical_path.name != expected_name
    ):
        raise ParityValidationError(
            "parity.bundle.staging_invalid",
            "Parity bundle staged artifact path is invalid.",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(canonical_path, flags)
    except OSError as exc:
        raise ParityValidationError(
            "parity.bundle.staging_unsafe",
            "Parity bundle staged artifact cannot be opened safely.",
        ) from exc
    try:
        state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(state.st_mode)
            or stat.S_IMODE(state.st_mode) != 0o600
            or state.st_size != len(expected_payload)
        ):
            raise ParityValidationError(
                "parity.bundle.staging_unsafe",
                "Parity bundle staged artifact state is invalid.",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > len(expected_payload):
                raise ParityValidationError(
                    "parity.bundle.staging_unsafe",
                    "Parity bundle staged artifact exceeds its payload.",
                )
            chunks.append(chunk)
        if not hmac.compare_digest(b"".join(chunks), expected_payload):
            raise ParityValidationError(
                "parity.bundle.staging_mismatch",
                "Parity bundle staged artifact bytes do not match.",
            )
    finally:
        os.close(descriptor)
    return canonical_path


@dataclass(frozen=True)
class ParityBundle:
    receipt: ParityReceipt
    overlay: ParityOverlayArtifact
    staging_root: Path
    staged_receipt_path: Path
    staged_overlay_path: Path
    receipt_payload: bytes
    manifest_metadata: Mapping[str, object]
    config_projection: ConfigProjection | None = None
    active_runtime_config_path: Path | None = None
    active_runtime_config_payload: bytes | None = None
    staged_runtime_config_path: Path | None = None
    staged_runtime_config_payload: bytes | None = None
    staged_capability_receipt_path: Path | None = None
    staged_capability_receipt_payload: bytes | None = None
    candidate: ParityCandidate | None = None

    def __post_init__(self) -> None:
        paths = _validate_parity_bundle_artifacts(
            self.receipt,
            self.overlay,
        )
        if (
            not isinstance(self.receipt_payload, bytes)
            or self.receipt_payload != self.receipt.canonical_bytes
        ):
            raise ParityValidationError(
                "parity.bundle.receipt_invalid",
                "Parity bundle receipt payload is not canonical.",
            )
        staging_root = _validate_private_staging_root(self.staging_root)
        staged_receipt_path = _read_private_staged_payload(
            staging_root=staging_root,
            staged_path=self.staged_receipt_path,
            expected_name="receipt.json",
            expected_payload=self.receipt_payload,
        )
        staged_overlay_path = _read_private_staged_payload(
            staging_root=staging_root,
            staged_path=self.staged_overlay_path,
            expected_name="model-catalog.json",
            expected_payload=self.overlay.overlay_payload,
        )
        if not isinstance(self.manifest_metadata, Mapping):
            raise ParityValidationError(
                "parity.bundle.manifest_incomplete",
                "Parity bundle manifest metadata is incomplete.",
            )
        manifest = dict(self.manifest_metadata)
        if set(manifest) != _PARITY_BUNDLE_MANIFEST_KEYS:
            raise ParityValidationError(
                "parity.bundle.manifest_incomplete",
                "Parity bundle manifest metadata is incomplete.",
            )
        expected_manifest = dict(
            _parity_bundle_manifest_metadata(
                receipt=self.receipt,
                overlay=self.overlay,
                paths=paths,
            )
        )
        if manifest != expected_manifest:
            raise ParityValidationError(
                "parity.bundle.manifest_mismatch",
                "Parity bundle manifest metadata does not match its artifacts.",
            )
        object.__setattr__(self, "staging_root", staging_root)
        object.__setattr__(
            self,
            "staged_receipt_path",
            staged_receipt_path,
        )
        object.__setattr__(
            self,
            "staged_overlay_path",
            staged_overlay_path,
        )
        object.__setattr__(
            self,
            "manifest_metadata",
            MappingProxyType(expected_manifest),
        )
        extended_values = (
            self.config_projection,
            self.active_runtime_config_path,
            self.active_runtime_config_payload,
            self.staged_runtime_config_path,
            self.staged_runtime_config_payload,
            self.staged_capability_receipt_path,
            self.staged_capability_receipt_payload,
            self.candidate,
        )
        if self.config_projection is None:
            if any(
                value is not None and value != ""
                for value in extended_values
            ):
                raise ParityValidationError(
                    "parity.bundle.preparation_incomplete",
                    "Parity bundle preparation metadata is incomplete.",
                )
            return
        if (
            not isinstance(self.config_projection, ConfigProjection)
            or not self.config_projection.healthy
            or self.config_projection.overlay_path != paths.overlay_path
            or not isinstance(self.candidate, ParityCandidate)
            or self.candidate.source_config
            is not self.config_projection.config_inputs
        ):
            raise ParityValidationError(
                "parity.bundle.preparation_incomplete",
                "Parity bundle config preparation is incomplete.",
            )
        if (
            not isinstance(self.staged_runtime_config_payload, bytes)
            or not isinstance(
                self.staged_capability_receipt_payload,
                bytes,
            )
            or not isinstance(self.staged_runtime_config_path, Path)
            or not isinstance(self.staged_capability_receipt_path, Path)
        ):
            raise ParityValidationError(
                "parity.bundle.preparation_incomplete",
                "Parity bundle staged runtime preparation is incomplete.",
            )
        staged_runtime_config_path = _read_private_staged_payload(
            staging_root=staging_root,
            staged_path=self.staged_runtime_config_path,
            expected_name="config.toml",
            expected_payload=self.staged_runtime_config_payload,
        )
        staged_capability_receipt_path = _read_private_staged_payload(
            staging_root=staging_root,
            staged_path=self.staged_capability_receipt_path,
            expected_name="capability-receipt.json",
            expected_payload=self.staged_capability_receipt_payload,
        )
        try:
            staged_config = ConfigDocument.parse(
                self.staged_runtime_config_payload.decode("utf-8"),
                "staged parity runtime config",
            )
        except (UnicodeDecodeError, SwitchError) as exc:
            raise ParityValidationError(
                "parity.bundle.staged_config_invalid",
                "Parity bundle staged runtime config is invalid.",
            ) from exc
        if (
            staged_config.data.get("model_catalog_json")
            != str(staged_overlay_path)
            or not isinstance(staged_config.data.get("features"), Mapping)
            or staged_config.data["features"].get("multi_agent_v2") is not True
            or (
                isinstance(staged_config.data.get("agents"), Mapping)
                and "max_threads" in staged_config.data["agents"]
            )
        ):
            raise ParityValidationError(
                "parity.bundle.staged_config_invalid",
                "Parity bundle staged runtime config is not the proven v2 projection.",
            )
        active_path = self.active_runtime_config_path
        active_payload = self.active_runtime_config_payload
        if (active_path is None) != (active_payload is None):
            raise ParityValidationError(
                "parity.bundle.active_config_invalid",
                "Parity bundle active runtime config is incomplete.",
            )
        if active_path is not None and active_payload is not None:
            active_path = _canonical_path(
                active_path,
                code="parity.bundle.active_config_invalid",
                field_name="active runtime config",
            )
            if (
                active_path
                != self.candidate.internal_binding.codex_home / "config.toml"
                or len(active_payload) > MAX_PARITY_CONFIG_BYTES
            ):
                raise ParityValidationError(
                    "parity.bundle.active_config_invalid",
                    "Parity bundle active runtime config is invalid.",
                )
            try:
                active_document = ConfigDocument.parse(
                    active_payload.decode("utf-8"),
                    "active parity runtime config",
                )
            except (UnicodeDecodeError, SwitchError) as exc:
                raise ParityValidationError(
                    "parity.bundle.active_config_invalid",
                    "Parity bundle active runtime config is invalid.",
                ) from exc
            if (
                active_document.data.get("model_catalog_json")
                != str(paths.overlay_path)
                or not isinstance(
                    active_document.data.get("features"),
                    Mapping,
                )
                or active_document.data["features"].get(
                    "multi_agent_v2"
                )
                is not True
                or (
                    isinstance(active_document.data.get("agents"), Mapping)
                    and "max_threads" in active_document.data["agents"]
                )
            ):
                raise ParityValidationError(
                    "parity.bundle.active_config_invalid",
                    "Parity bundle active runtime config is not the proven v2 projection.",
                )
        custom_findings = tuple(
            finding for finding in self.receipt.findings
            if finding.code == "parity.model.custom_catalog_not_applicable"
        )
        if (
            len(custom_findings) != 1
            or custom_findings[0].category != "model_metadata"
            or custom_findings[0].severity != "info"
        ):
            raise ParityValidationError(
                "parity.bundle.model_scope_invalid",
                "Custom catalog comparison applicability is missing or invalid.",
            )
        capability_artifact = self.candidate.capability_receipt
        if (
            not isinstance(capability_artifact, CapabilityReceiptArtifact)
            or capability_artifact.payload
            != self.staged_capability_receipt_payload
        ):
            raise ParityValidationError(
                "parity.bundle.capability_invalid",
                "Parity bundle capability receipt is invalid.",
            )
        object.__setattr__(
            self,
            "active_runtime_config_path",
            active_path,
        )
        object.__setattr__(
            self,
            "staged_runtime_config_path",
            staged_runtime_config_path,
        )
        object.__setattr__(
            self,
            "staged_capability_receipt_path",
            staged_capability_receipt_path,
        )

    @property
    def healthy(self) -> bool:
        return (
            self.receipt.healthy
            and self.config_projection is not None
            and self.config_projection.healthy
        )

    @property
    def findings(self) -> tuple[ParityFinding, ...]:
        config_findings = (
            self.config_projection.findings
            if self.config_projection is not None
            else ()
        )
        return tuple(
            sorted(
                (*self.receipt.findings, *config_findings),
                key=lambda finding: (
                    finding.category,
                    finding.code,
                    finding.message,
                ),
            )
        )

    @property
    def synchronization_queue(self) -> tuple[ParityQueueItem, ...]:
        return self.receipt.synchronization_queue

    @property
    def overlay_payload(self) -> bytes:
        return self.overlay.overlay_payload

    @property
    def active_runtime_config(self) -> tuple[Path, bytes] | None:
        if (
            self.active_runtime_config_path is None
            or self.active_runtime_config_payload is None
        ):
            return None
        return (
            self.active_runtime_config_path,
            self.active_runtime_config_payload,
        )


def _validate_parity_work_root(work_root: Path) -> Path:
    canonical_root = _canonical_path(
        work_root,
        code="parity.bundle.work_root_invalid",
        field_name="parity bundle work root",
    )
    try:
        state = canonical_root.lstat()
    except OSError as exc:
        raise ParityValidationError(
            "parity.bundle.work_root_unsafe",
            "Parity bundle work root cannot be inspected safely.",
        ) from exc
    if not stat.S_ISDIR(state.st_mode):
        raise ParityValidationError(
            "parity.bundle.work_root_unsafe",
            "Parity bundle work root must be a directory.",
        )
    return canonical_root


def _write_private_staged_payload(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short parity staging write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _retire_incomplete_parity_staging(
    staging_root: Path,
    staged_paths: tuple[Path, ...],
) -> None:
    for path in staged_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        staging_root.rmdir()
    except FileNotFoundError:
        pass


def prepare_parity_bundle_artifacts(
    *,
    receipt: ParityReceipt,
    overlay: ParityOverlayArtifact,
    work_root: Path,
) -> ParityBundle:
    paths = _validate_parity_bundle_artifacts(receipt, overlay)
    canonical_work_root = _validate_parity_work_root(work_root)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix="parity-bundle-",
            dir=str(canonical_work_root),
        )
    )
    staging_root.chmod(0o700)
    staged_receipt_path = staging_root / "receipt.json"
    staged_overlay_path = staging_root / "model-catalog.json"
    staged_paths = (staged_receipt_path, staged_overlay_path)
    try:
        _write_private_staged_payload(
            staged_receipt_path,
            receipt.canonical_bytes,
        )
        _write_private_staged_payload(
            staged_overlay_path,
            overlay.overlay_payload,
        )
        directory_descriptor = os.open(
            staging_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return ParityBundle(
            receipt=receipt,
            overlay=overlay,
            staging_root=staging_root,
            staged_receipt_path=staged_receipt_path,
            staged_overlay_path=staged_overlay_path,
            receipt_payload=receipt.canonical_bytes,
            manifest_metadata=_parity_bundle_manifest_metadata(
                receipt=receipt,
                overlay=overlay,
                paths=paths,
            ),
        )
    except BaseException:
        _retire_incomplete_parity_staging(staging_root, staged_paths)
        raise


@dataclass(frozen=True)
class _ParityConfigIdentity:
    active_model: str
    provider_id: str
    wire_api: str
    endpoint_sha256: str
    auth_source_kind: str
    source_catalog: Path
    source_catalog_sha256: str


def _load_cli_version(cli_path: Path, timeout_seconds: float) -> str:
    try:
        result = subprocess.run(
            [str(cli_path), "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ParityValidationError(
            "parity.preparation.version_failed",
            "Parity CLI version command failed.",
        ) from exc
    output = result.stdout or b""
    if result.returncode != 0 or len(output) > 16 * 1024:
        raise ParityValidationError(
            "parity.preparation.version_failed",
            "Parity CLI version command failed.",
        )
    try:
        version = output.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ParityValidationError(
            "parity.preparation.version_failed",
            "Parity CLI version output is invalid.",
        ) from exc
    _require_text(
        version,
        code="parity.preparation.version_failed",
        field_name="Parity CLI version",
    )
    return version


def _load_bundle_metadata(bundle_root: Path) -> tuple[str, str]:
    plist_path = bundle_root / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (
        OSError,
        plistlib.InvalidFileException,
        TypeError,
        ValueError,
    ) as exc:
        raise ParityValidationError(
            "parity.reference.bundle_version_invalid",
            "Official bundle version metadata is invalid.",
        ) from exc
    bundle_id = (
        payload.get("CFBundleIdentifier")
        if isinstance(payload, Mapping)
        else None
    )
    if bundle_id != CURRENT_CHATGPT_BUNDLE_ID:
        raise ParityValidationError(
            "parity.reference.bundle_id_invalid",
            "Official bundle id is not the current ChatGPT host.",
        )
    version = (
        payload.get("CFBundleShortVersionString")
        if isinstance(payload, Mapping)
        else None
    )
    _require_text(
        version,
        code="parity.reference.bundle_version_invalid",
        field_name="official bundle version",
    )
    return bundle_id, version


def _load_bundle_version(bundle_root: Path) -> str:
    return _load_bundle_metadata(bundle_root)[1]


def _schema_documents_from_payload(
    schema_payload: bytes,
) -> Mapping[str, object]:
    try:
        parsed = json.loads(schema_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParityValidationError(
            "parity.preparation.schema_invalid",
            "Generated parity schema payload is invalid.",
        ) from exc
    documents = (
        parsed.get("schema_documents")
        if isinstance(parsed, Mapping)
        else None
    )
    if not isinstance(documents, Mapping) or not documents:
        raise ParityValidationError(
            "parity.preparation.schema_invalid",
            "Generated parity schema payload has no documents.",
        )
    return documents


def _known_model_metadata(
    document: Mapping[str, object],
    *,
    active_model: str,
) -> Mapping[str, object]:
    index = _active_model_index(
        document,
        active_model_slug=active_model,
        source=False,
    )
    model = _catalog_models(document, source=False)[index]
    return MappingProxyType(
        {
            key: _freeze_value(model[key])
            for key in ("multi_agent_version", "tool_mode")
            if key in model
        }
    )


def _source_catalog_from_candidate(
    candidate: ParityCandidate,
    *,
    configured_path: Path,
) -> Path:
    config_inputs = candidate.source_config
    if not isinstance(config_inputs, ConfigInputs):
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Parity candidate config inputs are invalid.",
        )
    managed_overlay = (
        config_inputs.profile_config.parent
        / "parity"
        / "model-catalog.json"
    )
    if configured_path != managed_overlay:
        return configured_path

    manifest = candidate.internal_manifest
    previous_overlay = manifest.get("parity_overlay_path")
    previous_source = manifest.get("parity_source_catalog_path")
    previous_source_sha256 = manifest.get(
        "parity_source_catalog_sha256"
    )
    if (
        previous_overlay != str(managed_overlay)
        or not isinstance(previous_source, str)
        or not previous_source
    ):
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Managed parity overlay has no complete source-catalog provenance.",
        )
    _require_sha256(
        previous_source_sha256,
        code="parity.preparation.config_invalid",
        field_name="managed parity source catalog digest",
    )
    source_catalog = _canonical_path(
        Path(previous_source).expanduser(),
        code="parity.preparation.config_invalid",
        field_name="managed parity source model catalog",
    )
    if source_catalog == managed_overlay:
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Managed parity overlay cannot be its own source catalog.",
        )
    return source_catalog


def _config_identity(
    candidate: ParityCandidate,
) -> _ParityConfigIdentity:
    config_inputs = candidate.source_config
    if not isinstance(config_inputs, ConfigInputs):
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Parity candidate config inputs are invalid.",
        )
    expected_sources = {
        config_inputs.profile_config,
        candidate.official_binding.codex_home / "config.toml",
    }
    if (
        {path for path, _digest in config_inputs.sources}
        != expected_sources
        or {
            path
            for path, _device, _inode, _mode
            in config_inputs.source_states
        }
        != expected_sources
    ):
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Parity candidate config sources are incomplete.",
        )
    profile_source = _read_parity_config_source(
        path=config_inputs.profile_config,
        expected_sha256=config_inputs.expected_sha256(
            config_inputs.profile_config
        ),
        _source_observer=None,
    )
    document = _parse_parity_config_source(profile_source)
    data = document.data
    active_model = data.get("model")
    provider_id = data.get("model_provider")
    source_catalog_value = data.get("model_catalog_json")
    providers = data.get("model_providers")
    provider = (
        providers.get(provider_id)
        if isinstance(providers, Mapping)
        and isinstance(provider_id, str)
        else None
    )
    if (
        not isinstance(active_model, str)
        or not isinstance(provider_id, str)
        or not isinstance(source_catalog_value, str)
        or not isinstance(provider, Mapping)
    ):
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Parity candidate model/provider config is incomplete.",
        )
    _validate_active_model_slug(
        active_model,
        code="parity.preparation.config_invalid",
    )
    _require_text(
        provider_id,
        code="parity.preparation.config_invalid",
        field_name="internal provider id",
    )
    configured_source_catalog = _canonical_path(
        Path(source_catalog_value).expanduser(),
        code="parity.preparation.config_invalid",
        field_name="internal source model catalog",
    )
    source_catalog = _source_catalog_from_candidate(
        candidate,
        configured_path=configured_source_catalog,
    )
    base_url = provider.get("base_url")
    wire_api = provider.get("wire_api")
    if not isinstance(base_url, str) or not isinstance(wire_api, str):
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Parity candidate provider endpoint or wire API is missing.",
        )
    _require_text(
        base_url,
        code="parity.preparation.config_invalid",
        field_name="internal provider endpoint",
    )
    _require_text(
        wire_api,
        code="parity.preparation.config_invalid",
        field_name="internal provider wire API",
    )
    query_params = provider.get("query_params")
    if isinstance(provider.get("env_key"), str):
        auth_source_kind = "env"
    elif isinstance(provider.get("api_key"), str):
        auth_source_kind = "config"
    elif (
        isinstance(query_params, Mapping)
        and any(
            key not in {"api-version", "api_version"}
            for key in query_params
        )
    ):
        auth_source_kind = "query-param"
    elif provider.get("requires_openai_auth") is True:
        auth_source_kind = "openai"
    else:
        auth_source_kind = "unspecified"
    source_snapshot = _regular_file_snapshot(
        source_catalog,
        code="parity.overlay.source_unsafe",
        label="source model catalog",
        max_bytes=MAX_PARITY_CATALOG_BYTES,
    )
    return _ParityConfigIdentity(
        active_model=active_model,
        provider_id=provider_id,
        wire_api=wire_api,
        endpoint_sha256=hashlib.sha256(base_url.encode("utf-8")).hexdigest(),
        auth_source_kind=auth_source_kind,
        source_catalog=source_catalog,
        source_catalog_sha256=source_snapshot.sha256,
    )


def project_parity_runtime_config_payload(
    payload: bytes,
    *,
    overlay_path: Path,
) -> bytes:
    if not isinstance(payload, bytes) or len(payload) > MAX_PARITY_CONFIG_BYTES:
        raise ParityValidationError(
            "parity.bundle.staged_config_invalid",
            "Parity runtime config payload is invalid.",
        )
    try:
        document = ConfigDocument.parse(
            payload.decode("utf-8"),
            "parity runtime config",
        )
        projected = _project_internal_profile_config(
            document,
            overlay_path=overlay_path,
        )
    except (UnicodeDecodeError, SwitchError) as exc:
        raise ParityValidationError(
            "parity.bundle.staged_config_invalid",
            "Parity runtime config payload cannot be projected.",
        ) from exc
    agents = projected.data.get("agents")
    features = projected.data.get("features")
    if (
        projected.data.get("model_catalog_json") != str(overlay_path)
        or not isinstance(features, Mapping)
        or features.get("multi_agent_v2") is not True
        or (
            isinstance(agents, Mapping)
            and "max_threads" in agents
        )
    ):
        raise ParityValidationError(
            "parity.bundle.staged_config_invalid",
            "Parity runtime config projection is incomplete.",
        )
    return projected.text.encode("utf-8")


def _raise_unhealthy_preparation(
    *,
    code: str,
    findings: tuple[ParityFinding, ...],
) -> None:
    finding_codes = ", ".join(
        finding.code
        for finding in findings
        if finding.severity == "error"
    )
    suffix = f": {finding_codes}" if finding_codes else ""
    raise ParityValidationError(
        code,
        f"Parity preparation is unhealthy{suffix}",
    )


def _preparation_snapshot_matches(
    expected: _RegularFileSnapshot,
    observed: _RegularFileSnapshot,
) -> bool:
    return (
        expected.path == observed.path
        and expected.sha256 == observed.sha256
        and expected.mode == observed.mode
        and expected.device == observed.device
        and expected.inode == observed.inode
    )


def _revalidate_preparation_fingerprints(
    *,
    candidate: ParityCandidate,
    official_binary: _RegularFileSnapshot,
    internal_binary: _RegularFileSnapshot,
    overlay: ParityOverlayArtifact,
    internal_schema_sha256: str,
) -> None:
    if candidate.adapter_rule_set_sha256 != protocol_adapter_rule_set_digest():
        raise ParityValidationError(
            "parity.preparation.adapter_stale",
            "Parity candidate adapter rule-set digest changed during preparation.",
        )
    refreshed_official_binary = _regular_file_snapshot(
        candidate.official_binding.backend_cli,
        code="parity.preparation.reference_stale",
        label="official bundled CLI",
        executable=True,
    )
    refreshed_internal_binary = _regular_file_snapshot(
        candidate.internal_binding.backend_cli,
        code="parity.preparation.candidate_stale",
        label="internal backend CLI",
        executable=True,
    )
    if not _preparation_snapshot_matches(
        official_binary,
        refreshed_official_binary,
    ):
        raise ParityValidationError(
            "parity.preparation.reference_stale",
            "Official binary identity changed during parity preparation.",
        )
    if not _preparation_snapshot_matches(
        internal_binary,
        refreshed_internal_binary,
    ):
        raise ParityValidationError(
            "parity.preparation.candidate_stale",
            "Internal binary identity changed during parity preparation.",
        )

    refreshed_source_catalog = _regular_file_snapshot(
        overlay.source_catalog,
        code="parity.preparation.candidate_stale",
        label="source model catalog",
        max_bytes=MAX_PARITY_CATALOG_BYTES,
    )
    if (
        refreshed_source_catalog.sha256
        != overlay.source_catalog_sha256
        or refreshed_source_catalog.mode != overlay.source_mode
        or refreshed_source_catalog.device != overlay.source_device
        or refreshed_source_catalog.inode != overlay.source_inode
    ):
        raise ParityValidationError(
            "parity.preparation.candidate_stale",
            "Source model catalog changed during parity preparation.",
        )

    source_config = candidate.source_config
    if not isinstance(source_config, ConfigInputs):
        raise ParityValidationError(
            "parity.preparation.config_invalid",
            "Parity candidate config inputs are invalid.",
        )
    refreshed_config = ConfigInputs.capture(
        profile_config=source_config.profile_config,
        source_paths=tuple(
            path
            for path, _digest in source_config.sources
        ),
    )
    if refreshed_config != source_config:
        raise ParityValidationError(
            "parity.preparation.candidate_stale",
            "Parity config sources changed during preparation.",
        )

    capability_artifact = candidate.capability_receipt
    if (
        not isinstance(capability_artifact, CapabilityReceiptArtifact)
        or capability_artifact.receipt.backend_sha256
        != internal_binary.sha256
        or capability_artifact.receipt.schema_sha256
        != internal_schema_sha256
        or capability_artifact.payload_sha256
        != hashlib.sha256(capability_artifact.payload).hexdigest()
    ):
        raise ParityValidationError(
            "parity.preparation.capability_stale",
            "Parity capability evidence changed during preparation.",
        )


def prepare_parity_bundle(
    candidate: ParityCandidate,
    *,
    work_root: Path,
    timeouts: ParityTimeouts,
    _schema_loader: Callable[[Path, float], bytes] | None = None,
    _version_loader: Callable[[Path, float], str] | None = None,
    _feature_runner: FeatureRunner | None = None,
    _probe_runner: ParityProbeRunner | None = None,
) -> ParityBundle:
    if not isinstance(candidate, ParityCandidate):
        raise ParityValidationError(
            "parity.preparation.candidate_invalid",
            "Parity preparation requires a ParityCandidate.",
        )
    if not isinstance(timeouts, ParityTimeouts):
        raise ParityValidationError(
            "parity.preparation.timeouts_invalid",
            "Parity preparation timeouts are invalid.",
        )
    canonical_work_root = _validate_parity_work_root(work_root)
    schema_loader = _schema_loader or (
        lambda path, timeout: generate_app_server_schema(
            path,
            timeout_seconds=timeout,
        )
    )
    version_loader = _version_loader or _load_cli_version
    if not callable(schema_loader) or not callable(version_loader):
        raise ParityValidationError(
            "parity.preparation.runner_invalid",
            "Parity preparation runner is invalid.",
        )
    if candidate.adapter_rule_set_sha256 != protocol_adapter_rule_set_digest():
        raise ParityValidationError(
            "parity.preparation.adapter_stale",
            "Parity candidate adapter rule-set digest is stale.",
        )
    config_identity = _config_identity(candidate)
    artifact_paths = resolve_parity_artifact_paths(
        profile_dir=candidate.source_config.profile_config.parent
    )
    overlay = prepare_parity_overlay(
        source_catalog=config_identity.source_catalog,
        expected_source_sha256=config_identity.source_catalog_sha256,
        active_model_slug=config_identity.active_model,
    )
    config_projection = prepare_parity_config_projection(
        config_inputs=candidate.source_config,
        overlay_path=artifact_paths.overlay_path,
    )
    if not config_projection.healthy:
        _raise_unhealthy_preparation(
            code="parity.preparation.config_unhealthy",
            findings=config_projection.findings,
        )
    from codex_switch_home_sync import build_internal_home_config

    runtime_config_path = (
        candidate.internal_binding.codex_home / "config.toml"
    )
    runtime_payload = build_internal_home_config(
        candidate.official_binding.codex_home,
        "internal",
        runtime_config_path,
        candidate.source_config.profile_config,
        config_projection=config_projection,
    ).encode("utf-8")
    project_parity_runtime_config_payload(
        runtime_payload,
        overlay_path=artifact_paths.overlay_path,
    )
    active_runtime_path = candidate.active_runtime_config_path
    active_runtime_payload = (
        runtime_payload if active_runtime_path is not None else None
    )
    # _config_identity validates an explicit provider catalog and resolves the
    # original source behind any managed overlay. The same slug in an official
    # catalog cannot establish equivalence with this provider-owned model.
    overlay_document = _parse_parity_catalog_source(
        overlay.overlay_payload
    )
    internal_model_metadata = _known_model_metadata(
        overlay_document,
        active_model=config_identity.active_model,
    )
    official_binary = _regular_file_snapshot(
        candidate.official_binding.backend_cli,
        code="parity.reference.binary_invalid",
        label="official bundled CLI",
        executable=True,
    )
    internal_binary = _regular_file_snapshot(
        candidate.internal_binding.backend_cli,
        code="parity.internal.binary_invalid",
        label="internal backend CLI",
        executable=True,
    )
    official_schema_payload = schema_loader(
        candidate.official_binding.backend_cli,
        timeouts.command_seconds,
    )
    internal_schema_payload = schema_loader(
        candidate.internal_binding.backend_cli,
        timeouts.command_seconds,
    )
    if not isinstance(official_schema_payload, bytes) or not isinstance(
        internal_schema_payload,
        bytes,
    ):
        raise ParityValidationError(
            "parity.preparation.schema_invalid",
            "Parity schema runner returned invalid payloads.",
        )
    official_schema_sha256 = hashlib.sha256(
        official_schema_payload
    ).hexdigest()
    internal_schema_sha256 = hashlib.sha256(
        internal_schema_payload
    ).hexdigest()
    capability_artifact = candidate.capability_receipt
    if (
        not isinstance(capability_artifact, CapabilityReceiptArtifact)
        or capability_artifact.receipt.backend_sha256
        != internal_binary.sha256
        or capability_artifact.receipt.schema_sha256
        != internal_schema_sha256
        or capability_artifact.payload_sha256
        != hashlib.sha256(capability_artifact.payload).hexdigest()
    ):
        raise ParityValidationError(
            "parity.preparation.capability_invalid",
            "Parity candidate capability receipt does not match the backend schema.",
        )
    capability_values = (
        capability_artifact.receipt.capabilities.canonical_dynamic_tools,
        capability_artifact.receipt.capabilities.remote_marketplace_kind,
        capability_artifact.receipt.capabilities.versioned_config_write_preserves_unrelated,
    )
    if any(value is None for value in capability_values):
        raise ParityValidationError(
            "parity.preparation.capability_unknown",
            "Parity candidate capability evidence is unknown.",
        )

    with tempfile.TemporaryDirectory(
        prefix="parity-probe-",
        dir=str(canonical_work_root),
    ) as probe_temp:
        probe_root = Path(probe_temp)
        probe_root.chmod(0o700)
        official_isolated_home = probe_root / "official-default"
        internal_isolated_home = probe_root / "internal-default"
        probe_home = probe_root / "candidate-home"
        probe_parity_dir = probe_home / "parity"
        probe_workspace = probe_root / "workspace"
        for path in (
            official_isolated_home,
            internal_isolated_home,
            probe_home,
            probe_parity_dir,
            probe_workspace,
        ):
            path.mkdir(mode=0o700)
        probe_overlay_path = probe_parity_dir / "model-catalog.json"
        probe_capability_path = (
            probe_parity_dir / "capability-receipt.json"
        )
        probe_config_path = probe_home / "config.toml"
        probe_config_payload = project_parity_runtime_config_payload(
            runtime_payload,
            overlay_path=probe_overlay_path,
        )
        _write_private_staged_payload(
            probe_overlay_path,
            overlay.overlay_payload,
        )
        _write_private_staged_payload(
            probe_capability_path,
            capability_artifact.payload,
        )
        _write_private_staged_payload(
            probe_config_path,
            probe_config_payload,
        )
        official_features = collect_feature_inventory(
            side="official",
            cli_path=candidate.official_binding.backend_cli,
            isolated_home=official_isolated_home,
            effective_home=candidate.official_binding.codex_home,
            runner=_feature_runner,
            timeout_seconds=timeouts.command_seconds,
            max_output_bytes=timeouts.feature_output_bytes,
        )
        internal_features = collect_feature_inventory(
            side="internal",
            cli_path=candidate.internal_binding.backend_cli,
            isolated_home=internal_isolated_home,
            effective_home=probe_home,
            runner=_feature_runner,
            timeout_seconds=timeouts.command_seconds,
            max_output_bytes=timeouts.feature_output_bytes,
        )
        official_protocol = collect_protocol_inventory(
            side="official",
            schema_documents=_schema_documents_from_payload(
                official_schema_payload
            ),
        )
        internal_protocol = collect_protocol_inventory(
            side="internal",
            schema_documents=_schema_documents_from_payload(
                internal_schema_payload
            ),
        )
        feature_comparison = compare_feature_inventories(
            official_features,
            internal_features,
        )
        protocol_comparison = compare_protocol_inventories(
            official_protocol,
            internal_protocol,
        )
        acceptance_trace = current_parity_acceptance_trace()
        method_coverage = build_method_coverage(
            protocol_comparison
        )
        eligibility_evaluation = evaluate_parity_policy(
            feature_comparison=feature_comparison,
            protocol_comparison=protocol_comparison,
            official_model_metadata={},
            custom_model_catalog=True,
            internal_model_metadata=internal_model_metadata,
            method_coverage=method_coverage,
            evaluation_stage="eligibility",
            acceptance_trace=acceptance_trace,
            multi_agent_v2_evidence=MultiAgentV2Evidence(
                overlay_projected=True,
                config_projected=True,
            ),
        )
        if not eligibility_evaluation.probe_eligible:
            _raise_unhealthy_preparation(
                code="parity.preparation.policy_unhealthy",
                findings=eligibility_evaluation.findings,
            )
        probe_report = run_parity_probes(
            inputs=ParityProbeInputs(
                backend_cli=candidate.internal_binding.backend_cli,
                backend_sha256=internal_binary.sha256,
                codex_home=probe_home,
                workspace=probe_workspace,
                config_path=probe_config_path,
                config_sha256=hashlib.sha256(
                    probe_config_payload
                ).hexdigest(),
                overlay_path=probe_overlay_path,
                overlay_sha256=overlay.overlay_sha256,
                capability_receipt_path=probe_capability_path,
                capability_receipt_sha256=(
                    capability_artifact.payload_sha256
                ),
            ),
            runner=_probe_runner,
            timeout_seconds=timeouts.probe_seconds,
            max_output_bytes=timeouts.probe_output_bytes,
        )
        if not probe_report.healthy:
            _raise_unhealthy_preparation(
                code="parity.preparation.probe_unhealthy",
                findings=probe_report.findings,
            )
        _revalidate_preparation_fingerprints(
            candidate=candidate,
            official_binary=official_binary,
            internal_binary=internal_binary,
            overlay=overlay,
            internal_schema_sha256=internal_schema_sha256,
        )
        typed_probe_result = next(
            (
                result.result_code
                for result in probe_report.results
                if result.name == "typed_subagent_v2"
            ),
            None,
        )
        final_policy_evaluation = evaluate_parity_policy(
            feature_comparison=feature_comparison,
            protocol_comparison=protocol_comparison,
            official_model_metadata={},
            custom_model_catalog=True,
            internal_model_metadata=internal_model_metadata,
            method_coverage=method_coverage,
            evaluation_stage="final",
            acceptance_trace=acceptance_trace,
            multi_agent_v2_evidence=MultiAgentV2Evidence(
                overlay_projected=True,
                config_projected=True,
                typed_probe_result=typed_probe_result,
                fingerprints_revalidated=True,
            ),
        )
        if not final_policy_evaluation.healthy:
            _raise_unhealthy_preparation(
                code="parity.preparation.policy_unhealthy",
                findings=final_policy_evaluation.findings,
            )

    official_reference = OfficialReference.from_runtime_binding(
        candidate.official_binding,
        bundle_version=_load_bundle_version(
            candidate.official_binding.desktop_host.bundle_root
        )
        if isinstance(
            candidate.official_binding.desktop_host,
            ChatGPTDesktopHost,
        )
        else "",
        cli_version=version_loader(
            candidate.official_binding.backend_cli,
            timeouts.command_seconds,
        ),
        binary_sha256=official_binary.sha256,
        schema_sha256=official_schema_sha256,
        feature_inventory_sha256=official_features.payload_sha256,
    )
    projected_payloads = dict(config_projection.payloads)
    config_sha256s = (
        (
            "profile",
            hashlib.sha256(
                projected_payloads[
                    candidate.source_config.profile_config
                ]
            ).hexdigest(),
        ),
        (
            "shared",
            hashlib.sha256(
                projected_payloads[
                    candidate.official_binding.codex_home / "config.toml"
                ]
            ).hexdigest(),
        ),
        ("runtime", hashlib.sha256(runtime_payload).hexdigest()),
    )
    canonical_internal_binding = candidate.canonical_internal_binding
    if canonical_internal_binding is None:
        raise ParityValidationError(
            "parity.preparation.candidate_invalid",
            "Parity candidate canonical Runtime Binding is missing.",
        )
    internal_fingerprint = InternalFingerprint(
        backend_cli=canonical_internal_binding.backend_cli,
        cli_version=version_loader(
            candidate.internal_binding.backend_cli,
            timeouts.command_seconds,
        ),
        binary_sha256=internal_binary.sha256,
        active_model=config_identity.active_model,
        provider_id=config_identity.provider_id,
        wire_api=config_identity.wire_api,
        endpoint_sha256=config_identity.endpoint_sha256,
        auth_source_kind=config_identity.auth_source_kind,
        capability_receipt_sha256=capability_artifact.payload_sha256,
        source_catalog=config_identity.source_catalog,
        source_catalog_sha256=config_identity.source_catalog_sha256,
        config_sha256s=config_sha256s,
    )
    receipt = ParityReceipt(
        schema_version=PARITY_RECEIPT_SCHEMA_VERSION,
        official_reference=official_reference,
        internal_fingerprint=internal_fingerprint,
        feature_inventory_sha256s=(
            ("official", official_features.payload_sha256),
            ("internal", internal_features.payload_sha256),
        ),
        protocol_inventory_sha256s=(
            ("official", official_protocol.payload_sha256),
            ("internal", internal_protocol.payload_sha256),
        ),
        adapter_rule_set_sha256=candidate.adapter_rule_set_sha256,
        acceptance_trace=acceptance_trace,
        overlay_path=artifact_paths.overlay_path,
        overlay_sha256=overlay.overlay_sha256,
        overlay_changes=overlay.changes,
        probe_results=tuple(
            (
                result.name,
                result.result_code,
                result.evidence_sha256,
            )
            for result in probe_report.results
        ),
        policy_evaluation=final_policy_evaluation,
    )
    bundle = prepare_parity_bundle_artifacts(
        receipt=receipt,
        overlay=overlay,
        work_root=canonical_work_root,
    )
    staged_runtime_config_path = bundle.staging_root / "config.toml"
    staged_capability_receipt_path = (
        bundle.staging_root / "capability-receipt.json"
    )
    staged_runtime_config_payload = project_parity_runtime_config_payload(
        runtime_payload,
        overlay_path=bundle.staged_overlay_path,
    )
    extended_paths = (
        staged_runtime_config_path,
        staged_capability_receipt_path,
        bundle.staged_receipt_path,
        bundle.staged_overlay_path,
    )
    try:
        _write_private_staged_payload(
            staged_runtime_config_path,
            staged_runtime_config_payload,
        )
        _write_private_staged_payload(
            staged_capability_receipt_path,
            capability_artifact.payload,
        )
        directory_descriptor = os.open(
            bundle.staging_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        prepared = replace(
            bundle,
            config_projection=config_projection,
            active_runtime_config_path=(
                active_runtime_path
                if active_runtime_payload is not None
                else None
            ),
            active_runtime_config_payload=active_runtime_payload,
            staged_runtime_config_path=staged_runtime_config_path,
            staged_runtime_config_payload=staged_runtime_config_payload,
            staged_capability_receipt_path=(
                staged_capability_receipt_path
            ),
            staged_capability_receipt_payload=capability_artifact.payload,
            candidate=candidate,
        )
        revalidate_parity_bundle_inputs(prepared)
        return prepared
    except BaseException:
        _retire_incomplete_parity_staging(
            bundle.staging_root,
            extended_paths,
        )
        raise


def _revalidate_parity_bundle_inputs(
    bundle: ParityBundle,
    *,
    include_config_sources: bool,
) -> None:
    if (
        not isinstance(bundle, ParityBundle)
        or bundle.config_projection is None
        or bundle.candidate is None
    ):
        raise ParityValidationError(
            "parity.bundle.preparation_incomplete",
            "Parity bundle cannot be revalidated.",
        )
    replace(bundle)
    candidate = bundle.candidate
    official_binary = _regular_file_snapshot(
        candidate.official_binding.backend_cli,
        code="parity.bundle.reference_stale",
        label="official bundled CLI",
        executable=True,
    )
    canonical_internal_binding = candidate.canonical_internal_binding
    if canonical_internal_binding is None:
        raise ParityValidationError(
            "parity.bundle.preparation_incomplete",
            "Parity candidate canonical Runtime Binding is missing.",
        )
    execution_backend = candidate.internal_binding.backend_cli
    canonical_backend = canonical_internal_binding.backend_cli
    selected_backend = execution_backend
    if execution_backend != canonical_backend:
        try:
            execution_backend.lstat()
        except FileNotFoundError:
            selected_backend = canonical_backend
        except OSError as exc:
            raise ParityValidationError(
                "parity.bundle.candidate_stale",
                "Parity candidate backend cannot be inspected.",
            ) from exc
    internal_binary = _regular_file_snapshot(
        selected_backend,
        code="parity.bundle.candidate_stale",
        label="internal backend CLI",
        executable=True,
    )
    if (
        official_binary.sha256
        != bundle.receipt.official_reference.binary_sha256
        or internal_binary.sha256
        != bundle.receipt.internal_fingerprint.binary_sha256
    ):
        raise ParityValidationError(
            "parity.bundle.candidate_stale",
            "Parity binary identity changed before promotion.",
        )
    try:
        observed_bundle_id, observed_bundle_version = _load_bundle_metadata(
            bundle.receipt.official_reference.bundle_root
        )
    except ParityValidationError as exc:
        raise ParityValidationError(
            "parity.bundle.reference_stale",
            "Official bundle identity changed before promotion.",
        ) from exc
    if (
        observed_bundle_id != bundle.receipt.official_reference.bundle_id
        or observed_bundle_version
        != bundle.receipt.official_reference.bundle_version
    ):
        raise ParityValidationError(
            "parity.bundle.reference_stale",
            "Official bundle identity changed before promotion.",
        )
    source_catalog = _regular_file_snapshot(
        bundle.overlay.source_catalog,
        code="parity.bundle.candidate_stale",
        label="source model catalog",
        max_bytes=MAX_PARITY_CATALOG_BYTES,
    )
    if (
        source_catalog.sha256 != bundle.overlay.source_catalog_sha256
        or source_catalog.mode != bundle.overlay.source_mode
        or source_catalog.device != bundle.overlay.source_device
        or source_catalog.inode != bundle.overlay.source_inode
    ):
        raise ParityValidationError(
            "parity.bundle.candidate_stale",
            "Source model catalog changed before promotion.",
        )
    if include_config_sources:
        for path, expected_sha256 in (
            bundle.config_projection.config_inputs.sources
        ):
            observed = _regular_file_snapshot(
                path,
                code="parity.bundle.candidate_stale",
                label="parity config source",
                max_bytes=MAX_PARITY_CONFIG_BYTES,
            )
            try:
                expected_device, expected_inode, expected_mode = (
                    bundle.config_projection.config_inputs.expected_state(path)
                )
            except KeyError as exc:
                raise ParityValidationError(
                    "parity.bundle.preparation_incomplete",
                    "Parity config source identity is missing.",
                ) from exc
            if (
                observed.sha256 != expected_sha256
                or observed.device != expected_device
                or observed.inode != expected_inode
                or observed.mode != expected_mode
            ):
                raise ParityValidationError(
                    "parity.bundle.candidate_stale",
                    "Parity config source changed before promotion.",
                )


def revalidate_parity_bundle_inputs(bundle: ParityBundle) -> None:
    _revalidate_parity_bundle_inputs(
        bundle,
        include_config_sources=True,
    )


def revalidate_parity_bundle_immutable_inputs(bundle: ParityBundle) -> None:
    _revalidate_parity_bundle_inputs(
        bundle,
        include_config_sources=False,
    )


def _open_parity_profile_directory(
    paths: ParityArtifactPaths,
    *,
    for_write: bool,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(paths.profile_dir, flags)
    except FileNotFoundError as exc:
        code = (
            "parity.receipt.path_invalid"
            if for_write
            else "parity.receipt.missing"
        )
        raise ParityValidationError(
            code,
            "Internal profile directory is unavailable.",
        ) from exc
    except OSError as exc:
        raise ParityValidationError(
            "parity.receipt.unsafe",
            "Internal profile directory cannot be opened safely.",
        ) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise ParityValidationError(
            "parity.receipt.unsafe",
            "Internal profile path is not a directory.",
        )
    return descriptor


def _open_parity_directory(
    profile_descriptor: int,
    *,
    create: bool,
) -> int:
    if create:
        try:
            os.mkdir("parity", 0o700, dir_fd=profile_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ParityValidationError(
                "parity.receipt.write_failed",
                "Parity artifact directory cannot be created.",
            ) from exc
    try:
        before = os.stat(
            "parity",
            dir_fd=profile_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise ParityValidationError(
            "parity.receipt.missing",
            "Parity artifact directory is missing.",
        ) from exc
    except OSError as exc:
        raise ParityValidationError(
            "parity.receipt.unsafe",
            "Parity artifact directory cannot be inspected safely.",
        ) from exc
    if not stat.S_ISDIR(before.st_mode):
        raise ParityValidationError(
            "parity.receipt.unsafe",
            "Parity artifact path is not a directory.",
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            "parity",
            flags,
            dir_fd=profile_descriptor,
        )
    except OSError as exc:
        raise ParityValidationError(
            "parity.receipt.unsafe",
            "Parity artifact directory cannot be opened safely.",
        ) from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != before.st_dev
        or opened.st_ino != before.st_ino
    ):
        os.close(descriptor)
        raise ParityValidationError(
            "parity.receipt.unsafe",
            "Parity artifact directory identity changed.",
        )
    if create:
        os.fchmod(descriptor, 0o700)
        opened = os.fstat(descriptor)
    if stat.S_IMODE(opened.st_mode) != 0o700:
        os.close(descriptor)
        raise ParityValidationError(
            "parity.receipt.mode_invalid",
            "Parity artifact directory must use mode 0700.",
        )
    return descriptor


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short parity receipt write")
        offset += written


def write_parity_receipt_artifact(
    *,
    receipt: ParityReceipt,
    profile_dir: Path,
) -> ParityReceiptArtifact:
    if not isinstance(receipt, ParityReceipt):
        raise ParityValidationError(
            "parity.receipt.invalid",
            "Parity receipt writer requires a validated receipt.",
        )
    payload = receipt.canonical_bytes
    if len(payload) > MAX_PARITY_RECEIPT_BYTES:
        raise ParityValidationError(
            "parity.receipt.oversized",
            "Parity receipt exceeds the persisted size limit.",
        )
    paths = resolve_parity_artifact_paths(profile_dir=profile_dir)
    profile_descriptor = _open_parity_profile_directory(
        paths,
        for_write=True,
    )
    parity_descriptor = -1
    temporary_name = (
        f".receipt.json.{os.getpid()}."
        f"{threading.get_ident()}.{time.time_ns()}"
    )
    temporary_descriptor = -1
    temporary_exists = False
    try:
        parity_descriptor = _open_parity_directory(
            profile_descriptor,
            create=True,
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        temporary_descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parity_descriptor,
        )
        temporary_exists = True
        os.fchmod(temporary_descriptor, 0o600)
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        os.rename(
            temporary_name,
            "receipt.json",
            src_dir_fd=parity_descriptor,
            dst_dir_fd=parity_descriptor,
        )
        temporary_exists = False
        os.fsync(parity_descriptor)
        installed = os.stat(
            "receipt.json",
            dir_fd=parity_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(installed.st_mode)
            or stat.S_IMODE(installed.st_mode) != 0o600
        ):
            raise ParityValidationError(
                "parity.receipt.write_failed",
                "Persisted parity receipt identity or mode is invalid.",
            )
    except ParityValidationError:
        raise
    except OSError as exc:
        raise ParityValidationError(
            "parity.receipt.write_failed",
            "Parity receipt could not be written safely.",
        ) from exc
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_exists and parity_descriptor >= 0:
            try:
                os.unlink(
                    temporary_name,
                    dir_fd=parity_descriptor,
                )
            except OSError:
                pass
        if parity_descriptor >= 0:
            os.close(parity_descriptor)
        os.close(profile_descriptor)
    return ParityReceiptArtifact(
        receipt=receipt,
        receipt_path=paths.receipt_path,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        reused=False,
    )


def _read_parity_receipt_payload(
    paths: ParityArtifactPaths,
) -> bytes:
    profile_descriptor = _open_parity_profile_directory(
        paths,
        for_write=False,
    )
    parity_descriptor = -1
    receipt_descriptor = -1
    try:
        parity_descriptor = _open_parity_directory(
            profile_descriptor,
            create=False,
        )
        try:
            before = os.stat(
                "receipt.json",
                dir_fd=parity_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise ParityValidationError(
                "parity.receipt.missing",
                "Parity receipt is missing.",
            ) from exc
        except OSError as exc:
            raise ParityValidationError(
                "parity.receipt.unsafe",
                "Parity receipt cannot be inspected safely.",
            ) from exc
        if not stat.S_ISREG(before.st_mode):
            raise ParityValidationError(
                "parity.receipt.unsafe",
                "Parity receipt must be a regular non-symlink file.",
            )
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise ParityValidationError(
                "parity.receipt.mode_invalid",
                "Parity receipt must use mode 0600.",
            )
        if before.st_size > MAX_PARITY_RECEIPT_BYTES:
            raise ParityValidationError(
                "parity.receipt.oversized",
                "Parity receipt exceeds the persisted size limit.",
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            receipt_descriptor = os.open(
                "receipt.json",
                flags,
                dir_fd=parity_descriptor,
            )
        except OSError as exc:
            raise ParityValidationError(
                "parity.receipt.unsafe",
                "Parity receipt cannot be opened safely.",
            ) from exc
        opened = os.fstat(receipt_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise ParityValidationError(
                "parity.receipt.unsafe",
                "Parity receipt identity changed before reading.",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(receipt_descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PARITY_RECEIPT_BYTES:
                raise ParityValidationError(
                    "parity.receipt.oversized",
                    "Parity receipt exceeds the persisted size limit.",
                )
            chunks.append(chunk)
        after = os.stat(
            "receipt.json",
            dir_fd=parity_descriptor,
            follow_symlinks=False,
        )
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or stat.S_IMODE(after.st_mode) != 0o600
        ):
            raise ParityValidationError(
                "parity.receipt.unsafe",
                "Parity receipt identity changed while reading.",
            )
        return b"".join(chunks)
    finally:
        if receipt_descriptor >= 0:
            os.close(receipt_descriptor)
        if parity_descriptor >= 0:
            os.close(parity_descriptor)
        os.close(profile_descriptor)


def _reject_receipt_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate parity receipt key")
        result[key] = value
    return result


def _reject_receipt_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant {value}")


def load_parity_receipt_artifact(
    *,
    profile_dir: Path,
    expected_payload_sha256: str,
    expected_official_reference: OfficialReference,
    expected_internal_fingerprint: InternalFingerprint,
    expected_adapter_rule_set_sha256: str,
) -> ParityReceiptArtifact:
    _require_sha256(
        expected_payload_sha256,
        code="parity.receipt.expectation_invalid",
        field_name="expected parity receipt digest",
    )
    _require_sha256(
        expected_adapter_rule_set_sha256,
        code="parity.receipt.expectation_invalid",
        field_name="expected adapter rule-set digest",
    )
    if not isinstance(expected_official_reference, OfficialReference):
        raise ParityValidationError(
            "parity.receipt.expectation_invalid",
            "Expected official parity reference is invalid.",
        )
    if not isinstance(expected_internal_fingerprint, InternalFingerprint):
        raise ParityValidationError(
            "parity.receipt.expectation_invalid",
            "Expected internal parity fingerprint is invalid.",
        )
    paths = resolve_parity_artifact_paths(profile_dir=profile_dir)
    payload = _read_parity_receipt_payload(paths)
    observed_payload_sha256 = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(
        expected_payload_sha256,
        observed_payload_sha256,
    ):
        raise ParityValidationError(
            "parity.receipt.digest_mismatch",
            "Parity receipt payload digest does not match the manifest.",
        )
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_reject_receipt_duplicate_keys,
            parse_constant=_reject_receipt_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ParityValidationError(
            "parity.receipt.malformed",
            "Parity receipt JSON is malformed.",
        ) from exc
    if not isinstance(document, Mapping):
        raise ParityValidationError(
            "parity.receipt.malformed",
            "Parity receipt root must be an object.",
        )
    receipt = ParityReceipt.from_payload(document)
    if receipt.canonical_bytes != payload:
        raise ParityValidationError(
            "parity.receipt.malformed",
            "Parity receipt payload is not canonical JSON.",
        )
    if (
        receipt.official_reference.fingerprint_sha256
        != expected_official_reference.fingerprint_sha256
        or receipt.internal_fingerprint.fingerprint_sha256
        != expected_internal_fingerprint.fingerprint_sha256
        or not hmac.compare_digest(
            receipt.adapter_rule_set_sha256,
            expected_adapter_rule_set_sha256,
        )
    ):
        raise ParityValidationError(
            "parity.receipt.stale",
            "Parity receipt does not match the current runtime evidence.",
        )
    return ParityReceiptArtifact(
        receipt=receipt,
        receipt_path=paths.receipt_path,
        payload=payload,
        payload_sha256=observed_payload_sha256,
        reused=True,
    )


# Reviewed empty-home states for the local text/typed-agent core contract.
_REVIEWED_FEATURE_PAIRS = MappingProxyType({
    "analytics_plan_history": (
        FeatureRecord(
            name="analytics_plan_history", stage="experimental",
            isolated_default=False, effective_state=False,
        ),
        None,
    ),
    "guardian_ext": (
        FeatureRecord(
            name="guardian_ext", stage="removed",
            isolated_default=False, effective_state=False,
        ),
        FeatureRecord(
            name="guardian_ext", stage="under development",
            isolated_default=False, effective_state=False,
        ),
    ),
    "personality": (
        FeatureRecord(
            name="personality", stage="removed",
            isolated_default=False, effective_state=False,
        ),
        FeatureRecord(
            name="personality", stage="stable",
            isolated_default=True, effective_state=True,
        ),
    ),
    "realtime_conversation": (
        FeatureRecord(
            name="realtime_conversation", stage="stable",
            isolated_default=True, effective_state=True,
        ),
        FeatureRecord(
            name="realtime_conversation", stage="experimental",
            isolated_default=False, effective_state=False,
        ),
    ),
    "send_message_to_user_async": (
        FeatureRecord(
            name="send_message_to_user_async", stage="under development",
            isolated_default=False, effective_state=False,
        ),
        None,
    ),
    "use_xaa": (
        FeatureRecord(
            name="use_xaa", stage="under development",
            isolated_default=False, effective_state=False,
        ),
        None,
    ),
    "worktrees": (
        FeatureRecord(
            name="worktrees", stage="stable",
            isolated_default=True, effective_state=True,
        ),
        FeatureRecord(
            name="worktrees", stage="experimental",
            isolated_default=False, effective_state=False,
        ),
    ),
})

_OPTIONAL_BACKEND_REQUESTS = MappingProxyType({
    ("client_request", "thread/rollback"): "7d8534143179f149e8d580a43813282ed06efc5df01f768a2d6263677b2be9fd",
})


def _current_parity_classification_table() -> Mapping[str, object]:
    table = _PARITY_CLASSIFICATION_TABLES.get(PARITY_POLICY_VERSION)
    if table is None:
        raise ParityValidationError(
            "parity.policy.version_invalid",
            "Parity classification table is unavailable.",
        )
    return table


def _policy_finding(
    *,
    category: str,
    code: str,
    severity: str,
    identifier: str,
    expected: str,
    observed: str,
) -> ParityFinding:
    return ParityFinding(
        category=category,
        code=code,
        severity=severity,
        message=f"{identifier} does not match parity policy.",
        expected=expected,
        observed=observed,
    )


def _feature_drift_kind(
    entry: FeatureInventoryComparisonEntry,
) -> str | None:
    if entry.official == entry.internal:
        return None
    if entry.official is None:
        return "internal_only"
    if entry.internal is None:
        return "missing"
    return "drift"


def _protocol_drift_kind(
    entry: ProtocolInventoryComparisonEntry,
) -> str | None:
    if entry.official is None and entry.internal is not None:
        return "internal_only"
    if entry.official is not None and entry.internal is None:
        return "missing"
    if not entry.compatible:
        return "incompatible"
    return None


def _is_core_protocol_method(
    table: Mapping[str, object],
    method: str,
) -> bool:
    exact = table["core_protocol_exact"]
    prefixes = table["core_protocol_prefixes"]
    assert isinstance(exact, frozenset)
    assert isinstance(prefixes, tuple)
    return method in exact or method.startswith(prefixes)


def _coverage_for_protocol_entry(
    entry: ProtocolInventoryComparisonEntry,
    coverage: MethodCoverageRecord,
) -> bool:
    if (
        entry.official is None
        or entry.internal is None
        or coverage.direction != entry.direction
        or coverage.method != entry.method
        or coverage.official_schema_sha256
        != entry.official.schema_sha256
        or coverage.internal_schema_sha256
        != entry.internal.schema_sha256
        or coverage.reason_codes != entry.reason_codes
    ):
        return False
    expected = build_method_coverage(
        ProtocolInventoryComparison(entries=(entry,))
    )
    return (
        len(expected) == 1
        and expected[0].disposition != "uncovered"
        and coverage == expected[0]
    )


def _append_optional_extension_evidence(
    *,
    coverage: MethodCoverageRecord,
    findings: list[ParityFinding],
    queue: list[ParityQueueItem],
) -> None:
    for extension_id in coverage.optional_extension_ids:
        identifier = (
            f"{coverage.direction}:{coverage.method}#{extension_id}"
        )
        code = "parity.protocol.optional_extension"
        findings.append(
            _policy_finding(
                category="protocol",
                code=code,
                severity="warning",
                identifier=identifier,
                expected="optional-unobserved",
                observed="exact-schema-extension",
            )
        )
        queue.append(
            ParityQueueItem(
                category="protocol",
                identifier=identifier,
                finding_code=code,
            )
        )


def evaluate_parity_policy(
    *,
    feature_comparison: FeatureInventoryComparison,
    protocol_comparison: ProtocolInventoryComparison,
    official_model_metadata: Mapping[str, object],
    internal_model_metadata: Mapping[str, object],
    custom_model_catalog: bool = False,
    acceptance_trace: ParityAcceptanceTrace | None = None,
    observed_protocol_methods: frozenset[tuple[str, str]] = frozenset(),
    observed_features: frozenset[str] = frozenset(),
    method_coverage: tuple[MethodCoverageRecord, ...] = (),
    evaluation_stage: str = "final",
    multi_agent_v2_evidence: MultiAgentV2Evidence | None = None,
    item_ids_observed_dependencies: frozenset[str] = frozenset(),
    observed_protocol_extensions: frozenset[
        tuple[str, str, str]
    ] = frozenset(),
) -> ParityPolicyEvaluation:
    if not isinstance(feature_comparison, FeatureInventoryComparison):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy feature comparison is invalid.",
        )
    if not isinstance(protocol_comparison, ProtocolInventoryComparison):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy protocol comparison is invalid.",
        )
    if not isinstance(official_model_metadata, Mapping) or not isinstance(
        internal_model_metadata,
        Mapping,
    ):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy model metadata must be mappings.",
        )
    if type(custom_model_catalog) is not bool:
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Custom catalog context must be boolean.",
        )
    if evaluation_stage not in {"eligibility", "final"}:
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy evaluation stage is invalid.",
        )
    if acceptance_trace is not None:
        if (
            not isinstance(acceptance_trace, ParityAcceptanceTrace)
            or observed_protocol_methods
            or observed_features
            or item_ids_observed_dependencies
            or observed_protocol_extensions
        ):
            raise ParityValidationError(
                "parity.policy.input_invalid",
                "Parity policy acceptance trace inputs conflict.",
            )
        observed_protocol_methods = (
            acceptance_trace.observed_protocol_methods
        )
        observed_features = acceptance_trace.observed_features
        item_ids_observed_dependencies = (
            acceptance_trace.item_ids_observed_dependencies
        )
        observed_protocol_extensions = (
            acceptance_trace.observed_protocol_extensions
        )
    coverage_records = tuple(method_coverage)
    if (
        any(
            not isinstance(record, MethodCoverageRecord)
            for record in coverage_records
        )
        or len(coverage_records)
        != len(
            {
                (record.direction, record.method)
                for record in coverage_records
            }
        )
    ):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy method coverage is invalid.",
        )
    if (
        multi_agent_v2_evidence is not None
        and not isinstance(
            multi_agent_v2_evidence,
            MultiAgentV2Evidence,
        )
    ):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy typed-v2 evidence is invalid.",
        )
    item_dependencies = frozenset(item_ids_observed_dependencies)
    if any(
        not isinstance(dependency, str)
        or not dependency
        or dependency != dependency.strip()
        for dependency in item_dependencies
    ):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy item-ID dependencies are invalid.",
        )
    observed_extensions = frozenset(observed_protocol_extensions)
    if any(
        not isinstance(item, tuple)
        or len(item) != 3
        or item[0] not in _PROTOCOL_DIRECTION_INDEX
        or not isinstance(item[1], str)
        or _PROTOCOL_METHOD_RE.fullmatch(item[1]) is None
        or not isinstance(item[2], str)
        or _EXTENSION_ID_RE.fullmatch(item[2]) is None
        for item in observed_extensions
    ):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy observed extensions are invalid.",
        )
    try:
        observed_protocol = frozenset(observed_protocol_methods)
        observed_feature_names = frozenset(observed_features)
    except TypeError as exc:
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy acceptance trace is invalid.",
        ) from exc
    if any(
        not isinstance(item, tuple)
        or len(item) != 2
        or item[0] not in _PROTOCOL_DIRECTION_INDEX
        or not isinstance(item[1], str)
        or _PROTOCOL_METHOD_RE.fullmatch(item[1]) is None
        for item in observed_protocol
    ) or any(
        not isinstance(name, str)
        or _FEATURE_NAME_RE.fullmatch(name) is None
        for name in observed_feature_names
    ):
        raise ParityValidationError(
            "parity.policy.input_invalid",
            "Parity policy acceptance trace is invalid.",
        )
    table = _current_parity_classification_table()
    optional_protocol = table["optional_protocol"]
    core_features = table["core_features"]
    optional_unless_observed_features = table[
        "optional_unless_observed_features"
    ]
    optional_features = table["optional_features"]
    metadata_optional_features = table["metadata_optional_features"]
    assert isinstance(optional_protocol, frozenset)
    assert isinstance(core_features, frozenset)
    assert isinstance(optional_unless_observed_features, frozenset)
    assert isinstance(optional_features, frozenset)
    assert isinstance(metadata_optional_features, frozenset)

    findings: list[ParityFinding] = []
    queue: list[ParityQueueItem] = []
    provisional_capabilities: list[str] = []
    coverage_by_key = {
        (record.direction, record.method): record
        for record in coverage_records
    }
    used_coverage_keys: set[tuple[str, str]] = set()
    accepted_coverage_keys: set[tuple[str, str]] = set()

    for entry in protocol_comparison.entries:
        drift = _protocol_drift_kind(entry)
        if drift is None:
            continue
        identifier = f"{entry.direction}:{entry.method}"
        key = (entry.direction, entry.method)
        coverage = coverage_by_key.get(key)
        if coverage is not None:
            used_coverage_keys.add(key)
            if coverage.disposition == "uncovered":
                coverage = None
            elif not _coverage_for_protocol_entry(entry, coverage):
                findings.append(
                    _policy_finding(
                        category="protocol",
                        code="parity.protocol.coverage_invalid",
                        severity="error",
                        identifier=identifier,
                        expected="exact-current-method-coverage",
                        observed="stale-or-broader-evidence",
                    )
                )
                continue
            elif any(
                (
                    coverage.direction,
                    coverage.method,
                    extension_id,
                )
                in observed_extensions
                for extension_id in coverage.optional_extension_ids
            ):
                findings.append(
                    _policy_finding(
                        category="protocol",
                        code=(
                            "parity.protocol."
                            "observed_extension_incompatible"
                        ),
                        severity="error",
                        identifier=identifier,
                        expected="extension-unobserved",
                        observed="extension-observed",
                    )
                )
                continue
            else:
                accepted_coverage_keys.add(key)
                _append_optional_extension_evidence(
                    coverage=coverage,
                    findings=findings,
                    queue=queue,
                )
                continue
        if entry.official is None:
            expected_digest = _OPTIONAL_BACKEND_REQUESTS.get(key)
            if (
                entry.internal is not None
                and entry.compatible
                and entry.internal.schema_sha256 == expected_digest
                and key not in observed_protocol
                and not any(extension[:2] == key for extension in observed_extensions)
            ):
                code = "parity.protocol.optional_extension"
                extension_identifier = f"{identifier}#backend_only_request"
                findings.append(_policy_finding(
                    category="protocol", code=code, severity="warning",
                    identifier=extension_identifier,
                    expected="optional-unobserved", observed="exact-backend-request",
                ))
                queue.append(ParityQueueItem(
                    category="protocol", identifier=extension_identifier,
                    finding_code=code,
                ))
                continue
            findings.append(
                _policy_finding(
                    category="protocol",
                    code="parity.protocol.unclassified_drift",
                    severity="error",
                    identifier=identifier,
                    expected="official-reference-classified",
                    observed=drift,
                )
            )
            continue
        if key in optional_protocol:
            if key in observed_protocol:
                code = (
                    "parity.protocol.observed_core_missing"
                    if drift == "missing"
                    else "parity.protocol.observed_core_incompatible"
                )
                findings.append(
                    _policy_finding(
                        category="protocol",
                        code=code,
                        severity="error",
                        identifier=identifier,
                        expected="core-compatible",
                        observed=drift,
                    )
                )
            else:
                code = "parity.protocol.optional_missing"
                findings.append(
                    _policy_finding(
                        category="protocol",
                        code=code,
                        severity="warning",
                        identifier=identifier,
                        expected="optional-compatible",
                        observed=drift,
                    )
                )
                queue.append(
                    ParityQueueItem(
                        category="protocol",
                        identifier=identifier,
                        finding_code=code,
                    )
                )
            continue
        if entry.official is not None and _is_core_protocol_method(
            table,
            entry.method,
        ):
            code = (
                "parity.protocol.core_missing"
                if drift == "missing"
                else "parity.protocol.core_incompatible"
            )
            findings.append(
                _policy_finding(
                    category="protocol",
                    code=code,
                    severity="error",
                    identifier=identifier,
                    expected="core-compatible",
                    observed=drift,
                )
            )
            continue
        findings.append(
            _policy_finding(
                category="protocol",
                code="parity.protocol.unclassified_drift",
                severity="error",
                identifier=identifier,
                expected="classified",
                observed=drift,
            )
        )

    for record in coverage_records:
        key = (record.direction, record.method)
        if key in used_coverage_keys:
            continue
        findings.append(
            _policy_finding(
                category="protocol",
                code="parity.protocol.coverage_unbound",
                severity="error",
                identifier=f"{record.direction}:{record.method}",
                expected="current-incompatible-method",
                observed="unbound-evidence",
            )
        )

    for entry in feature_comparison.entries:
        drift = _feature_drift_kind(entry)
        if drift is None:
            continue
        name = entry.name
        reviewed_pair = _REVIEWED_FEATURE_PAIRS.get(name)
        if reviewed_pair is not None and (
            entry.official, entry.internal
        ) == reviewed_pair:
            observed = name in observed_feature_names
            code = (
                "parity.feature.observed_core_missing" if observed and drift == "missing"
                else "parity.feature.observed_core_drift" if observed
                else "parity.feature.optional_missing" if drift == "missing"
                else "parity.feature.optional_drift"
            )
            findings.append(_policy_finding(
                category="feature", code=code,
                severity="error" if observed else "warning",
                identifier=name, expected="reviewed-core-independent-state",
                observed="core-dependency" if observed else drift,
            ))
            if not observed:
                queue.append(ParityQueueItem(
                    category="feature", identifier=name, finding_code=code,
                ))
            continue
        if entry.official is None:
            findings.append(
                _policy_finding(
                    category="feature",
                    code="parity.feature.unclassified_drift",
                    severity="error",
                    identifier=name,
                    expected="official-reference-classified",
                    observed=drift,
                )
            )
            continue
        if name == "multi_agent_v2":
            if (
                evaluation_stage == "eligibility"
                and multi_agent_v2_evidence is not None
                and multi_agent_v2_evidence.eligibility_ready
            ):
                provisional_capabilities.append(name)
                continue
            if (
                evaluation_stage == "final"
                and multi_agent_v2_evidence is not None
                and multi_agent_v2_evidence.final_ready
            ):
                continue
            findings.append(
                _policy_finding(
                    category="feature",
                    code="parity.feature.core_drift",
                    severity="error",
                    identifier=name,
                    expected=(
                        "projected-v2-and-final-typed-probe"
                    ),
                    observed=drift,
                )
            )
            continue
        if name in core_features:
            findings.append(
                _policy_finding(
                    category="feature",
                    code="parity.feature.core_drift",
                    severity="error",
                    identifier=name,
                    expected="core-compatible",
                    observed=drift,
                )
            )
            continue
        if name in optional_unless_observed_features:
            if name in observed_feature_names:
                code = (
                    "parity.feature.observed_core_missing"
                    if drift == "missing"
                    else "parity.feature.observed_core_drift"
                )
                findings.append(
                    _policy_finding(
                        category="feature",
                        code=code,
                        severity="error",
                        identifier=name,
                        expected="observed-core-compatible",
                        observed=drift,
                    )
                )
            else:
                code = (
                    "parity.feature.optional_missing"
                    if drift == "missing"
                    else "parity.feature.optional_drift"
                )
                findings.append(
                    _policy_finding(
                        category="feature",
                        code=code,
                        severity="warning",
                        identifier=name,
                        expected="optional-compatible",
                        observed=drift,
                    )
                )
                queue.append(
                    ParityQueueItem(
                        category="feature",
                        identifier=name,
                        finding_code=code,
                    )
                )
            continue
        if name in optional_features:
            code = (
                "parity.feature.optional_missing"
                if drift == "missing"
                else "parity.feature.optional_drift"
            )
            findings.append(
                _policy_finding(
                    category="feature",
                    code=code,
                    severity="warning",
                    identifier=name,
                    expected="optional-compatible",
                    observed=drift,
                )
            )
            queue.append(
                ParityQueueItem(
                    category="feature",
                    identifier=name,
                    finding_code=code,
                )
            )
            continue
        if name in metadata_optional_features:
            behavior_compatible = (
                entry.official is not None
                and entry.internal is not None
                and entry.official.effective_state
                == entry.internal.effective_state
            )
            if name == "item_ids":
                resume_key = ("client_request", "thread/resume")
                resume_comparison = next(
                    (
                        protocol_entry
                        for protocol_entry
                        in protocol_comparison.entries
                        if (
                            protocol_entry.direction,
                            protocol_entry.method,
                        )
                        == resume_key
                    ),
                    None,
                )
                resume_natively_compatible = (
                    resume_comparison is not None
                    and resume_comparison.official is not None
                    and resume_comparison.internal is not None
                    and resume_comparison.compatible
                    and not resume_comparison.reason_codes
                )
                resume_coverage = coverage_by_key.get(
                    resume_key
                )
                resume_covered = (
                    resume_key in accepted_coverage_keys
                    and resume_coverage is not None
                    and resume_coverage.disposition
                    == "adapter_transformed"
                    and any(
                        rule_id == THREAD_RESUME_HISTORY_RULE_ID
                        for rule_id, _digest
                        in resume_coverage.adapter_rule_digests
                    )
                )
                dependencies_exact = item_dependencies == frozenset(
                    {_ITEM_IDS_RESUME_DEPENDENCY}
                )
                if (
                    name not in observed_feature_names
                    and not (
                        item_dependencies
                        - {_ITEM_IDS_RESUME_DEPENDENCY}
                    )
                    and (
                        (behavior_compatible and not item_dependencies)
                        or (
                            dependencies_exact
                            and (
                                resume_natively_compatible
                                or resume_covered
                            )
                        )
                    )
                ):
                    code = "parity.feature.optional_drift"
                    findings.append(
                        _policy_finding(
                            category="feature",
                            code=code,
                            severity="warning",
                            identifier=name,
                            expected="observed-resume-path-covered",
                            observed="metadata_drift",
                        )
                    )
                    queue.append(
                        ParityQueueItem(
                            category="feature",
                            identifier=name,
                            finding_code=code,
                        )
                    )
                else:
                    findings.append(
                        _policy_finding(
                            category="feature",
                            code="parity.feature.core_drift",
                            severity="error",
                            identifier=name,
                            expected="exact-resume-dependency-only",
                            observed=drift,
                        )
                    )
                continue
            if behavior_compatible and name not in observed_feature_names:
                code = "parity.feature.optional_drift"
                findings.append(
                    _policy_finding(
                        category="feature",
                        code=code,
                        severity="warning",
                        identifier=name,
                        expected="behavior-compatible",
                        observed="metadata_drift",
                    )
                )
                queue.append(
                    ParityQueueItem(
                        category="feature",
                        identifier=name,
                        finding_code=code,
                    )
                )
            else:
                findings.append(
                    _policy_finding(
                        category="feature",
                        code="parity.feature.core_drift",
                        severity="error",
                        identifier=name,
                        expected="core-behavior-compatible",
                        observed=drift,
                    )
                )
            continue
        findings.append(
            _policy_finding(
                category="feature",
                code="parity.feature.unclassified_drift",
                severity="error",
                identifier=name,
                expected="classified",
                observed=drift,
            )
        )

    if custom_model_catalog:
        findings.append(
            ParityFinding(
                category="model_metadata",
                code="parity.model.custom_catalog_not_applicable",
                severity="info",
                message="Official model comparison is not applicable to a custom catalog.",
            )
        )
    else:
        official_multi_agent = official_model_metadata.get("multi_agent_version")
        internal_multi_agent = internal_model_metadata.get("multi_agent_version")
        if official_multi_agent == "v2" and internal_multi_agent != "v2":
            findings.append(
                _policy_finding(
                    category="model_metadata",
                    code="parity.model.multi_agent_version_core",
                    severity="error",
                    identifier="multi_agent_version",
                    expected="v2",
                    observed="missing_or_non_v2",
                )
            )
        elif official_multi_agent != internal_multi_agent:
            findings.append(
                _policy_finding(
                    category="model_metadata",
                    code="parity.model.unclassified_drift",
                    severity="error",
                    identifier="multi_agent_version",
                    expected="classified",
                    observed="drift",
                )
            )

        official_tool_mode = official_model_metadata.get("tool_mode")
        internal_tool_mode = internal_model_metadata.get("tool_mode")
        if official_tool_mode != internal_tool_mode:
            if official_tool_mode is not None:
                code = "parity.model.tool_mode_pending_provider"
                findings.append(
                    _policy_finding(
                        category="model_metadata",
                        code=code,
                        severity="warning",
                        identifier="tool_mode",
                        expected="provider-evidence",
                        observed="pending",
                    )
                )
                queue.append(
                    ParityQueueItem(
                        category="model_metadata",
                        identifier="tool_mode",
                        finding_code=code,
                    )
                )
            else:
                findings.append(
                    _policy_finding(
                        category="model_metadata",
                        code="parity.model.unclassified_drift",
                        severity="error",
                        identifier="tool_mode",
                        expected="classified",
                        observed="internal_only",
                    )
                )

        known_model_keys = {"multi_agent_version", "tool_mode"}
        for key in sorted(
            (
                set(official_model_metadata)
                | set(internal_model_metadata)
            )
            - known_model_keys
        ):
            if official_model_metadata.get(key) == internal_model_metadata.get(key):
                continue
            findings.append(
                _policy_finding(
                    category="model_metadata",
                    code="parity.model.unclassified_drift",
                    severity="error",
                    identifier=key,
                    expected="classified",
                    observed="drift",
                )
            )

    return ParityPolicyEvaluation(
        healthy=(
            evaluation_stage == "final"
            and not any(
                finding.severity == "error"
                for finding in findings
            )
        ),
        policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
        findings=tuple(findings),
        synchronization_queue=tuple(queue),
        evaluation_stage=evaluation_stage,
        method_coverage=coverage_records,
        provisional_capabilities=tuple(provisional_capabilities),
    )


@dataclass(frozen=True)
class ParityReport:
    healthy: bool
    policy_version: ParityPolicyVersion
    official_reference: OfficialReference
    internal_fingerprint: InternalFingerprint
    findings: tuple[ParityFinding, ...] = ()
    synchronization_queue: tuple[ParityQueueItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.healthy, bool):
            raise ParityValidationError(
                "parity.report.invalid",
                "Parity report health must be boolean.",
            )
        if not isinstance(self.policy_version, ParityPolicyVersion):
            raise ParityValidationError(
                "parity.report.invalid",
                "Parity report policy version is invalid.",
            )
        if not isinstance(self.official_reference, OfficialReference):
            raise ParityValidationError(
                "parity.report.invalid",
                "Parity report official reference is invalid.",
            )
        if not isinstance(self.internal_fingerprint, InternalFingerprint):
            raise ParityValidationError(
                "parity.report.invalid",
                "Parity report internal fingerprint is invalid.",
            )
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(
            self,
            "synchronization_queue",
            tuple(self.synchronization_queue),
        )


def _parity_error_category(code: str) -> str:
    if code.startswith("parity.reference."):
        return "reference"
    if code.startswith("parity.config."):
        return "config"
    if code.startswith("parity.overlay."):
        return "overlay"
    if code.startswith("parity.preparation.adapter"):
        return "adapter"
    return "receipt"


def parity_error_report(
    *,
    code: str,
    message: str,
    profile_dir: Path,
    backend_cli: Path | None = None,
) -> ParityReport:
    bundle_root = Path("/Applications/ChatGPT.app")
    return ParityReport(
        healthy=False,
        policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
        official_reference=OfficialReference(
            authority=REFERENCE_AUTHORITY,
            bundle_root=bundle_root,
            bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
            bundle_version="unresolved",
            bundled_cli=bundle_root / "Contents" / "Resources" / "codex",
            cli_version="unresolved",
            binary_sha256=_UNKNOWN_PARITY_SHA256,
            schema_sha256=_UNKNOWN_PARITY_SHA256,
            feature_inventory_sha256=_UNKNOWN_PARITY_SHA256,
        ),
        internal_fingerprint=InternalFingerprint(
            backend_cli=(
                backend_cli
                if backend_cli is not None
                else profile_dir / "parity" / "unresolved-codex"
            ),
            cli_version="unresolved",
            binary_sha256=_UNKNOWN_PARITY_SHA256,
            active_model="unresolved",
            provider_id="unresolved",
            wire_api="unresolved",
            endpoint_sha256=_UNKNOWN_PARITY_SHA256,
            auth_source_kind="unresolved",
            capability_receipt_sha256=_UNKNOWN_PARITY_SHA256,
            source_catalog=(
                profile_dir
                / "parity"
                / "unresolved-model-catalog.json"
            ),
            source_catalog_sha256=_UNKNOWN_PARITY_SHA256,
            config_sha256s=(),
        ),
        findings=(
            ParityFinding(
                category=_parity_error_category(code),
                code=code,
                severity="error",
                message=message,
            ),
        ),
    )


__all__ = [
    "ALLOWED_IDENTITY_DIFFERENCES",
    "MAX_PARITY_CATALOG_BYTES",
    "MAX_PARITY_CONFIG_BYTES",
    "MAX_PARITY_PROBE_OUTPUT_BYTES",
    "MAX_PARITY_RECEIPT_BYTES",
    "PARITY_POLICY_VERSION",
    "PARITY_RECEIPT_SCHEMA_VERSION",
    "PROTOCOL_DIRECTIONS",
    "ConfigInputs",
    "ConfigProjection",
    "FeatureCommandRequest",
    "FeatureCommandResult",
    "FeatureInventory",
    "FeatureInventoryComparison",
    "FeatureInventoryComparisonEntry",
    "FeatureRecord",
    "InternalFingerprint",
    "MethodCoverageRecord",
    "MultiAgentV2Evidence",
    "OfficialReference",
    "ParityAcceptanceTrace",
    "ParityArtifactPaths",
    "ParityBundle",
    "ParityCandidate",
    "ParityFinding",
    "ParityOverlayArtifact",
    "ParityPolicyEvaluation",
    "ParityPolicyVersion",
    "ParityTimeouts",
    "ParityProbeCommandResult",
    "ParityProbeInputs",
    "ParityProbeReport",
    "ParityProbeRequest",
    "ParityProbeResult",
    "ParityQueueItem",
    "ParityReceipt",
    "ParityReceiptArtifact",
    "ParityReport",
    "ParityValidationError",
    "ProtocolInventory",
    "ProtocolInventoryComparison",
    "ProtocolInventoryComparisonEntry",
    "ProtocolMethodRecord",
    "collect_feature_inventory",
    "collect_protocol_inventory",
    "compare_feature_inventories",
    "compare_protocol_inventories",
    "build_method_coverage",
    "current_parity_acceptance_trace",
    "evaluate_parity_policy",
    "load_parity_receipt_artifact",
    "parity_error_report",
    "prepare_parity_bundle",
    "prepare_parity_bundle_artifacts",
    "prepare_parity_config_projection",
    "prepare_parity_overlay",
    "project_parity_runtime_config_payload",
    "revalidate_parity_bundle_immutable_inputs",
    "revalidate_parity_bundle_inputs",
    "resolve_parity_artifact_paths",
    "run_parity_probes",
    "validate_parity_overlay",
    "write_parity_receipt_artifact",
]
