from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from codex_switch_release_bundle import (
    BundleError,
    build_release_bundle,
    validate_release_outputs,
)


HANDSHAKE_SCHEMA = "codex-switch.promotion-handshake"
HANDSHAKE_VERSION = 1
STATE_SCHEMA = "codex-switch.promotion-state"
STATE_VERSION = 1
LOCK_NAME = "promotion.lock"
STATE_NAME = "promotion-state.json"
RELEASES_NAME = "releases"
CURRENT_NAME = "current"
ROLLBACK_NAME = "rollback"
LEGACY_BACKUP_PREFIX = ".legacy-current-"
LEGACY_CANONICAL_PREFIX = ".legacy-canonical-"
RELEASE_STAGE_PREFIX = ".promotion-release-"
# Pre-promotion releases lacked these manifest-required modules. The immutable
# rollback copy gets inert placeholders; the original legacy tree stays exact.
LEGACY_CANONICAL_PLACEHOLDER_MODULES = (
    "codex_switch_release_bundle.py",
    "codex_switch_promotion.py",
    "codex_switch_update_policy.py",
    "codex_switch_official_release.py",
    "codex_switch_parity.py",
    "codex_switch_runtime_binding.py",
    "codex_switch_app_proxy.py",
    "codex_switch_home_sync.py",
)
LEGACY_CANONICAL_PLACEHOLDER = (
    '"""Compatibility placeholder for a pre-promotion codex-switch release."""\n'
    "\n"
    "LEGACY_CANONICALIZATION_PLACEHOLDER = True\n"
)

FaultInjector = Callable[[str], None]
NodeIdentity = Tuple[object, ...]

VALID_STATE_PHASES = {
    "candidate_active",
    "candidate_prepared",
    "legacy_current_moved",
    "legacy_migrated",
    "legacy_prepared",
    "legacy_recovered",
    "legacy_rolled_back",
    "promoted",
    "rolled_back",
}


class PromotionError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PromotionCandidate:
    root: Path
    version: str
    digest: str


@dataclass(frozen=True)
class PromotionLayout:
    root: Path

    def __post_init__(self) -> None:
        normalized = Path(
            os.path.abspath(os.path.expanduser(os.fspath(self.root)))
        )
        object.__setattr__(self, "root", normalized)

    @property
    def releases_dir(self) -> Path:
        return self.root / RELEASES_NAME

    @property
    def current_ref(self) -> Path:
        return self.root / CURRENT_NAME

    @property
    def rollback_ref(self) -> Path:
        return self.root / ROLLBACK_NAME

    @property
    def state_path(self) -> Path:
        return self.root / STATE_NAME

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_NAME


@dataclass(frozen=True)
class PromotionReceipt:
    outcome: str
    active_root: Path
    rollback_root: Optional[Path]
    run_id: str
    version: str
    digest: str
    reused_release: bool
    health_command_count: int
    original_command_count: int
    original_command_returncode: Optional[int]


