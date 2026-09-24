from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from codex_switch_home_select import profile_home_binding
from codex_switch_home_sync import (
    plugin_support_snapshot_name,
    refresh_profile_plugin_support_snapshot,
)
from codex_switch_io import ensure_private_dir, now_stamp, read_json
from codex_switch_launch import read_launch_agent_cli_path
from codex_switch_paths import equivalent_paths, profile_app_cli_path
from codex_switch_parity import (
    MAX_PARITY_RECEIPT_BYTES,
    ConfigProjection,
    ParityReceipt,
    ParityReport,
    ParityValidationError,
    load_parity_receipt_artifact,
    parity_error_report,
)
from codex_switch_plugins import missing_enabled_plugins, repair_profile_plugins
from codex_switch_protocol_adapter import (
    capability_receipt_path_for_launcher,
    load_capability_receipt_artifact,
    managed_probe_process,
    protocol_adapter_rule_set_digest,
)
from codex_switch_running_app import (
    attestation_problem_messages,
    collect_store_runtime_observation,
    is_default_desktop_context,
    running_desktop_problems,
)
from codex_switch_runtime_binding import (
    RuntimeBinding,
    RuntimeBindingError,
    RuntimeObservation,
    manifest_has_internal_cli_generation,
    manifest_uses_canonical_binding,
    resolve_store_runtime_binding,
    validate_internal_runtime_generation,
)
from codex_switch_selection import (
    ActiveProfileSelectionSnapshot,
    active_profile_selection,
    read_active_profile_selection_snapshot,
)
from codex_switch_shared_configuration import (
    shared_configuration_diagnostic_lines,
    shared_configuration_report,
)
from codex_switch_store import Store, make_store
from codex_switch_toml_scan import toml_table_name
from codex_switch_toml_validate import commentless_line, validate_toml
from codex_switch_constants import SwitchError


AZURE_RESPONSES_RESOURCE_MISMATCH = (
    "The requested item was created under a different Azure OpenAI resource"
)
RESPONSES_REASONING_ITEM_NOT_FOUND = re.compile(
    r"Item with id '(rs_[A-Za-z0-9]+)' not found"
)
RESPONSES_TOOL_SMOKE_PROMPT = (
    "Use the shell tool to run exactly: printf codex_switch_responses_tool_smoke. "
    "Then reply with exactly: done"
)
SAFE_SMOKE_HEADERS = {
    "x-account-id": "accounts",
    "x-account-deployment": "deployments",
    "x-model-request-id": "model_request_ids",
    "x-tt-logid": "tt_log_ids",
}
SAFE_SMOKE_DETAIL_KEYS = frozenset(SAFE_SMOKE_HEADERS.values())
SAFE_ROUTING_VALUE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
SENSITIVE_HEADER = re.compile(
    r"(?im)(?P<prefix>\b(?:authorization|proxy-authorization|cookie|"
    r"set-cookie|x-api-key|api[-_]?key)\b[\"']?\s*:\s*)[^\r\n]*"
)
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>\b(?:[A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret)|password)\b[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\r\n]+)"
)
BEARER_TOKEN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+"
)
SIGNED_QUERY_VALUE = re.compile(
    r"(?i)(?P<prefix>[?&](?:sig|signature|x-amz-signature|"
    r"x-goog-signature|access_token|api_key|apikey|credential)=)"
    r"[^&#\s]+"
)
URL_USERINFO = re.compile(
    r"(?i)(?P<scheme>https?://)[^/\s:@]+:[^@\s/]+@"
)
APP_SERVER_INITIALIZE_ID = "__codex_initialize__"
APP_SERVER_PLUGIN_LIST_ID = "plugin-list-smoke"
APP_SERVER_RESPONSE_TIMEOUT_SECONDS = 6.0
APP_SERVER_SETTLE_SECONDS = 1.5
APP_SERVER_OUTPUT_LINE_LIMIT = 8
APP_SERVER_MAX_LINE_BYTES = 64 * 1024
APP_SERVER_MAX_STDERR_BYTES = 64 * 1024
DEFAULT_SMOKE_TIMEOUT_SECONDS = 15.0
DEFAULT_SMOKE_MAX_STREAM_BYTES = 64 * 1024
DEFAULT_TERMINATE_GRACE_SECONDS = 0.5
DEFAULT_KILL_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class SmokeOutcome:
    status: str
    kind: str
    summary: str
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float


@dataclass(frozen=True)
class VerificationRuntimeBindings:
    cli: RuntimeBinding | None
    cli_problem: str | None
    app: RuntimeBinding | None
    app_problem: str | None
    selection_problem: str | None = None


_SHARED_CONFIGURATION_REPORT_UNSET = object()


def selection_uses_shared_configuration(selection: object) -> bool:
    return (
        getattr(selection, "cli_profile", None),
        getattr(selection, "app_profile", None),
    ) == ("internal", "openai-official")


def shared_configuration_problem_messages(report: object | None) -> list[str]:
    if report is None:
        return []
    status = str(getattr(report, "status", "unknown"))
    findings = tuple(getattr(report, "findings", ()))
    unhealthy = (
        not bool(getattr(report, "cli_ready", False))
        or status
        in {
            "blocked",
            "incomplete",
            "materialization_failed",
            "stale",
            "unsafe",
        }
        or any(
            str(getattr(finding, "severity", "error"))
            in {"error", "warning"}
            for finding in findings
        )
    )
    return shared_configuration_diagnostic_lines(report) if unhealthy else []


class _BoundedByteCapture:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._discarded = 0
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            combined_size = len(self._buffer) + len(chunk)
            if combined_size <= self._max_bytes:
                self._buffer.extend(chunk)
                return
            discarded = combined_size - self._max_bytes
            self._discarded += discarded
            if len(chunk) >= self._max_bytes:
                self._buffer[:] = chunk[-self._max_bytes :]
                return
            del self._buffer[:discarded]
            self._buffer.extend(chunk)

    def render(self) -> tuple[str, bool]:
        with self._lock:
            payload = bytes(self._buffer)
            discarded = self._discarded
        text = payload.decode("utf-8", errors="replace")
        if not discarded:
            return text, False
        return f"[... truncated {discarded} bytes ...]\n{text}", True


def sanitize_external_text(text: str) -> str:
    sanitized = URL_USERINFO.sub(
        lambda match: f"{match.group('scheme')}[REDACTED]@",
        text,
    )
    sanitized = SENSITIVE_HEADER.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        sanitized,
    )
    sanitized = SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        sanitized,
    )
    sanitized = BEARER_TOKEN.sub("Bearer [REDACTED]", sanitized)
    return SIGNED_QUERY_VALUE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        sanitized,
    )


def sanitize_external_data(value):
    if isinstance(value, str):
        return sanitize_external_text(value)
    if isinstance(value, list):
        return [sanitize_external_data(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_external_data(item) for item in value]
    if isinstance(value, dict):
        sanitized: dict[object, object] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = key_text.lower().replace("-", "_")
            if normalized_key in SAFE_SMOKE_DETAIL_KEYS:
                if isinstance(item, list):
                    sanitized[key] = [
                        candidate
                        if isinstance(candidate, str)
                        and SAFE_ROUTING_VALUE.fullmatch(candidate)
                        else "[REDACTED]"
                        for candidate in item
                    ]
                elif (
                    isinstance(item, str)
                    and SAFE_ROUTING_VALUE.fullmatch(item)
                ):
                    sanitized[key] = item
                else:
                    sanitized[key] = "[REDACTED]"
            elif re.search(
                r"(?:authorization|cookie|api_?key|access_?token|"
                r"refresh_?token|client_?secret|password)",
                normalized_key,
                re.IGNORECASE,
            ):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_external_data(item)
        return sanitized
    return value


def _read_bounded_stream(
    stream,
    capture: _BoundedByteCapture,
) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                return
            capture.append(chunk)
    except (OSError, ValueError):
        return


def _wait_for_process_until(
    process: subprocess.Popen[bytes],
    deadline: float,
) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False
    return True


def _signal_process_group(
    process: subprocess.Popen[bytes],
    sig: signal.Signals,
) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass


def _process_group_exists(process: subprocess.Popen[bytes]) -> bool:
    process.poll()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_until(
    process: subprocess.Popen[bytes],
    deadline: float,
) -> bool:
    while time.monotonic() < deadline:
        if not _process_group_exists(process):
            return True
        time.sleep(0.01)
    return not _process_group_exists(process)


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    terminate_grace_seconds: float,
    kill_grace_seconds: float,
) -> None:
    _signal_process_group(process, signal.SIGTERM)
    if _wait_for_process_group_until(
        process,
        time.monotonic() + max(0.0, terminate_grace_seconds),
    ):
        return
    _signal_process_group(process, signal.SIGKILL)
    _wait_for_process_group_until(
        process,
        time.monotonic() + max(0.0, kill_grace_seconds),
    )
    if process.poll() is None:
        _wait_for_process_until(
            process,
            time.monotonic() + max(0.0, kill_grace_seconds),
        )


