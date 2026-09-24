#!/usr/bin/env python3
"""Durable candidate updates; caller configuration stays private until apply."""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager, redirect_stdout
import fcntl
import hashlib
import json
import os
import plistlib
from pathlib import Path
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_profile_switch import add_global_arguments
from codex_switch_constants import SwitchError
from codex_switch_first_install import create_directory
from codex_switch_home_select import resolve_runtime_homes
from codex_switch_internal_runtime import runtime_manifest
from codex_switch_runtime_binding import discover_desktop_hosts
from codex_switch_store import make_store
from codex_switch_update_policy import extract_semantic_version, parse_semantic_version


SCHEMA = 1
ID_PATTERN = re.compile(r"^[a-f0-9]{24}$")
TERMINAL = frozenset({"applied", "failed", "stale", "cancelled"})


class UpdateError(SwitchError):
    def __init__(self, message, *, reason="invalid", exit_code=1):
        super().__init__(message)
        self.reason = reason
        self.exit_code = exit_code


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_state(path, *, payload=False):
    """Read one regular file without following links, detecting concurrent changes."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise UpdateError(f"Not a regular update input: {path}")
    h = hashlib.sha256()
    chunks = []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise UpdateError(f"Input changed while opening: {path}", reason="stale")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
            if payload:
                chunks.append(block)
                if sum(map(len, chunks)) > 32 * 1024 * 1024:
                    raise UpdateError(f"Update input exceeds size limit: {path}")
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or _identity(after) != _identity(path.lstat()):
        raise UpdateError(f"Input changed while reading: {path}", reason="stale")
    result = {"sha256": h.hexdigest(), "mode": stat.S_IMODE(after.st_mode), "size": after.st_size}
    return (result, b"".join(chunks)) if payload else result


def directory_state(path, *, private=False, owned=True):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or (owned and info.st_uid != os.getuid()):
        raise UpdateError(f"Update directory is not an owned real directory: {path}")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise UpdateError(f"Update directory must be private: {path}")
    return {"device": info.st_dev, "inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode)}


def absolute_path(raw):
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise UpdateError("Update paths must be absolute and normalized")
    for parent in path.parents:
        if parent.is_symlink() and parent not in (Path("/tmp"), Path("/var"), Path("/etc")):
            raise UpdateError(f"Update path has a symlinked parent: {parent}")
    # Canonicalize platform aliases in the existing ancestor, not file links.
    suffix = []
    parent = path.parent
    while not parent.exists():
        suffix.insert(0, parent.name)
        parent = parent.parent
    return parent.resolve(strict=True).joinpath(*suffix, path.name)


def atomic_json(path, value):
    value = dict(value)
    value.pop("record_sha256", None)
    value["record_sha256"] = digest(value)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_record(path):
    result = file_state(path, payload=True)
    if result is None:
        raise UpdateError("Unknown update ID", reason="not_found")
    state, raw = result
    if state["mode"] != 0o600 or path.stat().st_uid != os.getuid():
        raise UpdateError("Update record is not private")
    try:
        value = json.loads(raw)
        checksum = value.pop("record_sha256")
        if checksum != digest(value) or value["schema_version"] != SCHEMA:
            raise ValueError()
        if value["update_id"] != path.parent.name:
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UpdateError("Update record integrity or schema is invalid") from exc


def update_root(store, update_id):
    if not ID_PATTERN.fullmatch(update_id):
        raise UpdateError("Invalid update ID")
    root = store.root / "updates" / update_id
    for p in (store.root, root.parent, root):
        if directory_state(p, private=p != store.root) is None:
            raise UpdateError("Unknown update ID", reason="not_found")
    return root


@contextmanager
def record_lock(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("Update is busy", reason="busy") from exc
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (root.stat().st_dev, root.stat().st_ino):
            raise UpdateError("Update directory changed", reason="stale")
        yield
    finally:
        os.close(fd)


def probe_version(path):
    from codex_switch_verify import run_bounded_process
    with tempfile.TemporaryDirectory(prefix="codex-update-version-") as temp:
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": temp,
               "CODEX_HOME": temp, "PYTHONDONTWRITEBYTECODE": "1"}
        result = run_bounded_process([str(path), "--version"], kind="runtime version",
                                     env=env, timeout_seconds=30, cwd=temp)
    version = extract_semantic_version(result.stdout)
    if result.timed_out:
        raise UpdateError("Runtime version probe timed out", reason="version_failed")
    if result.returncode:
        raise UpdateError(f"Runtime version probe failed (exit {result.returncode})", reason="version_failed")
    if not version:
        raise UpdateError("Runtime version could not be verified", reason="version_failed")
    return version


def runtime_identity(path):
    state = file_state(path)
    if state is None or not state["mode"] & 0o111 or not state["size"]:
        raise UpdateError("Runtime entry is not an executable file")
    result = {"entry": state, "directory": directory_state(path.parent)}
    # Materialized standalone launchers embed a closed package manifest. Read
    # literals only; never evaluate launcher code to discover its assets.
    with path.open("rb") as stream:
        marker = stream.read(100)
    if b"# codex-switch standalone runtime launcher\n" in marker:
        _, raw = file_state(path, payload=True)
        try:
            argv = shlex.split(raw.decode().split("\n", 2)[2])
            program = ast.parse(argv[argv.index("-c") + 1])
            assignments = {n.targets[0].id: n.value for n in program.body
                           if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
            root_expr = assignments["root"]
            if not isinstance(root_expr, ast.Call) or not isinstance(root_expr.func, ast.Name) or root_expr.func.id != "Path":
                raise ValueError()
            root = Path(ast.literal_eval(root_expr.args[0]))
            expected = ast.literal_eval(assignments["expected"])
            directory_state(root.parent, private=True)
            directory_state(root, private=True)
            observed = runtime_manifest(root)
            if observed != expected:
                raise ValueError()
            result["package_path"] = str(root)
            result["package_manifest"] = observed
            result["package_directory"] = directory_state(root, private=True)
        except (ValueError, KeyError, SyntaxError, IndexError, UnicodeError) as exc:
            raise UpdateError("Standalone runtime manifest is invalid") from exc
    return result


def desktop_reference():
    inventory = discover_desktop_hosts()
    host = inventory.current
    if host is None:
        return {"present": False}
    if not host.healthy:
        raise UpdateError("The installed Desktop reference is unhealthy")
    identity = file_state(host.bundled_cli)
    if identity is None:
        raise UpdateError("Desktop bundled CLI is missing")
    return {"present": True, "bundle_path": str(host.bundle_root),
            "bundled_cli_path": str(host.bundled_cli), "bundle_id": host.bundle_id,
            "version": probe_version(host.bundled_cli), "sha256": identity["sha256"],
            "bundle_identity": directory_state(host.bundle_root, owned=False),
            "info_plist": file_state(host.bundle_root / "Contents/Info.plist")}


def tree_state(root):
    identity = directory_state(root)
    if identity is None:
        return None
    files = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        info = path.lstat()
        files[key] = directory_state(path) if stat.S_ISDIR(info.st_mode) else file_state(path)
    return {"directory": identity, "files": files}


def context_snapshot(store, target):
    plan = resolve_runtime_homes(store)
    homes = {name: str(absolute_path(home.path)) for name, home in
             (("internal", plan.internal), ("official", plan.official))}
    reference = desktop_reference()
    # Released init supports explicit profile paths distinct from the actual
    # Desktop reference. Freeze both independently; require_frozen compares
    # each with its own prior state without rewriting the saved binding intent.
    return {"store": directory_state(store.root), "target": file_state(target),
            "target_parent": directory_state(target.parent),
            "profiles": {name: tree_state(store.profile_dir(name)) for name in ("internal", "openai-official")},
            "active": file_state(store.active_path),
            "homes": homes, "home_identity": {k: directory_state(Path(v)) for k, v in homes.items()},
            "home_inputs": {k: {n: file_state(Path(v) / n) for n in ("config.toml", "auth.json")}
                            for k, v in homes.items()},
            "desktop_reference": reference,
            "launcher": file_state(store.bin_dir / "codex-internal-app"),
            "launch_agent": file_state(store.launch_agent_path),
            "backup": file_state(target.with_name(".codex-internal-backup"))}


def require_no_pending(store):
    if os.path.lexists(store.root / ".runtime-binding-rebind.json") or any(store.root.glob(".pending*")):
        raise UpdateError("Unresolved store transaction requires recovery", reason="recovery_required")


def select_target(args, store):
    manifest_path = store.manifest_path("internal")
    if file_state(manifest_path) is not None:
        manifest = store.load_manifest("internal")
        target = absolute_path(str(manifest.get("codex_bin", "")))
        if args.internal_bin and absolute_path(args.internal_bin) != target:
            raise UpdateError("Requested target does not match the internal profile")
    else:
        if os.path.lexists(store.profile_dir("internal")):
            raise UpdateError("Partial internal profile requires repair")
        target = absolute_path(args.internal_bin or str(Path(args.install_dir or os.environ.get("CODEX_INSTALL_DIR") or (Path.home() / ".local/bin")) / "codex"))
    if target.name != "codex":
        raise UpdateError("Internal update target must be named codex")
    if args.install_dir and absolute_path(str(Path(args.install_dir) / "codex")) != target:
        raise UpdateError("Install directory disagrees with the bound target")
    return target


def public_record(record):
    keys = ("schema_version", "update_id", "state", "runtime_path", "actual_version",
            "runtime_digest", "source", "desktop_reference", "profile_present",
            "internal_app_bound", "reason", "message", "transaction_id", "scope")
    result = {key: record[key] for key in keys if key in record}
    result["safe_to_restore"] = record.get("state") in {"staged", "failed", "stale", "cancelled"}
    return result


def internal_app_bound(store):
    from codex_switch_selection import active_profile_selection
    raw = file_state(store.active_path, payload=True)
    if raw is not None:
        selection = active_profile_selection(json.loads(raw[1]))
        if selection.app_profile == "internal":
            return True
    raw = file_state(store.launch_agent_path, payload=True)
    if raw is not None:
        data = plistlib.loads(raw[1])
        cli = data.get("EnvironmentVariables", {}).get("CODEX_CLI_PATH", "")
        if cli and absolute_path(cli) == store.bin_dir / "codex-internal-app":
            return True
    return False


def require_bootstrap_target(store, target):
    raw = file_state(store.root / ".internal-cli-bootstrap.json", payload=True)
    state = file_state(target)
    if raw is None or state is None or raw[0]["mode"] != 0o600:
        raise UpdateError("Unregistered target requires a verified bootstrap or profile capture")
    try:
        receipt = json.loads(raw[1])
        if (receipt["schema_version"] != 1 or receipt["target_path"] != str(target)
                or receipt["sha256"] != state["sha256"] or receipt["mode"] != state["mode"]
                or receipt["version"] != probe_version(target)):
            raise ValueError()
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateError("Bootstrap runtime identity changed; repair or capture the existing CLI") from exc
    if os.path.lexists(target.with_name(".codex-internal-backup")):
        raise UpdateError("Unresolved backup prevents profileless runtime adoption")


def run_installer(command):
    child = subprocess.Popen(command, stdout=sys.stderr, stderr=sys.stderr, start_new_session=True)
    try:
        code = child.wait()
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        raise
    if code:
        raise UpdateError(f"Candidate installer exited {code}; live runtime unchanged",
                          reason="installer_failed", exit_code=code if code > 0 else 128-code)


def stage(args, store):
    target = select_target(args, store)
    if args.current and args.version:
        raise UpdateError("--current and --version are mutually exclusive", exit_code=2)
    if not args.current and (not args.version or parse_semantic_version(args.version) is None):
        raise UpdateError("Stage requires an exact semantic --version or --current", exit_code=2)
    require_no_pending(store)
    if not store.manifest_path("internal").exists() and os.path.lexists(target):
        require_bootstrap_target(store, target)
    if args.current and not os.path.lexists(target):
        raise UpdateError("--current requires an installed internal runtime")
    create_directory(store.root)
    create_directory(target.parent)
    updates = store.root / "updates"
    try:
        updates.mkdir(mode=0o700)
    except FileExistsError:
        pass
    directory_state(updates, private=True)
    update_id = secrets.token_hex(12)
    args.update_id = update_id
    root = updates / update_id
    root.mkdir(mode=0o700)
    with record_lock(root):
        snapshot = context_snapshot(store, target)
        candidate = target if args.current else target.parent / (".codex-internal-update-" + update_id) / "codex"
        record = {"schema_version": SCHEMA, "update_id": update_id, "state": "staging",
                  "store_path": str(store.root), "target_path": str(target),
                  "runtime_path": str(candidate), "source": "current" if args.current else "staged",
                  "desktop_reference": snapshot["desktop_reference"], "snapshot": snapshot,
                  "profile_present": snapshot["profiles"]["internal"] is not None,
                  "internal_app_bound": internal_app_bound(store)}
        atomic_json(root / "record.json", record)
        try:
            if not args.current:
                command = [args.helper, "update-internal", "--version", args.version,
                           "--internal-bin", str(target), "--install-dir", str(candidate.parent)]
                if not os.path.lexists(target):
                    command.append("--first-install")
                for key in ("installer_url", "latest_url", "model", "azure_base_url"):
                    if getattr(args, key, None):
                        command.extend(["--" + key.replace("_", "-"), getattr(args, key)])
                for key in ("skip_proxy", "skip_source_check"):
                    if getattr(args, key, False):
                        command.append("--" + key.replace("_", "-"))
                run_installer(command)
            runtime = runtime_identity(candidate)
            version = probe_version(candidate)
            if args.version and version != args.version:
                raise UpdateError(f"Candidate version mismatch: expected {args.version} but observed {version}")
            if runtime_identity(candidate) != runtime or context_snapshot(store, target) != snapshot:
                raise UpdateError("Stage inputs changed while preparing candidate", reason="stale")
            record.update(state="staged", actual_version=version, runtime_identity=runtime,
                          runtime_digest=digest(runtime))
            atomic_json(root / "record.json", record)
            return public_record(record)
        except BaseException as exc:
            record.update(state="stale" if getattr(exc, "reason", "") == "stale" else "failed",
                          reason=getattr(exc, "reason", "stage_failed"), message=str(exc))
            atomic_json(root / "record.json", record)
            raise


def status(args, store):
    root = update_root(store, args.update_id)
    record = read_record(root / "record.json")
    if record["store_path"] != str(store.root):
        raise UpdateError("Update belongs to a different store")
    terminal = terminal_result(store, record)
    if terminal is not None:
        return public_record(terminal)
    if record["state"] in {"staging", "applying"}:
        try:
            with record_lock(root):
                record["state"] = "recovery_required"
        except UpdateError as exc:
            if exc.reason != "busy":
                raise
    return public_record(record)


def terminal_result(store, record):
    if not record.get("input_fingerprint"):
        return None
    from codex_switch_transaction import read_runtime_binding_terminal_receipt
    receipt = read_runtime_binding_terminal_receipt(
        store, record["update_id"], input_fingerprint=record["input_fingerprint"])
    if receipt is None:
        return None
    result = dict(record)
    result["state"] = "applied" if receipt["state"] == "committed" else "failed"
    result["transaction_id"] = receipt["transaction_id"]
    return result


def source_inputs(store, source_home, *, committed_input=None):
    source = absolute_path(source_home) if source_home is not None else store.profile_dir("internal")
    if directory_state(source) is None:
        raise UpdateError("Apply requires an existing configuration source directory")
    states, payloads = {}, {}
    for name in ("config.toml", "auth.json"):
        result = file_state(source / name, payload=True)
        states[name] = None if result is None else result[0]
        payloads[name] = None if result is None else result[1]
    if payloads["config.toml"] is None:
        raise UpdateError("Apply requires config.toml in the selected profile input")
    try:
        config = tomllib.loads(payloads["config.toml"].decode())
        if payloads["auth.json"] is not None and not isinstance(json.loads(payloads["auth.json"]), dict):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("Apply configuration or authentication input is malformed") from exc
    catalog = config.get("model_catalog_json")
    source_catalog = None
    if catalog is not None:
        if not isinstance(catalog, str) or not catalog:
            raise UpdateError("model_catalog_json must be a nonempty absolute path")
        path = absolute_path(catalog)
        profile_config = store.profile_dir("internal") / "config.toml"
        previous_catalog = (committed_input or {}).get("catalog")
        managed_overlay = path == profile_config.parent / "parity/model-catalog.json"
        if (managed_overlay and previous_catalog and previous_catalog.get("path") == str(path)
                and previous_catalog.get("origin")):
            # Replay verifies caller inputs, not the derived overlay this
            # confirmed transaction replaced. This is a historical result;
            # checking current installation health remains verify's job.
            source_catalog = dict(previous_catalog)
            origin = dict(previous_catalog["origin"])
            origin["identity"] = file_state(absolute_path(origin["path"]))
            source_catalog["origin"] = origin
        else:
            source_catalog = {"path": str(path), "identity": file_state(path)}
            if source_catalog["identity"] is None:
                raise UpdateError("Configured catalog source is missing")
        if managed_overlay and "origin" not in source_catalog:
            # An overlay is a derived output. Bind the verified original too,
            # before preparation can collect a newer source generation.
            from codex_switch_parity import ConfigInputs, _source_catalog_from_candidate
            candidate = SimpleNamespace(
                source_config=ConfigInputs.capture(
                    profile_config=profile_config, profile_source=source / "config.toml",
                    source_paths=(source / "config.toml",)),
                internal_manifest=store.load_manifest("internal"),
                internal_binding=SimpleNamespace(codex_home=resolve_runtime_homes(store).internal.path),
            )
            origin, custom = _source_catalog_from_candidate(candidate, configured_path=path)
            origin = absolute_path(origin)
            source_catalog["origin"] = {"path": str(origin), "custom": custom,
                                        "identity": file_state(origin)}
    frozen = {"source_home": str(source), "source_kind": "explicit" if source_home is not None else "saved",
              "directory": directory_state(source), "files": states, "catalog": source_catalog}
    return frozen, payloads


def require_frozen(store, record, source_home=None, expected_inputs=None):
    target, candidate = Path(record["target_path"]), Path(record["runtime_path"])
    require_no_pending(store)
    if runtime_identity(candidate) != record["runtime_identity"]:
        raise UpdateError("Candidate runtime changed after stage", reason="stale")
    if context_snapshot(store, target) != record["snapshot"]:
        raise UpdateError("Profile, target, home or Desktop changed after stage", reason="stale")
    if expected_inputs is not None and source_inputs(store, source_home)[0] != expected_inputs:
        raise UpdateError("Apply source configuration or catalog changed", reason="stale")


def persist_payload(path, payload, *, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def copy_executable(source, destination):
    expected = file_state(source)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, expected["mode"])
    with os.fdopen(source_fd, "rb") as origin, os.fdopen(target_fd, "wb") as target:
        for chunk in iter(lambda: origin.read(1024 * 1024), b""):
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
    if file_state(source) != expected or file_state(destination) != expected:
        raise UpdateError("Publication candidate changed while copying", reason="stale")
    sync_directory(destination.parent)
    return expected


def apply_update(args, store):
    from codex_switch_bindings import (PrivateRuntimePreparation, cmd_set_bin,
                                       _verify_internal_update_promotion)
    from codex_switch_transaction import RuntimeBindingExecutableSwap
    root = update_root(store, args.update_id)
    with record_lock(root):
        record = read_record(root / "record.json")
        if record["store_path"] != str(store.root):
            raise UpdateError("Update belongs to another store")
        if record.get("scope") == "cli-only":
            raise UpdateError("CLI-only update cannot be reused for full apply; stage --current")
        confirmed = terminal_result(store, record)
        if confirmed is not None or record["state"] in TERMINAL:
            if record.get("input_fingerprint"):
                # The saved profile is itself a published output. An omitted
                # argument repeats its already frozen selection, not its new bytes.
                if args.from_codex_home is None and record["input"]["source_kind"] == "saved":
                    catalog = record["input"].get("catalog")
                    if catalog:
                        origin = catalog.get("origin", catalog)
                        if file_state(absolute_path(origin["path"])) != origin["identity"]:
                            raise UpdateError("Update ID was already bound to different inputs", reason="input_mismatch")
                elif digest(source_inputs(store, args.from_codex_home,
                        committed_input=record["input"] if confirmed and confirmed["state"] == "applied" else None)[0]) != record["input_fingerprint"]:
                    raise UpdateError("Update ID was already bound to different inputs", reason="input_mismatch")
            result = confirmed or record
            if result["state"] != "applied":
                raise UpdateError("This update is terminal; stage a new candidate", reason=result["state"])
            return public_record(result)
        if record["state"] != "staged":
            raise UpdateError("Interrupted update requires status/cancel before another apply", reason="recovery_required")
        try:
            require_frozen(store, record)
            if not record["desktop_reference"]["present"]:
                raise UpdateError("Full apply requires an installed verified Desktop reference")
            inputs, payloads = source_inputs(store, args.from_codex_home)
            fingerprint = digest(inputs)
            private_home = root / "inputs"
            private_home.mkdir(mode=0o700)
            for name, payload in payloads.items():
                if payload is not None:
                    persist_payload(private_home / name, payload)
            record.update(state="applying", input=inputs, input_fingerprint=fingerprint, scope="full")
            atomic_json(root / "record.json", record)
            target, candidate = Path(record["target_path"]), Path(record["runtime_path"])
            manifest = store.load_manifest("internal") if record["profile_present"] else {
                "name": "internal", "managed_files": ["config.toml", "auth.json"],
                "codex_bin": str(target), "codex_home": record["snapshot"]["homes"]["internal"],
                "home_selection_confirmed": True,
            }
            swap = None
            if record["source"] != "current":
                publication = target.parent / (".codex-internal-update-" + record["update_id"] + "-publish")
                publication.mkdir(mode=0o700)
                candidate_file = copy_executable(candidate, publication / "codex")
                old = record["snapshot"]["target"]
                swap = RuntimeBindingExecutableSwap(
                    bound_path=target, candidate_path=publication / "codex",
                    backup_path=target.with_name(".codex-internal-backup-" + record["update_id"]),
                    old_mode=None if old is None else old["mode"],
                    old_sha256=None if old is None else old["sha256"],
                    new_mode=candidate_file["mode"], new_sha256=candidate_file["sha256"],
                )
            preparation = PrivateRuntimePreparation(
                candidate_manifest=manifest,
                source_home=private_home if args.from_codex_home is not None else None,
                update_id=record["update_id"], input_fingerprint=fingerprint,
                frozen_input_validator=lambda: require_frozen(store, record, args.from_codex_home, inputs),
                allow_absent_profile=not record["profile_present"],
            )
            rebind_options = dict(vars(args))
            rebind_options.update(name="internal", codex_bin=str(target),
                preserve_app_cli=False, rebind_private_inputs=preparation,
                rebind_execution_codex_bin=str(candidate), rebind_executable_swap=swap,
                rebind_retire_executable_backup=swap is not None, suppress_rebind_success_output=True,
                rebind_prepared_validator=lambda result: _verify_internal_update_promotion(
                    store, result=result, executable_swap=swap, target_version=record["actual_version"]))
            rebind_args = SimpleNamespace(**rebind_options)
            result = cmd_set_bin(rebind_args)
            receipt = getattr(result, "terminal_receipt", None)
            if not receipt or receipt["state"] != "committed":
                raise UpdateError("Transaction outcome is not confirmed", reason="recovery_required")
            record.update(state="applied", transaction_id=receipt["transaction_id"])
            atomic_json(root / "record.json", record)
            return public_record(record)
        except BaseException as exc:
            terminal = terminal_result(store, record)
            if terminal is not None:
                record = terminal
            elif os.path.lexists(store.root / ".runtime-binding-rebind.json"):
                record["state"] = "recovery_required"
            else:
                record["state"] = "stale" if getattr(exc, "reason", "") == "stale" else "failed"
            record.update(reason=getattr(exc, "reason", "apply_failed"), message=str(exc))
            atomic_json(root / "record.json", record)
            raise


def cancel(args, store):
    from codex_switch_transaction import locked_store_mutation
    root = update_root(store, args.update_id)
    with record_lock(root):
        record = read_record(root / "record.json")
        if record["store_path"] != str(store.root):
            raise UpdateError("Update belongs to another store")
        terminal = terminal_result(store, record)
        if terminal is not None:
            if terminal["state"] == "applied":
                return public_record(terminal)
            record = terminal
        if record["state"] == "applied":
            raise UpdateError("Committed update terminal evidence is missing", reason="recovery_required")
        marker = store.root / ".runtime-binding-rebind.json"
        def require_owned_recovery():
            raw = file_state(marker, payload=True)
            if raw is None:
                return
            data = json.loads(raw[1])
            if data.get("update", {}).get("update_id") != record["update_id"]:
                raise UpdateError("Another transaction requires recovery", reason="recovery_required")
        if os.path.lexists(marker):
            # The existing recovery mechanism verifies its journal and refuses
            # changed destinations. Check ownership under the same lock before
            # its automatic recovery, including a marker replaced while waiting.
            with locked_store_mutation(store, operation="cancel internal update",
                                       recovery_guard=require_owned_recovery) as locked:
                locked.revalidate()
            terminal = terminal_result(store, record)
            if terminal and terminal["state"] == "applied":
                return public_record(terminal)
        record["state"] = "cancelled"
        # Retain the immutable generation: it may be referenced by another
        # launcher. Cancellation owns no caller files and never deletes current.
        atomic_json(root / "record.json", record)
        return public_record(record)


def one_shot(args, store):
    """Wrapper adapter for existing full/CLI-only update policy."""
    from codex_switch_bindings import _promote_internal_cli_update
    from codex_switch_transaction import RuntimeBindingExecutableSwap
    staged = stage(args, store)
    args.update_id = staged["update_id"]
    args.from_codex_home = None
    cli_only = args.scope == "cli-only" or (args.scope == "auto" and
        not staged["desktop_reference"]["present"] and not staged["internal_app_bound"])
    if not cli_only:
        return apply_update(args, store)
    if staged["internal_app_bound"]:
        raise UpdateError("The existing internal App binding requires full compatibility validation")
    root = update_root(store, args.update_id)
    with record_lock(root):
        record = read_record(root / "record.json")
        require_frozen(store, record)
        if not record["profile_present"]:
            raise UpdateError("Profileless CLI installation must use the strict bootstrap entry")
        target, candidate = Path(record["target_path"]), Path(record["runtime_path"])
        old = record["snapshot"]["target"]
        fingerprint = digest({"scope": "cli-only", "snapshot": record["snapshot"]})
        record.update(scope="cli-only", input_fingerprint=fingerprint, state="applying")
        atomic_json(root / "record.json", record)
        try:
            publication = target.parent / (".codex-internal-update-" + record["update_id"] + "-publish")
            publication.mkdir(mode=0o700)
            new = copy_executable(candidate, publication / "codex")
            swap = RuntimeBindingExecutableSwap(target, publication / "codex",
                target.with_name(".codex-internal-backup-" + record["update_id"]),
                old["mode"], old["sha256"], new["mode"], new["sha256"])
            receipt = _promote_internal_cli_update(store,
                manifest=store.load_manifest("internal"), executable_swap=swap,
                target_version=record["actual_version"], update_id=record["update_id"],
                input_fingerprint=fingerprint,
                frozen_input_validator=lambda: require_frozen(store, record))
            if not receipt or receipt["state"] != "committed":
                raise UpdateError("CLI-only transaction outcome is unresolved", reason="recovery_required")
            record.update(state="applied", transaction_id=receipt["transaction_id"])
            atomic_json(root / "record.json", record)
            return public_record(record)
        except BaseException as exc:
            confirmed = terminal_result(store, record)
            if confirmed is not None:
                record = confirmed
            else:
                record["state"] = "recovery_required" if os.path.lexists(store.root / ".runtime-binding-rebind.json") else "failed"
            atomic_json(root / "record.json", record)
            raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    add_global_arguments(result)
    result.add_argument("--helper", default=str(Path(__file__).with_name("codex_env_setup")))
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("stage", "_one-shot"):
        prepare = sub.add_parser(name, help="Retain a private candidate" if name == "stage" else argparse.SUPPRESS)
        prepare.add_argument("--version", "--codex-install-version")
        prepare.add_argument("--current", action="store_true")
        prepare.add_argument("--internal-bin")
        prepare.add_argument("--install-dir")
        for option in ("installer-url", "latest-url", "model", "azure-base-url"):
            prepare.add_argument("--" + option)
        prepare.add_argument("--codex-install-base-url", dest="azure_base_url")
        for option in ("skip-source-check", "skip-proxy"):
            prepare.add_argument("--" + option, action="store_true")
        prepare.add_argument("--json", action="store_true")
        if name == "_one-shot":
            prepare.add_argument("--scope", choices=("full", "cli-only", "auto"), required=True)
    for name in ("apply", "status", "cancel"):
        action = sub.add_parser(name)
        action.add_argument("update_id")
        action.add_argument("--json", action="store_true")
        if name == "apply":
            action.add_argument("--from-codex-home", type=Path)
    return result


def main():
    args = parser().parse_args()
    store = make_store(args)
    def interrupted(signum, _frame):
        # Once unwinding starts, repeated terminal signals must not interrupt
        # bounded child cleanup or leave a partially recorded result.
        for pending in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(pending, signal.SIG_IGN)
        raise SystemExit(128 + signum)
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    store.root = absolute_path(store.root)
    # Keep derived Store paths canonical too.
    args.store_dir = store.root
    store = make_store(args)
    try:
        with redirect_stdout(sys.stderr):
            if args.command == "stage":
                result = stage(args, store)
            elif args.command == "status":
                result = status(args, store)
            elif args.command == "apply":
                result = apply_update(args, store)
            elif args.command == "cancel":
                result = cancel(args, store)
            elif args.command == "_one-shot":
                result = one_shot(args, store)
            else:
                raise UpdateError("Update operation is unavailable")
        print(json.dumps(result, sort_keys=True) if args.json else
              f"update-internal: {result['update_id']} {result['state']}")
        if args.command == "_one-shot" and not args.json and result.get("scope") == "full" and result["state"] == "applied":
            print(f"update-internal: verified installed version {result['actual_version']}.")
            print("App-server smoke: passed")
            print("update-internal: capability and parity receipts verified.")
            print("Restart required: quit ChatGPT completely and reopen it to use the new internal backend.")
        return 0
    except (SwitchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        error = {"schema_version": SCHEMA, "state": "failed", "safe_to_restore": False,
                 "reason": getattr(exc, "reason", "invalid"), "message": str(exc)}
        if getattr(args, "update_id", None):
            error["update_id"] = args.update_id
            try:
                saved = status(args, store)
            except (SwitchError, OSError, ValueError, subprocess.SubprocessError):
                pass
            else:
                # Preserve the actual persisted outcome, especially a commit
                # confirmed after an interrupted response. Never expose inputs.
                error.update(saved)
                error["reason"] = getattr(exc, "reason", "invalid")
                error["message"] = str(exc)
        if args.json:
            print(json.dumps(error, sort_keys=True))
        else:
            print(f"update-internal: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
