#!/usr/bin/env python3
"""Preserve standalone installer output without retaining installer secrets."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


def runtime_manifest(root):
    """Fingerprint a closed runtime tree; also embedded in its stable launcher."""
    import hashlib
    import os
    import stat

    entries = {}

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def visit(directory_fd, prefix=""):
        directory_before = os.fstat(directory_fd)
        with os.scandir(directory_fd) as iterator:
            items = sorted(iterator, key=lambda item: item.name)
        for item in items:
            name = prefix + item.name
            if item.name.lower() in {
                "auth.json", "config.toml", "credentials.json", ".env",
                ".zshrc", ".zprofile", ".bashrc", ".bash_profile", ".profile",
            }:
                raise ValueError("private state is not a runtime asset: " + name)
            before = item.stat(follow_symlinks=False)
            mode = stat.S_IMODE(before.st_mode)
            if stat.S_ISLNK(before.st_mode):
                # The published package's only supported alias is relocatable.
                target = os.readlink(item.name, dir_fd=directory_fd)
                if name != "codex" or target != "bin/codex":
                    raise ValueError("unsupported runtime symlink: " + name)
                entries[name] = {"link": target}
            elif stat.S_ISDIR(before.st_mode):
                entries[name] = {"directory": mode}
                child_fd = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=directory_fd)
                try:
                    if identity(before) != identity(os.fstat(child_fd)):
                        raise ValueError("runtime directory changed: " + name)
                    visit(child_fd, name + "/")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode):
                digest = hashlib.sha256()
                fd = os.open(item.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise ValueError("runtime file changed: " + name)
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                    after = os.fstat(stream.fileno())
                if identity(before) != identity(after):
                    raise ValueError("runtime file changed: " + name)
                entries[name] = {"mode": mode, "size": before.st_size, "sha256": digest.hexdigest()}
            else:
                raise ValueError("unsupported runtime file type: " + name)
            observed = os.stat(item.name, dir_fd=directory_fd, follow_symlinks=False)
            if identity(before) != identity(observed):
                raise ValueError("runtime entry changed: " + name)
        if identity(directory_before) != identity(os.fstat(directory_fd)):
            raise ValueError("runtime directory changed while scanning")

    # Do not traverse a replaced generation or a symlinked parent.
    for path in (root, *root.parents):
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("runtime directory is not a real directory")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(root_fd)
        if identity(os.fstat(root_fd)) != identity(root.lstat()):
            raise ValueError("runtime root changed while scanning")
    finally:
        os.close(root_fd)
    return entries


def private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("runtime store must be an owned private directory")


def standalone_source(candidate: Path, scratch: Path, version: str) -> tuple[Path, str]:
    releases = scratch / "codex-home/packages/standalone/releases"
    # current and the command may be links; release directories must be real.
    for path in (releases, *releases.parents):
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("installer releases directory is not a real directory")
    target = candidate.resolve(strict=True)
    relative = target.relative_to(releases)
    if len(relative.parts) < 2:
        raise ValueError("standalone entrypoint has no release directory")
    source = releases / relative.parts[0]
    platform = re.fullmatch(
        re.escape(version) + r"-((?:aarch64|x86_64)-(?:apple-darwin|unknown-linux-(?:gnu|musl)))",
        source.name,
    )
    if platform is None:
        raise ValueError("unsupported standalone release directory")
    allowed = {"bin", "codex", "codex-path", "codex-resources", "codex-package.json"}
    if set(os.listdir(source)) - allowed:
        raise ValueError("unexpected standalone package content")
    entries = runtime_manifest(source)
    for name in ("bin", "codex-path", "codex-resources"):
        if name in entries and "directory" not in entries[name]:
            raise ValueError("standalone asset directory has wrong type: " + name)
    if "codex-package.json" in entries and not entries["codex-package.json"].get("size"):
        raise ValueError("standalone package metadata must be a non-empty file")
    entry = "/".join(relative.parts[1:])
    if entry == "bin/codex":
        required = [entry, "bin/codex-code-mode-host", "codex-path/rg"]
        if entries.get("codex", {}).get("link") != "bin/codex":
            raise ValueError("standalone package is missing its codex alias")
    elif entry == "codex" and "bin" not in entries:
        required = [entry, "codex-resources/rg"]
    else:
        raise ValueError("unsupported standalone entrypoint layout")
    if "linux" in platform.group(1) and (entry == "codex" or "codex-package.json" in entries):
        required.append("codex-resources/bwrap")
    for name in required:
        info = entries.get(name, {})
        if not info.get("size") or not info.get("mode", 0) & 0o111:
            raise ValueError("missing standalone executable: " + name)
    return source, entry


def launcher_text(root: Path, entry: str, manifest: dict) -> str:
    code = (
        "import os, stat, sys\nfrom pathlib import Path\n"
        + inspect.getsource(runtime_manifest)
        + f"\nroot = Path({str(root)!r})\nexpected = {manifest!r}\n"
        + "try:\n"
        + "    for directory in (root, root.parent):\n"
        + "        info = directory.lstat()\n"
        + "        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:\n"
        + "            raise ValueError('runtime generation is not private')\n"
        + "    if runtime_manifest(root) != expected:\n"
        + "        raise ValueError('runtime manifest mismatch')\n"
        + "except (OSError, ValueError) as error:\n"
        + "    print('Standalone runtime validation failed: ' + str(error), file=sys.stderr)\n"
        + "    sys.exit(1)\n"
        + f"binary = str(root / {entry!r})\n"
        + "os.execv(binary, [os.path.abspath(sys.argv[1]), *sys.argv[2:]])\n"
    )
    return (
        "#!/bin/sh\n# codex-switch standalone runtime launcher\nexec "
        + shlex.quote(sys.executable) + " -I -B -c " + shlex.quote(code) + ' "$0" "$@"\n'
    )


def sync_runtime(root: Path, manifest: dict) -> None:
    # A durable launcher must never precede the package data it references.
    directories = [root]
    for name, info in manifest.items():
        if "directory" in info:
            directories.append(root / name)
        elif "sha256" in info:
            fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def materialize(candidate: Path, scratch: Path, version: str, *, codesign: bool) -> str:
    candidate = candidate.absolute()
    scratch = scratch.resolve(strict=True)
    private_directory(candidate.parent)
    private_directory(scratch)
    candidate_info = candidate.lstat()
    if stat.S_ISREG(candidate_info.st_mode):
        return "legacy"
    if not stat.S_ISLNK(candidate_info.st_mode):
        raise ValueError("installer candidate is not a regular file or standalone link")
    source, entry = standalone_source(candidate, scratch, version)
    source_manifest = runtime_manifest(source)
    store = candidate.parent.parent.resolve() / ".codex-internal-runtimes"
    try:
        store.mkdir(mode=0o700)
    except FileExistsError:
        pass
    private_directory(store)
    # Register exact staging ownership before copying; never clean generations.
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=store))
    stage_identity = stage.stat()
    try:
        shutil.copytree(source, stage, symlinks=True, dirs_exist_ok=True)
        stage.chmod(0o700)
        if runtime_manifest(stage) != source_manifest:
            raise ValueError("standalone package changed while copying")
        if codesign:
            for name, info in source_manifest.items():
                if info.get("mode", 0) & 0o111:
                    subprocess.run(
                        ["codesign", "--force", "--sign", "-", str(stage / name)],
                        stdout=subprocess.DEVNULL, check=True,
                    )
        manifest = runtime_manifest(stage)
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        generation = store / digest
        if os.path.lexists(generation):
            private_directory(generation)
            if runtime_manifest(generation) != manifest:
                raise ValueError("existing standalone generation failed validation")
        else:
            # mkdir claims this digest without replacing concurrent/foreign state.
            generation.mkdir(mode=0o700)
            # An interrupted publication is retained, never selected or reused
            # unless its complete manifest matches on a later preparation.
            for child in stage.iterdir():
                child.rename(generation / child.name)
        if runtime_manifest(generation) != manifest:
            raise ValueError("standalone generation changed before launcher creation")
        sync_runtime(generation, manifest)
        store_fd = os.open(store, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(store_fd)
        finally:
            os.close(store_fd)
        observed = candidate.lstat()
        if (observed.st_dev, observed.st_ino, observed.st_ctime_ns) != (
            candidate_info.st_dev, candidate_info.st_ino, candidate_info.st_ctime_ns
        ):
            raise ValueError("standalone candidate changed during preparation")
        fd, name = tempfile.mkstemp(prefix=".launcher-", dir=candidate.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(launcher_text(generation, entry, manifest))
                stream.flush()
                os.fchmod(stream.fileno(), 0o755)
                os.fsync(stream.fileno())
            os.replace(temporary, candidate)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        observed = stage.lstat()
        if (observed.st_dev, observed.st_ino) == (stage_identity.st_dev, stage_identity.st_ino):
            shutil.rmtree(stage)
    return "standalone"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--codesign", action="store_true")
    args = parser.parse_args()

    def interrupted(signum, _frame):
        # Allow owned-stage cleanup to finish if the parent repeats a signal.
        for item in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(item, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    for item in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(item, interrupted)
    try:
        print(materialize(args.candidate, args.scratch, args.version, codesign=args.codesign))
    except subprocess.CalledProcessError as error:
        print("Standalone executable code-sign failed.", file=sys.stderr)
        return error.returncode
    except (OSError, ValueError) as error:
        print("Standalone runtime preparation failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