def run_bounded_process(
    command: Sequence[str],
    *,
    kind: str,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout_seconds: float = DEFAULT_SMOKE_TIMEOUT_SECONDS,
    max_stream_bytes: int = DEFAULT_SMOKE_MAX_STREAM_BYTES,
    terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> SmokeOutcome:
    command_tuple = tuple(str(part) for part in command)
    if not command_tuple:
        raise ValueError("command must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command_tuple,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            start_new_session=True,
        )
    except FileNotFoundError:
        return SmokeOutcome(
            status="not_run",
            kind=kind,
            summary=f"{kind} not run: binary not found: {command_tuple[0]}",
            command=command_tuple,
            returncode=None,
            stdout="",
            stderr="",
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_seconds=time.monotonic() - started,
        )
    except OSError as error:
        return SmokeOutcome(
            status="not_run",
            kind=kind,
            summary=f"{kind} not run: {error}",
            command=command_tuple,
            returncode=None,
            stdout="",
            stderr=str(error),
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_seconds=time.monotonic() - started,
        )

    threads: list[threading.Thread] = []
    with managed_probe_process(
        process, threads=threads,
        terminate=lambda child: _terminate_process_group(
            child, terminate_grace_seconds=terminate_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
        ),
    ):
        stdout_capture = _BoundedByteCapture(max_stream_bytes)
        stderr_capture = _BoundedByteCapture(max_stream_bytes)
        threads.extend((
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
        ))
        for thread in threads:
            thread.start()

        timed_out = not _wait_for_process_until(
            process,
            started + timeout_seconds,
        )
        if timed_out:
            _terminate_process_group(
                process,
                terminate_grace_seconds=terminate_grace_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )

        drain_deadline = time.monotonic() + max(0.0, kill_grace_seconds)
        for thread in threads:
            thread.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        output_timed_out = any(thread.is_alive() for thread in threads)
        if output_timed_out:
            _terminate_process_group(
                process,
                terminate_grace_seconds=terminate_grace_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )
            final_drain_deadline = time.monotonic() + max(
                0.0,
                kill_grace_seconds,
            )
            for thread in threads:
                thread.join(
                    timeout=max(
                        0.0,
                        final_drain_deadline - time.monotonic(),
                    )
                )
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

        stdout, stdout_truncated = stdout_capture.render()
        stderr, stderr_truncated = stderr_capture.render()
        returncode = process.poll()
        timed_out = timed_out or output_timed_out
        if output_timed_out:
            status = "failed"
            summary = f"{kind} timed out waiting for process output"
        elif timed_out:
            status = "failed"
            summary = f"{kind} timed out after {timeout_seconds:g}s"
        elif returncode == 0:
            status = "passed"
            summary = f"{kind} passed"
        else:
            status = "failed"
            summary = f"{kind} failed with exit {returncode}"
        return SmokeOutcome(
            status=status,
            kind=kind,
            summary=summary,
            command=command_tuple,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_seconds=time.monotonic() - started,
        )


def has_assignment(text: str, key: str) -> bool:
    for line in text.splitlines():
        stripped = commentless_line(line).strip()
        if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
            return "=" in stripped
    return False


def has_plugin_support(text: str) -> bool:
    for line in text.splitlines():
        table = toml_table_name(line)
        if not table:
            continue
        if table == "skills.config":
            return True
        if table.startswith("marketplaces."):
            return True
        if table.startswith("plugins."):
            return True
        if table.startswith("hooks.state."):
            return True
    return False


def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result


def profile_home(store: Store, name: str) -> Path:
    manifest = store.load_manifest(name)
    return profile_home_binding(store, name, manifest).path


def plugin_snapshot_paths(store: Store, name: str, home: Path) -> list[Path]:
    snapshot = plugin_support_snapshot_name(name)
    return unique_paths([home / snapshot, store.profile_dir(name) / snapshot])


def collect_plugin_snapshot_problems(
    store: Store,
    name: str,
    home: Path,
    runtime_text: str,
) -> list[str]:
    if not has_plugin_support(runtime_text):
        return []
    problems: list[str] = []
    for path in plugin_snapshot_paths(store, name, home):
        if not path.exists():
            problems.append(f"{name}: plugin support snapshot is missing: {path}")
            continue
        try:
            validate_toml(path)
        except SwitchError as exc:
            problems.append(str(exc))
            continue
        if not has_plugin_support(path.read_text()):
            problems.append(
                f"{name}: plugin support snapshot has no marketplace/plugin/skill/hook blocks: {path}"
            )
    return problems


def refresh_plugin_support_snapshots(
    store: Store,
    name: str,
    home: Path,
    messages: list[str],
) -> None:
    config_path = home / "config.toml"
    if not config_path.exists():
        return
    if not has_plugin_support(config_path.read_text()):
        return
    paths = plugin_snapshot_paths(store, name, home)
    refresh_profile_plugin_support_snapshot(name, config_path, paths)
    for path in paths:
        messages.append(f"Refreshed plugin support snapshot: {path}")


def run_safe_repair(store: Store, name: str, home: Path) -> list[str]:
    messages: list[str] = []
    refresh_plugin_support_snapshots(store, name, home, messages)
    if missing_enabled_plugins(store, name):
        messages.append(f"Running plugin repair for {name}")
        repair_profile_plugins(store, name)
    return messages


def collect_active_state_problems(
    store: Store,
    name: str,
    home: Path,
    *,
    runtime_binding: RuntimeBinding | None = None,
    app_runtime_binding: RuntimeBinding | None = None,
    runtime_binding_problem: str | None = None,
    app_runtime_binding_problem: str | None = None,
    runtime_observation: RuntimeObservation | None = None,
    shared_configuration: object = _SHARED_CONFIGURATION_REPORT_UNSET,
    selection_snapshot: ActiveProfileSelectionSnapshot | None = None,
) -> list[str]:
    problems: list[str] = []
    if runtime_binding_problem is not None:
        problems.append(runtime_binding_problem)
    snapshot = selection_snapshot or read_active_profile_selection_snapshot(
        store.active_path,
        fallback_cli_profile=name,
    )
    if snapshot.record is None and snapshot.problem is None:
        problems.append(f"{name}: active profile record is missing")
        return problems
    if snapshot.problem is not None:
        problems.append(snapshot.problem)
        return problems
    active = snapshot.record
    selection = snapshot.selection
    if active is None or selection is None:
        problems.append("active.selection.invalid: active selection is unavailable")
        return problems

    shared_report = shared_configuration
    if shared_report is _SHARED_CONFIGURATION_REPORT_UNSET:
        shared_report = (
            shared_configuration_report(store, selection)
            if selection_uses_shared_configuration(selection)
            else None
        )
    if shared_report is not None:
        problems.extend(shared_configuration_problem_messages(shared_report))

    if selection.cli_profile != name:
        problems.append(
            f"active CLI profile is {selection.cli_profile}, expected {name}"
        )

    active_home = active.get("codex_home") or active.get("live_codex_home")
    if isinstance(active_home, str) and active_home:
        if not equivalent_paths(active_home, str(home)):
            problems.append(f"{name}: active CODEX_HOME is {active_home}, expected {home}")
    else:
        problems.append(f"{name}: active CODEX_HOME is missing")

    manifest = store.load_manifest(name)
    app_manifest = (
        manifest
        if selection.app_profile == name
        else store.load_manifest(selection.app_profile)
    )
    binding = runtime_binding
    if (
        runtime_binding_problem is None
        and
        binding is None
        and name in {"internal", "openai-official", "official"}
        and manifest_uses_canonical_binding(name, manifest)
    ):
        try:
            binding = resolve_store_runtime_binding(
                store,
                name,
                manifest=manifest,
            )
        except RuntimeBindingError as exc:
            problems.append(f"{exc.code}: {exc}")
    app_binding = app_runtime_binding
    if (
        app_runtime_binding_problem is not None
        and app_runtime_binding_problem != runtime_binding_problem
    ):
        problems.append(app_runtime_binding_problem)
    if (
        app_runtime_binding_problem is None
        and app_binding is None
        and selection.app_profile in {"internal", "openai-official", "official"}
        and manifest_uses_canonical_binding(selection.app_profile, app_manifest)
    ):
        try:
            app_binding = (
                binding
                if selection.app_profile == name
                else resolve_store_runtime_binding(
                    store,
                    selection.app_profile,
                    manifest=app_manifest,
                )
            )
        except RuntimeBindingError as exc:
            problems.append(f"{exc.code}: {exc}")
    observation = runtime_observation
    if observation is None and app_binding is not None:
        observation = collect_store_runtime_observation(store, app_binding)
    expected_shell_cli = (
        str(binding.shell_cli)
        if binding is not None
        else str(manifest.get("codex_bin", ""))
    )
    active_shell_cli = active.get("shell_cli_path")
    active_record_stale = False
    if expected_shell_cli and active_shell_cli and not equivalent_paths(
        str(active_shell_cli), expected_shell_cli
    ):
        active_record_stale = True
        problems.append(
            f"{name}: active shell CLI is {active_shell_cli}, expected {expected_shell_cli}"
        )

    active_app_cli = active.get("app_cli_path")
    expected_app_cli = (
        str(app_binding.desktop_cli)
        if app_binding is not None
        else profile_app_cli_path(app_manifest)
    )
    if expected_app_cli and active_app_cli and not equivalent_paths(
        str(active_app_cli), expected_app_cli
    ):
        active_record_stale = True
        problems.append(
            f"{selection.app_profile}: active App CLI is {active_app_cli}, "
            f"expected {expected_app_cli}"
        )
    if active_record_stale:
        problems.append(
            "binding.observation.active_stale: The active record differs "
            "from manifest-derived runtime intent."
        )

    launch_agent_cli = (
        observation.launch_agent_cli
        if observation is not None
        else read_launch_agent_cli_path(store.launch_agent_path)
    )
    if expected_app_cli and launch_agent_cli and not equivalent_paths(
        launch_agent_cli, expected_app_cli
    ):
        problems.append(
            f"{selection.app_profile}: LaunchAgent CODEX_CLI_PATH is "
            f"{launch_agent_cli}, expected {expected_app_cli}"
        )
    elif (
        binding is None
        and expected_app_cli
        and is_default_desktop_context(store)
    ):
        from codex_switch_paths import detect_current_app_cli_path

        gui_app_cli = detect_current_app_cli_path()
        if gui_app_cli and not equivalent_paths(gui_app_cli, expected_app_cli):
            problems.append(
                f"{selection.app_profile}: GUI CODEX_CLI_PATH is {gui_app_cli}, "
                f"expected {expected_app_cli}"
            )

    if app_binding is not None and observation is not None:
        problems.extend(attestation_problem_messages(app_binding, observation))
    elif expected_app_cli:
        problems.extend(
            running_desktop_problems(
                store,
                selection.app_profile,
                expected_app_cli,
            )
        )
    return problems


def collect_runtime_config_problems(store: Store, name: str, home: Path) -> list[str]:
    config_path = home / "config.toml"
    if not config_path.exists():
        return [f"{name}: runtime config is missing: {config_path}"]
    try:
        validate_toml(config_path)
    except SwitchError as exc:
        return [str(exc)]

    runtime_text = config_path.read_text()
    problems: list[str] = []
    if name == "openai-official" and has_assignment(runtime_text, "model_provider"):
        problems.append(
            "openai-official runtime config contains model_provider; "
            "official profile should not be seeded from internal provider settings"
        )
    problems.extend(collect_plugin_snapshot_problems(store, name, home, runtime_text))
    return problems


def run_profile_command(
    codex_bin: str,
    home: Path,
    args: list[str],
    *,
    kind: str,
    timeout_seconds: float = DEFAULT_SMOKE_TIMEOUT_SECONDS,
    max_stream_bytes: int = DEFAULT_SMOKE_MAX_STREAM_BYTES,
) -> SmokeOutcome:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    return run_bounded_process(
        [codex_bin, *args],
        kind=kind,
        env=env,
        timeout_seconds=timeout_seconds,
        max_stream_bytes=max_stream_bytes,
    )


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def safe_header_values(output: str, header: str) -> list[str]:
    pattern = re.compile(rf"(?im)^\s*{re.escape(header)}\s*:\s*([^,\r\n ]+)")
    return unique_preserve_order(pattern.findall(output))


def responses_tool_smoke_args() -> list[str]:
    return [
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-s",
        "read-only",
        "-C",
        str(Path.cwd()),
        RESPONSES_TOOL_SMOKE_PROMPT,
    ]


def app_server_smoke_args() -> list[str]:
    return ["app-server", "--analytics-default-enabled"]


def app_server_initialize_message() -> dict[str, object]:
    return {
        "id": APP_SERVER_INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "codex-switch-smoke",
                "version": "0.0.0",
            },
            "capabilities": {
                "experimentalApi": True,
            },
        },
    }