def _lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _path_exists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _node_identity(path: Path) -> NodeIdentity:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ("missing",)
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    identity: NodeIdentity = (
        kind,
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    if kind == "symlink":
        identity += (os.readlink(path),)
    return identity


def _identity_kind(identity: NodeIdentity) -> str:
    return str(identity[0])


def _same_object(left: NodeIdentity, right: NodeIdentity) -> bool:
    if _identity_kind(left) == "missing" or _identity_kind(right) == "missing":
        return left == right
    return left[:3] == right[:3]


def _require_identity(
    path: Path,
    expected: NodeIdentity,
    *,
    reason: str,
    label: str,
) -> None:
    if _node_identity(path) != expected:
        raise PromotionError(reason, f"{label} changed during promotion: {path}")


def _invoke_fault(
    fault_injector: Optional[FaultInjector],
    phase: str,
) -> None:
    if fault_injector is not None:
        fault_injector(phase)


def _cleanup_owned_temp(path: Path, identity: Optional[NodeIdentity]) -> None:
    if identity is None or _node_identity(path) != identity:
        return
    kind = _identity_kind(identity)
    if kind not in {"file", "symlink"}:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _cleanup_owned_tree(
    path: Path,
    identity: Optional[NodeIdentity],
    snapshot: Optional[Mapping[str, Tuple[object, ...]]],
) -> None:
    if (
        identity is None
        or snapshot is None
        or _node_identity(path) != identity
    ):
        return
    try:
        current = _tree_snapshot(path)
    except OSError:
        return
    if current == snapshot:
        try:
            shutil.rmtree(path)
        except OSError:
            pass


def _tree_snapshot(root: Path) -> Dict[str, Tuple[object, ...]]:
    paths = [root, *sorted(root.rglob("*"))]
    snapshot: Dict[str, Tuple[object, ...]] = {}
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            snapshot[relative] = (
                "file",
                mode,
                info.st_size,
                _sha256(path),
            )
        elif stat.S_ISDIR(info.st_mode):
            snapshot[relative] = ("directory", mode)
        else:
            snapshot[relative] = ("other", mode)
    return snapshot


def _tree_digest(root: Path) -> str:
    snapshot = _tree_snapshot(root)
    payload = [
        [relative, list(snapshot[relative])]
        for relative in sorted(snapshot)
    ]
    return hashlib.sha256(
        (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [os.fspath(item) for item in command],
            cwd=cwd,
            env=dict(env),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise
    except OSError as error:
        raise PromotionError(
            "command_start_failed",
            f"Promotion command could not start: {command!r}: {error}",
        ) from error


def _validate_shell_syntax(root: Path, timeout: float) -> None:
    shell_paths = (
        root / "run.sh",
        root / "scripts" / "codex-switch",
        root / "scripts" / "package-release.sh",
    )
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = _run_bounded(
            ["bash", "-n", *[str(path) for path in shell_paths]],
            cwd=root,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PromotionError(
            "shell_syntax_timeout",
            f"Candidate shell syntax check timed out: {root}",
        ) from error
    if result.returncode != 0:
        raise PromotionError(
            "shell_syntax_invalid",
            f"Candidate shell syntax is invalid: {result.stderr.strip()}",
        )


def _python_files(root: Path) -> Tuple[Path, ...]:
    return tuple(
        path
        for path in sorted((root / "scripts").rglob("*.py"))
        if not path.name.startswith("test_")
    )


def _validate_python_syntax(root: Path) -> Tuple[Path, ...]:
    python_files = _python_files(root)
    for path in python_files:
        try:
            ast.parse(path.read_text(), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            raise PromotionError(
                "python_syntax_invalid",
                f"Candidate Python syntax is invalid: {path}: {error}",
            ) from error
    return python_files


def _validate_python_imports(
    root: Path,
    python_files: Sequence[Path],
    timeout: float,
) -> None:
    root_modules = [
        path.stem
        for path in python_files
        if path.parent == root / "scripts" and path.stem != "__init__"
    ]
    if not root_modules:
        return
    code = (
        "import importlib, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "for name in sys.argv[2:]:\n"
        "    importlib.import_module(name)\n"
    )
    with tempfile.TemporaryDirectory(
        prefix="codex-switch-candidate-import-"
    ) as raw:
        import_root = Path(raw) / "candidate"
        shutil.copytree(root, import_root, symlinks=True)
        before = _tree_snapshot(import_root)
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(Path(raw) / "home"),
                "CODEX_HOME": str(Path(raw) / "codex-home"),
                "CODEX_SWITCH_HOME": str(Path(raw) / "switch-home"),
                "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        Path(env["HOME"]).mkdir()
        try:
            result = _run_bounded(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(import_root / "scripts"),
                    *root_modules,
                ],
                cwd=import_root,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise PromotionError(
                "python_import_timeout",
                f"Candidate Python import check timed out: {root}",
            ) from error
        after = _tree_snapshot(import_root)
        if before != after:
            raise PromotionError(
                "python_import_invalid",
                f"Candidate Python import check mutated its release root: {root}",
            )
        if result.returncode != 0:
            raise PromotionError(
                "python_import_invalid",
                f"Candidate Python imports are invalid: {result.stderr.strip()}",
            )


def _validate_candidate_smoke(root: Path, timeout: float) -> None:
    with tempfile.TemporaryDirectory(prefix="codex-switch-candidate-smoke-") as raw:
        smoke_root = Path(raw) / "candidate"
        shutil.copytree(root, smoke_root, symlinks=True)
        before = _tree_snapshot(smoke_root)
        smoke_home = Path(raw) / "home"
        smoke_home.mkdir()
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(smoke_home),
                "CODEX_HOME": str(Path(raw) / "codex-home"),
                "CODEX_SWITCH_HOME": str(Path(raw) / "switch-home"),
                "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        try:
            result = _run_bounded(
                [str(smoke_root / "scripts" / "codex-switch"), "--version"],
                cwd=smoke_root,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            after = _tree_snapshot(smoke_root)
            if before != after:
                raise PromotionError(
                    "candidate_smoke_mutated",
                    f"Candidate command smoke mutated its release root: {root}",
                ) from error
            raise PromotionError(
                "candidate_smoke_timeout",
                f"Candidate command smoke timed out: {root}",
            ) from error
        after = _tree_snapshot(smoke_root)
        if before != after:
            raise PromotionError(
                "candidate_smoke_mutated",
                f"Candidate command smoke mutated its release root: {root}",
            )
        if result.returncode != 0:
            raise PromotionError(
                "candidate_smoke_failed",
                f"Candidate command smoke failed: {result.stderr.strip()}",
            )


def validate_candidate(
    root: Path,
    expected_version: Optional[str] = None,
    *,
    smoke_timeout: float = 5.0,
    import_timeout: float = 5.0,
    allow_historical_required_paths: bool = False,
) -> PromotionCandidate:
    candidate_root = _lexical_path(root)
    if candidate_root.is_symlink():
        raise PromotionError(
            "candidate_invalid",
            f"Promotion candidate must not be a symlink: {candidate_root}",
        )
    try:
        manifest = validate_release_outputs(
            candidate_root,
            allow_historical_required_paths=allow_historical_required_paths,
        )
    except BundleError as error:
        raise PromotionError(
            "candidate_invalid",
            f"Promotion candidate failed bundle validation: {error}",
        ) from error

    version = manifest.get("version")
    digest = manifest.get("payload_sha256")
    if not isinstance(version, str) or not version:
        raise PromotionError("candidate_invalid", "Candidate VERSION is invalid")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise PromotionError("candidate_invalid", "Candidate digest is invalid")
    if expected_version is not None and version != expected_version:
        raise PromotionError(
            "version_mismatch",
            f"Candidate version mismatch: expected {expected_version}, got {version}",
        )

    _validate_shell_syntax(candidate_root, import_timeout)
    python_files = _validate_python_syntax(candidate_root)
    _validate_python_imports(candidate_root, python_files, import_timeout)
    _validate_candidate_smoke(candidate_root, smoke_timeout)
    return PromotionCandidate(
        root=candidate_root.resolve(),
        version=version,
        digest=digest,
    )


class _PromotionLock:
    def __init__(self, layout: PromotionLayout) -> None:
        self.layout = layout
        self._descriptor: Optional[int] = None
        self._identity: Optional[Tuple[int, int]] = None

    def __enter__(self) -> "_PromotionLock":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.layout.lock_path, flags)
        except OSError as error:
            raise PromotionError(
                "lock_invalid",
                "Promotion lock cannot be opened safely: "
                f"{self.layout.lock_path}: {error}",
            ) from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise PromotionError(
                "lock_busy",
                f"Promotion layout is busy: {self.layout.root}",
            ) from error
        observed = os.fstat(descriptor)
        self._descriptor = descriptor
        self._identity = (observed.st_dev, observed.st_ino)
        return self

    def revalidate(self) -> None:
        descriptor = self._descriptor
        identity = self._identity
        if descriptor is None or identity is None:
            raise PromotionError("lock_invalid", "Promotion lock is not active")
        try:
            observed = self.layout.lock_path.lstat()
        except FileNotFoundError as error:
            raise PromotionError(
                "lock_changed",
                f"Promotion lock changed while held: {self.layout.lock_path}",
            ) from error
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISDIR(observed.st_mode)
            or (observed.st_dev, observed.st_ino) != identity
        ):
            raise PromotionError(
                "lock_changed",
                f"Promotion lock changed while held: {self.layout.lock_path}",
            )

    def __exit__(
        self,
        exc_type: Optional[type],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        del exc_type, exc, traceback
        descriptor = self._descriptor
        self._descriptor = None
        self._identity = None
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _prepare_lock(layout: PromotionLayout) -> None:
    if _path_exists(layout.root):
        if layout.root.is_symlink() or not layout.root.is_dir():
            raise PromotionError(
                "layout_invalid",
                f"Promotion layout root is invalid: {layout.root}",
            )
    else:
        layout.root.mkdir(parents=True, mode=0o700)
    if _path_exists(layout.lock_path):
        if layout.lock_path.is_symlink() or not layout.lock_path.is_dir():
            raise PromotionError(
                "lock_invalid",
                f"Promotion lock is invalid: {layout.lock_path}",
            )
    else:
        layout.lock_path.mkdir(mode=0o700)


def _prepare_layout_locked(layout: PromotionLayout, lock: _PromotionLock) -> None:
    lock.revalidate()
    if _path_exists(layout.releases_dir):
        if layout.releases_dir.is_symlink() or not layout.releases_dir.is_dir():
            raise PromotionError(
                "layout_invalid",
                f"Promotion releases directory is invalid: {layout.releases_dir}",
            )
    else:
        layout.releases_dir.mkdir(mode=0o700)
    if _path_exists(layout.state_path):
        if layout.state_path.is_symlink() or not layout.state_path.is_file():
            raise PromotionError(
                "state_invalid",
                f"Promotion state path is invalid: {layout.state_path}",
            )


def _validate_state_payload(value: object) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise PromotionError("state_invalid", "Promotion state must be an object")
    if (
        value.get("schema") != STATE_SCHEMA
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != STATE_VERSION
    ):
        raise PromotionError("state_invalid", "Promotion state schema is invalid")
    phase = value.get("phase")
    outcome = value.get("outcome")
    run_id = value.get("run_id")
    if (
        phase not in VALID_STATE_PHASES
        or not isinstance(outcome, str)
        or not outcome
        or not isinstance(run_id, str)
        or not run_id
    ):
        raise PromotionError("state_invalid", "Promotion state fields are invalid")
    expected_outcomes = {
        "candidate_active": "in_progress",
        "candidate_prepared": "in_progress",
        "legacy_current_moved": "in_progress",
        "legacy_migrated": "migrated",
        "legacy_prepared": "in_progress",
        "legacy_recovered": "recovered",
        "legacy_rolled_back": "rolled_back",
        "promoted": "promoted",
        "rolled_back": "rolled_back",
    }
    if outcome != expected_outcomes[phase]:
        raise PromotionError("state_invalid", "Promotion state outcome is invalid")
    digest = value.get("digest")
    if phase in {
        "candidate_active",
        "candidate_prepared",
        "promoted",
        "rolled_back",
    }:
        version = value.get("version")
        active_root = value.get("active_root")
        rollback_root = value.get("rollback_root")
        previous_current_root = value.get("previous_current_root")
        previous_rollback_root = value.get("previous_rollback_root")
        if (
            not isinstance(version, str)
            or not version
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or active_root is not None
            and not isinstance(active_root, str)
            or rollback_root is not None
            and not isinstance(rollback_root, str)
            or "previous_current_root" not in value
            or previous_current_root is not None
            and not isinstance(previous_current_root, str)
            or "previous_rollback_root" not in value
            or previous_rollback_root is not None
            and not isinstance(previous_rollback_root, str)
            or phase == "rolled_back"
            and not isinstance(value.get("failure_reason"), str)
        ):
            raise PromotionError(
                "state_invalid",
                "Promotion candidate state evidence is invalid",
            )
    if phase.startswith("legacy_"):
        raw_backup = value.get("legacy_backup")
        legacy_digest = value.get("legacy_digest")
        legacy_release = value.get("legacy_release")
        legacy_version = value.get("legacy_version")
        if (
            not isinstance(raw_backup, str)
            or not raw_backup.startswith(LEGACY_BACKUP_PREFIX)
            or Path(raw_backup).name != raw_backup
            or not isinstance(legacy_digest, str)
            or len(legacy_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in legacy_digest
            )
            or not isinstance(legacy_release, str)
            or not legacy_release
            or not isinstance(legacy_version, str)
            or not legacy_version
        ):
            raise PromotionError(
                "state_invalid",
                "Promotion legacy state evidence is invalid",
            )
    return value


def _read_state(layout: PromotionLayout) -> Optional[Dict[str, object]]:
    if not _path_exists(layout.state_path):
        return None
    before = _node_identity(layout.state_path)
    if _identity_kind(before) != "file":
        raise PromotionError(
            "state_invalid",
            f"Promotion state path is invalid: {layout.state_path}",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(layout.state_path, flags)
    except OSError as error:
        raise PromotionError(
            "state_invalid",
            f"Promotion state cannot be opened safely: {layout.state_path}: {error}",
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before[1]
            or opened.st_ino != before[2]
        ):
            raise PromotionError(
                "state_changed",
                f"Promotion state changed while opening: {layout.state_path}",
            )
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _require_identity(
        layout.state_path,
        before,
        reason="state_changed",
        label="Promotion state",
    )
    if (
        opened.st_size != after_open.st_size
        or opened.st_mtime_ns != after_open.st_mtime_ns
        or opened.st_ctime_ns != after_open.st_ctime_ns
    ):
        raise PromotionError(
            "state_changed",
            f"Promotion state changed while reading: {layout.state_path}",
        )
    try:
        value = json.loads(b"".join(chunks).decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError(
            "state_invalid",
            f"Promotion state is invalid: {layout.state_path}: {error}",
        ) from error
    return _validate_state_payload(value)


def _write_state(
    layout: PromotionLayout,
    value: Mapping[str, object],
    *,
    expected_identity: Optional[NodeIdentity] = None,
    fault_injector: Optional[FaultInjector] = None,
) -> NodeIdentity:
    expected = (
        _node_identity(layout.state_path)
        if expected_identity is None
        else expected_identity
    )
    if _identity_kind(expected) not in {"missing", "file"}:
        raise PromotionError(
            "state_invalid",
            f"Promotion state path is invalid: {layout.state_path}",
        )
    _require_identity(
        layout.state_path,
        expected,
        reason="state_changed",
        label="Promotion state",
    )
    payload = dict(value)
    payload["schema"] = STATE_SCHEMA
    payload["schema_version"] = STATE_VERSION
    _validate_state_payload(payload)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".promotion-state-",
        dir=layout.root,
    )
    temp_path = Path(raw_path)
    temp_identity: Optional[NodeIdentity] = None
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        temp_identity = _node_identity(temp_path)
        if _identity_kind(temp_identity) != "file":
            raise PromotionError(
                "state_changed",
                f"Promotion state temporary path is invalid: {temp_path}",
            )
        phase = str(payload["phase"])
        _invoke_fault(fault_injector, f"state_before_replace:{phase}")
        _require_identity(
            layout.state_path,
            expected,
            reason="state_changed",
            label="Promotion state destination",
        )
        _require_identity(
            temp_path,
            temp_identity,
            reason="state_changed",
            label="Promotion state temporary file",
        )
        os.replace(temp_path, layout.state_path)
        published_identity = _node_identity(layout.state_path)
        if not _same_object(published_identity, temp_identity):
            raise PromotionError(
                "state_changed",
                f"Published promotion state changed during promotion: "
                f"{layout.state_path}",
            )
        return published_identity
    finally:
        _cleanup_owned_temp(temp_path, temp_identity)


def _relative_release_target(layout: PromotionLayout, release: Path) -> str:
    layout_root = layout.root.resolve(strict=False)
    release_path = _lexical_path(release).resolve(strict=False)
    try:
        relative = release_path.relative_to(layout_root)
    except ValueError as error:
        raise PromotionError(
            "ref_invalid",
            f"Promotion release escapes layout: {release}",
        ) from error
    if (
        len(relative.parts) != 2
        or relative.parts[0] != RELEASES_NAME
        or not relative.parts[1]
    ):
        raise PromotionError(
            "ref_invalid",
            f"Promotion release has invalid layout: {release}",
        )
    return relative.as_posix()


def _atomic_ref(
    layout: PromotionLayout,
    name: str,
    release: Path,
    *,
    expected_identity: Optional[NodeIdentity] = None,
    fault_injector: Optional[FaultInjector] = None,
    phase: str,
) -> NodeIdentity:
    ref_path = layout.root / name
    expected = (
        _node_identity(ref_path)
        if expected_identity is None
        else expected_identity
    )
    if _identity_kind(expected) not in {"missing", "symlink"}:
        raise PromotionError(
            "ref_invalid",
            f"Promotion reference is not a symlink: {ref_path}",
        )
    _require_identity(
        ref_path,
        expected,
        reason="ref_changed",
        label="Promotion reference",
    )
    target = _relative_release_target(layout, release)
    temp_path = layout.root / f".{name}.{uuid.uuid4().hex}"
    temp_identity: Optional[NodeIdentity] = None
    try:
        os.symlink(target, temp_path)
        temp_identity = _node_identity(temp_path)
        if _identity_kind(temp_identity) != "symlink":
            raise PromotionError(
                "ref_changed",
                f"Promotion reference temporary path is invalid: {temp_path}",
            )
        _invoke_fault(
            fault_injector,
            f"ref_before_replace:{name}:{phase}",
        )
        _require_identity(
            ref_path,
            expected,
            reason="ref_changed",
            label="Promotion reference destination",
        )
        _require_identity(
            temp_path,
            temp_identity,
            reason="ref_changed",
            label="Promotion reference temporary path",
        )
        os.replace(temp_path, ref_path)
        published_identity = _node_identity(ref_path)
        if not _same_object(published_identity, temp_identity):
            raise PromotionError(
                "ref_changed",
                f"Published promotion reference changed during promotion: "
                f"{ref_path}",
            )
        return published_identity
    finally:
        _cleanup_owned_temp(temp_path, temp_identity)


def _remove_ref(
    layout: PromotionLayout,
    name: str,
    *,
    expected_identity: Optional[NodeIdentity] = None,
) -> NodeIdentity:
    ref_path = layout.root / name
    expected = (
        _node_identity(ref_path)
        if expected_identity is None
        else expected_identity
    )
    if _identity_kind(expected) == "missing":
        _require_identity(
            ref_path,
            expected,
            reason="ref_changed",
            label="Promotion reference",
        )
        return expected
    if _identity_kind(expected) != "symlink":
        raise PromotionError(
            "ref_invalid",
            f"Promotion reference is not a symlink: {ref_path}",
        )
    _require_identity(
        ref_path,
        expected,
        reason="ref_changed",
        label="Promotion reference",
    )
    ref_path.unlink()
    missing = ("missing",)
    _require_identity(
        ref_path,
        missing,
        reason="ref_changed",
        label="Removed promotion reference",
    )
    return missing


def _read_ref(layout: PromotionLayout, name: str) -> Optional[Path]:
    ref_path = layout.root / name
    identity = _node_identity(ref_path)
    if _identity_kind(identity) == "missing":
        return None
    if _identity_kind(identity) != "symlink":
        raise PromotionError(
            "ref_invalid",
            f"Promotion reference is not a symlink: {ref_path}",
        )
    raw_target = os.readlink(ref_path)
    pure_target = PurePosixPath(raw_target)
    if (
        pure_target.is_absolute()
        or len(pure_target.parts) != 2
        or pure_target.parts[0] != RELEASES_NAME
        or "." in pure_target.parts
        or ".." in pure_target.parts
    ):
        raise PromotionError(
            "ref_invalid",
            f"Promotion reference target is invalid: {ref_path} -> {raw_target}",
        )
    release = layout.root.joinpath(*pure_target.parts)
    candidate = validate_candidate(
        release,
        allow_historical_required_paths=True,
    )
    if candidate.digest != release.name:
        raise PromotionError(
            "release_digest_mismatch",
            f"Promotion release digest path does not match content: {release}",
        )
    _require_identity(
        ref_path,
        identity,
        reason="ref_changed",
        label="Promotion reference",
    )
    return release.resolve()


def _validate_supplied_candidate(
    candidate: PromotionCandidate,
) -> PromotionCandidate:
    validated = validate_candidate(
        candidate.root,
        expected_version=candidate.version,
    )
    if validated.digest != candidate.digest:
        raise PromotionError(
            "candidate_changed",
            f"Promotion candidate changed after validation: {candidate.root}",
        )
    return validated


def _ensure_release(
    candidate: PromotionCandidate,
    layout: PromotionLayout,
    *,
    fault_injector: Optional[FaultInjector] = None,
    allow_historical_required_paths: bool = False,
) -> Tuple[Path, bool]:
    destination = layout.releases_dir / candidate.digest
    if _path_exists(destination):
        if destination.is_symlink() or not destination.is_dir():
            raise PromotionError(
                "release_digest_mismatch",
                f"Promotion digest destination is invalid: {destination}",
            )
        try:
            existing = validate_candidate(
                destination,
                allow_historical_required_paths=allow_historical_required_paths,
            )
        except PromotionError as error:
            raise PromotionError(
                "release_digest_mismatch",
                f"Existing promotion release is invalid: {destination}: {error}",
            ) from error
        if (
            existing.digest != candidate.digest
            or existing.version != candidate.version
        ):
            raise PromotionError(
                "release_digest_mismatch",
                f"Existing promotion release does not match digest: {destination}",
            )
        return destination.resolve(), True

    stage = layout.releases_dir / f"{RELEASE_STAGE_PREFIX}{uuid.uuid4().hex}"
    stage_identity: Optional[NodeIdentity] = None
    stage_snapshot: Optional[Dict[str, Tuple[object, ...]]] = None
    try:
        shutil.copytree(candidate.root, stage, symlinks=True)
        stage_identity = _node_identity(stage)
        stage_snapshot = _tree_snapshot(stage)
        copied = validate_candidate(
            stage, expected_version=candidate.version,
            allow_historical_required_paths=allow_historical_required_paths,
        )
        if copied.digest != candidate.digest:
            raise PromotionError(
                "candidate_changed",
                f"Copied promotion candidate digest changed: {stage}",
            )
        _invoke_fault(fault_injector, "release_before_publish")
        _require_identity(
            stage,
            stage_identity,
            reason="release_stage_changed",
            label="Promotion release stage",
        )
        if _tree_snapshot(stage) != stage_snapshot:
            raise PromotionError(
                "release_stage_changed",
                f"Promotion release stage changed after validation: {stage}",
            )
        if _path_exists(destination):
            raise PromotionError(
                "release_digest_mismatch",
                f"Promotion digest destination appeared during staging: {destination}",
            )
        os.rename(stage, destination)
        published_identity = _node_identity(destination)
        if not _same_object(published_identity, stage_identity):
            raise PromotionError(
                "release_stage_changed",
                f"Published promotion release changed: {destination}",
            )
        if _tree_snapshot(destination) != stage_snapshot:
            raise PromotionError(
                "release_stage_changed",
                f"Published promotion release content changed: {destination}",
            )
        return destination.resolve(), False
    except PromotionError:
        raise
    except Exception as error:
        raise PromotionError(
            "release_stage_failed",
            f"Promotion release staging failed: {error}",
        ) from error
    finally:
        _cleanup_owned_tree(stage, stage_identity, stage_snapshot)


def _legacy_source_version(root: Path) -> str:
    try:
        version = (root / "VERSION").read_text().strip()
    except (OSError, UnicodeDecodeError) as error:
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy current VERSION is unreadable: {root}: {error}",
        ) from error
    if not version:
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy current VERSION is empty: {root}",
        )
    for relative in (
        "run.sh",
        "scripts/codex-switch",
        "scripts/package-release.sh",
    ):
        path = root / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or not stat.S_IMODE(path.stat().st_mode) & 0o111
        ):
            raise PromotionError(
                "legacy_migration_failed",
                f"Legacy current executable is invalid: {path}",
            )
    return version


def _materialize_legacy_canonical_source(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)
    scripts_dir = destination / "scripts"
    if scripts_dir.is_symlink() or not scripts_dir.is_dir():
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy canonical scripts directory is invalid: {scripts_dir}",
        )
    for module_name in LEGACY_CANONICAL_PLACEHOLDER_MODULES:
        module_path = scripts_dir / module_name
        if _path_exists(module_path):
            continue
        module_path.write_text(LEGACY_CANONICAL_PLACEHOLDER)
        module_path.chmod(0o644)


def _validate_legacy_source_root(
    root: Path,
    *,
    digest: str,
    version: Optional[str],
) -> None:
    if root.is_symlink() or not root.is_dir():
        raise PromotionError(
            "legacy_recovery_failed",
            f"Legacy source root is invalid: {root}",
        )
    observed_version = _legacy_source_version(root)
    if version is not None and observed_version != version:
        raise PromotionError(
            "legacy_recovery_failed",
            f"Legacy source VERSION changed: {root}",
        )
    try:
        observed_digest = _tree_digest(root)
    except OSError as error:
        raise PromotionError(
            "legacy_recovery_failed",
            f"Legacy source root cannot be read: {root}: {error}",
        ) from error
    if observed_digest != digest:
        raise PromotionError(
            "legacy_recovery_failed",
            f"Legacy source root changed: {root}",
        )


def _validate_saved_legacy_root(
    root: Path,
    *,
    source_kind: str,
    source_digest: str,
    release_digest: str,
    version: Optional[str],
) -> None:
    if source_kind == "source":
        _validate_legacy_source_root(
            root,
            digest=source_digest,
            version=version,
        )
        return
    _validate_legacy_root(
        root,
        digest=release_digest,
        version=version,
    )


def _legacy_state_paths(
    layout: PromotionLayout,
    state: Mapping[str, object],
) -> Tuple[Path, Path, str, Optional[str], str, str]:
    raw_backup = state.get("legacy_backup")
    raw_release = state.get("legacy_release")
    digest = state.get("legacy_digest")
    version = state.get("legacy_version")
    source_kind = state.get("legacy_source_kind", "bundle")
    source_digest = state.get("legacy_source_digest", digest)
    if (
        not isinstance(raw_backup, str)
        or not raw_backup.startswith(LEGACY_BACKUP_PREFIX)
        or Path(raw_backup).name != raw_backup
        or not isinstance(raw_release, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or version is not None
        and not isinstance(version, str)
        or source_kind not in {"bundle", "source"}
        or not isinstance(source_digest, str)
        or len(source_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in source_digest
        )
    ):
        raise PromotionError(
            "legacy_recovery_failed",
            "Interrupted legacy migration state is invalid",
        )
    release = _lexical_path(Path(raw_release)).resolve(strict=False)
    if _relative_release_target(layout, release) != f"{RELEASES_NAME}/{digest}":
        raise PromotionError(
            "legacy_recovery_failed",
            "Interrupted legacy release path is invalid",
        )
    return (
        layout.root / raw_backup,
        release,
        digest,
        version,
        str(source_kind),
        source_digest,
    )


def _validate_legacy_root(
    root: Path,
    *,
    digest: str,
    version: Optional[str],
) -> PromotionCandidate:
    candidate = validate_candidate(
        root, expected_version=version, allow_historical_required_paths=True,
    )
    if candidate.digest != digest:
        raise PromotionError(
            "legacy_recovery_failed",
            f"Interrupted legacy root digest does not match: {root}",
        )
    return candidate


def _recover_interrupted_legacy(layout: PromotionLayout) -> None:
    state_identity = _node_identity(layout.state_path)
    state = _read_state(layout)
    _require_identity(
        layout.state_path,
        state_identity,
        reason="state_changed",
        label="Promotion state",
    )
    if state is None or state.get("phase") not in {
        "legacy_prepared",
        "legacy_current_moved",
        "legacy_migrated",
    }:
        return
    (
        backup,
        release,
        digest,
        version,
        source_kind,
        source_digest,
    ) = _legacy_state_paths(layout, state)
    current = layout.current_ref
    current_identity = _node_identity(current)
    backup_identity = _node_identity(backup)
    current_kind = _identity_kind(current_identity)
    backup_kind = _identity_kind(backup_identity)

    if current_kind == "symlink":
        current_release = _read_ref(layout, CURRENT_NAME)
        if current_release is None or current_release.resolve() != release:
            raise PromotionError(
                "legacy_recovery_failed",
                "Interrupted legacy current points to an unexpected release",
            )
        _validate_legacy_root(release, digest=digest, version=version)
        if backup_kind == "missing":
            migrated = dict(state)
            migrated.update(
                {
                    "phase": "legacy_migrated",
                    "outcome": "migrated",
                    "active_root": str(release),
                }
            )
            _write_state(
                layout,
                migrated,
                expected_identity=state_identity,
            )
            return
        if backup_kind != "directory":
            raise PromotionError(
                "legacy_recovery_failed",
                f"Interrupted legacy backup is invalid: {backup}",
            )
        _validate_saved_legacy_root(
            backup,
            source_kind=source_kind,
            source_digest=source_digest,
            release_digest=digest,
            version=version,
        )
        _remove_ref(
            layout,
            CURRENT_NAME,
            expected_identity=current_identity,
        )
        os.replace(backup, current)
    elif current_kind == "missing" and backup_kind == "directory":
        _validate_saved_legacy_root(
            backup,
            source_kind=source_kind,
            source_digest=source_digest,
            release_digest=digest,
            version=version,
        )
        os.replace(backup, current)
    elif current_kind == "directory" and backup_kind == "missing":
        _validate_saved_legacy_root(
            current,
            source_kind=source_kind,
            source_digest=source_digest,
            release_digest=digest,
            version=version,
        )
    else:
        raise PromotionError(
            "legacy_recovery_failed",
            "Interrupted legacy migration paths are ambiguous",
        )

    _validate_saved_legacy_root(
        current,
        source_kind=source_kind,
        source_digest=source_digest,
        release_digest=digest,
        version=version,
    )
    recovered = dict(state)
    recovered.update(
        {
            "phase": "legacy_recovered",
            "outcome": "recovered",
            "active_root": str(current.resolve()),
        }
    )
    _write_state(
        layout,
        recovered,
        expected_identity=state_identity,
    )


def _prepare_legacy_release(
    current: Path,
    layout: PromotionLayout,
    *,
    current_identity: NodeIdentity,
    fault_injector: Optional[FaultInjector],
) -> Tuple[PromotionCandidate, Path, str, str]:
    manifest_path = current / "bundle-manifest.json"
    try:
        if _path_exists(manifest_path):
            candidate = validate_candidate(current, allow_historical_required_paths=True)
            _require_identity(
                current,
                current_identity,
                reason="legacy_migration_failed",
                label="Legacy current",
            )
            release, _reused = _ensure_release(
                candidate,
                layout,
                fault_injector=fault_injector,
                allow_historical_required_paths=True,
            )
            return candidate, release, "bundle", candidate.digest

        version = _legacy_source_version(current)
        source_digest = _tree_digest(current)
        with tempfile.TemporaryDirectory(
            prefix=LEGACY_CANONICAL_PREFIX,
            dir=layout.root,
        ) as raw:
            canonical_source = Path(raw) / "source"
            output_root = Path(raw) / "bundle"
            _materialize_legacy_canonical_source(
                current,
                canonical_source,
            )
            receipt = build_release_bundle(canonical_source, output_root)
            candidate = validate_candidate(
                receipt.package_dir,
                expected_version=version,
            )
            _require_identity(
                current,
                current_identity,
                reason="legacy_migration_failed",
                label="Legacy current",
            )
            if _tree_digest(current) != source_digest:
                raise PromotionError(
                    "legacy_migration_failed",
                    f"Legacy current changed during canonicalization: {current}",
                )
            release, _reused = _ensure_release(
                candidate,
                layout,
                fault_injector=fault_injector,
            )
        return candidate, release, "source", source_digest
    except PromotionError as error:
        if error.reason == "legacy_migration_failed":
            raise
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy current validation failed: {error}",
        ) from error
    except (BundleError, OSError) as error:
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy current canonicalization failed: {error}",
        ) from error


def _migrate_legacy_current(
    layout: PromotionLayout,
    *,
    run_id: str,
    fault_injector: Optional[FaultInjector],
) -> Path:
    current = layout.current_ref
    if current.is_symlink() or not current.is_dir():
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy current is not a directory: {current}",
        )
    state_identity = _node_identity(layout.state_path)
    current_identity = _node_identity(current)
    (
        legacy_candidate,
        legacy_release,
        legacy_source_kind,
        legacy_source_digest,
    ) = _prepare_legacy_release(
        current,
        layout,
        current_identity=current_identity,
        fault_injector=fault_injector,
    )
    backup = layout.root / f"{LEGACY_BACKUP_PREFIX}{uuid.uuid4().hex}"
    if _path_exists(backup):
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy migration backup already exists: {backup}",
        )
    state: Dict[str, object] = {
        "phase": "legacy_prepared",
        "outcome": "in_progress",
        "run_id": run_id,
        "legacy_backup": backup.name,
        "legacy_digest": legacy_candidate.digest,
        "legacy_release": str(legacy_release),
        "legacy_version": legacy_candidate.version,
        "legacy_source_kind": legacy_source_kind,
        "legacy_source_digest": legacy_source_digest,
    }
    state_identity = _write_state(
        layout,
        state,
        expected_identity=state_identity,
        fault_injector=fault_injector,
    )
    installed_ref_identity: Optional[NodeIdentity] = None
    try:
        _require_identity(
            current,
            current_identity,
            reason="legacy_migration_failed",
            label="Legacy current",
        )
        _require_identity(
            backup,
            ("missing",),
            reason="legacy_migration_failed",
            label="Legacy backup destination",
        )
        _invoke_fault(fault_injector, "legacy_before_move")
        os.replace(current, backup)
        backup_identity = _node_identity(backup)
        if (
            _identity_kind(backup_identity) != "directory"
            or backup_identity[1:3] != current_identity[1:3]
        ):
            raise PromotionError(
                "legacy_migration_failed",
                f"Legacy backup identity is invalid: {backup}",
        )
        state["phase"] = "legacy_current_moved"
        state_identity = _write_state(
            layout,
            state,
            expected_identity=state_identity,
            fault_injector=fault_injector,
        )
        _invoke_fault(fault_injector, "legacy_current_moved")
        installed_ref_identity = _atomic_ref(
            layout,
            CURRENT_NAME,
            legacy_release,
            expected_identity=("missing",),
            fault_injector=fault_injector,
            phase="legacy_ref_installed",
        )
        _invoke_fault(fault_injector, "legacy_ref_installed")
        _validate_saved_legacy_root(
            backup,
            source_kind=legacy_source_kind,
            source_digest=legacy_source_digest,
            release_digest=legacy_candidate.digest,
            version=legacy_candidate.version,
        )
        _require_identity(
            backup,
            backup_identity,
            reason="legacy_migration_failed",
            label="Legacy backup",
        )
        state.update(
            {
                "phase": "legacy_migrated",
                "outcome": "migrated",
                "active_root": str(legacy_release),
            }
        )
        state_identity = _write_state(
            layout,
            state,
            expected_identity=state_identity,
            fault_injector=fault_injector,
        )
        _require_identity(
            backup,
            backup_identity,
            reason="legacy_migration_failed",
            label="Legacy backup",
        )
        shutil.rmtree(backup)
        return legacy_release
    except Exception as error:
        try:
            observed_current = _node_identity(current)
            observed_kind = _identity_kind(observed_current)
            if observed_kind == "directory":
                if (
                    observed_current != current_identity
                    or _path_exists(backup)
                ):
                    raise PromotionError(
                        "legacy_migration_failed",
                        f"Legacy current became unclassified: {current}",
                    )
            elif observed_kind == "symlink":
                if (
                    installed_ref_identity is None
                    or observed_current != installed_ref_identity
                ):
                    raise PromotionError(
                        "legacy_migration_failed",
                        f"Legacy current became unclassified: {current}",
                    )
                _remove_ref(
                    layout,
                    CURRENT_NAME,
                    expected_identity=installed_ref_identity,
                )
            elif observed_kind != "missing":
                raise PromotionError(
                    "legacy_migration_failed",
                    f"Legacy current became unclassified: {current}",
                )
            if _path_exists(backup):
                os.replace(backup, current)
            if not current.is_dir() or current.is_symlink():
                raise PromotionError(
                    "legacy_migration_failed",
                    f"Legacy current was not restored: {current}",
                )
            _validate_saved_legacy_root(
                current,
                source_kind=legacy_source_kind,
                source_digest=legacy_source_digest,
                release_digest=legacy_candidate.digest,
                version=legacy_candidate.version,
            )
        except Exception as rollback_error:
            raise PromotionError(
                "legacy_migration_failed",
                "Legacy migration failed and restoration was incomplete: "
                f"{error}; restore: {rollback_error}",
            ) from rollback_error
        state.update(
            {
                "phase": "legacy_rolled_back",
                "outcome": "rolled_back",
                "failure_reason": "legacy_migration_failed",
                "active_root": str(current.resolve()),
            }
        )
        try:
            _write_state(
                layout,
                state,
                expected_identity=state_identity,
                fault_injector=fault_injector,
            )
        except PromotionError:
            pass
        raise PromotionError(
            "legacy_migration_failed",
            f"Legacy migration failed and was restored: {error}",
        ) from error


