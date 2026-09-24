#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)


PACKAGE_NAME = "codex-switch"
RUNNER_NAME = "run.sh"
ARCHIVE_NAME = "codex-switch.tar.gz"
MANIFEST_NAME = "bundle-manifest.json"
WORKDIR_MARKER_NAME = ".codex-switch-release-workdir.json"

MANIFEST_SCHEMA = "codex-switch.release-bundle"
MANIFEST_VERSION = 1
MANIFEST_CLASSIFICATION = "codex-switch-release-bundle"
WORKDIR_SCHEMA = "codex-switch.release-workdir"
WORKDIR_VERSION = 1
WORKDIR_CLASSIFICATION = "codex-switch-release-workdir"

STAGING_PREFIX = ".codex-switch-stage-"
BACKUP_PREFIX = ".codex-switch-backup-"

FIXED_FILES = ("README.md", "SKILL.md", "VERSION", "run.sh")
FIXED_DIRECTORIES = ("agents", "docs", "evals", "scripts")
REQUIRED_PYTHON_MODULES = (
    "codex_profile_switch.py",
    "codex_switch_release_bundle.py",
    "codex_switch_promotion.py",
    "codex_switch_update_policy.py",
    "codex_switch_official_release.py",
    "codex_switch_parity.py",
    "codex_switch_runtime_binding.py",
    "codex_switch_app_proxy.py",
    "codex_switch_home_sync.py",
    "codex_switch_selection.py",
    "codex_switch_shared_configuration.py",
    "codex_switch_update.py",
)
SUPPORTED_HISTORICAL_REQUIRED_PATHS = (
    "README.md",
    "SKILL.md",
    "VERSION",
    "run.sh",
    "agents",
    "docs",
    "evals",
    "scripts",
    "scripts/codex-switch",
    "scripts/codex_profile_switch.py",
    "scripts/codex_switch_release_bundle.py",
    "scripts/codex_switch_promotion.py",
    "scripts/codex_switch_update_policy.py",
    "scripts/codex_switch_official_release.py",
    "scripts/package-release.sh",
    MANIFEST_NAME,
)
IMMEDIATELY_PREVIOUS_REQUIRED_PATHS = (
    "README.md",
    "SKILL.md",
    "VERSION",
    "run.sh",
    "agents",
    "docs",
    "evals",
    "scripts",
    "scripts/codex-switch",
    "scripts/codex_profile_switch.py",
    "scripts/codex_switch_release_bundle.py",
    "scripts/codex_switch_promotion.py",
    "scripts/codex_switch_update_policy.py",
    "scripts/codex_switch_official_release.py",
    "scripts/codex_switch_parity.py",
    "scripts/codex_switch_runtime_binding.py",
    "scripts/codex_switch_app_proxy.py",
    "scripts/codex_switch_home_sync.py",
    "scripts/package-release.sh",
    MANIFEST_NAME,
)
SUPPORTED_HISTORICAL_REQUIRED_PATH_SETS = (
    SUPPORTED_HISTORICAL_REQUIRED_PATHS,
    IMMEDIATELY_PREVIOUS_REQUIRED_PATHS,
    (
        "README.md", "SKILL.md", "VERSION", "run.sh", "agents", "docs",
        "evals", "scripts", "scripts/codex-switch",
        *(f"scripts/{name}" for name in REQUIRED_PYTHON_MODULES if name != "codex_switch_update.py"),
        "scripts/package-release.sh", MANIFEST_NAME,
    ),
)
REQUIRED_PATHS = (
    "README.md",
    "SKILL.md",
    "VERSION",
    "run.sh",
    "agents",
    "docs",
    "evals",
    "scripts",
    "scripts/codex-switch",
    *(f"scripts/{name}" for name in REQUIRED_PYTHON_MODULES),
    "scripts/package-release.sh",
    MANIFEST_NAME,
)
EXECUTABLE_EXPECTATIONS = (
    ("run.sh", "0755"),
    ("scripts/codex-switch", "0755"),
    ("scripts/package-release.sh", "0755"),
)
RUNTIME_IMPORT_TIMEOUT_SECONDS = 15.0
RUNTIME_SCRIPT_REFERENCE = re.compile(
    r"\$(?:SCRIPT_DIR|SWITCH_SCRIPTS)/([A-Za-z0-9_]+\.py)"
)

CopyPath = Callable[[Path, Path], None]
ReplacePath = Callable[[Path, Path], None]


class BundleError(RuntimeError):
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        preserve_workdirs: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.preserve_workdirs = preserve_workdirs


@dataclass(frozen=True)
class BundleReceipt:
    package_dir: Path
    runner: Path
    archive: Path
    manifest: Path
    payload_sha256: str


@dataclass(frozen=True)
class PathIdentity:
    device: int
    inode: int
    file_type: int


PathInput = Union[os.PathLike[str], str]


def _lexical_path(path: PathInput) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _path_exists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _path_identity(path: Path, phase: str) -> PathIdentity:
    try:
        observed = path.lstat()
    except OSError as error:
        raise BundleError(
            "destination_changed",
            f"Release output changed during {phase}: {path}: {error}",
        ) from error
    return PathIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        file_type=stat.S_IFMT(observed.st_mode),
    )


def _capture_output_identities(
    destinations: Iterable[Path],
    phase: str,
) -> Dict[Path, PathIdentity]:
    identities: Dict[Path, PathIdentity] = {}
    for destination in destinations:
        if _path_exists(destination):
            identities[destination] = _path_identity(destination, phase)
    return identities


def _require_output_identity(
    path: Path,
    expected: PathIdentity,
    phase: str,
) -> None:
    observed = _path_identity(path, phase)
    if observed != expected:
        raise BundleError(
            "destination_changed",
            f"Release output identity changed during {phase}: {path}; "
            f"expected={expected}; observed={observed}",
        )


def _is_strict_ancestor(candidate: Path, path: Path) -> bool:
    if candidate == path:
        return False
    try:
        path.relative_to(candidate)
    except ValueError:
        return False
    return True