def app_server_initialized_message() -> dict[str, object]:
    return {"method": "initialized"}


def app_server_plugin_list_message() -> dict[str, object]:
    return {
        "id": APP_SERVER_PLUGIN_LIST_ID,
        "method": "plugin/list",
        "params": {
            "marketplaceKinds": [
                "local",
                "vertical",
                "shared-with-me",
                "created-by-me-remote",
            ],
        },
    }


def write_app_server_message(
    process: subprocess.Popen[bytes],
    message: dict[str, object],
) -> None:
    if process.stdin is None:
        raise BrokenPipeError("app-server stdin is unavailable")
    process.stdin.write(
        (json.dumps(message, separators=(",", ":")) + "\n").encode()
    )
    process.stdin.flush()


def response_message_outcome_problem(
    message: dict[str, object],
    request_id: str,
    *,
    allow_plugin_auth_error: bool = False,
) -> str | None:
    error = message.get("error")
    if error is not None:
        error_text = json.dumps(error, sort_keys=True, separators=(",", ":"))
        if (
            allow_plugin_auth_error
            and "authentication required" in error_text.lower()
            and "plugin" in error_text.lower()
        ):
            return None
        return f"`{request_id}` returned error: {error_text}"
    if "result" not in message:
        return f"`{request_id}` response has neither result nor error"
    return None


def output_excerpt(lines: list[str]) -> str:
    selected = [line for line in lines if line][-APP_SERVER_OUTPUT_LINE_LIMIT:]
    if not selected:
        return "<no output>"
    return " | ".join(selected)


class AppServerSmokeProtocol:
    def __init__(
        self,
        *,
        max_line_bytes: int = APP_SERVER_MAX_LINE_BYTES,
        output_line_limit: int = APP_SERVER_OUTPUT_LINE_LIMIT,
    ) -> None:
        if max_line_bytes <= 0:
            raise ValueError("max_line_bytes must be positive")
        if output_line_limit <= 0:
            raise ValueError("output_line_limit must be positive")
        self.max_line_bytes = max_line_bytes
        self.state = "await_initialize"
        self.problem: str | None = None
        self.closed = False
        self.lines: deque[str] = deque(maxlen=output_line_limit)
        self._pending = bytearray()
        self._discarding_oversized = False
        self._condition = threading.Condition()

    def _set_problem(self, problem: str) -> None:
        if self.problem is None:
            self.problem = problem

    def _accept_message(self, message: dict[str, object]) -> None:
        request_id = message.get("id")
        if request_id == APP_SERVER_INITIALIZE_ID:
            if self.state != "await_initialize":
                self._set_problem(
                    "duplicate or out-of-order initialize response"
                )
                return
            problem = response_message_outcome_problem(
                message,
                APP_SERVER_INITIALIZE_ID,
            )
            if problem is not None:
                self._set_problem(problem)
                return
            self.state = "initialize_complete"
            return
        if request_id == APP_SERVER_PLUGIN_LIST_ID:
            if self.state == "await_initialize":
                self._set_problem(
                    "plugin response arrived before initialize completed"
                )
                return
            if self.state != "await_plugin":
                self._set_problem(
                    "plugin response arrived before plugin request"
                )
                return
            problem = response_message_outcome_problem(
                message,
                APP_SERVER_PLUGIN_LIST_ID,
                allow_plugin_auth_error=True,
            )
            if problem is not None:
                self._set_problem(problem)
                return
            self.state = "plugin_complete"

    def _accept_line(self, raw_line: bytes) -> None:
        line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
        if line:
            self.lines.append(line)
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            self._set_problem(f"malformed app-server JSON line: {error}")
            return
        if not isinstance(message, dict):
            self._set_problem("malformed app-server JSON line: root is not an object")
            return
        self._accept_message(message)

    def feed(self, chunk: bytes) -> None:
        with self._condition:
            start = 0
            while start < len(chunk):
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    part = chunk[start:]
                    complete = False
                    start = len(chunk)
                else:
                    part = chunk[start:newline]
                    complete = True
                    start = newline + 1
                if self._discarding_oversized:
                    if complete:
                        self._discarding_oversized = False
                    continue
                if len(self._pending) + len(part) > self.max_line_bytes:
                    self._pending.clear()
                    self._discarding_oversized = not complete
                    self.lines.append("<oversized app-server line omitted>")
                    self._set_problem(
                        "oversized app-server JSON line exceeded "
                        f"{self.max_line_bytes} bytes"
                    )
                    continue
                self._pending.extend(part)
                if complete:
                    line = bytes(self._pending)
                    self._pending.clear()
                    self._accept_line(line)
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if self._pending and not self._discarding_oversized:
                line = bytes(self._pending)
                self._pending.clear()
                self._accept_line(line)
            self.closed = True
            self._condition.notify_all()

    def expect_plugin_response(self) -> str | None:
        with self._condition:
            if self.problem is not None:
                return self.problem
            if self.state != "initialize_complete":
                self._set_problem(
                    "plugin request attempted before initialize completed"
                )
                return self.problem
            self.state = "await_plugin"
            self._condition.notify_all()
            return None

    def wait_for_state(
        self,
        process: subprocess.Popen[bytes],
        expected_state: str,
        request_id: str,
        *,
        timeout_seconds: float,
    ) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                if self.problem is not None:
                    return self.problem
                if self.state == expected_state:
                    return None
                returncode = process.poll()
                if returncode is not None:
                    return (
                        f"app-server exited before `{request_id}` response "
                        f"(exit {returncode})"
                    )
                if self.closed:
                    return f"app-server stdout closed before `{request_id}` response"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return f"timed out waiting for `{request_id}` response"
                self._condition.wait(timeout=min(0.05, remaining))

    def wait_for_settle(
        self,
        process: subprocess.Popen[bytes],
        *,
        settle_seconds: float,
    ) -> str | None:
        deadline = time.monotonic() + settle_seconds
        with self._condition:
            while True:
                if self.problem is not None:
                    return self.problem
                returncode = process.poll()
                if returncode is not None:
                    return (
                        "app-server exited during startup settle window "
                        f"(exit {returncode})"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=min(0.05, remaining))