def _current_release(
    layout: PromotionLayout,
    *,
    run_id: str,
    fault_injector: Optional[FaultInjector],
) -> Optional[Path]:
    current = layout.current_ref
    if not _path_exists(current):
        return None
    if current.is_symlink():
        return _read_ref(layout, CURRENT_NAME)
    if current.is_dir():
        return _migrate_legacy_current(
            layout,
            run_id=run_id,
            fault_injector=fault_injector,
        )
    raise PromotionError(
        "ref_invalid",
        f"Promotion current path is invalid: {current}",
    )


def _run_health(
    command: Sequence[str],
    *,
    release: Path,
    candidate: PromotionCandidate,
    run_id: str,
    timeout: float,
) -> None:
    env = dict(os.environ)
    env.update(
        {
            "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
            "CODEX_SWITCH_PROMOTION_RUN_ID": run_id,
            "CODEX_SWITCH_PROMOTION_VERSION": candidate.version,
            "CODEX_SWITCH_PROMOTION_DIGEST": candidate.digest,
            "CODEX_SWITCH_PROMOTION_ROOT": str(release.resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        result = _run_bounded(
            command,
            cwd=release,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PromotionError(
            "health_timeout",
            f"Promoted health command timed out after {timeout}s",
        ) from error
    if result.returncode != 0:
        raise PromotionError(
            "health_nonzero",
            f"Promoted health command failed with exit {result.returncode}",
        )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise PromotionError(
            "handshake_invalid",
            f"Promoted health handshake is not valid JSON: {error}",
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != HANDSHAKE_SCHEMA
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != HANDSHAKE_VERSION
    ):
        raise PromotionError(
            "handshake_invalid",
            "Promoted health handshake schema is invalid",
        )
    expected = {
        "run_id": run_id,
        "version": candidate.version,
        "digest": candidate.digest,
        "root": str(release.resolve()),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise PromotionError(
            "handshake_mismatch",
            f"Promoted health handshake does not match expected values: {payload}",
        )


def _restore_ref(
    layout: PromotionLayout,
    name: str,
    release: Optional[Path],
    *,
    expected_identity: NodeIdentity,
    phase: str,
) -> NodeIdentity:
    if release is None:
        return _remove_ref(
            layout,
            name,
            expected_identity=expected_identity,
        )
    return _atomic_ref(
        layout,
        name,
        release,
        expected_identity=expected_identity,
        phase=phase,
    )


def _promotion_state(
    *,
    phase: str,
    outcome: str,
    run_id: str,
    candidate: PromotionCandidate,
    active_root: Optional[Path],
    rollback_root: Optional[Path],
    previous_current_root: Optional[Path],
    previous_rollback_root: Optional[Path],
    failure_reason: Optional[str] = None,
) -> Dict[str, object]:
    state: Dict[str, object] = {
        "phase": phase,
        "outcome": outcome,
        "run_id": run_id,
        "version": candidate.version,
        "digest": candidate.digest,
        "active_root": str(active_root.resolve()) if active_root is not None else None,
        "rollback_root": (
            str(rollback_root.resolve()) if rollback_root is not None else None
        ),
        "previous_current_root": (
            str(previous_current_root.resolve())
            if previous_current_root is not None
            else None
        ),
        "previous_rollback_root": (
            str(previous_rollback_root.resolve())
            if previous_rollback_root is not None
            else None
        ),
    }
    if failure_reason is not None:
        state["failure_reason"] = failure_reason
    return state


def _state_release_root(
    layout: PromotionLayout,
    raw_root: object,
    *,
    label: str,
) -> Optional[Path]:
    if raw_root is None:
        return None
    if not isinstance(raw_root, str) or not raw_root:
        raise PromotionError(
            "candidate_recovery_failed",
            f"Interrupted candidate {label} is invalid",
        )
    root = _lexical_path(Path(raw_root)).resolve(strict=False)
    target = _relative_release_target(layout, root)
    candidate = validate_candidate(root)
    if target != f"{RELEASES_NAME}/{candidate.digest}":
        raise PromotionError(
            "candidate_recovery_failed",
            f"Interrupted candidate {label} does not match its digest path",
        )
    return root


def _same_optional_root(left: Optional[Path], right: Optional[Path]) -> bool:
    if left is None or right is None:
        return left is right
    return left.resolve() == right.resolve()


def _recover_interrupted_candidate(layout: PromotionLayout) -> None:
    state_identity = _node_identity(layout.state_path)
    state = _read_state(layout)
    _require_identity(
        layout.state_path,
        state_identity,
        reason="state_changed",
        label="Promotion state",
    )
    if state is None or state.get("phase") not in {
        "candidate_prepared",
        "candidate_active",
    }:
        return
    phase = str(state["phase"])
    digest = str(state["digest"])
    version = str(state["version"])
    candidate_root = layout.releases_dir / digest
    candidate = validate_candidate(candidate_root, expected_version=version)
    if candidate.digest != digest:
        raise PromotionError(
            "candidate_recovery_failed",
            "Interrupted candidate digest does not match its release path",
        )
    previous_current = _state_release_root(
        layout,
        state.get("previous_current_root"),
        label="previous current root",
    )
    previous_rollback = _state_release_root(
        layout,
        state.get("previous_rollback_root"),
        label="previous rollback root",
    )
    recorded_active = _state_release_root(
        layout,
        state.get("active_root"),
        label="active root",
    )
    recorded_rollback = _state_release_root(
        layout,
        state.get("rollback_root"),
        label="rollback root",
    )
    expected_recorded_active = (
        previous_current if phase == "candidate_prepared" else candidate.root
    )
    expected_recorded_rollback = (
        previous_rollback if phase == "candidate_prepared" else previous_current
    )
    if not _same_optional_root(
        recorded_active,
        expected_recorded_active,
    ) or not _same_optional_root(
        recorded_rollback,
        expected_recorded_rollback,
    ):
        raise PromotionError(
            "candidate_recovery_failed",
            "Interrupted candidate state roots are inconsistent",
        )

    current = _read_ref(layout, CURRENT_NAME)
    current_identity = _node_identity(layout.current_ref)
    if _same_optional_root(current, previous_current):
        pass
    elif _same_optional_root(current, candidate.root):
        _restore_ref(
            layout,
            CURRENT_NAME,
            previous_current,
            expected_identity=current_identity,
            phase="interrupted_candidate_current",
        )
    else:
        raise PromotionError(
            "candidate_recovery_failed",
            "Interrupted candidate current reference is inconsistent",
        )

    rollback = _read_ref(layout, ROLLBACK_NAME)
    rollback_identity = _node_identity(layout.rollback_ref)
    if _same_optional_root(rollback, previous_rollback):
        pass
    elif (
        phase == "candidate_active"
        and _same_optional_root(rollback, previous_current)
    ):
        _restore_ref(
            layout,
            ROLLBACK_NAME,
            previous_rollback,
            expected_identity=rollback_identity,
            phase="interrupted_candidate_rollback",
        )
    else:
        raise PromotionError(
            "candidate_recovery_failed",
            "Interrupted candidate rollback reference is inconsistent",
        )

    _write_state(
        layout,
        _promotion_state(
            phase="rolled_back",
            outcome="rolled_back",
            run_id=str(state["run_id"]),
            candidate=candidate,
            active_root=previous_current,
            rollback_root=previous_rollback,
            previous_current_root=previous_current,
            previous_rollback_root=previous_rollback,
            failure_reason="interrupted_before_handshake",
        ),
        expected_identity=state_identity,
    )


def _run_original_command(
    command: Sequence[str],
    *,
    active_root: Path,
    timeout: float,
) -> int:
    env = dict(os.environ)
    env.update(
        {
            "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        result = subprocess.run(
            [os.fspath(item) for item in command],
            cwd=active_root,
            env=env,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PromotionError(
            "original_timeout",
            f"Original command timed out after {timeout}s",
        ) from error
    except OSError as error:
        raise PromotionError(
            "original_start_failed",
            f"Original command could not start: {command!r}: {error}",
        ) from error
    return result.returncode


def promote_candidate(
    candidate: PromotionCandidate,
    layout: PromotionLayout,
    health_command: Sequence[str],
    *,
    health_timeout: float = 10.0,
    original_command: Optional[Sequence[str]] = None,
    original_timeout: float = 60.0,
    fault_injector: Optional[FaultInjector] = None,
    run_id: Optional[str] = None,
) -> PromotionReceipt:
    validated = _validate_supplied_candidate(candidate)
    actual_run_id = run_id or uuid.uuid4().hex
    _prepare_lock(layout)

    active_root: Path
    rollback_root: Optional[Path]
    reused_release = False
    with _PromotionLock(layout) as lock:
        _prepare_layout_locked(layout, lock)
        _recover_interrupted_legacy(layout)
        _recover_interrupted_candidate(layout)
        lock.revalidate()
        prior_current = _current_release(
            layout,
            run_id=actual_run_id,
            fault_injector=fault_injector,
        )
        prior_rollback = _read_ref(layout, ROLLBACK_NAME)
        prior_current_identity = _node_identity(layout.current_ref)
        prior_rollback_identity = _node_identity(layout.rollback_ref)
        prior_state_identity = _node_identity(layout.state_path)
        release, reused_release = _ensure_release(
            validated,
            layout,
            fault_injector=fault_injector,
        )
        owned_state_identity = _write_state(
            layout,
            _promotion_state(
                phase="candidate_prepared",
                outcome="in_progress",
                run_id=actual_run_id,
                candidate=validated,
                active_root=prior_current,
                rollback_root=prior_rollback,
                previous_current_root=prior_current,
                previous_rollback_root=prior_rollback,
            ),
            expected_identity=prior_state_identity,
            fault_injector=fault_injector,
        )
        current_active_identity: Optional[NodeIdentity] = None
        rollback_active_identity = prior_rollback_identity
        rollback_changed = False
        try:
            current_active_identity = _atomic_ref(
                layout,
                CURRENT_NAME,
                release,
                expected_identity=prior_current_identity,
                fault_injector=fault_injector,
                phase="candidate_active",
            )
            _invoke_fault(fault_injector, "candidate_ref_installed")
            owned_state_identity = _write_state(
                layout,
                _promotion_state(
                    phase="candidate_active",
                    outcome="in_progress",
                    run_id=actual_run_id,
                    candidate=validated,
                    active_root=release,
                    rollback_root=prior_current,
                    previous_current_root=prior_current,
                    previous_rollback_root=prior_rollback,
                ),
                expected_identity=owned_state_identity,
                fault_injector=fault_injector,
            )
            _invoke_fault(fault_injector, "candidate_active_recorded")
            _run_health(
                health_command,
                release=release,
                candidate=validated,
                run_id=actual_run_id,
                timeout=health_timeout,
            )

            if (
                prior_current is not None
                and prior_current.resolve() != release.resolve()
            ):
                rollback_active_identity = _atomic_ref(
                    layout,
                    ROLLBACK_NAME,
                    prior_current,
                    expected_identity=prior_rollback_identity,
                    fault_injector=fault_injector,
                    phase="candidate_promoted",
                )
                rollback_changed = True
                rollback_root = prior_current.resolve()
            else:
                rollback_root = prior_rollback
                if rollback_root is None:
                    rollback_active_identity = _remove_ref(
                        layout,
                        ROLLBACK_NAME,
                        expected_identity=prior_rollback_identity,
                    )
                    rollback_changed = (
                        rollback_active_identity != prior_rollback_identity
                    )
            active_root = release.resolve()
            owned_state_identity = _write_state(
                layout,
                _promotion_state(
                    phase="promoted",
                    outcome="promoted",
                    run_id=actual_run_id,
                    candidate=validated,
                    active_root=active_root,
                    rollback_root=rollback_root,
                    previous_current_root=prior_current,
                    previous_rollback_root=prior_rollback,
                ),
                expected_identity=owned_state_identity,
                fault_injector=fault_injector,
            )
        except PromotionError as error:
            if current_active_identity is None:
                raise
            rollback_errors = []
            try:
                _restore_ref(
                    layout,
                    CURRENT_NAME,
                    prior_current,
                    expected_identity=current_active_identity,
                    phase="candidate_rollback",
                )
            except PromotionError as rollback_error:
                rollback_errors.append(str(rollback_error))
            if rollback_changed:
                try:
                    _restore_ref(
                        layout,
                        ROLLBACK_NAME,
                        prior_rollback,
                        expected_identity=rollback_active_identity,
                        phase="rollback_ref_restore",
                    )
                except PromotionError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            elif _node_identity(layout.rollback_ref) != prior_rollback_identity:
                rollback_errors.append(
                    f"Rollback reference changed externally: {layout.rollback_ref}"
                )
            if rollback_errors:
                raise PromotionError(
                    "rollback_failed",
                    "Promotion failed and reference rollback was incomplete: "
                    + "; ".join(rollback_errors),
                ) from error
            if _node_identity(layout.state_path) == owned_state_identity:
                try:
                    _write_state(
                        layout,
                        _promotion_state(
                            phase="rolled_back",
                            outcome="rolled_back",
                            run_id=actual_run_id,
                            candidate=validated,
                            active_root=prior_current,
                            rollback_root=prior_rollback,
                            previous_current_root=prior_current,
                            previous_rollback_root=prior_rollback,
                            failure_reason=error.reason,
                        ),
                        expected_identity=owned_state_identity,
                        fault_injector=fault_injector,
                    )
                except PromotionError:
                    pass
            raise

    original_count = 0
    original_returncode: Optional[int] = None
    if original_command is not None:
        original_count = 1
        original_returncode = _run_original_command(
            original_command,
            active_root=active_root,
            timeout=original_timeout,
        )

    return PromotionReceipt(
        outcome="promoted",
        active_root=active_root,
        rollback_root=rollback_root,
        run_id=actual_run_id,
        version=validated.version,
        digest=validated.digest,
        reused_release=reused_release,
        health_command_count=1,
        original_command_count=original_count,
        original_command_returncode=original_returncode,
    )


def _adapter_health_command() -> Tuple[str, ...]:
    code = (
        "import json, os, pathlib, subprocess, sys\n"
        "root = pathlib.Path.cwd().resolve()\n"
        "command = root / 'scripts' / 'codex-switch'\n"
        "manifest = json.loads((root / 'bundle-manifest.json').read_text())\n"
        "try:\n"
        "    result = subprocess.run(\n"
        "        [str(command), '--version'],\n"
        "        cwd=root,\n"
        "        env=dict(os.environ),\n"
        "        check=False,\n"
        "        text=True,\n"
        "        stdout=subprocess.PIPE,\n"
        "        stderr=subprocess.PIPE,\n"
        "        timeout=5.0,\n"
        "    )\n"
        "except (OSError, subprocess.TimeoutExpired) as error:\n"
        "    print(str(error), file=sys.stderr)\n"
        "    raise SystemExit(70)\n"
        "if result.returncode != 0:\n"
        "    if result.stderr:\n"
        "        print(result.stderr.strip(), file=sys.stderr)\n"
        "    raise SystemExit(result.returncode)\n"
        "reported_version = result.stdout.strip()\n"
        "if reported_version.startswith('codex-switch '):\n"
        "    reported_version = reported_version[len('codex-switch '):].strip()\n"
        "if reported_version.startswith('v'):\n"
        "    reported_version = reported_version[1:]\n"
        "print(json.dumps({\n"
        "    'schema': 'codex-switch.promotion-handshake',\n"
        "    'schema_version': 1,\n"
        "    'run_id': os.environ['CODEX_SWITCH_PROMOTION_RUN_ID'],\n"
        "    'version': reported_version,\n"
        "    'digest': manifest.get('payload_sha256'),\n"
        "    'root': str(root),\n"
        "}, sort_keys=True))\n"
    )
    return (sys.executable, "-I", "-B", "-c", code)


def _receipt_payload(receipt: PromotionReceipt) -> Dict[str, object]:
    return {
        "outcome": receipt.outcome,
        "active_root": str(receipt.active_root),
        "rollback_root": (
            str(receipt.rollback_root)
            if receipt.rollback_root is not None
            else None
        ),
        "run_id": receipt.run_id,
        "version": receipt.version,
        "digest": receipt.digest,
        "reused_release": receipt.reused_release,
        "health_command_count": receipt.health_command_count,
        "original_command_count": receipt.original_command_count,
        "original_command_returncode": receipt.original_command_returncode,
    }


def _adapter_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and immutably promote a codex-switch candidate."
    )
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--layout-root", required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--health-timeout", type=float, default=10.0)
    parser.add_argument(
        "--exec-command",
        action="store_true",
        help="Run current/scripts/codex-switch once after promotion.",
    )
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _adapter_parser().parse_args(argv)
    command_args = list(args.command_args)
    if command_args[:1] == ["--"]:
        command_args = command_args[1:]
    if command_args and not args.exec_command:
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "reason": "unexpected_command",
                    "message": "Command arguments require --exec-command",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        candidate = validate_candidate(
            Path(args.candidate_root),
            expected_version=args.expected_version,
        )
        original_command = None
        if args.exec_command:
            original_command = ("scripts/codex-switch", *command_args)
        receipt = promote_candidate(
            candidate,
            PromotionLayout(Path(args.layout_root)),
            _adapter_health_command(),
            health_timeout=args.health_timeout,
            original_command=original_command,
        )
    except PromotionError as error:
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "reason": error.reason,
                    "message": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        if error.reason == "original_start_failed":
            return 126
        return 2

    if not args.exec_command:
        print(json.dumps(_receipt_payload(receipt), sort_keys=True))
        return 0

    returncode = receipt.original_command_returncode
    if returncode is None:
        print(
            "codex-switch promotion: promoted command result is missing",
            file=sys.stderr,
        )
        return 70
    if returncode < 0:
        return 128 - returncode
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