def validate_package_destination(
    repo_root: PathInput,
    output_root: PathInput,
    package_dir: PathInput,
) -> Tuple[Path, Path, Path]:
    raw_repo = _lexical_path(repo_root)
    raw_output = _lexical_path(output_root)
    raw_package = _lexical_path(package_dir)

    if raw_output.is_symlink():
        raise BundleError(
            "symlink_output_root",
            f"Release output root must not be a symlink: {raw_output}",
        )
    if raw_package.is_symlink():
        raise BundleError(
            "symlink_package_destination",
            f"Release package destination must not be a symlink: {raw_package}",
        )

    repo = raw_repo.resolve(strict=False)
    output = raw_output.resolve(strict=False)
    package = raw_package.resolve(strict=False)
    filesystem_root = Path(repo.anchor).resolve(strict=False)

    if not repo.is_dir():
        raise BundleError(
            "invalid_repository",
            f"Release repository is not a directory: {repo}",
        )
    if output == filesystem_root:
        raise BundleError(
            "filesystem_root_output_root",
            f"Release output root resolves to filesystem root: {output}",
        )
    if package == filesystem_root:
        raise BundleError(
            "filesystem_root_package_destination",
            f"Release package destination resolves to filesystem root: {package}",
        )
    if output == repo:
        raise BundleError(
            "repository_output_root",
            f"Release output root resolves to the repository: {output}",
        )
    if package == repo:
        raise BundleError(
            "repository_package_destination",
            f"Release package destination resolves to the repository: {package}",
        )
    if _is_strict_ancestor(output, repo):
        raise BundleError(
            "repository_ancestor_output_root",
            f"Release output root is a repository ancestor: {output}",
        )
    if _is_strict_ancestor(package, repo):
        raise BundleError(
            "repository_ancestor_package_destination",
            f"Release package destination is a repository ancestor: {package}",
        )
    if _path_exists(output) and not output.is_dir():
        raise BundleError(
            "invalid_output_root",
            f"Release output root is not a directory: {output}",
        )

    expected_package = output / PACKAGE_NAME
    if package != expected_package:
        raise BundleError(
            "invalid_package_layout",
            f"Release package destination must be {expected_package}, got {package}",
        )
    return repo, output, package