def read_app_server_stdout(
    stream,
    protocol: AppServerSmokeProtocol,
) -> None:
    if stream is None:
        protocol.close()
        return
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                return
            protocol.feed(chunk)
    except (OSError, ValueError):
        return
    finally:
        protocol.close()


def terminate_app_server_smoke(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if _process_group_exists(process):
        _terminate_process_group(
            process,
            terminate_grace_seconds=0.5,
            kill_grace_seconds=1.0,
        )


def run_app_server_smoke(
    codex_bin: str,
    home: Path,
    *,
    extra_env: dict[str, str] | None = None,
    isolate_home: bool = False,
    response_timeout_seconds: float = APP_SERVER_RESPONSE_TIMEOUT_SECONDS,
    settle_seconds: float = APP_SERVER_SETTLE_SECONDS,
    max_line_bytes: int = APP_SERVER_MAX_LINE_BYTES,
) -> tuple[int, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    if isolate_home:
        home = home.resolve()
        env.update({"HOME": str(home), "CODEX_HOME": str(home),
                    "XDG_CONFIG_HOME": str(home / ".config")})
    protocol = AppServerSmokeProtocol(max_line_bytes=max_line_bytes)
    stderr_capture = _BoundedByteCapture(APP_SERVER_MAX_STDERR_BYTES)
    try:
        process = subprocess.Popen(
            [codex_bin, *app_server_smoke_args()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
            cwd=home if isolate_home else None,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, f"not found: {codex_bin}"
    except OSError as exc:
        return 1, str(exc)

    threads: list[threading.Thread] = []
    with managed_probe_process(process, threads=threads, terminate=terminate_app_server_smoke):
        stdout_thread = threading.Thread(
            target=read_app_server_stdout,
            args=(process.stdout, protocol),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_bounded_stream,
            args=(process.stderr, stderr_capture),
            daemon=True,
        )
        threads.extend((stdout_thread, stderr_thread))
        stdout_thread.start()
        stderr_thread.start()
        reason: str | None = None
        try:
            write_app_server_message(process, app_server_initialize_message())
            reason = protocol.wait_for_state(
                process,
                "initialize_complete",
                APP_SERVER_INITIALIZE_ID,
                timeout_seconds=response_timeout_seconds,
            )
            if reason is None:
                reason = protocol.expect_plugin_response()
            if reason is None:
                write_app_server_message(process, app_server_initialized_message())
                write_app_server_message(process, app_server_plugin_list_message())
                reason = protocol.wait_for_state(
                    process,
                    "plugin_complete",
                    APP_SERVER_PLUGIN_LIST_ID,
                    timeout_seconds=response_timeout_seconds,
                )
            if reason is None:
                reason = protocol.wait_for_settle(
                    process,
                    settle_seconds=settle_seconds,
                )
        except (BrokenPipeError, OSError) as exc:
            returncode = process.poll()
            reason = f"unable to write app-server smoke request: {exc}"
            if returncode is not None:
                reason = f"{reason} (exit {returncode})"

    if reason is not None:
        stderr, _stderr_truncated = stderr_capture.render()
        return (
            1,
            f"{reason}; stdout: {output_excerpt(list(protocol.lines))}; "
            f"stderr: {output_excerpt(stderr.splitlines())}",
        )
    return 0, "app-server smoke passed"


def _managed_capability_receipt_problem(
    binding: RuntimeBinding,
    manifest: Mapping[str, object],
) -> str | None:
    expected_path = capability_receipt_path_for_launcher(
        binding.desktop_cli
    )
    raw_path = manifest.get("app_capability_receipt_path")
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or Path(raw_path).expanduser() != expected_path
    ):
        return (
            "managed capability receipt path drift: "
            f"expected {expected_path}, observed {raw_path or '<missing>'}"
        )
    expected_payload_sha256 = manifest.get(
        "app_capability_receipt_sha256"
    )
    expected_schema_sha256 = manifest.get("app_schema_sha256")
    if (
        not isinstance(expected_payload_sha256, str)
        or not isinstance(expected_schema_sha256, str)
    ):
        return "managed capability receipt digest metadata is missing"
    artifact = load_capability_receipt_artifact(
        binding.backend_cli,
        receipt_path=expected_path,
        expected_payload_sha256=expected_payload_sha256,
        expected_schema_sha256=expected_schema_sha256,
    )
    if artifact is None:
        return (
            "managed capability receipt payload, schema, or backend digest "
            "drift"
        )
    return None


def run_binding_app_server_smoke(
    binding: RuntimeBinding,
    manifest: Mapping[str, object],
) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(
        prefix="codex-switch-app-server-smoke-"
    ) as temp_dir:
        smoke_root = Path(temp_dir)
        smoke_home = smoke_root / "home"
        smoke_home.mkdir(mode=0o700)
        (smoke_home / "config.toml").write_text("")
        extra_env: dict[str, str] | None = None
        child_receipt_path: Path | None = None
        if binding.requires_proxy:
            receipt_problem = _managed_capability_receipt_problem(
                binding,
                manifest,
            )
            if receipt_problem is not None:
                return 1, receipt_problem
            child_receipt_path = smoke_root / "proxy-child.json"
            extra_env = {
                "CODEX_SWITCH_REBIND_SMOKE": "1",
                "CODEX_SWITCH_REBIND_SMOKE_HOME": str(smoke_home),
                "CODEX_SWITCH_PROXY_CHILD_RECEIPT": str(
                    child_receipt_path
                ),
            }
        code, output = run_app_server_smoke(
            str(binding.desktop_cli),
            smoke_home,
            extra_env=extra_env,
        )
        if code != 0 or not binding.requires_proxy:
            return code, output
        assert child_receipt_path is not None
        try:
            child_receipt = read_json(child_receipt_path)
        except (OSError, SwitchError, ValueError, TypeError) as exc:
            return (
                1,
                "managed app-server smoke produced no valid proxy child "
                f"receipt: {exc}",
            )
        if not isinstance(child_receipt, dict):
            return 1, "managed app-server smoke proxy child receipt is invalid"
        observed_backend = child_receipt.get("codex_bin")
        observed_args = child_receipt.get("args")
        if (
            not isinstance(observed_backend, str)
            or Path(observed_backend).expanduser().resolve()
            != binding.backend_cli.resolve()
        ):
            return (
                1,
                "managed app-server smoke child backend drift: "
                f"expected {binding.backend_cli}, observed "
                f"{observed_backend or '<missing>'}",
            )
        expected_args = app_server_smoke_args()
        if observed_args != expected_args:
            return (
                1,
                "managed app-server smoke child arguments drift: "
                f"expected {expected_args}, observed {observed_args}",
            )
        if child_receipt.get("codex_home") != str(smoke_home):
            return (
                1,
                "managed app-server smoke did not retain its isolated home",
            )
        expected_receipt_path = str(
            capability_receipt_path_for_launcher(binding.desktop_cli)
        )
        expected_schema_sha256 = manifest.get("app_schema_sha256")
        expected_receipt_sha256 = manifest.get(
            "app_capability_receipt_sha256"
        )
        if (
            child_receipt.get("capability_receipt_path")
            != expected_receipt_path
            or child_receipt.get("expected_schema_sha256")
            != expected_schema_sha256
            or child_receipt.get("expected_receipt_sha256")
            != expected_receipt_sha256
            or child_receipt.get("config_write_proven") is not True
        ):
            return (
                1,
                "managed capability receipt binding drift was observed in "
                "the proxy child",
            )
        return 0, output


def azure_responses_resource_mismatch_diagnostic(output: str) -> dict[str, object] | None:
    if AZURE_RESPONSES_RESOURCE_MISMATCH not in output:
        return None
    diagnostic: dict[str, object] = {
        "kind": "azure_responses_resource_mismatch",
        "message": "Responses context follow-up must stay on the same Azure OpenAI resource",
    }
    for header, key in SAFE_SMOKE_HEADERS.items():
        diagnostic[key] = safe_header_values(output, header)
    return diagnostic


def responses_tool_smoke_diagnostic(output: str) -> dict[str, object] | None:
    diagnostic = azure_responses_resource_mismatch_diagnostic(output)
    if diagnostic is not None:
        return diagnostic
    item_ids = list(
        dict.fromkeys(RESPONSES_REASONING_ITEM_NOT_FOUND.findall(output))
    )
    if not item_ids:
        return None
    diagnostic = {
        "kind": "responses_reasoning_item_unavailable",
        "message": (
            "Stateless Responses continuation requires encrypted reasoning "
            "content or stable upstream item routing"
        ),
        "item_ids": item_ids,
    }
    for header, key in SAFE_SMOKE_HEADERS.items():
        diagnostic[key] = safe_header_values(output, header)
    return diagnostic


def format_responses_tool_smoke_problem(
    name: str,
    diagnostic: dict[str, object],
) -> str:
    if diagnostic.get("kind") == "responses_reasoning_item_unavailable":
        parts = [
            f"{name}: internal Responses reasoning continuity failure",
            str(diagnostic["message"]),
        ]
        item_ids = diagnostic.get("item_ids")
        if isinstance(item_ids, list) and item_ids:
            parts.append(
                f"unavailable reasoning item: {', '.join(str(item) for item in item_ids)}"
            )
    else:
        parts = [
            f"{name}: internal Responses resource-stickiness failure",
            "Responses context follow-up must stay on the same Azure OpenAI resource",
        ]
    accounts = diagnostic.get("accounts")
    if isinstance(accounts, list) and accounts:
        parts.append(f"x-account-id route: {' -> '.join(str(account) for account in accounts)}")
    deployments = diagnostic.get("deployments")
    if isinstance(deployments, list) and deployments:
        parts.append(f"deployment: {', '.join(str(item) for item in deployments)}")
    model_request_ids = diagnostic.get("model_request_ids")
    if isinstance(model_request_ids, list) and model_request_ids:
        parts.append(f"x-model-request-id: {', '.join(str(item) for item in model_request_ids)}")
    tt_log_ids = diagnostic.get("tt_log_ids")
    if isinstance(tt_log_ids, list) and tt_log_ids:
        parts.append(f"x-tt-logid: {', '.join(str(item) for item in tt_log_ids)}")
    return "; ".join(parts)


def recorded_smoke_args(label: str, args: list[str]) -> list[str]:
    if label in {"exec smoke", "Responses tool smoke"} and args:
        return [*args[:-1], "<prompt omitted>"]
    return list(args)


def runtime_smoke_problems(
    store: Store,
    name: str,
    home: Path,
    *,
    app_server_smoke: bool = False,
    exec_smoke: str | None = None,
    runtime_smoke: bool = False,
    responses_tool_smoke: bool = False,
    runtime_binding: RuntimeBinding | None = None,
    app_runtime_binding: RuntimeBinding | None = None,
) -> tuple[
    list[str],
    list[dict[str, object]],
    list[SmokeOutcome],
]:
    manifest = store.load_manifest(name)
    codex_bin = (
        str(runtime_binding.backend_cli)
        if runtime_binding is not None
        else str(manifest.get("codex_bin", ""))
    )
    if (
        name == "internal"
        and manifest_has_internal_cli_generation(manifest)
    ):
        codex_bin = str(store.bin_dir / "codex")
    runtime_home = (
        runtime_binding.codex_home
        if runtime_binding is not None
        else home
    )
    app_binding = (
        app_runtime_binding
        if app_runtime_binding is not None
        else runtime_binding
    )
    app_manifest = (
        store.load_manifest(app_binding.profile)
        if app_binding is not None
        else manifest
    )
    app_name = app_binding.profile if app_binding is not None else name
    if not codex_bin:
        return [f"{name}: missing codex_bin for runtime smoke"], [], []

    problems: list[str] = []
    smoke_diagnostics: list[dict[str, object]] = []
    smoke_outcomes: list[SmokeOutcome] = []
    commands: list[tuple[str, list[str]]] = []
    if runtime_smoke:
        commands.extend(
            [
                ("runtime smoke", ["--version"]),
                ("runtime smoke", ["plugin", "list", "--json"]),
            ]
        )
    if exec_smoke is not None:
        commands.append(("exec smoke", ["exec", "--json", exec_smoke]))
    if responses_tool_smoke:
        commands.append(("Responses tool smoke", responses_tool_smoke_args()))
    for label, args in commands:
        outcome = run_profile_command(
            codex_bin,
            runtime_home,
            args,
            kind=label,
        )
        recorded_args = recorded_smoke_args(label, args)
        if recorded_args != args:
            outcome = replace(
                outcome,
                command=(codex_bin, *recorded_args),
            )
        smoke_outcomes.append(outcome)
        if outcome.status != "passed":
            output = "\n".join(
                part
                for part in (outcome.stdout.strip(), outcome.stderr.strip())
                if part
            )
            if label == "Responses tool smoke":
                diagnostic = responses_tool_smoke_diagnostic(output)
                if diagnostic is not None:
                    smoke_diagnostics.append(diagnostic)
                    problems.append(format_responses_tool_smoke_problem(name, diagnostic))
                    continue
            detail = output or outcome.summary
            problems.append(
                f"{name}: {label} failed for "
                f"`{codex_bin} {' '.join(recorded_args)}` "
                f"({outcome.status}, exit {outcome.returncode}): {detail}"
            )
    if app_server_smoke:
        started = time.monotonic()
        app_server_bin = (
            str(app_binding.desktop_cli)
            if app_binding is not None
            else codex_bin
        )
        if app_binding is not None:
            code, output = run_binding_app_server_smoke(
                app_binding,
                app_manifest,
            )
        else:
            code, output = run_app_server_smoke(
                app_server_bin,
                runtime_home,
            )
        app_server_outcome = SmokeOutcome(
            status=(
                "passed"
                if code == 0
                else "not_run"
                if code == 127
                else "failed"
            ),
            kind="app-server smoke",
            summary=(
                "app-server smoke passed"
                if code == 0
                else "app-server smoke not run: binary not found"
                if code == 127
                else "app-server smoke failed"
            ),
            command=(app_server_bin, *app_server_smoke_args()),
            returncode=code,
            stdout=output,
            stderr="",
            timed_out="timed out" in output.lower(),
            stdout_truncated=False,
            stderr_truncated=False,
            duration_seconds=time.monotonic() - started,
        )
        smoke_outcomes.append(app_server_outcome)
        if code != 0:
            problems.append(
                f"{app_name}: app-server smoke failed for "
                f"`{app_server_bin} {' '.join(app_server_smoke_args())}` "
                f"(exit {code}): {output}"
            )
    return problems, smoke_diagnostics, smoke_outcomes


def collect_verification_problems(
    store: Store,
    name: str,
    *,
    app_server_smoke: bool = False,
    runtime_smoke: bool = False,
    exec_smoke: str | None = None,
    responses_tool_smoke: bool = False,
    runtime_binding: RuntimeBinding | None = None,
    app_runtime_binding: RuntimeBinding | None = None,
    runtime_binding_problem: str | None = None,
    app_runtime_binding_problem: str | None = None,
    selection_problem: str | None = None,
    runtime_observation: RuntimeObservation | None = None,
    parity_report: ParityReport | None = None,
    shared_configuration: object = _SHARED_CONFIGURATION_REPORT_UNSET,
    selection_snapshot: ActiveProfileSelectionSnapshot | None = None,
) -> tuple[
    list[str],
    list[dict[str, object]],
    list[SmokeOutcome],
]:
    binding = runtime_binding
    binding_problem = runtime_binding_problem
    if binding is None and binding_problem is None:
        manifest = store.load_manifest(name)
        if (
            name in {"internal", "openai-official", "official"}
            and manifest_uses_canonical_binding(name, manifest)
        ):
            try:
                binding = resolve_store_runtime_binding(
                    store,
                    name,
                    manifest=manifest,
                )
            except RuntimeBindingError as exc:
                binding_problem = f"{exc.code}: {exc}"
    app_binding = app_runtime_binding
    app_binding_problem = app_runtime_binding_problem
    effective_selection_problem = selection_problem
    snapshot = selection_snapshot or read_active_profile_selection_snapshot(
        store.active_path,
        fallback_cli_profile=name,
    )
    if snapshot.problem is not None or snapshot.selection is None:
        effective_selection_problem = effective_selection_problem or (
            snapshot.problem
            or "active.selection.invalid: active selection is unavailable"
        )
        app_profile = name
        app_binding = None
    else:
        app_profile = snapshot.selection.app_profile
    if app_binding is None and app_binding_problem is None:
        if app_profile == name:
            app_binding = binding
            app_binding_problem = binding_problem
        elif app_profile in {"internal", "openai-official", "official"}:
            app_manifest = store.load_manifest(app_profile)
            if manifest_uses_canonical_binding(app_profile, app_manifest):
                try:
                    app_binding = resolve_store_runtime_binding(
                        store,
                        app_profile,
                        manifest=app_manifest,
                    )
                except RuntimeBindingError as exc:
                    app_binding_problem = f"{exc.code}: {exc}"
    home = binding.codex_home if binding is not None else profile_home(store, name)
    problems: list[str] = []
    smoke_diagnostics: list[dict[str, object]] = []
    smoke_outcomes: list[SmokeOutcome] = []
    problems.extend(
        collect_active_state_problems(
            store,
            name,
            home,
            runtime_binding=binding,
            app_runtime_binding=app_binding,
            runtime_binding_problem=binding_problem,
            app_runtime_binding_problem=app_binding_problem,
            runtime_observation=runtime_observation,
            shared_configuration=shared_configuration,
            selection_snapshot=snapshot,
        )
    )
    if (
        effective_selection_problem is not None
        and effective_selection_problem not in problems
    ):
        problems.append(effective_selection_problem)
    problems.extend(collect_runtime_config_problems(store, name, home))
    if name == "internal" and app_profile == "internal":
        effective_parity_report = (
            parity_report
            if parity_report is not None
            else collect_parity_report(store, binding)
        )
        parity_diagnostic = parity_report_data(effective_parity_report)
        parity_diagnostic["kind"] = "parity"
        smoke_diagnostics.append(parity_diagnostic)
        problems.extend(parity_problem_messages(effective_parity_report))
    if (
        effective_selection_problem is None
        and binding_problem is None
        and app_binding_problem is None
        and (
            app_server_smoke
            or runtime_smoke
            or exec_smoke is not None
            or responses_tool_smoke
        )
    ):
        (
            smoke_problems,
            runtime_diagnostics,
            smoke_outcomes,
        ) = runtime_smoke_problems(
            store,
            name,
            home,
            app_server_smoke=app_server_smoke,
            exec_smoke=exec_smoke,
            runtime_smoke=runtime_smoke,
            responses_tool_smoke=responses_tool_smoke,
            runtime_binding=binding,
            app_runtime_binding=app_binding,
        )
        problems.extend(smoke_problems)
        smoke_diagnostics.extend(runtime_diagnostics)
    return problems, smoke_diagnostics, smoke_outcomes


def _read_parity_receipt_candidate(path: Path) -> ParityReceipt:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > MAX_PARITY_RECEIPT_BYTES
        ):
            raise ParityValidationError(
                "parity.receipt.malformed",
                "Parity receipt identity, mode, or size is invalid.",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PARITY_RECEIPT_BYTES:
                raise ParityValidationError(
                    "parity.receipt.malformed",
                    "Parity receipt exceeds the persisted size limit.",
                )
            chunks.append(chunk)
        document = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError("parity receipt root is not an object")
        return ParityReceipt.from_payload(document)
    except FileNotFoundError as exc:
        raise ParityValidationError(
            "parity.receipt.missing",
            "Parity receipt is missing.",
        ) from exc
    except ParityValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ParityValidationError(
            "parity.receipt.malformed",
            "Parity receipt is malformed.",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _regular_file_sha256(
    path: Path,
    *,
    code: str,
    label: str,
) -> str:
    digest = hashlib.sha256()
    descriptor = -1
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular non-symlink file")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise OSError("file identity changed before reading")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = path.lstat()
    except OSError as exc:
        raise ParityValidationError(
            code,
            f"{label} is unavailable.",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ParityValidationError(
            code,
            f"{label} changed while reading.",
        )
    return digest.hexdigest()


def _official_bundle_version(reference: OfficialReference) -> str:
    try:
        with (
            reference.bundle_root / "Contents" / "Info.plist"
        ).open("rb") as handle:
            document = plistlib.load(handle)
    except (
        OSError,
        plistlib.InvalidFileException,
        TypeError,
        ValueError,
    ) as exc:
        raise ParityValidationError(
            "parity.reference.stale",
            "Official bundle version evidence is unavailable.",
        ) from exc
    bundle_id = (
        document.get("CFBundleIdentifier")
        if isinstance(document, Mapping)
        else None
    )
    version = (
        document.get("CFBundleShortVersionString")
        if isinstance(document, Mapping)
        else None
    )
    if bundle_id != reference.bundle_id or version != reference.bundle_version:
        raise ParityValidationError(
            "parity.reference.stale",
            "Official bundle identity changed after parity preparation.",
        )
    return str(version)


def _validate_parity_fingerprints(
    store: Store,
    binding: RuntimeBinding,
    receipt: ParityReceipt,
) -> None:
    reference = receipt.official_reference
    if (
        _regular_file_sha256(
            reference.bundled_cli,
            code="parity.reference.stale",
            label="Official bundled CLI",
        )
        != reference.binary_sha256
    ):
        raise ParityValidationError(
            "parity.reference.stale",
            "Official bundled CLI changed after parity preparation.",
        )
    _official_bundle_version(reference)

    internal = receipt.internal_fingerprint
    try:
        bound_backend = binding.backend_cli.resolve(strict=True)
        receipt_backend = internal.backend_cli.resolve(strict=True)
    except OSError as exc:
        raise ParityValidationError(
            "parity.receipt.stale",
            "Internal backend evidence is unavailable.",
        ) from exc
    if (
        bound_backend != receipt_backend
        or _regular_file_sha256(
            bound_backend,
            code="parity.receipt.stale",
            label="Internal backend",
        )
        != internal.binary_sha256
    ):
        raise ParityValidationError(
            "parity.receipt.stale",
            "Internal backend changed after parity preparation.",
        )
    if (
        _regular_file_sha256(
            internal.source_catalog,
            code="parity.overlay.source_stale",
            label="Parity source catalog",
        )
        != internal.source_catalog_sha256
    ):
        raise ParityValidationError(
            "parity.overlay.source_stale",
            "Parity source catalog changed after preparation.",
        )

    from codex_switch_home_select import resolve_runtime_homes

    try:
        official_home = resolve_runtime_homes(store).official.path
    except SwitchError as exc:
        raise ParityValidationError(
            "parity.config.source_stale", "Parity runtime home binding is invalid."
        ) from exc
    config_paths = {
        "profile": store.profile_dir("internal") / "config.toml",
        "shared": official_home / "config.toml",
        "runtime": binding.codex_home / "config.toml",
    }
    for name, expected_sha256 in internal.config_sha256s:
        path = config_paths.get(name)
        if (
            path is None
            or _regular_file_sha256(
                path,
                code="parity.config.source_stale",
                label=f"Parity {name} config",
            )
            != expected_sha256
        ):
            raise ParityValidationError(
                "parity.config.source_stale",
                f"Parity {name} config changed after preparation.",
            )


def validate_prepared_parity_config(
    store: Store,
    projection: ConfigProjection,
    runtime_payload: bytes,
) -> None:
    """Reject changed prepared inputs before activation writes any state."""
    manifest = store.load_manifest("internal")
    profile_dir = store.profile_dir("internal")
    receipt_path = profile_dir / "parity" / "receipt.json"
    if manifest.get("parity_receipt_path") != str(receipt_path):
        raise ParityValidationError("parity.receipt.malformed", "Parity receipt path is invalid.")
    candidate = _read_parity_receipt_candidate(receipt_path)
    artifact = load_parity_receipt_artifact(
        profile_dir=profile_dir,
        expected_payload_sha256=manifest.get("parity_receipt_sha256", ""),
        expected_official_reference=candidate.official_reference,
        expected_internal_fingerprint=candidate.internal_fingerprint,
        expected_adapter_rule_set_sha256=protocol_adapter_rule_set_digest(),
    )
    config_hashes = {
        "profile" if path == projection.config_inputs.profile_config else "shared":
        hashlib.sha256(payload).hexdigest()
        for path, payload in projection.payloads
    }
    config_hashes["runtime"] = hashlib.sha256(runtime_payload).hexdigest()
    if (
        not artifact.receipt.healthy
        or config_hashes != dict(artifact.receipt.internal_fingerprint.config_sha256s)
    ):
        raise ParityValidationError(
            "parity.config.source_stale",
            "Prepared parity configuration changed; run set-bin internal with the current backend before switching.",
        )


def _runtime_generation_parity_error(
    error: RuntimeBindingError,
) -> tuple[str, str]:
    message = str(error)
    lowered = message.lower()
    if "overlay" in lowered:
        return "parity.overlay.source_stale", message
    if "config" in lowered:
        return "parity.config.source_stale", message
    if "parity receipt" in lowered and (
        "json" in lowered
        or "schema" in lowered
        or "digest" in lowered
        or "path" in lowered
    ):
        return "parity.receipt.malformed", message
    return "parity.receipt.stale", message


def collect_parity_report(
    store: Store,
    binding: RuntimeBinding | None,
) -> ParityReport:
    try:
        manifest = store.load_manifest("internal")
    except SwitchError as exc:
        return parity_error_report(
            code="parity.receipt.missing",
            message=str(exc),
            profile_dir=store.profile_dir("internal"),
            backend_cli=(
                binding.backend_cli if binding is not None else None
            ),
        )

    raw_receipt_path = manifest.get("parity_receipt_path")
    expected_payload_sha256 = manifest.get("parity_receipt_sha256")
    if (
        not isinstance(raw_receipt_path, str)
        or not raw_receipt_path
        or not isinstance(expected_payload_sha256, str)
        or not expected_payload_sha256
    ):
        return parity_error_report(
            code="parity.receipt.missing",
            message="Internal manifest has no complete parity receipt evidence.",
            profile_dir=store.profile_dir("internal"),
            backend_cli=(
                binding.backend_cli if binding is not None else None
            ),
        )

    receipt_path = Path(raw_receipt_path).expanduser()
    expected_receipt_path = (
        store.profile_dir("internal") / "parity" / "receipt.json"
    )
    if (
        not receipt_path.is_absolute()
        or receipt_path.resolve(strict=False)
        != expected_receipt_path.resolve(strict=False)
    ):
        return parity_error_report(
            code="parity.receipt.malformed",
            message="Internal manifest parity receipt path is invalid.",
            profile_dir=store.profile_dir("internal"),
            backend_cli=(
                binding.backend_cli if binding is not None else None
            ),
        )

    try:
        candidate = _read_parity_receipt_candidate(receipt_path)
        adapter_sha256 = protocol_adapter_rule_set_digest()
        if (
            candidate.adapter_rule_set_sha256 != adapter_sha256
            or manifest.get("parity_adapter_rule_set_sha256")
            != adapter_sha256
        ):
            raise ParityValidationError(
                "parity.preparation.adapter_stale",
                "Protocol Adapter rules changed after parity preparation.",
            )
        if (
            manifest.get("parity_official_reference_sha256")
            != candidate.official_reference.fingerprint_sha256
        ):
            raise ParityValidationError(
                "parity.reference.stale",
                "Official reference fingerprint does not match the manifest.",
            )
        if (
            manifest.get("parity_internal_fingerprint_sha256")
            != candidate.internal_fingerprint.fingerprint_sha256
        ):
            raise ParityValidationError(
                "parity.receipt.stale",
                "Internal fingerprint does not match the manifest.",
            )
        if (
            manifest.get("parity_policy_version")
            != str(candidate.policy_version)
        ):
            raise ParityValidationError(
                "parity.receipt.stale",
                "Parity policy version does not match the manifest.",
            )
        if (
            manifest.get("parity_source_catalog_path")
            != str(candidate.internal_fingerprint.source_catalog)
            or manifest.get("parity_source_catalog_sha256")
            != candidate.internal_fingerprint.source_catalog_sha256
        ):
            raise ParityValidationError(
                "parity.overlay.source_stale",
                "Source catalog evidence does not match the manifest.",
            )
        artifact = load_parity_receipt_artifact(
            profile_dir=store.profile_dir("internal"),
            expected_payload_sha256=expected_payload_sha256,
            expected_official_reference=candidate.official_reference,
            expected_internal_fingerprint=candidate.internal_fingerprint,
            expected_adapter_rule_set_sha256=adapter_sha256,
        )
        receipt = artifact.receipt
        report = ParityReport(
            healthy=receipt.healthy,
            policy_version=receipt.policy_version,
            official_reference=receipt.official_reference,
            internal_fingerprint=receipt.internal_fingerprint,
            findings=receipt.findings,
            synchronization_queue=receipt.synchronization_queue,
        )
        if not report.healthy:
            return report
        if binding is None:
            try:
                binding = resolve_store_runtime_binding(
                    store,
                    "internal",
                    manifest=manifest,
                )
            except RuntimeBindingError as exc:
                raise ParityValidationError(
                    "parity.receipt.stale",
                    "Canonical internal Runtime Binding is unavailable.",
                ) from exc
        try:
            generation = validate_internal_runtime_generation(
                store_root=store.root,
                fallback_home=binding.codex_home,
                launcher_path=binding.desktop_cli,
                expected_backend=binding.backend_cli,
                expected_home=binding.codex_home,
            )
        except RuntimeBindingError as exc:
            code, message = _runtime_generation_parity_error(exc)
            raise ParityValidationError(code, message) from exc
        if (
            generation.parity_receipt_path != artifact.receipt_path
            or generation.parity_receipt_sha256 != artifact.payload_sha256
        ):
            raise ParityValidationError(
                "parity.receipt.stale",
                "Runtime generation does not use the loaded parity receipt.",
            )
        _validate_parity_fingerprints(store, binding, receipt)
        return report
    except ParityValidationError as exc:
        code = exc.code
        if code.startswith("parity.receipt.") and code not in {
            "parity.receipt.missing",
            "parity.receipt.stale",
        }:
            code = "parity.receipt.malformed"
        return parity_error_report(
            code=code,
            message=str(exc),
            profile_dir=store.profile_dir("internal"),
            backend_cli=(
                binding.backend_cli if binding is not None else None
            ),
        )


def parity_report_data(report: ParityReport) -> dict[str, object]:
    return {
        "healthy": (
            report.healthy
            and not any(
                finding.severity == "error"
                for finding in report.findings
            )
        ),
        "policy_version": str(report.policy_version),
        "findings": [
            {
                "category": finding.category,
                "code": finding.code,
                "severity": finding.severity,
                "message": finding.message,
                "expected": finding.expected,
                "observed": finding.observed,
            }
            for finding in report.findings
        ],
        "synchronization_queue": [
            {
                "category": item.category,
                "identifier": item.identifier,
                "finding_code": item.finding_code,
            }
            for item in sorted(
                report.synchronization_queue,
                key=lambda item: item.sort_key,
            )
        ],
    }


def parity_problem_messages(report: ParityReport) -> list[str]:
    return [
        f"{finding.code}: {sanitize_external_text(finding.message)}"
        for finding in report.findings
        if finding.severity == "error"
    ]


def print_parity_diagnostics(report: ParityReport) -> None:
    health = (
        "healthy"
        if parity_report_data(report)["healthy"]
        else "unhealthy"
    )
    print(f"Parity health: {health}")
    for finding in sorted(report.findings, key=lambda item: item.sort_key):
        print(f"Parity finding: {finding.code}")
    for item in sorted(
        report.synchronization_queue,
        key=lambda queue_item: queue_item.sort_key,
    ):
        print(
            "Parity sync: "
            f"{item.category} {item.identifier} ({item.finding_code})"
        )


def internal_app_parity_not_applicable_message(app_profile: str) -> str:
    return (
        "Internal App parity: not applicable "
        f"(App profile: {app_profile})"
    )


def write_verification_report(
    store: Store,
    *,
    name: str,
    repair: str,
    app_server_smoke: bool,
    runtime_smoke: bool,
    exec_smoke: str | None,
    responses_tool_smoke: bool,
    problems: list[str],
    smoke_diagnostics: list[dict[str, object]],
    repair_messages: list[str],
    smoke_outcomes: Sequence[SmokeOutcome] = (),
    parity_report: ParityReport | None = None,
) -> Path:
    report_dir = store.root / "verification"
    ensure_private_dir(report_dir)
    raw_report_data: dict[str, object] = {
        "profile": name,
        "ok": not problems,
        "repair": repair,
        "app_server_smoke": app_server_smoke,
        "runtime_smoke": runtime_smoke,
        "exec_smoke": exec_smoke is not None,
        "responses_tool_smoke": responses_tool_smoke,
        "smoke_diagnostics": smoke_diagnostics,
        "smoke_outcomes": [
            {
                "status": outcome.status,
                "kind": outcome.kind,
                "summary": outcome.summary,
                "returncode": outcome.returncode,
                "timed_out": outcome.timed_out,
                "stdout_truncated": outcome.stdout_truncated,
                "stderr_truncated": outcome.stderr_truncated,
                "duration_seconds": outcome.duration_seconds,
            }
            for outcome in smoke_outcomes
        ],
        "problems": problems,
        "repair_messages": repair_messages,
    }
    if parity_report is not None:
        parity_data = parity_report_data(parity_report)
        raw_report_data["parity"] = parity_data
        raw_report_data["ok"] = (
            not problems
            and bool(parity_data["healthy"])
        )
    report_data = sanitize_external_data(raw_report_data)
    payload = (
        json.dumps(
            report_data,
            indent=2,
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    for _attempt in range(32):
        path = report_dir / (
            f"{now_stamp()}-{time.time_ns()}-{secrets.token_hex(4)}-{name}.json"
        )
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            directory_descriptor = os.open(
                report_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return path
    raise SwitchError("could not allocate a unique verification report path")


def repair_internal_parity(
    args: argparse.Namespace,
    binding: RuntimeBinding,
    selection_snapshot: ActiveProfileSelectionSnapshot | None = None,
) -> None:
    from codex_switch_bindings import cmd_set_bin

    rebind_args = argparse.Namespace(
        **{
            **vars(args),
            "name": "internal",
            "codex_bin": str(binding.backend_cli),
            "preserve_app_cli": False,
            **(
                {
                    "expected_active_selection_payload": (
                        selection_snapshot.payload
                    )
                }
                if selection_snapshot is not None
                else {}
            ),
        }
    )
    cmd_set_bin(rebind_args)


def active_app_profile_for_cli(store: Store, cli_profile: str) -> str:
    snapshot = read_active_profile_selection_snapshot(
        store.active_path,
        fallback_cli_profile=cli_profile,
    )
    if snapshot.problem is not None:
        raise SwitchError(snapshot.problem)
    if snapshot.selection is None:
        raise SwitchError(
            "active.selection.invalid: active selection is unavailable"
        )
    return snapshot.selection.app_profile


def resolve_verification_runtime_bindings(
    store: Store,
    cli_profile: str,
    *,
    selection_snapshot: ActiveProfileSelectionSnapshot | None = None,
) -> VerificationRuntimeBindings:
    def resolve_profile(
        profile: str,
    ) -> tuple[RuntimeBinding | None, str | None]:
        try:
            manifest = store.load_manifest(profile)
            if (
                profile in {"internal", "openai-official", "official"}
                and manifest_uses_canonical_binding(profile, manifest)
            ):
                return (
                    resolve_store_runtime_binding(
                        store,
                        profile,
                        manifest=manifest,
                    ),
                    None,
                )
        except RuntimeBindingError as exc:
            return None, f"{exc.code}: {exc}"
        except SwitchError:
            return None, None
        return None, None

    cli_binding, cli_problem = resolve_profile(cli_profile)
    snapshot = selection_snapshot or read_active_profile_selection_snapshot(
        store.active_path,
        fallback_cli_profile=cli_profile,
    )
    if snapshot.problem is not None or snapshot.selection is None:
        return VerificationRuntimeBindings(
            cli=cli_binding,
            cli_problem=cli_problem,
            app=None,
            app_problem=None,
            selection_problem=(
                snapshot.problem
                or "active.selection.invalid: active selection is unavailable"
            ),
        )
    app_profile = snapshot.selection.app_profile
    if app_profile == cli_profile:
        return VerificationRuntimeBindings(
            cli=cli_binding,
            cli_problem=cli_problem,
            app=cli_binding,
            app_problem=cli_problem,
            selection_problem=None,
        )
    app_binding, app_problem = resolve_profile(app_profile)
    return VerificationRuntimeBindings(
        cli=cli_binding,
        cli_problem=cli_problem,
        app=app_binding,
        app_problem=app_problem,
        selection_problem=None,
    )


def cmd_verify(args: argparse.Namespace) -> None:
    store = make_store(args)
    home = profile_home(store, args.name)
    selection_snapshot = read_active_profile_selection_snapshot(
        store.active_path,
        fallback_cli_profile=args.name,
    )
    preflight_selection_problem = selection_snapshot.problem
    resolved_app_profile = args.name
    shared_report: object | None = None
    selection = selection_snapshot.selection
    if selection is not None:
        resolved_app_profile = selection.app_profile
        if (
            selection_snapshot.record is not None
            and selection_uses_shared_configuration(selection)
        ):
            shared_report = shared_configuration_report(store, selection)
    if (
        shared_report is not None
        and not shared_configuration_problem_messages(shared_report)
    ):
        for line in shared_configuration_diagnostic_lines(shared_report):
            print(line)
    repair_messages: list[str] = []
    if args.repair == "safe" and preflight_selection_problem is None:
        repair_messages = run_safe_repair(store, args.name, home)
        repair_messages = [
            sanitize_external_text(message)
            for message in repair_messages
        ]
        for message in repair_messages:
            print(message)

    bindings = resolve_verification_runtime_bindings(
        store,
        args.name,
        selection_snapshot=selection_snapshot,
    )
    binding = bindings.cli
    binding_problem = bindings.cli_problem
    app_binding = bindings.app
    app_binding_problem = bindings.app_problem
    selection_problem = (
        preflight_selection_problem or bindings.selection_problem
    )
    parity_applicable = (
        args.name == "internal"
        and resolved_app_profile == "internal"
    )
    parity_report = (
        collect_parity_report(store, binding)
        if parity_applicable and selection_problem is None
        else None
    )
    if (
        args.repair == "safe"
        and selection_problem is None
        and parity_applicable
        and binding is not None
        and parity_report is not None
        and not parity_report.healthy
    ):
        repair_internal_parity(args, binding, selection_snapshot)
        bindings = resolve_verification_runtime_bindings(
            store,
            args.name,
            selection_snapshot=selection_snapshot,
        )
        binding = bindings.cli
        binding_problem = bindings.cli_problem
        app_binding = bindings.app
        app_binding_problem = bindings.app_problem
        selection_problem = bindings.selection_problem
        parity_report = collect_parity_report(store, binding)
    observation = (
        collect_store_runtime_observation(store, app_binding)
        if selection_problem is None
        else None
    )
    if parity_report is not None:
        print_parity_diagnostics(parity_report)
    elif (
        args.name == "internal"
        and selection_problem is None
        and not parity_applicable
    ):
        print(
            internal_app_parity_not_applicable_message(
                resolved_app_profile
            )
        )

    problems, smoke_diagnostics, smoke_outcomes = collect_verification_problems(
        store,
        args.name,
        app_server_smoke=args.app_server_smoke,
        runtime_smoke=args.runtime_smoke,
        exec_smoke=args.exec_smoke,
        responses_tool_smoke=args.responses_tool_smoke,
        runtime_binding=binding,
        app_runtime_binding=app_binding,
        runtime_binding_problem=binding_problem,
        app_runtime_binding_problem=app_binding_problem,
        selection_problem=selection_problem,
        runtime_observation=observation,
        parity_report=parity_report,
        shared_configuration=shared_report,
        selection_snapshot=selection_snapshot,
    )
    problems = [
        sanitize_external_text(problem)
        for problem in problems
    ]
    sanitized_diagnostics = sanitize_external_data(smoke_diagnostics)
    if isinstance(sanitized_diagnostics, list):
        smoke_diagnostics = sanitized_diagnostics

    def smoke_passed(kind: str) -> bool:
        matching = [
            outcome
            for outcome in smoke_outcomes
            if outcome.kind == kind
        ]
        return bool(matching) and all(
            outcome.status == "passed" for outcome in matching
        )

    if args.app_server_smoke and smoke_passed("app-server smoke"):
        print("App-server smoke: passed")
    if args.runtime_smoke and smoke_passed("runtime smoke"):
        print("Runtime smoke: passed")
    if args.exec_smoke is not None and smoke_passed("exec smoke"):
        print("Exec smoke: passed")
    if args.responses_tool_smoke and smoke_passed("Responses tool smoke"):
        print("Responses tool smoke: passed")

    report_path: Path | None = None
    if args.report:
        report_path = write_verification_report(
            store,
            name=args.name,
            repair=args.repair,
            app_server_smoke=args.app_server_smoke,
            runtime_smoke=args.runtime_smoke,
            exec_smoke=args.exec_smoke,
            responses_tool_smoke=args.responses_tool_smoke,
            problems=problems,
            smoke_diagnostics=smoke_diagnostics,
            repair_messages=repair_messages,
            smoke_outcomes=smoke_outcomes,
            parity_report=parity_report,
        )

    if problems:
        print("Verification found issues:")
        for problem in problems:
            print(f"- {problem}")
        if report_path is not None:
            print(f"Verification report: {report_path}")
        raise SystemExit(1)

    print(f"Verification passed for {args.name}")
    if report_path is not None:
        print(f"Verification report: {report_path}")
