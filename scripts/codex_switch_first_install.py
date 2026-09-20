#!/usr/bin/env python3
"""Prepare and publish a first internal CLI without inventing a profile."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_switch_update_policy import extract_semantic_version, parse_semantic_version


def first_version(tag: str, blocked_text: str, fallback: str) -> str:
    version = tag
    for prefix in ("internal-rust-v", "rust-v", "v"):
        if tag.startswith(prefix):
            version = tag[len(prefix):]
            break
    parsed = parse_semantic_version(version)
    blocked = [parse_semantic_version(item) for item in blocked_text.replace(",", " ").split()]
    if parsed is None or version != version.strip() or None in blocked:
        raise ValueError("invalid first-install release tag or blocked-version policy")
    if parsed not in blocked:
        return version
    selected = parse_semantic_version(fallback)
    if selected is None or selected in blocked or fallback != fallback.strip():
        raise ValueError("blocked latest requires a valid unblocked first-install fallback")
    return fallback


def require_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError(f"first-install path already exists: {path}; capture an existing CLI or repair the profile before updating")


def directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"first-install directory is not a regular directory: {path}")


def create_directory(path: Path) -> None:
    if path.exists():
        directory(path)
        return
    create_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    directory(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def check_empty(target: Path, store: Path) -> None:
    if not target.is_absolute() or target.name != "codex" or ".." in target.parts:
        raise ValueError("first-install target must be an absolute codex path")
    if not store.is_absolute() or ".." in store.parts:
        raise ValueError("profile store must be an absolute path")
    directory(target.parent)
    directory(store)
    directory(store / "profiles")
    require_absent(store / "profiles/internal")
    require_absent(store / ".runtime-binding-rebind.json")
    require_absent(target)
    require_absent(target.with_name(".codex-internal-backup"))
    # A store with pending transaction state needs normal recovery, not bootstrap.
    if store.is_dir() and any(store.glob(".pending*")):
        raise ValueError("pending profile-store state blocks first installation")


def identity(path: Path) -> tuple:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111 or not before.st_size:
            raise ValueError(f"candidate is not a regular executable: {path}")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        linked = path.lstat()
        def fields(s):
            return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if fields(before) != fields(after) or fields(after) != fields(linked):
            raise ValueError("candidate changed during validation")
        os.fsync(stream.fileno())
        # Hard-link publication changes ctime/nlink, but not these fields or bytes.
        return (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                after.st_mtime_ns, digest.hexdigest())


def verify_version(path: Path, version: str) -> None:
    result = subprocess.run([str(path), "--version"], capture_output=True,
                            text=True, timeout=30)
    if result.returncode or extract_semantic_version(result.stdout) != version:
        raise ValueError(f"first-install version verification failed: {path}")


def check_directory_identity(path: Path, descriptor: int) -> None:
    linked, opened = path.lstat(), os.fstat(descriptor)
    if not stat.S_ISDIR(linked.st_mode) or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValueError(f"first-install directory changed: {path}")


def publish(target: Path, store: Path, candidate: Path, version: str) -> None:
    directory(store)
    create_directory(store)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    store_fd = os.open(store, flags)
    try:
        # Same directory lock as profile capture and update transactions.
        fcntl.flock(store_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_directory_identity(store, store_fd)
        check_empty(target, store)
        parent_fd = os.open(target.parent, flags)
        try:
            expected = identity(candidate)
            verify_version(candidate, version)
            if identity(candidate) != expected:
                raise ValueError("candidate changed during version verification")
            check_directory_identity(store, store_fd)
            check_directory_identity(target.parent, parent_fd)
            check_empty(target, store)
            # link() fails atomically with EEXIST, even for a dangling target link.
            complete = False
            link_failed = False
            try:
                try:
                    os.link(candidate, target.name, dst_dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    # A rejected syscall did not create our link. Even an equal
                    # hard link at the destination belongs to another publisher.
                    link_failed = True
                    raise
                if identity(target) != expected:
                    raise ValueError("published command does not match candidate")
                verify_version(target, version)
                if identity(target) != expected:
                    raise ValueError("published command changed during verification")
                check_directory_identity(target.parent, parent_fd)
                check_directory_identity(store, store_fd)
                require_absent(store / "profiles/internal")
                os.fsync(parent_fd)
                complete = True
            finally:
                if not complete and not link_failed:
                    try:
                        owned = identity(target) == expected
                    except (OSError, ValueError):
                        owned = False
                    if owned:
                        check_directory_identity(target.parent, parent_fd)
                        os.unlink(target.name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(store_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--helper")
    parser.add_argument("--version")
    parser.add_argument("--release-tag")
    parser.add_argument("--blocked-versions", default="")
    parser.add_argument("--fallback-version", default="")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    def interrupted(signum, _frame):
        for item in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(item, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    for item in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(item, interrupted)
    try:
        target, store = args.target, args.store
        check_empty(target, store)
        if args.release_tag is not None:
            print(first_version(args.release_tag, args.blocked_versions, args.fallback_version))
            return 0
        if args.check:
            return 0
        if not args.version or not args.helper:
            raise ValueError("first installation requires an intended version and installer helper")
        if args.dry_run:
            print(f"[DRY-RUN] First installation of {args.version}: {target}")
            print("[DRY-RUN] Stage, validate, and publish without replacing any command; profile capture follows configuration.")
            return 0
        # Resolve already-checked roots once, including platform aliases such as /tmp.
        target = target.parent.resolve() / target.name
        store = store.resolve()
        create_directory(target.parent)
        check_empty(target, store)
        candidate_dir = target.parent / (".codex-internal-update-" + secrets.token_hex(12))
        forwarded = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        command = [args.helper, "update-internal", *forwarded,
                   "--version", args.version, "--first-install", "--internal-bin", str(target),
                   "--install-dir", str(candidate_dir)]
        child = subprocess.Popen(command, start_new_session=True)
        try:
            status = child.wait()
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise
        if status:
            return status if status > 0 else 128 - status
        publish(target, store, candidate_dir / "codex", args.version)
        print(f"First installation complete: Codex CLI {args.version} at {target}")
        print("Configure Codex, then run init --capture-current internal. Desktop compatibility has not been verified.")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"update-internal: first installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