def _json_payload(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_bytes(
        (
            json.dumps(
                value,
                sort_keys=True,
                indent=2,
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")
    )


def _read_json(path: Path, reason: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError(reason, f"Invalid JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise BundleError(reason, f"Expected a JSON object at {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mode(path: Path) -> str:
    return f"{stat.S_IMODE(path.stat().st_mode):04o}"


def _safe_relative_path(package_dir: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise BundleError("manifest_invalid", "Manifest path must be a non-empty string")
    pure_path = PurePosixPath(raw_path)
    if pure_path.is_absolute() or "." in pure_path.parts or ".." in pure_path.parts:
        raise BundleError("manifest_invalid", f"Unsafe manifest path: {raw_path!r}")
    path = package_dir.joinpath(*pure_path.parts)
    try:
        path.resolve(strict=False).relative_to(package_dir.resolve(strict=False))
    except ValueError as error:
        raise BundleError(
            "manifest_invalid",
            f"Manifest path escapes package root: {raw_path!r}",
        ) from error
    return path


def _validate_source_repo(repo_root: Path) -> str:
    for relative in FIXED_FILES:
        path = repo_root / relative
        if path.is_symlink() or not path.is_file():
            raise BundleError(
                "source_invalid",
                f"Required release file is missing or not regular: {path}",
            )
    for relative in FIXED_DIRECTORIES:
        path = repo_root / relative
        if path.is_symlink() or not path.is_dir():
            raise BundleError(
                "source_invalid",
                f"Required release directory is missing or invalid: {path}",
            )
        for child in path.rglob("*"):
            if child.is_symlink():
                raise BundleError(
                    "source_invalid",
                    f"Release allowlist contains a symlink: {child}",
                )

    for relative, _expected_mode in EXECUTABLE_EXPECTATIONS:
        path = repo_root / relative
        if not path.is_file() or not stat.S_IMODE(path.stat().st_mode) & 0o111:
            raise BundleError(
                "source_invalid",
                f"Required release executable is not executable: {path}",
            )

    for name in REQUIRED_PYTHON_MODULES:
        path = repo_root / "scripts" / name
        if path.is_symlink() or not path.is_file():
            raise BundleError(
                "source_invalid",
                f"Required release Python module is missing or invalid: {path}",
            )

    version = (repo_root / "VERSION").read_text().strip()
    if not version:
        raise BundleError("source_invalid", "Release VERSION must not be empty")
    return version


def _default_copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _copy_release_allowlist(
    repo_root: Path,
    package_dir: Path,
    copy_path: CopyPath,
) -> None:
    try:
        for relative in FIXED_FILES:
            copy_path(repo_root / relative, package_dir / relative)
        for relative in FIXED_DIRECTORIES:
            copy_path(repo_root / relative, package_dir / relative)
    except Exception as error:
        raise BundleError("copy_failed", f"Release allowlist copy failed: {error}") from error

    cache_dir = package_dir / "scripts" / "__pycache__"
    if cache_dir.is_dir() and not cache_dir.is_symlink():
        shutil.rmtree(cache_dir)
    for relative, _expected_mode in EXECUTABLE_EXPECTATIONS:
        (package_dir / relative).chmod(0o755)


def _directory_entries(package_dir: Path) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    for path in sorted(package_dir.rglob("*")):
        if path.is_symlink():
            raise BundleError(
                "candidate_invalid",
                f"Release candidate contains a symlink: {path}",
            )
        if path.is_dir():
            entries.append(
                {
                    "path": path.relative_to(package_dir).as_posix(),
                    "mode": _mode(path),
                }
            )
        elif not path.is_file():
            raise BundleError(
                "candidate_invalid",
                f"Release candidate contains a special file: {path}",
            )
    return entries


def _file_entries(package_dir: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    root_manifest = package_dir / MANIFEST_NAME
    for path in sorted(package_dir.rglob("*")):
        if path.is_symlink():
            raise BundleError(
                "candidate_invalid",
                f"Release candidate contains a symlink: {path}",
            )
        if path == root_manifest:
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleError(
                "candidate_invalid",
                f"Release candidate contains a special file: {path}",
            )
        entries.append(
            {
                "path": path.relative_to(package_dir).as_posix(),
                "mode": _mode(path),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return entries


def _runtime_tree_snapshot(
    package_dir: Path,
) -> Tuple[
    str,
    Tuple[Tuple[str, str], ...],
    Tuple[Tuple[str, str, int, str], ...],
]:
    directories = tuple(
        (entry["path"], entry["mode"])
        for entry in _directory_entries(package_dir)
    )
    files = tuple(
        (
            entry["path"],
            entry["mode"],
            entry["size"],
            entry["sha256"],
        )
        for entry in _file_entries(package_dir)
    )
    return _mode(package_dir), directories, files


def _runtime_source_paths(package_dir: Path) -> Tuple[Path, ...]:
    scripts_dir = package_dir / "scripts"
    paths = [
        package_dir / "run.sh",
        scripts_dir / "codex-switch",
        scripts_dir / "package-release.sh",
    ]
    # Historical bundles may omit this helper. If present, its Python module
    # references carry the same packaging contract as the main wrapper.
    if (scripts_dir / "codex_env_setup").is_file():
        paths.append(scripts_dir / "codex_env_setup")
    paths.extend(
        path
        for path in sorted(scripts_dir.glob("*.py"))
        if not path.name.startswith("test_")
    )
    return tuple(paths)


def _validate_generated_script_references(package_dir: Path) -> None:
    scripts_dir = package_dir / "scripts"
    for source in _runtime_source_paths(package_dir):
        try:
            text = source.read_text()
        except (OSError, UnicodeDecodeError) as error:
            raise BundleError(
                "runtime_reference_invalid",
                f"Could not inspect packaged runtime source {source}: {error}",
            ) from error
        for module_name in RUNTIME_SCRIPT_REFERENCE.findall(text):
            target = scripts_dir / module_name
            if target.is_symlink() or not target.is_file():
                raise BundleError(
                    "runtime_reference_invalid",
                    f"Packaged runtime source {source} references missing "
                    f"payload module: {module_name}",
                )


def _resolve_runtime_validation_python() -> str:
    candidates = (
        os.environ.get("CODEX_SWITCH_PYTHON"),
        sys.executable,
        "python3",
        "python3.13",
        "python3.12",
        "python3.11",
        "/opt/homebrew/bin/python3",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/usr/local/bin/python3",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3.11",
    )
    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        resolved = (
            Path(raw).expanduser()
            if os.sep in raw
            else Path(shutil.which(raw) or "")
        )
        if not resolved or not resolved.is_file():
            continue
        resolved_text = str(resolved.resolve())
        if resolved_text in seen or not os.access(resolved_text, os.X_OK):
            continue
        seen.add(resolved_text)
        try:
            result = subprocess.run(
                [
                    resolved_text,
                    "-I",
                    "-B",
                    "-c",
                    (
                        "import sys, tomllib; "
                        "raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
                    ),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return resolved_text
    raise BundleError(
        "runtime_python_unavailable",
        "Python 3.11+ with tomllib is required to validate release runtime imports",
    )


def _validate_runtime_imports(package_dir: Path) -> None:
    modules = tuple(
        path.stem
        for path in sorted((package_dir / "scripts").glob("*.py"))
        if not path.name.startswith("test_") and path.stem != "__init__"
    )
    if not modules:
        raise BundleError(
            "runtime_import_invalid",
            "Release payload contains no runtime Python modules",
        )
    python_bin = _resolve_runtime_validation_python()
    code = (
        "import importlib, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "for name in sys.argv[2:]:\n"
        "    importlib.import_module(name)\n"
    )
    with tempfile.TemporaryDirectory(
        prefix="codex-switch-release-import-"
    ) as raw:
        runtime_root = Path(raw) / PACKAGE_NAME
        shutil.copytree(package_dir, runtime_root, symlinks=True)
        before = _runtime_tree_snapshot(runtime_root)
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
            result = subprocess.run(
                [
                    python_bin,
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(runtime_root / "scripts"),
                    *modules,
                ],
                cwd=runtime_root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=RUNTIME_IMPORT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise BundleError(
                "runtime_import_timeout",
                "Release runtime import validation timed out",
            ) from error
        except OSError as error:
            raise BundleError(
                "runtime_import_invalid",
                f"Release runtime import validation could not start: {error}",
            ) from error
        after = _runtime_tree_snapshot(runtime_root)
        if before != after:
            raise BundleError(
                "runtime_import_mutated",
                "Release runtime imports changed the immutable payload file set",
            )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise BundleError(
                "runtime_import_invalid",
                f"Release runtime imports are invalid: {detail}",
            )


def _payload_sha256(
    files: Iterable[Mapping[str, Any]],
    directories: Iterable[Mapping[str, Any]],
) -> str:
    payload = {
        "directories": list(directories),
        "files": list(files),
    }
    return hashlib.sha256(_json_payload(payload)).hexdigest()


def _create_manifest(package_dir: Path, version: str) -> Dict[str, Any]:
    directories = _directory_entries(package_dir)
    files = _file_entries(package_dir)
    payload_sha256 = _payload_sha256(files, directories)
    run_path = package_dir / RUNNER_NAME
    manifest: Dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_VERSION,
        "classification": MANIFEST_CLASSIFICATION,
        "version": version,
        "allowlist": {
            "files": list(FIXED_FILES),
            "directories": list(FIXED_DIRECTORIES),
        },
        "required_paths": list(REQUIRED_PATHS),
        "executable_expectations": [
            {"path": path, "mode": mode}
            for path, mode in EXECUTABLE_EXPECTATIONS
        ],
        "directories": directories,
        "files": files,
        "payload_sha256": payload_sha256,
        "top_level_runner": {
            "path": RUNNER_NAME,
            "mode": "0755",
            "sha256": _sha256_file(run_path),
        },
        "archive": {
            "name": ARCHIVE_NAME,
            "root": PACKAGE_NAME,
            "payload_sha256": payload_sha256,
        },
    }
    _write_json(package_dir / MANIFEST_NAME, manifest)
    return manifest


def _records_by_path(
    raw_records: object,
    label: str,
) -> Dict[str, Mapping[str, Any]]:
    if not isinstance(raw_records, list):
        raise BundleError("manifest_invalid", f"Manifest {label} must be a list")
    records: Dict[str, Mapping[str, Any]] = {}
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise BundleError(
                "manifest_invalid",
                f"Manifest {label} entries must be objects",
            )
        raw_path = raw_record.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise BundleError(
                "manifest_invalid",
                f"Manifest {label} entry has no path",
            )
        if raw_path in records:
            raise BundleError(
                "manifest_invalid",
                f"Manifest {label} contains duplicate path: {raw_path}",
            )
        records[raw_path] = raw_record
    return records


def _validate_package(
    package_dir: Path,
    *,
    allow_historical_required_paths: bool = False,
) -> Dict[str, Any]:
    if package_dir.is_symlink() or not package_dir.is_dir():
        raise BundleError(
            "manifest_invalid",
            f"Release package is not a regular directory: {package_dir}",
        )
    if _mode(package_dir) != "0755":
        raise BundleError(
            "manifest_invalid",
            f"Release package root mode mismatch: {_mode(package_dir)}",
        )
    manifest_path = package_dir / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BundleError(
            "manifest_invalid",
            f"Release package manifest is missing: {manifest_path}",
        )
    manifest = _read_json(manifest_path, "manifest_invalid")
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != MANIFEST_VERSION
        or manifest.get("classification") != MANIFEST_CLASSIFICATION
    ):
        raise BundleError(
            "manifest_invalid",
            f"Unsupported release package manifest: {manifest_path}",
        )
    if manifest.get("allowlist") != {
        "files": list(FIXED_FILES),
        "directories": list(FIXED_DIRECTORIES),
    }:
        raise BundleError("manifest_invalid", "Release manifest allowlist mismatch")
    raw_required_paths = manifest.get("required_paths")
    historical_required_paths = next(
        (
            candidate
            for candidate in SUPPORTED_HISTORICAL_REQUIRED_PATH_SETS
            if raw_required_paths == list(candidate)
        ),
        None,
    )
    if raw_required_paths == list(REQUIRED_PATHS):
        required_paths = REQUIRED_PATHS
    elif allow_historical_required_paths and historical_required_paths is not None:
        required_paths = historical_required_paths
    else:
        raise BundleError("manifest_invalid", "Release manifest required paths mismatch")

    for child in package_dir.rglob("*"):
        if child.is_symlink():
            raise BundleError(
                "manifest_invalid",
                f"Release package contains a symlink: {child}",
            )
        if not (child.is_file() or child.is_dir()):
            raise BundleError(
                "manifest_invalid",
                f"Release package contains a special file: {child}",
            )
    for relative in required_paths:
        path = _safe_relative_path(package_dir, relative)
        if not _path_exists(path):
            raise BundleError(
                "manifest_invalid",
                f"Required release path is missing: {relative}",
            )
    _validate_generated_script_references(package_dir)

    version = (package_dir / "VERSION").read_text().strip()
    if not version or manifest.get("version") != version:
        raise BundleError("manifest_invalid", "Release manifest VERSION mismatch")

    file_records = _records_by_path(manifest.get("files"), "files")
    directory_records = _records_by_path(
        manifest.get("directories"),
        "directories",
    )
    actual_files = {
        path.relative_to(package_dir).as_posix()
        for path in package_dir.rglob("*")
        if path.is_file() and path != manifest_path
    }
    actual_directories = {
        path.relative_to(package_dir).as_posix()
        for path in package_dir.rglob("*")
        if path.is_dir()
    }
    if set(file_records) != actual_files:
        raise BundleError("manifest_invalid", "Release manifest file set mismatch")
    if set(directory_records) != actual_directories:
        raise BundleError("manifest_invalid", "Release manifest directory set mismatch")

    normalized_files: List[Mapping[str, Any]] = []
    for relative in sorted(file_records):
        record = file_records[relative]
        path = _safe_relative_path(package_dir, relative)
        if not path.is_file():
            raise BundleError(
                "manifest_invalid",
                f"Manifest file is not regular: {relative}",
            )
        expected = {
            "path": relative,
            "mode": _mode(path),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        if record != expected:
            raise BundleError(
                "manifest_invalid",
                f"Release file evidence mismatch: {relative}",
            )
        normalized_files.append(record)

    normalized_directories: List[Mapping[str, Any]] = []
    for relative in sorted(directory_records):
        record = directory_records[relative]
        path = _safe_relative_path(package_dir, relative)
        expected = {"path": relative, "mode": _mode(path)}
        if not path.is_dir() or record != expected:
            raise BundleError(
                "manifest_invalid",
                f"Release directory evidence mismatch: {relative}",
            )
        normalized_directories.append(record)

    expected_executables = [
        {"path": path, "mode": mode}
        for path, mode in EXECUTABLE_EXPECTATIONS
    ]
    if manifest.get("executable_expectations") != expected_executables:
        raise BundleError(
            "manifest_invalid",
            "Release executable expectations mismatch",
        )
    for relative, expected_mode in EXECUTABLE_EXPECTATIONS:
        path = _safe_relative_path(package_dir, relative)
        if not path.is_file() or _mode(path) != expected_mode:
            raise BundleError(
                "manifest_invalid",
                f"Release executable mode mismatch: {relative}",
            )

    payload_sha256 = _payload_sha256(
        normalized_files,
        normalized_directories,
    )
    if manifest.get("payload_sha256") != payload_sha256:
        raise BundleError("manifest_invalid", "Release payload digest mismatch")
    run_path = package_dir / RUNNER_NAME
    if manifest.get("top_level_runner") != {
        "path": RUNNER_NAME,
        "mode": "0755",
        "sha256": _sha256_file(run_path),
    }:
        raise BundleError("manifest_invalid", "Top-level runner evidence mismatch")
    if manifest.get("archive") != {
        "name": ARCHIVE_NAME,
        "root": PACKAGE_NAME,
        "payload_sha256": payload_sha256,
    }:
        raise BundleError("manifest_invalid", "Release archive evidence mismatch")
    return manifest


def _validate_runner(runner: Path, manifest: Mapping[str, Any]) -> None:
    if runner.is_symlink() or not runner.is_file():
        raise BundleError("runner_invalid", f"Top-level runner is missing: {runner}")
    expected = manifest["top_level_runner"]
    if _mode(runner) != expected["mode"] or _sha256_file(runner) != expected["sha256"]:
        raise BundleError("runner_invalid", "Top-level runner evidence mismatch")


def _validate_archive(
    archive_path: Path,
    package_dir: Path,
    *,
    allow_appledouble: bool = False,
) -> None:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise BundleError("archive_invalid", f"Release archive is missing: {archive_path}")
    expected_paths = {PACKAGE_NAME}
    expected_paths.update(
        f"{PACKAGE_NAME}/{path.relative_to(package_dir).as_posix()}"
        for path in package_dir.rglob("*")
    )
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            member_map: Dict[str, tarfile.TarInfo] = {}
            appledouble_targets = set()
            for member in members:
                normalized_name = member.name.rstrip("/")
                pure_name = PurePosixPath(normalized_name)
                if (
                    allow_appledouble
                    and normalized_name
                    and not pure_name.is_absolute()
                    and ".." not in pure_name.parts
                    and pure_name.name.startswith("._")
                ):
                    target_name = pure_name.with_name(
                        pure_name.name.removeprefix("._")
                    ).as_posix()
                    if (
                        target_name not in expected_paths
                        or not member.isfile()
                        or member.size > 64 * 1024
                        or target_name in appledouble_targets
                    ):
                        raise BundleError(
                            "archive_invalid",
                            f"Unsafe legacy archive metadata member: {member.name}",
                        )
                    appledouble_targets.add(target_name)
                    continue
                if (
                    not normalized_name
                    or pure_name.is_absolute()
                    or ".." in pure_name.parts
                    or normalized_name != PACKAGE_NAME
                    and not normalized_name.startswith(f"{PACKAGE_NAME}/")
                    or member.issym()
                    or member.islnk()
                    or not (member.isfile() or member.isdir())
                ):
                    raise BundleError(
                        "archive_invalid",
                        f"Unsafe release archive member: {member.name}",
                    )
                if normalized_name in member_map:
                    raise BundleError(
                        "archive_invalid",
                        f"Duplicate release archive member: {normalized_name}",
                    )
                member_map[normalized_name] = member
            if set(member_map) != expected_paths:
                raise BundleError(
                    "archive_invalid",
                    "Release archive member set mismatch",
                )

            root_member = member_map[PACKAGE_NAME]
            if not root_member.isdir() or f"{root_member.mode & 0o7777:04o}" != _mode(
                package_dir
            ):
                raise BundleError("archive_invalid", "Release archive root mismatch")

            for path in package_dir.rglob("*"):
                relative = path.relative_to(package_dir).as_posix()
                member = member_map[f"{PACKAGE_NAME}/{relative}"]
                expected_mode = _mode(path)
                if f"{member.mode & 0o7777:04o}" != expected_mode:
                    raise BundleError(
                        "archive_invalid",
                        f"Release archive mode mismatch: {relative}",
                    )
                if path.is_dir():
                    if not member.isdir():
                        raise BundleError(
                            "archive_invalid",
                            f"Release archive type mismatch: {relative}",
                        )
                    continue
                if not member.isfile():
                    raise BundleError(
                        "archive_invalid",
                        f"Release archive type mismatch: {relative}",
                    )
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BundleError(
                        "archive_invalid",
                        f"Release archive file is unreadable: {relative}",
                    )
                digest = hashlib.sha256()
                for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != _sha256_file(path):
                    raise BundleError(
                        "archive_invalid",
                        f"Release archive digest mismatch: {relative}",
                    )
    except (OSError, tarfile.TarError) as error:
        raise BundleError(
            "archive_invalid",
            f"Release archive could not be validated: {error}",
        ) from error


def validate_legacy_release_outputs(
    package_dir: PathInput,
    runner: PathInput,
    archive: PathInput,
    *,
    expected_version: str,
    required_files: Sequence[str] = FIXED_FILES,
    required_directories: Sequence[str] = FIXED_DIRECTORIES,
    executable_expectations: Sequence[Tuple[str, str]] = EXECUTABLE_EXPECTATIONS,
    strict_executable_modes: bool = True,
    allow_appledouble_archive: bool = False,
) -> Dict[str, Any]:
    raw_package = _lexical_path(package_dir)
    if raw_package.is_symlink() or not raw_package.is_dir():
        raise BundleError(
            "legacy_package_invalid",
            f"Legacy release package is not a regular directory: {raw_package}",
        )
    package = raw_package.resolve(strict=False)
    for path in package.rglob("*"):
        if path.is_symlink():
            raise BundleError(
                "legacy_package_invalid",
                f"Legacy release package contains a symlink: {path}",
            )
        if not (path.is_file() or path.is_dir()):
            raise BundleError(
                "legacy_package_invalid",
                f"Legacy release package contains a special file: {path}",
            )

    for relative in required_files:
        path = package / relative
        if path.is_symlink() or not path.is_file():
            raise BundleError(
                "legacy_package_invalid",
                f"Legacy release file is missing or invalid: {relative}",
            )
    for relative in required_directories:
        path = package / relative
        if path.is_symlink() or not path.is_dir():
            raise BundleError(
                "legacy_package_invalid",
                f"Legacy release directory is missing or invalid: {relative}",
            )

    version = (package / "VERSION").read_text().strip()
    if not version or version != expected_version:
        raise BundleError(
            "legacy_package_invalid",
            f"Legacy release VERSION {version!r} does not match "
            f"{expected_version!r}",
        )
    for relative, expected_mode in executable_expectations:
        path = package / relative
        observed_mode = int(_mode(path), 8) if path.is_file() else 0
        executable_mode_ok = (
            _mode(path) == expected_mode
            if strict_executable_modes
            else bool(observed_mode & 0o100)
            and not bool(observed_mode & 0o6022)
        )
        if (
            path.is_symlink()
            or not path.is_file()
            or not executable_mode_ok
        ):
            raise BundleError(
                "legacy_package_invalid",
                f"Legacy release executable mode mismatch: {relative}",
            )

    raw_runner = _lexical_path(runner)
    if raw_runner.is_symlink() or not raw_runner.is_file():
        raise BundleError(
            "legacy_runner_invalid",
            f"Legacy top-level runner is missing or invalid: {raw_runner}",
        )
    resolved_runner = raw_runner.resolve(strict=False)
    packaged_runner = package / RUNNER_NAME
    runner_mode = int(_mode(resolved_runner), 8)
    runner_mode_ok = (
        _mode(resolved_runner) == "0755"
        if strict_executable_modes
        else bool(runner_mode & 0o100) and not bool(runner_mode & 0o6022)
    )
    if (
        not runner_mode_ok
        or _sha256_file(resolved_runner) != _sha256_file(packaged_runner)
    ):
        raise BundleError(
            "legacy_runner_invalid",
            "Legacy top-level runner differs from package run.sh",
        )

    raw_archive = _lexical_path(archive)
    if raw_archive.is_symlink() or not raw_archive.is_file():
        raise BundleError(
            "archive_invalid",
            f"Release archive is missing: {raw_archive}",
        )
    _validate_archive(
        raw_archive.resolve(strict=False),
        package,
        allow_appledouble=allow_appledouble_archive,
    )
    return {
        "format": "legacy",
        "version": version,
        "top_level_runner": {
            "path": RUNNER_NAME,
            "mode": "0755",
            "sha256": _sha256_file(resolved_runner),
        },
    }


def canonicalize_legacy_release_archive(
    package_dir: PathInput,
    archive: PathInput,
) -> Path:
    raw_package = _lexical_path(package_dir)
    raw_archive = _lexical_path(archive)
    if raw_package.is_symlink() or not raw_package.is_dir():
        raise BundleError(
            "legacy_package_invalid",
            f"Legacy release package is not a regular directory: {raw_package}",
        )
    if raw_archive.is_symlink() or not raw_archive.is_file():
        raise BundleError(
            "archive_invalid",
            f"Release archive is missing: {raw_archive}",
        )

    package = raw_package.resolve(strict=False)
    archive_path = raw_archive.resolve(strict=False)
    for path in [package, *sorted(package.rglob("*"))]:
        if path.is_symlink():
            raise BundleError(
                "legacy_package_invalid",
                f"Legacy release package contains a symlink: {path}",
            )
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            observed_mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o755 if observed_mode & 0o100 else 0o644)
        else:
            raise BundleError(
                "legacy_package_invalid",
                f"Legacy release package contains a special file: {path}",
            )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.canonical.",
        dir=str(archive_path.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _create_archive(package, temporary)
        _validate_archive(temporary, package)
        temporary.chmod(0o644)
        os.replace(temporary, archive_path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return archive_path


def validate_release_outputs(
    package_dir: PathInput,
    runner: Optional[PathInput] = None,
    archive: Optional[PathInput] = None,
    *,
    allow_historical_required_paths: bool = False,
) -> Dict[str, Any]:
    raw_package = _lexical_path(package_dir)
    if raw_package.is_symlink():
        raise BundleError(
            "manifest_invalid",
            f"Release package must not be a symlink: {raw_package}",
        )
    package = raw_package.resolve(strict=False)
    manifest = _validate_package(
        package,
        allow_historical_required_paths=allow_historical_required_paths,
    )
    if runner is not None:
        raw_runner = _lexical_path(runner)
        if raw_runner.is_symlink():
            raise BundleError(
                "runner_invalid",
                f"Top-level runner must not be a symlink: {raw_runner}",
            )
        _validate_runner(raw_runner.resolve(strict=False), manifest)
    if archive is not None:
        raw_archive = _lexical_path(archive)
        if raw_archive.is_symlink():
            raise BundleError(
                "archive_invalid",
                f"Release archive must not be a symlink: {raw_archive}",
            )
        _validate_archive(raw_archive.resolve(strict=False), package)
    return manifest


def _validate_existing_outputs(
    package_dir: Path,
    runner: Path,
    archive: Path,
) -> None:
    package_exists = _path_exists(package_dir)
    runner_exists = _path_exists(runner)
    archive_exists = _path_exists(archive)
    if not package_exists:
        if runner_exists or archive_exists:
            raise BundleError(
                "unclassified_destination",
                "Release runner/archive exists without a classified package",
            )
        return
    if package_dir.is_symlink():
        raise BundleError(
            "symlink_package_destination",
            f"Release package destination must not be a symlink: {package_dir}",
        )
    if not package_dir.is_dir():
        raise BundleError(
            "unclassified_destination",
            f"Release package destination is not a directory: {package_dir}",
        )
    manifest_path = package_dir / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BundleError(
            "missing_build_marker",
            f"Existing release package has no valid build marker: {package_dir}",
        )
    marker = _read_json(manifest_path, "invalid_build_marker")
    if (
        marker.get("schema") != MANIFEST_SCHEMA
        or marker.get("classification") != MANIFEST_CLASSIFICATION
    ):
        raise BundleError(
            "unclassified_destination",
            f"Existing release package is not classified for replacement: {package_dir}",
        )
    if (
        type(marker.get("schema_version")) is not int
        or marker.get("schema_version") != MANIFEST_VERSION
    ):
        raise BundleError(
            "invalid_build_marker",
            f"Existing release package marker version is unsupported: {package_dir}",
        )
    try:
        validate_release_outputs(
            package_dir,
            runner if runner_exists else None,
            archive if archive_exists else None,
        )
    except BundleError as error:
        raise BundleError(
            "invalid_existing_bundle",
            f"Existing classified release output is invalid: {error}",
        ) from error


def _classify_existing_outputs(
    package_dir: Path,
    runner: Path,
    archive: Path,
) -> Dict[Path, PathIdentity]:
    destinations = (package_dir, runner, archive)
    before = _capture_output_identities(destinations, "classification precheck")
    _validate_existing_outputs(package_dir, runner, archive)
    after = _capture_output_identities(destinations, "classification postcheck")
    if before != after:
        raise BundleError(
            "destination_changed",
            "Release outputs changed while their classification was validated",
        )
    return after


def _create_owned_workdir(output_root: Path, prefix: str, kind: str) -> Path:
    try:
        workdir = Path(tempfile.mkdtemp(prefix=prefix, dir=str(output_root)))
        _write_json(
            workdir / WORKDIR_MARKER_NAME,
            {
                "schema": WORKDIR_SCHEMA,
                "schema_version": WORKDIR_VERSION,
                "classification": WORKDIR_CLASSIFICATION,
                "kind": kind,
                "token": workdir.name,
            },
        )
    except OSError as error:
        raise BundleError(
            "workdir_create_failed",
            f"Could not create release {kind} directory: {error}",
        ) from error
    return workdir


def _cleanup_owned_workdir(workdir: Path, kind: str) -> None:
    if not _path_exists(workdir):
        return
    if workdir.is_symlink() or not workdir.is_dir():
        raise BundleError(
            "cleanup_failed",
            f"Refusing to clean unclassified release {kind} path: {workdir}",
        )
    marker_path = workdir / WORKDIR_MARKER_NAME
    marker = _read_json(marker_path, "cleanup_failed")
    if marker != {
        "schema": WORKDIR_SCHEMA,
        "schema_version": WORKDIR_VERSION,
        "classification": WORKDIR_CLASSIFICATION,
        "kind": kind,
        "token": workdir.name,
    }:
        raise BundleError(
            "cleanup_failed",
            f"Refusing to clean release {kind} path with invalid marker: {workdir}",
        )
    try:
        shutil.rmtree(workdir)
    except OSError as error:
        raise BundleError(
            "cleanup_failed",
            f"Could not clean release {kind} directory {workdir}: {error}",
        ) from error


def _create_archive(package_dir: Path, archive_path: Path) -> None:
    try:
        paths = [package_dir, *sorted(package_dir.rglob("*"))]
        with archive_path.open("wb") as raw_archive:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_archive,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    for path in paths:
                        relative = path.relative_to(package_dir)
                        arcname = (
                            PACKAGE_NAME
                            if not relative.parts
                            else f"{PACKAGE_NAME}/{relative.as_posix()}"
                        )
                        info = archive.gettarinfo(str(path), arcname=arcname)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        info.pax_headers = {}
                        if path.is_file():
                            with path.open("rb") as source:
                                archive.addfile(info, source)
                        else:
                            archive.addfile(info)
    except (OSError, tarfile.TarError) as error:
        raise BundleError(
            "archive_failed",
            f"Could not create release archive: {error}",
        ) from error


def _rollback_finalization(
    promoted: List[Tuple[Path, Path]],
    backed_up: List[Tuple[Path, Path]],
) -> List[str]:
    errors: List[str] = []
    for candidate, destination in reversed(promoted):
        if not _path_exists(destination):
            continue
        if _path_exists(candidate):
            errors.append(f"candidate rollback path already exists: {candidate}")
            continue
        try:
            os.replace(destination, candidate)
        except OSError as error:
            errors.append(f"could not retain failed candidate {destination}: {error}")
    for destination, backup in reversed(backed_up):
        if not _path_exists(backup):
            continue
        if _path_exists(destination):
            errors.append(f"rollback destination is occupied: {destination}")
            continue
        try:
            os.replace(backup, destination)
        except OSError as error:
            errors.append(f"could not restore {destination}: {error}")
    return errors


def _validate_moved_classified_outputs(
    backup_dir: Path,
    approved: Mapping[Path, PathIdentity],
    moved: Mapping[Path, Path],
    package_dir: Path,
    runner: Path,
    archive: Path,
) -> None:
    for destination, backup in moved.items():
        _require_output_identity(
            backup,
            approved[destination],
            "backup validation precheck",
        )

    if package_dir in approved:
        backup_package = moved[package_dir]
        backup_runner = moved.get(runner)
        backup_archive = moved.get(archive)
        try:
            validate_release_outputs(
                backup_package,
                backup_runner,
                backup_archive,
            )
        except BundleError as error:
            raise BundleError(
                "destination_changed",
                f"Moved release outputs no longer match their classification: {error}",
            ) from error
    elif approved:
        raise BundleError(
            "destination_changed",
            "Classified release outputs have no package root",
        )

    for destination, backup in moved.items():
        _require_output_identity(
            backup,
            approved[destination],
            "backup validation postcheck",
        )
    if set(backup_dir.iterdir()) != {
        backup_dir / WORKDIR_MARKER_NAME,
        *moved.values(),
    }:
        raise BundleError(
            "destination_changed",
            f"Release backup directory contains unexpected entries: {backup_dir}",
        )


def _cleanup_empty_backup(backup_dir: Path) -> None:
    expected_entries = {backup_dir / WORKDIR_MARKER_NAME}
    try:
        actual_entries = set(backup_dir.iterdir())
    except OSError as error:
        raise BundleError(
            "rollback_failed",
            f"Could not inspect rolled-back release backup {backup_dir}: {error}",
            preserve_workdirs=True,
        ) from error
    if actual_entries != expected_entries:
        raise BundleError(
            "rollback_failed",
            f"Rolled-back release backup contains unbound entries and was preserved: "
            f"{backup_dir}",
            preserve_workdirs=True,
        )
    _cleanup_owned_workdir(backup_dir, "backup")


def _finalize_outputs(
    output_root: Path,
    staged_package: Path,
    staged_runner: Path,
    staged_archive: Path,
    replace_path: ReplacePath,
) -> None:
    package_dir = output_root / PACKAGE_NAME
    runner = output_root / RUNNER_NAME
    archive = output_root / ARCHIVE_NAME
    destinations = (
        (staged_package, package_dir),
        (staged_runner, runner),
        (staged_archive, archive),
    )
    approved = _classify_existing_outputs(package_dir, runner, archive)
    backup_dir = _create_owned_workdir(output_root, BACKUP_PREFIX, "backup")
    backed_up: List[Tuple[Path, Path]] = []
    moved: Dict[Path, Path] = {}
    promoted: List[Tuple[Path, Path]] = []
    try:
        for _candidate, destination in destinations:
            expected = approved.get(destination)
            if expected is None:
                if _path_exists(destination):
                    raise BundleError(
                        "destination_changed",
                        f"Unclassified release output appeared before backup: "
                        f"{destination}",
                    )
                continue
            _require_output_identity(
                destination,
                expected,
                "before backup rename",
            )
            backup = backup_dir / destination.name
            replace_path(destination, backup)
            backed_up.append((destination, backup))
            moved[destination] = backup
            _require_output_identity(
                backup,
                expected,
                "after backup rename",
            )
            if _path_exists(destination):
                raise BundleError(
                    "destination_changed",
                    f"Release output reappeared after backup rename: {destination}",
                )
        _validate_moved_classified_outputs(
            backup_dir,
            approved,
            moved,
            package_dir,
            runner,
            archive,
        )
        for candidate, destination in destinations:
            if _path_exists(destination):
                raise BundleError(
                    "destination_changed",
                    f"Unclassified release output appeared before promotion: "
                    f"{destination}",
                )
            replace_path(candidate, destination)
            promoted.append((candidate, destination))
        validate_release_outputs(package_dir, runner, archive)
        _validate_moved_classified_outputs(
            backup_dir,
            approved,
            moved,
            package_dir,
            runner,
            archive,
        )
    except Exception as error:
        rollback_errors = _rollback_finalization(promoted, backed_up)
        if rollback_errors:
            raise BundleError(
                "rollback_failed",
                "Release finalization failed and rollback was incomplete: "
                + "; ".join(rollback_errors),
                preserve_workdirs=True,
            ) from error
        _cleanup_empty_backup(backup_dir)
        if isinstance(error, BundleError):
            raise error
        raise BundleError(
            "finalization_failed",
            f"Release finalization failed and prior outputs were restored: {error}",
        ) from error
    _cleanup_owned_workdir(backup_dir, "backup")


def build_release_bundle(
    repo_root: PathInput,
    output_root: PathInput,
    *,
    copy_path: Optional[CopyPath] = None,
    replace_path: Optional[ReplacePath] = None,
) -> BundleReceipt:
    raw_output = _lexical_path(output_root)
    raw_package = raw_output / PACKAGE_NAME
    repo, output, package_dir = validate_package_destination(
        repo_root,
        raw_output,
        raw_package,
    )
    version = _validate_source_repo(repo)
    runner = output / RUNNER_NAME
    archive = output / ARCHIVE_NAME
    _classify_existing_outputs(package_dir, runner, archive)

    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BundleError(
            "output_create_failed",
            f"Could not create release output root {output}: {error}",
        ) from error

    repo, output, package_dir = validate_package_destination(
        repo,
        output,
        output / PACKAGE_NAME,
    )
    runner = output / RUNNER_NAME
    archive = output / ARCHIVE_NAME
    _classify_existing_outputs(package_dir, runner, archive)

    stage_dir = _create_owned_workdir(output, STAGING_PREFIX, "staging")
    stage_package = stage_dir / PACKAGE_NAME
    stage_runner = stage_dir / RUNNER_NAME
    stage_archive = stage_dir / ARCHIVE_NAME
    copy_operation = copy_path or _default_copy_path
    replace_operation = replace_path or os.replace
    primary_error: Optional[BaseException] = None
    preserve_workdirs = False
    receipt: Optional[BundleReceipt] = None
    try:
        stage_package.mkdir(mode=0o755)
        stage_package.chmod(0o755)
        _copy_release_allowlist(repo, stage_package, copy_operation)
        _validate_generated_script_references(stage_package)
        _validate_runtime_imports(stage_package)
        try:
            copy_operation(stage_package / RUNNER_NAME, stage_runner)
        except Exception as error:
            raise BundleError(
                "copy_failed",
                f"Top-level runner copy failed: {error}",
            ) from error
        stage_runner.chmod(0o755)
        manifest = _create_manifest(stage_package, version)
        _create_archive(stage_package, stage_archive)
        validate_release_outputs(stage_package, stage_runner, stage_archive)
        _finalize_outputs(
            output,
            stage_package,
            stage_runner,
            stage_archive,
            replace_operation,
        )
        receipt = BundleReceipt(
            package_dir=package_dir,
            runner=runner,
            archive=archive,
            manifest=package_dir / MANIFEST_NAME,
            payload_sha256=str(manifest["payload_sha256"]),
        )
    except BaseException as error:
        primary_error = error
        preserve_workdirs = bool(
            isinstance(error, BundleError) and error.preserve_workdirs
        )

    if not preserve_workdirs:
        try:
            _cleanup_owned_workdir(stage_dir, "staging")
        except BundleError as cleanup_error:
            if primary_error is None:
                raise
            raise BundleError(
                "cleanup_failed",
                f"{primary_error}; staging cleanup also failed: {cleanup_error}",
                preserve_workdirs=True,
            ) from primary_error
    if primary_error is not None:
        raise primary_error
    if receipt is None:
        raise BundleError("internal_error", "Release bundle produced no receipt")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and safely publish the codex-switch release bundle."
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = build_release_bundle(args.repo_root, args.output_root)
    except BundleError as error:
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
        return 2
    print(receipt.archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
