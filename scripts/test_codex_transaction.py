#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
from itertools import product
import multiprocessing
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_switch_constants import SwitchError
from codex_switch_store import Store


def _hold_directory_lock(
    root: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    descriptor = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class _FakeLaunchctlRunner:
    def __init__(
        self,
        *,
        gui_env: str | None,
        service_loaded: bool,
        fail_on_occurrence: dict[str, int] | None = None,
        mutate_before_failure_on_occurrence: dict[str, int] | None = None,
    ) -> None:
        self.gui_env = gui_env
        self.service_loaded = service_loaded
        self.fail_on_occurrence = dict(fail_on_occurrence or {})
        self.mutate_before_failure_on_occurrence = dict(
            mutate_before_failure_on_occurrence or {}
        )
        self.occurrences: dict[str, int] = {}
        self.events: list[str] = []

    def __call__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        del env
        operation = command[1]
        self.occurrences[operation] = self.occurrences.get(operation, 0) + 1
        if operation == "getenv":
            self.events.append("observe:getenv")
            return (0, self.gui_env) if self.gui_env is not None else (1, "")
        if operation == "print":
            self.events.append("observe:service")
            return (0, "loaded") if self.service_loaded else (1, "not loaded")
        if operation == "setenv":
            value = command[3]
            self.events.append(f"setenv:{value}")
        else:
            self.events.append(operation)
        should_fail = (
            self.fail_on_occurrence.get(operation) == self.occurrences[operation]
        )
        should_mutate_before_failure = (
            self.mutate_before_failure_on_occurrence.get(operation)
            == self.occurrences[operation]
        )
        if should_fail and not should_mutate_before_failure:
            return 1, f"injected {operation} failure"
        if operation == "setenv":
            self.gui_env = command[3]
        elif operation == "unsetenv":
            self.gui_env = None
        elif operation == "bootout":
            self.service_loaded = False
        elif operation == "bootstrap":
            self.service_loaded = True
            payload = plistlib.loads(Path(command[-1]).read_bytes())
            arguments = payload.get("ProgramArguments", [])
            if isinstance(arguments, list) and len(arguments) >= 4:
                self.gui_env = str(arguments[3])
        if should_fail:
            return 1, f"injected {operation} failure after side effect"
        return 0, ""


class TransactionTests(unittest.TestCase):
    def supported_python_for_transaction_test(self) -> str:
        if sys.version_info >= (3, 11):
            return sys.executable
        python = shutil.which("python3.12") or shutil.which("python3.11")
        if python is None:
            self.fail("Python 3.11+ is required for transaction tests")
        return python

    def tomllib_parser_for_transaction_test(self) -> object:
        if sys.version_info >= (3, 11):
            import tomllib

            return tomllib
        python = self.supported_python_for_transaction_test()

        def loads(text: str) -> object:
            result = subprocess.run(
                [
                    python,
                    "-c",
                    (
                        "import json, sys, tomllib; "
                        "print(json.dumps(tomllib.loads(sys.stdin.read())))"
                    ),
                ],
                input=text,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(result.stdout)

        return SimpleNamespace(loads=loads)

    def setUp(self) -> None:
        shell_profile_dir = tempfile.TemporaryDirectory(
            prefix="codex-switch-transaction-shell-"
        )
        self.addCleanup(shell_profile_dir.cleanup)
        environment_updates = {
            "CODEX_SWITCH_SHELL_PROFILE": str(
                Path(shell_profile_dir.name) / ".zshrc"
            ),
        }
        if sys.version_info < (3, 11):
            tomllib_parser = self.tomllib_parser_for_transaction_test()
            for target in (
                "codex_switch_toml_validate.tomllib",
                "codex_switch_config_document.tomllib",
            ):
                patcher = patch(target, tomllib_parser)
                patcher.start()
                self.addCleanup(patcher.stop)
            environment_updates["CODEX_SWITCH_PYTHON"] = (
                self.supported_python_for_transaction_test()
            )
        environment = patch.dict(os.environ, environment_updates)
        environment.start()
        self.addCleanup(environment.stop)

    def make_store(self, root: Path) -> Store:
        store_root = root / "store"
        store_root.mkdir()
        official_home = root / "official"
        official_home.mkdir()
        internal_home = root / "internal"
        internal_home.mkdir()
        return Store(
            root=store_root,
            official_codex_home=official_home,
            internal_codex_home=internal_home,
            launch_agent_path=root / "agent.plist",
            launch_agent_label="test.codex-switch",
        )

    def make_executable(self, root: Path, name: str = "codex-internal") -> Path:
        executable = root / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        return executable

    def file_state(self, path: Path) -> dict[str, object]:
        data = path.read_bytes()
        return {
            "kind": "file",
            "mode": path.stat().st_mode & 0o777,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def runtime_binding_bundle_seams(self) -> dict[str, object]:
        import codex_switch_transaction as transaction

        seams = {
            name: getattr(transaction, name, None)
            for name in (
                "RuntimeBindingTextArtifact",
                "commit_runtime_binding_bundle",
            )
        }
        missing = [
            name
            for name, value in seams.items()
            if value is None
        ]
        self.assertFalse(
            missing,
            "Runtime binding bundle seams are missing: "
            + ", ".join(missing),
        )
        return seams

    def runtime_binding_executable_swap_seams(self) -> dict[str, object]:
        import codex_switch_transaction as transaction

        seams = {
            name: getattr(transaction, name, None)
            for name in (
                "RuntimeBindingExecutableSwap",
                "commit_runtime_binding_bundle",
            )
        }
        missing = [
            name
            for name, value in seams.items()
            if value is None
        ]
        self.assertFalse(
            missing,
            "Runtime binding executable-swap seams are missing: "
            + ", ".join(missing),
        )
        return seams

    def arrange_runtime_binding_bundle(
        self,
        root: Path,
        *,
        include_shared_config: bool,
        include_active_runtime_config: bool,
    ) -> tuple[
        Store,
        tuple[object, ...],
        dict[str, Path],
        dict[Path, bytes],
    ]:
        from codex_switch_parity import resolve_parity_artifact_paths
        from codex_switch_protocol_adapter import (
            capability_receipt_path_for_launcher,
        )

        artifact_type = self.runtime_binding_bundle_seams()[
            "RuntimeBindingTextArtifact"
        ]
        self.assertTrue(callable(artifact_type))
        store = self.make_store(root)
        store.ensure()
        profile = store.profile_dir("internal")
        profile.mkdir(parents=True, exist_ok=True)
        launcher = store.bin_dir / "codex-internal-app"
        parity_paths = resolve_parity_artifact_paths(profile_dir=profile)
        paths = {
            "manifest": store.manifest_path("internal"),
            "launcher": launcher,
            "capability_receipt": capability_receipt_path_for_launcher(
                launcher
            ),
            "parity_receipt": parity_paths.receipt_path,
            "parity_overlay": parity_paths.overlay_path,
            "profile_config": profile / "config.toml",
            "shared_config": store.official_codex_home / "config.toml",
            "active_runtime_config": (
                store.internal_codex_home / "config.toml"
            ),
        }
        old_payloads = {
            paths["manifest"]: b'{"generation":"old"}\n',
            paths["launcher"]: b"#!/bin/sh\n# old launcher\n",
            paths["capability_receipt"]: (
                b'{"schema_version":2,"generation":"old"}\n'
            ),
            paths["parity_receipt"]: (
                b'{"schema_version":1,"generation":"old"}\n'
            ),
            paths["parity_overlay"]: (
                b'{"models":[{"slug":"old"}]}\n'
            ),
            paths["profile_config"]: b'model = "old-profile"\n',
            paths["shared_config"]: b'model = "old-shared"\n',
            paths["active_runtime_config"]: b'model = "old-runtime"\n',
        }
        new_payloads = {
            "manifest": b'{"generation":"new"}\n',
            "launcher": b"#!/bin/sh\n# new launcher\n",
            "capability_receipt": (
                b'{"schema_version":2,"generation":"new"}\n'
            ),
            "parity_receipt": (
                b'{"schema_version":1,"generation":"new"}\n'
            ),
            "parity_overlay": b'{"models":[{"slug":"new"}]}\n',
            "profile_config": b'model = "new-profile"\n',
            "shared_config": b'model = "new-shared"\n',
            "active_runtime_config": b'model = "new-runtime"\n',
        }
        modes = {
            "manifest": 0o600,
            "launcher": 0o755,
            "capability_receipt": 0o600,
            "parity_receipt": 0o600,
            "parity_overlay": 0o600,
            "profile_config": 0o600,
            "shared_config": 0o600,
            "active_runtime_config": 0o600,
        }
        for path, payload in old_payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(
                0o755 if path == paths["launcher"] else 0o600
            )
        roles = [
            "manifest",
            "launcher",
            "capability_receipt",
            "parity_receipt",
            "parity_overlay",
            "profile_config",
        ]
        if include_shared_config:
            roles.append("shared_config")
        if include_active_runtime_config:
            roles.append("active_runtime_config")
        artifacts = tuple(
            artifact_type(
                role=role,
                path=paths[role],
                payload=new_payloads[role],
                mode=modes[role],
            )
            for role in reversed(roles)
        )
        return store, artifacts, paths, old_payloads

    def prepared_runtime_binding_bundle_marker(
        self,
        store: Store,
        artifacts: tuple[object, ...],
    ) -> dict[str, object]:
        commit_bundle = self.runtime_binding_bundle_seams()[
            "commit_runtime_binding_bundle"
        ]
        self.assertTrue(callable(commit_bundle))

        class HardInterruption(BaseException):
            pass

        def interrupt(phase: str) -> None:
            if phase == "after_marker":
                raise HardInterruption(phase)

        with self.assertRaises(HardInterruption):
            from codex_switch_transaction import locked_store_mutation

            with locked_store_mutation(
                store,
                operation="schema v3 bundle marker",
            ) as locked_store:
                commit_bundle(
                    locked_store,
                    artifacts=artifacts,
                    fault_hook=interrupt,
                )
        marker_path = store.root / ".runtime-binding-rebind.json"
        self.assertTrue(marker_path.is_file())
        marker = json.loads(marker_path.read_text())
        self.assertIsInstance(marker, dict)
        return marker

    def clone_runtime_binding_artifact(
        self,
        artifact: object,
        **changes: object,
    ) -> object:
        values = {
            "role": getattr(artifact, "role"),
            "path": getattr(artifact, "path"),
            "payload": getattr(artifact, "payload"),
            "mode": getattr(artifact, "mode"),
        }
        values.update(changes)
        return type(artifact)(**values)

    def arrange_runtime_binding_executable_swap(
        self,
        root: Path,
    ) -> SimpleNamespace:
        store, artifacts, artifact_paths, old_payloads = (
            self.arrange_runtime_binding_bundle(
                root,
                include_shared_config=True,
                include_active_runtime_config=True,
            )
        )
        install_root = root / "internal-bin"
        install_root.mkdir()
        bound = install_root / "codex"
        candidate_dir = (
            install_root / ".codex-internal-update-transaction-test"
        )
        candidate_dir.mkdir(mode=0o700)
        candidate = candidate_dir / "codex"
        backup = install_root / ".codex-internal-backup"
        old_binary = (
            b"#!/bin/sh\n"
            b"printf 'codex-cli 1.0.0\\n'\n"
        )
        new_binary = (
            b"#!/bin/sh\n"
            b"printf 'codex-cli 2.0.0\\n'\n"
        )
        bound.write_bytes(old_binary)
        bound.chmod(0o755)
        candidate.write_bytes(new_binary)
        candidate.chmod(0o755)
        swap_type = self.runtime_binding_executable_swap_seams()[
            "RuntimeBindingExecutableSwap"
        ]
        self.assertTrue(callable(swap_type))
        swap = swap_type(
            bound_path=bound,
            candidate_path=candidate,
            backup_path=backup,
            old_mode=0o755,
            old_sha256=hashlib.sha256(old_binary).hexdigest(),
            new_mode=0o755,
            new_sha256=hashlib.sha256(new_binary).hexdigest(),
        )
        return SimpleNamespace(
            store=store,
            artifacts=artifacts,
            artifact_paths=artifact_paths,
            old_payloads=old_payloads,
            swap=swap,
            bound=bound,
            candidate=candidate,
            backup=backup,
            old_binary=old_binary,
            new_binary=new_binary,
        )

    def clone_runtime_binding_executable_swap(
        self,
        swap: object,
        **changes: object,
    ) -> object:
        values = {
            "bound_path": getattr(swap, "bound_path"),
            "candidate_path": getattr(swap, "candidate_path"),
            "backup_path": getattr(swap, "backup_path"),
            "old_mode": getattr(swap, "old_mode"),
            "old_sha256": getattr(swap, "old_sha256"),
            "new_mode": getattr(swap, "new_mode"),
            "new_sha256": getattr(swap, "new_sha256"),
        }
        values.update(changes)
        return type(swap)(**values)

    def interrupt_runtime_binding_executable_swap(
        self,
        fixture: SimpleNamespace,
        *,
        phase: str,
    ) -> Path:
        from codex_switch_transaction import locked_store_mutation

        commit_bundle = self.runtime_binding_executable_swap_seams()[
            "commit_runtime_binding_bundle"
        ]
        self.assertTrue(callable(commit_bundle))

        class HardInterruption(BaseException):
            pass

        def interrupt(observed_phase: str) -> None:
            if observed_phase == phase:
                raise HardInterruption(phase)

        with self.assertRaises(HardInterruption):
            with locked_store_mutation(
                fixture.store,
                operation=f"executable-swap fault at {phase}",
            ) as locked_store:
                commit_bundle(
                    locked_store,
                    artifacts=fixture.artifacts,
                    executable_swap=fixture.swap,
                    fault_hook=interrupt,
                )
        return (
            fixture.store.root / ".runtime-binding-rebind.json"
        )

    def runtime_binding_path_snapshot(
        self,
        path: Path,
    ) -> tuple[object, ...]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return ("missing",)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            return ("symlink", mode, os.readlink(path))
        if stat.S_ISDIR(info.st_mode):
            return (
                "directory",
                mode,
                tuple(sorted(child.name for child in path.iterdir())),
            )
        if stat.S_ISREG(info.st_mode):
            return ("file", mode, path.read_bytes())
        return ("other", mode)

    def runtime_rebind_file_state_fixture(
        self,
        payload: bytes,
        mode: int,
    ) -> dict[str, object]:
        return {
            "kind": "file",
            "mode": mode,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "payload": base64.b64encode(payload).decode("ascii"),
        }

    def assert_runtime_binding_paths_unchanged(
        self,
        snapshots: dict[Path, tuple[object, ...]],
    ) -> None:
        for path, before in snapshots.items():
            self.assertEqual(
                before,
                self.runtime_binding_path_snapshot(path),
                str(path),
            )

    def assert_runtime_binding_bundle_rejected_before_marker(
        self,
        store: Store,
        artifacts: tuple[object, ...],
        *,
        observed_paths: set[Path],
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        commit_bundle = self.runtime_binding_bundle_seams()[
            "commit_runtime_binding_bundle"
        ]
        self.assertTrue(callable(commit_bundle))
        marker_path = store.root / ".runtime-binding-rebind.json"
        self.assertFalse(marker_path.exists())
        snapshots = {
            path: self.runtime_binding_path_snapshot(path)
            for path in observed_paths
        }

        with self.assertRaises(SwitchError):
            with locked_store_mutation(
                store,
                operation="invalid schema v3 bundle",
            ) as locked_store:
                commit_bundle(
                    locked_store,
                    artifacts=artifacts,
                )

        self.assertFalse(marker_path.exists())
        self.assert_runtime_binding_paths_unchanged(snapshots)

    def write_runtime_binding_bundle_marker(
        self,
        store: Store,
        marker: dict[str, object],
    ) -> Path:
        marker_path = store.root / ".runtime-binding-rebind.json"
        marker_path.write_text(
            json.dumps(
                marker,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        marker_path.chmod(0o600)
        return marker_path

    def test_runtime_rebind_bundle_schema_v3_has_exact_required_target_set(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, paths, old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=False,
                    include_active_runtime_config=False,
                )
            )

            marker = self.prepared_runtime_binding_bundle_marker(
                store,
                artifacts,
            )

            self.assertEqual(3, marker.get("schema_version"))
            self.assertEqual("prepared", marker.get("state"))
            entries = marker.get("artifacts")
            self.assertIsInstance(entries, list)
            expected_paths = {
                role: str(paths[role])
                for role in (
                    "manifest",
                    "launcher",
                    "capability_receipt",
                    "parity_receipt",
                    "parity_overlay",
                    "profile_config",
                )
            }
            self.assertEqual(
                expected_paths,
                {
                    entry["role"]: entry["path"]
                    for entry in entries
                },
            )
            self.assertEqual(
                {
                    role: 0o755 if role == "launcher" else 0o600
                    for role in expected_paths
                },
                {
                    entry["role"]: entry["new_state"]["mode"]
                    for entry in entries
                },
            )
            for path, payload in old_payloads.items():
                self.assertEqual(payload, path.read_bytes(), str(path))

    def test_runtime_rebind_bundle_schema_v3_optional_config_targets_are_explicit(
        self,
    ) -> None:
        for include_shared_config, include_active_runtime_config in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            with (
                self.subTest(
                    shared=include_shared_config,
                    runtime=include_active_runtime_config,
                ),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=include_shared_config,
                        include_active_runtime_config=(
                            include_active_runtime_config
                        ),
                    )
                )

                marker = self.prepared_runtime_binding_bundle_marker(
                    store,
                    artifacts,
                )

                entries = marker.get("artifacts")
                self.assertIsInstance(entries, list)
                expected_roles = {
                    "manifest",
                    "launcher",
                    "capability_receipt",
                    "parity_receipt",
                    "parity_overlay",
                    "profile_config",
                }
                if include_shared_config:
                    expected_roles.add("shared_config")
                if include_active_runtime_config:
                    expected_roles.add("active_runtime_config")
                self.assertEqual(
                    {
                        role: str(paths[role])
                        for role in expected_roles
                    },
                    {
                        entry["role"]: entry["path"]
                        for entry in entries
                    },
                )
                for path, payload in old_payloads.items():
                    self.assertEqual(payload, path.read_bytes(), str(path))

    def test_runtime_rebind_bundle_uses_default_managed_internal_home(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, _paths, _old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=False,
                    include_active_runtime_config=False,
                )
            )
            artifact_type = self.runtime_binding_bundle_seams()[
                "RuntimeBindingTextArtifact"
            ]
            self.assertTrue(callable(artifact_type))
            store.internal_codex_home = None
            managed_runtime_config = (
                store.managed_home("internal") / "config.toml"
            )
            managed_runtime_config.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            managed_runtime_config.write_bytes(
                b'model = "old-managed-runtime"\n'
            )
            managed_runtime_config.chmod(0o600)
            new_payload = b'model = "new-managed-runtime"\n'
            artifacts = artifacts + (
                artifact_type(
                    role="active_runtime_config",
                    path=managed_runtime_config,
                    payload=new_payload,
                    mode=0o600,
                ),
            )

            with locked_store_mutation(
                store,
                operation="default managed schema v3 bundle",
            ) as locked_store:
                self.runtime_binding_bundle_seams()[
                    "commit_runtime_binding_bundle"
                ](
                    locked_store,
                    artifacts=artifacts,
                )

            self.assertEqual(
                new_payload,
                managed_runtime_config.read_bytes(),
            )
            self.assertFalse(
                (store.root / ".runtime-binding-rebind.json").exists()
            )

    def test_runtime_rebind_bundle_rejects_invalid_target_sets_before_marker(
        self,
    ) -> None:
        for case in (
            "duplicate",
            "unexpected",
            "parent_child_overlap",
            "missing_required",
        ):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=False,
                        include_active_runtime_config=False,
                    )
                )
                by_role = {
                    getattr(artifact, "role"): artifact
                    for artifact in artifacts
                }
                invalid_artifacts = list(artifacts)
                if case == "duplicate":
                    invalid_artifacts.append(by_role["manifest"])
                elif case == "unexpected":
                    invalid_artifacts.append(
                        self.clone_runtime_binding_artifact(
                            by_role["manifest"],
                            role="unexpected",
                            path=root / "unexpected-runtime-target",
                        )
                    )
                elif case == "parent_child_overlap":
                    overlap_root = root / "overlapping-runtime-targets"
                    replacements = {
                        "manifest": self.clone_runtime_binding_artifact(
                            by_role["manifest"],
                            path=overlap_root,
                        ),
                        "launcher": self.clone_runtime_binding_artifact(
                            by_role["launcher"],
                            path=overlap_root / "launcher",
                        ),
                    }
                    invalid_artifacts = [
                        replacements.get(
                            getattr(artifact, "role"),
                            artifact,
                        )
                        for artifact in artifacts
                    ]
                else:
                    invalid_artifacts = [
                        artifact
                        for artifact in artifacts
                        if getattr(artifact, "role") != "parity_overlay"
                    ]
                observed_paths = set(paths.values())
                observed_paths.update(
                    Path(getattr(artifact, "path"))
                    for artifact in invalid_artifacts
                )

                self.assert_runtime_binding_bundle_rejected_before_marker(
                    store,
                    tuple(invalid_artifacts),
                    observed_paths=observed_paths,
                )

    def test_runtime_rebind_bundle_rejects_unsafe_target_types_before_marker(
        self,
    ) -> None:
        for case in ("symlink", "directory"):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=False,
                        include_active_runtime_config=False,
                    )
                )
                target = paths["parity_receipt"]
                target.unlink()
                observed_paths = set(paths.values())
                if case == "symlink":
                    external = root / "external-parity-receipt.json"
                    external.write_bytes(b'{"external":true}\n')
                    target.symlink_to(external)
                    observed_paths.add(external)
                else:
                    target.mkdir(mode=0o700)

                self.assert_runtime_binding_bundle_rejected_before_marker(
                    store,
                    artifacts,
                    observed_paths=observed_paths,
                )

    def test_runtime_rebind_bundle_rejects_symlinked_target_ancestor_before_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, paths, _old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=False,
                    include_active_runtime_config=True,
                )
            )
            external_root = root / "external-homes"
            external_home = external_root / "internal"
            external_home.mkdir(parents=True)
            external_config = external_home / "config.toml"
            external_payload = b'model = "external-runtime"\n'
            external_config.write_bytes(external_payload)
            external_config.chmod(0o600)
            linked_homes = root / "linked-homes"
            linked_homes.symlink_to(
                external_root,
                target_is_directory=True,
            )
            store.internal_codex_home = linked_homes / "internal"
            linked_config = store.internal_codex_home / "config.toml"
            artifacts = tuple(
                self.clone_runtime_binding_artifact(
                    artifact,
                    path=linked_config,
                )
                if getattr(artifact, "role") == "active_runtime_config"
                else artifact
                for artifact in artifacts
            )

            self.assert_runtime_binding_bundle_rejected_before_marker(
                store,
                artifacts,
                observed_paths={
                    *paths.values(),
                    linked_config,
                    external_config,
                },
            )
            self.assertEqual(
                external_payload,
                external_config.read_bytes(),
            )

    def test_runtime_rebind_bundle_rejects_invalid_modes_and_oversized_payload_before_marker(
        self,
    ) -> None:
        from codex_switch_parity import MAX_PARITY_CATALOG_BYTES

        for case in (
            "manifest_executable",
            "launcher_private_file",
            "oversized_payload",
        ):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=False,
                        include_active_runtime_config=False,
                    )
                )
                by_role = {
                    getattr(artifact, "role"): artifact
                    for artifact in artifacts
                }
                if case == "manifest_executable":
                    role = "manifest"
                    replacement = self.clone_runtime_binding_artifact(
                        by_role[role],
                        mode=0o755,
                    )
                elif case == "launcher_private_file":
                    role = "launcher"
                    replacement = self.clone_runtime_binding_artifact(
                        by_role[role],
                        mode=0o600,
                    )
                else:
                    role = "parity_overlay"
                    replacement = self.clone_runtime_binding_artifact(
                        by_role[role],
                        payload=b"x" * (MAX_PARITY_CATALOG_BYTES + 1),
                    )
                invalid_artifacts = tuple(
                    replacement
                    if getattr(artifact, "role") == role
                    else artifact
                    for artifact in artifacts
                )

                self.assert_runtime_binding_bundle_rejected_before_marker(
                    store,
                    invalid_artifacts,
                    observed_paths=set(paths.values()),
                )

    def test_runtime_rebind_bundle_recovery_rejects_invalid_digests_before_target_write(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        for state_name in ("old_state", "new_state"):
            with (
                self.subTest(state=state_name),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=True,
                        include_active_runtime_config=True,
                    )
                )
                marker = self.prepared_runtime_binding_bundle_marker(
                    store,
                    artifacts,
                )
                entries = marker.get("artifacts")
                self.assertIsInstance(entries, list)
                receipt_entry = next(
                    entry
                    for entry in entries
                    if entry["role"] == "parity_receipt"
                )
                state_value = receipt_entry[state_name]
                self.assertIsInstance(state_value, dict)
                state_value["sha256"] = "0" * 64
                marker_path = self.write_runtime_binding_bundle_marker(
                    store,
                    marker,
                )
                snapshots = {
                    path: self.runtime_binding_path_snapshot(path)
                    for path in paths.values()
                }

                with self.assertRaisesRegex(
                    SwitchError,
                    "(?i)digest",
                ):
                    with locked_store_mutation(
                        store,
                        operation="invalid schema v3 digest",
                    ):
                        pass

                self.assertTrue(marker_path.is_file())
                self.assert_runtime_binding_paths_unchanged(snapshots)

    def test_runtime_rebind_bundle_recovery_rejects_foreign_old_and_new_states_before_target_write(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        for marker_state in ("prepared", "committed"):
            with (
                self.subTest(state=marker_state),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=True,
                        include_active_runtime_config=True,
                    )
                )
                marker = self.prepared_runtime_binding_bundle_marker(
                    store,
                    artifacts,
                )
                entries = marker.get("artifacts")
                self.assertIsInstance(entries, list)
                entries_by_role = {
                    entry["role"]: entry
                    for entry in entries
                }
                marker["state"] = marker_state
                marker_path = self.write_runtime_binding_bundle_marker(
                    store,
                    marker,
                )
                if marker_state == "prepared":
                    overlay_state = entries_by_role["parity_overlay"][
                        "new_state"
                    ]
                    overlay_path = paths["parity_overlay"]
                    overlay_path.write_bytes(
                        base64.b64decode(
                            overlay_state["payload"],
                            validate=True,
                        )
                    )
                    overlay_path.chmod(overlay_state["mode"])
                manifest_path = paths["manifest"]
                manifest_path.write_bytes(b'{"generation":"foreign"}\n')
                manifest_path.chmod(0o600)
                snapshots = {
                    path: self.runtime_binding_path_snapshot(path)
                    for path in paths.values()
                }

                with self.assertRaisesRegex(
                    SwitchError,
                    "foreign target state",
                ):
                    with locked_store_mutation(
                        store,
                        operation="foreign schema v3 target",
                    ):
                        pass

                self.assertTrue(marker_path.is_file())
                self.assert_runtime_binding_paths_unchanged(snapshots)

    def test_runtime_rebind_bundle_promotes_in_deterministic_manifest_last_order(
        self,
    ) -> None:
        import codex_switch_transaction as transaction
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, paths, _old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=True,
                    include_active_runtime_config=True,
                )
            )
            commit_bundle = self.runtime_binding_bundle_seams()[
                "commit_runtime_binding_bundle"
            ]
            self.assertTrue(callable(commit_bundle))
            applied_paths: list[Path] = []
            original_apply = transaction._apply_runtime_rebind_bundle_state

            def record_apply(
                observed_store: Store,
                path: Path,
                state: dict[str, object],
            ) -> None:
                applied_paths.append(path)
                original_apply(observed_store, path, state)

            with patch.object(
                transaction,
                "_apply_runtime_rebind_bundle_state",
                side_effect=record_apply,
            ):
                with locked_store_mutation(
                    store,
                    operation="schema v3 bundle promotion",
                ) as locked_store:
                    commit_bundle(
                        locked_store,
                        artifacts=artifacts,
                    )

            expected_roles = (
                "parity_overlay",
                "capability_receipt",
                "parity_receipt",
                "shared_config",
                "profile_config",
                "active_runtime_config",
                "launcher",
                "manifest",
            )
            self.assertEqual(
                [paths[role] for role in expected_roles],
                applied_paths,
            )
            by_role = {
                getattr(artifact, "role"): artifact
                for artifact in artifacts
            }
            for role in expected_roles:
                artifact = by_role[role]
                path = paths[role]
                self.assertEqual(
                    getattr(artifact, "payload"),
                    path.read_bytes(),
                    role,
                )
                self.assertEqual(
                    getattr(artifact, "mode"),
                    stat.S_IMODE(path.stat().st_mode),
                    role,
                )
            self.assertFalse(
                (store.root / ".runtime-binding-rebind.json").exists()
            )

    def test_runtime_rebind_bundle_revalidates_inputs_after_old_state_capture(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, paths, old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=True,
                    include_active_runtime_config=False,
                )
            )
            profile_config = paths["profile_config"]
            foreign_payload = b'model = "foreign-after-validation"\n'
            validation_calls = 0

            def validate_inputs() -> None:
                nonlocal validation_calls
                validation_calls += 1
                if validation_calls == 1:
                    profile_config.write_bytes(foreign_payload)
                    profile_config.chmod(0o600)
                    return
                if profile_config.read_bytes() != old_payloads[
                    profile_config
                ]:
                    raise SwitchError(
                        "parity bundle input changed before journal write"
                    )

            with self.assertRaisesRegex(
                SwitchError,
                "input changed",
            ):
                with locked_store_mutation(
                    store,
                    operation="schema v3 input revalidation",
                ) as locked_store:
                    self.runtime_binding_bundle_seams()[
                        "commit_runtime_binding_bundle"
                    ](
                        locked_store,
                        artifacts=artifacts,
                        input_validator=validate_inputs,
                    )

            self.assertGreaterEqual(validation_calls, 2)
            self.assertEqual(
                foreign_payload,
                profile_config.read_bytes(),
            )
            self.assertFalse(
                (store.root / ".runtime-binding-rebind.json").exists()
            )

    def test_runtime_rebind_recovery_detects_marker_changed_after_read(
        self,
    ) -> None:
        import codex_switch_transaction as transaction
        from codex_switch_transaction import locked_store_mutation

        for mutation in ("rewrite", "remove"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=True,
                        include_active_runtime_config=True,
                    )
                )
                marker = self.prepared_runtime_binding_bundle_marker(
                    store,
                    artifacts,
                )
                entries = marker.get("artifacts")
                self.assertIsInstance(entries, list)
                entries_by_role = {
                    entry["role"]: entry
                    for entry in entries
                }
                overlay_state = entries_by_role["parity_overlay"][
                    "new_state"
                ]
                overlay_path = paths["parity_overlay"]
                overlay_path.write_bytes(
                    base64.b64decode(
                        overlay_state["payload"],
                        validate=True,
                    )
                )
                overlay_path.chmod(overlay_state["mode"])
                target_snapshots = {
                    path: self.runtime_binding_path_snapshot(path)
                    for path in paths.values()
                }
                marker_path = store.root / ".runtime-binding-rebind.json"
                foreign_payload = b'{"foreign":"marker-generation"}\n'
                original_validate = (
                    transaction._validated_runtime_rebind_marker
                )

                def replace_marker_after_read(
                    observed_store: Store,
                    raw: object,
                ) -> dict[str, object]:
                    if mutation == "rewrite":
                        marker_path.write_bytes(foreign_payload)
                        marker_path.chmod(0o600)
                    else:
                        marker_path.unlink()
                    return original_validate(observed_store, raw)

                with patch.object(
                    transaction,
                    "_validated_runtime_rebind_marker",
                    side_effect=replace_marker_after_read,
                ), self.assertRaisesRegex(
                    SwitchError,
                    "marker changed",
                ):
                    with locked_store_mutation(
                        store,
                        operation=(
                            "schema v3 marker generation replacement"
                        ),
                    ):
                        pass

                self.assertEqual(
                    mutation == "rewrite",
                    marker_path.is_file(),
                )
                if mutation == "rewrite":
                    self.assertEqual(
                        foreign_payload,
                        marker_path.read_bytes(),
                    )
                self.assert_runtime_binding_paths_unchanged(
                    target_snapshots
                )

    def test_runtime_rebind_bundle_requires_prepared_marker_before_promotion(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, paths, _old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=True,
                    include_active_runtime_config=True,
                )
            )
            marker_path = store.root / ".runtime-binding-rebind.json"
            target_snapshots = {
                path: self.runtime_binding_path_snapshot(path)
                for path in paths.values()
            }

            def remove_prepared_marker(phase: str) -> None:
                if phase == "after_marker":
                    marker_path.unlink()

            with self.assertRaisesRegex(
                SwitchError,
                "marker changed",
            ):
                with locked_store_mutation(
                    store,
                    operation="schema v3 missing prepared marker",
                ) as locked_store:
                    self.runtime_binding_bundle_seams()[
                        "commit_runtime_binding_bundle"
                    ](
                        locked_store,
                        artifacts=artifacts,
                        fault_hook=remove_prepared_marker,
                    )

            self.assertFalse(marker_path.exists())
            self.assert_runtime_binding_paths_unchanged(target_snapshots)

    def test_runtime_rebind_marker_schema_requires_exact_integer(
        self,
    ) -> None:
        import codex_switch_transaction as transaction

        for schema_version in (True, 1.0):
            with (
                self.subTest(schema_version=schema_version),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store = self.make_store(root)
                store.ensure()
                profile = store.profile_dir("internal")
                profile.mkdir(parents=True, exist_ok=True)
                manifest = store.manifest_path("internal")
                launcher = store.bin_dir / "codex-internal-app"
                manifest_payload = b'{"codex_bin":"old"}\n'
                launcher_payload = b"#!/bin/sh\n# old\n"
                manifest.write_bytes(manifest_payload)
                manifest.chmod(0o600)
                launcher.write_bytes(launcher_payload)
                launcher.chmod(0o755)
                marker = {
                    "schema_version": schema_version,
                    "state": "prepared",
                    "manifest_path": str(manifest),
                    "launcher_path": str(launcher),
                    "old_manifest": (
                        self.runtime_rebind_file_state_fixture(
                            manifest_payload,
                            0o600,
                        )
                    ),
                    "old_launcher": (
                        self.runtime_rebind_file_state_fixture(
                            launcher_payload,
                            0o755,
                        )
                    ),
                    "new_manifest": (
                        self.runtime_rebind_file_state_fixture(
                            b'{"codex_bin":"new"}\n',
                            0o600,
                        )
                    ),
                    "new_launcher": (
                        self.runtime_rebind_file_state_fixture(
                            b"#!/bin/sh\n# new\n",
                            0o755,
                        )
                    ),
                }
                with self.assertRaisesRegex(
                    SwitchError,
                    "marker schema is invalid",
                ):
                    transaction._validated_runtime_rebind_marker(
                        store,
                        marker,
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, _paths, _old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=True,
                    include_active_runtime_config=True,
                )
            )
            marker = self.prepared_runtime_binding_bundle_marker(
                store,
                artifacts,
            )
            marker["schema_version"] = 3.0
            with self.assertRaisesRegex(
                SwitchError,
                "marker schema is invalid",
            ):
                transaction._validated_runtime_rebind_marker(
                    store,
                    marker,
                )

    def test_runtime_rebind_marker_read_is_bounded_before_json_parse(
        self,
    ) -> None:
        import codex_switch_transaction as transaction
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            marker_path = store.root / ".runtime-binding-rebind.json"
            marker_path.write_bytes(b"x" * 65)
            marker_path.chmod(0o600)

            with patch.object(
                transaction,
                "_MAX_RUNTIME_REBIND_MARKER_BYTES",
                64,
                create=True,
            ), patch.object(
                transaction.json,
                "loads",
                side_effect=AssertionError(
                    "oversized marker reached JSON parsing"
                ),
            ), self.assertRaisesRegex(
                SwitchError,
                "marker is oversized",
            ):
                with locked_store_mutation(
                    store,
                    operation="oversized runtime rebind marker",
                ):
                    pass

    def test_runtime_rebind_bundle_recovery_does_not_overwrite_foreign_state_after_preflight(
        self,
    ) -> None:
        import codex_switch_transaction as transaction
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, artifacts, paths, _old_payloads = (
                self.arrange_runtime_binding_bundle(
                    root,
                    include_shared_config=True,
                    include_active_runtime_config=True,
                )
            )
            marker = self.prepared_runtime_binding_bundle_marker(
                store,
                artifacts,
            )
            entries = marker.get("artifacts")
            self.assertIsInstance(entries, list)
            entries_by_role = {
                entry["role"]: entry
                for entry in entries
            }
            overlay_state = entries_by_role["parity_overlay"]["new_state"]
            overlay_path = paths["parity_overlay"]
            overlay_path.write_bytes(
                base64.b64decode(
                    overlay_state["payload"],
                    validate=True,
                )
            )
            overlay_path.chmod(overlay_state["mode"])
            manifest_path = paths["manifest"]
            foreign_payload = b'{"generation":"late-foreign"}\n'
            original_apply = transaction._apply_runtime_rebind_bundle_state
            mutated = False

            def mutate_later_target(
                observed_store: Store,
                path: Path,
                state: dict[str, object],
            ) -> None:
                nonlocal mutated
                if not mutated:
                    manifest_path.write_bytes(foreign_payload)
                    manifest_path.chmod(0o600)
                    mutated = True
                original_apply(observed_store, path, state)

            with self.assertRaisesRegex(
                SwitchError,
                "foreign target state",
            ), patch.object(
                transaction,
                "_apply_runtime_rebind_bundle_state",
                side_effect=mutate_later_target,
            ):
                with locked_store_mutation(
                    store,
                    operation="late foreign schema v3 target",
                ):
                    pass

            self.assertTrue(mutated)
            self.assertEqual(foreign_payload, manifest_path.read_bytes())
            self.assertTrue(
                (store.root / ".runtime-binding-rebind.json").is_file()
            )

    def test_runtime_rebind_bundle_prepared_fault_matrix_rolls_back_every_target(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        class HardInterruption(BaseException):
            pass

        activation_order = (
            "parity_overlay",
            "capability_receipt",
            "parity_receipt",
            "shared_config",
            "profile_config",
            "active_runtime_config",
            "launcher",
            "manifest",
        )
        phases = ("after_marker",) + tuple(
            f"after_{role}"
            for role in activation_order
        )
        for phase in phases:
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=True,
                        include_active_runtime_config=True,
                    )
                )
                commit_bundle = self.runtime_binding_bundle_seams()[
                    "commit_runtime_binding_bundle"
                ]
                self.assertTrue(callable(commit_bundle))
                old_snapshots = {
                    path: self.runtime_binding_path_snapshot(path)
                    for path in paths.values()
                }
                by_role = {
                    getattr(artifact, "role"): artifact
                    for artifact in artifacts
                }

                def interrupt(observed_phase: str) -> None:
                    if observed_phase == phase:
                        raise HardInterruption(phase)

                with self.assertRaises(HardInterruption):
                    with locked_store_mutation(
                        store,
                        operation=f"prepared fault at {phase}",
                    ) as locked_store:
                        commit_bundle(
                            locked_store,
                            artifacts=artifacts,
                            fault_hook=interrupt,
                        )

                marker_path = store.root / ".runtime-binding-rebind.json"
                marker = json.loads(marker_path.read_text())
                self.assertEqual("prepared", marker.get("state"))
                promoted_count = (
                    0
                    if phase == "after_marker"
                    else activation_order.index(
                        phase.removeprefix("after_")
                    )
                    + 1
                )
                promoted_roles = set(
                    activation_order[:promoted_count]
                )
                for role in activation_order:
                    path = paths[role]
                    if role in promoted_roles:
                        artifact = by_role[role]
                        self.assertEqual(
                            getattr(artifact, "payload"),
                            path.read_bytes(),
                            role,
                        )
                        self.assertEqual(
                            getattr(artifact, "mode"),
                            stat.S_IMODE(path.stat().st_mode),
                            role,
                        )
                    else:
                        self.assertEqual(
                            old_snapshots[path],
                            self.runtime_binding_path_snapshot(path),
                            role,
                        )

                with locked_store_mutation(
                    store,
                    operation=f"prepared recovery after {phase}",
                ):
                    pass

                self.assertFalse(marker_path.exists())
                self.assert_runtime_binding_paths_unchanged(
                    old_snapshots
                )

    def test_runtime_rebind_bundle_committed_fault_matrix_rolls_forward_every_target(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        class HardInterruption(BaseException):
            pass

        for phase in (
            "after_committed_marker",
            "after_marker_retirement",
        ):
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, artifacts, paths, _old_payloads = (
                    self.arrange_runtime_binding_bundle(
                        root,
                        include_shared_config=True,
                        include_active_runtime_config=True,
                    )
                )
                commit_bundle = self.runtime_binding_bundle_seams()[
                    "commit_runtime_binding_bundle"
                ]
                self.assertTrue(callable(commit_bundle))
                by_role = {
                    getattr(artifact, "role"): artifact
                    for artifact in artifacts
                }

                def interrupt(observed_phase: str) -> None:
                    if observed_phase == phase:
                        raise HardInterruption(phase)

                with self.assertRaises(HardInterruption):
                    with locked_store_mutation(
                        store,
                        operation=f"committed fault at {phase}",
                    ) as locked_store:
                        commit_bundle(
                            locked_store,
                            artifacts=artifacts,
                            fault_hook=interrupt,
                        )

                marker_path = store.root / ".runtime-binding-rebind.json"
                if phase == "after_committed_marker":
                    marker = json.loads(marker_path.read_text())
                    self.assertEqual("committed", marker.get("state"))
                else:
                    self.assertFalse(marker_path.exists())
                for role, path in paths.items():
                    artifact = by_role[role]
                    self.assertEqual(
                        getattr(artifact, "payload"),
                        path.read_bytes(),
                        role,
                    )
                    self.assertEqual(
                        getattr(artifact, "mode"),
                        stat.S_IMODE(path.stat().st_mode),
                        role,
                    )

                with locked_store_mutation(
                    store,
                    operation=f"committed recovery after {phase}",
                ):
                    pass

                self.assertFalse(marker_path.exists())
                for role, path in paths.items():
                    artifact = by_role[role]
                    self.assertEqual(
                        getattr(artifact, "payload"),
                        path.read_bytes(),
                        role,
                    )
                    self.assertEqual(
                        getattr(artifact, "mode"),
                        stat.S_IMODE(path.stat().st_mode),
                        role,
                    )

    def test_runtime_rebind_executable_swap_marker_binds_exact_paths_modes_and_digests_without_binary_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self.arrange_runtime_binding_executable_swap(root)

            marker_path = self.interrupt_runtime_binding_executable_swap(
                fixture,
                phase="after_marker",
            )

            marker_text = marker_path.read_text()
            marker = json.loads(marker_text)
            self.assertEqual(
                {
                    "schema_version",
                    "state",
                    "artifacts",
                    "executable_swap",
                },
                set(marker),
            )
            self.assertEqual(3, marker.get("schema_version"))
            self.assertEqual("prepared", marker.get("state"))
            executable_swap = marker.get("executable_swap")
            self.assertEqual(
                {
                    "bound_path": str(fixture.bound),
                    "candidate_path": str(fixture.candidate),
                    "backup_path": str(fixture.backup),
                    "old_mode": 0o755,
                    "old_sha256": hashlib.sha256(
                        fixture.old_binary
                    ).hexdigest(),
                    "new_mode": 0o755,
                    "new_sha256": hashlib.sha256(
                        fixture.new_binary
                    ).hexdigest(),
                },
                executable_swap,
            )
            self.assertIsInstance(executable_swap, dict)
            self.assertNotIn("payload", executable_swap)
            self.assertNotIn(
                base64.b64encode(fixture.old_binary).decode("ascii"),
                marker_text,
            )
            self.assertNotIn(
                base64.b64encode(fixture.new_binary).decode("ascii"),
                marker_text,
            )

    def test_cli_only_runtime_rebind_commits_exact_manifest_and_executable_swap(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.arrange_runtime_binding_executable_swap(
                Path(temp_dir)
            )
            manifest_artifact = next(
                artifact
                for artifact in fixture.artifacts
                if getattr(artifact, "role") == "manifest"
            )
            desktop_snapshots = {
                path: self.runtime_binding_path_snapshot(path)
                for role, path in fixture.artifact_paths.items()
                if role != "manifest"
            }

            with locked_store_mutation(
                fixture.store,
                operation="CLI-only runtime promotion",
            ) as locked_store:
                self.runtime_binding_executable_swap_seams()[
                    "commit_runtime_binding_bundle"
                ](
                    locked_store,
                    artifacts=(manifest_artifact,),
                    executable_swap=fixture.swap,
                    bundle_scope="cli-only",
                )

            self.assertEqual(fixture.new_binary, fixture.bound.read_bytes())
            self.assertFalse(fixture.candidate.exists())
            self.assertEqual(fixture.old_binary, fixture.backup.read_bytes())
            self.assertEqual(
                getattr(manifest_artifact, "payload"),
                fixture.artifact_paths["manifest"].read_bytes(),
            )
            self.assert_runtime_binding_paths_unchanged(desktop_snapshots)
            self.assertFalse(
                (
                    fixture.store.root
                    / ".runtime-binding-rebind.json"
                ).exists()
            )

    def test_cli_only_runtime_rebind_marker_is_scoped_schema_v4_and_recovers(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        class HardInterruption(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.arrange_runtime_binding_executable_swap(
                Path(temp_dir)
            )
            manifest_artifact = next(
                artifact
                for artifact in fixture.artifacts
                if getattr(artifact, "role") == "manifest"
            )
            old_manifest = fixture.artifact_paths["manifest"].read_bytes()

            def interrupt(phase: str) -> None:
                if phase == "after_manifest":
                    raise HardInterruption(phase)

            with self.assertRaises(HardInterruption):
                with locked_store_mutation(
                    fixture.store,
                    operation="interrupted CLI-only runtime promotion",
                ) as locked_store:
                    self.runtime_binding_executable_swap_seams()[
                        "commit_runtime_binding_bundle"
                    ](
                        locked_store,
                        artifacts=(manifest_artifact,),
                        executable_swap=fixture.swap,
                        bundle_scope="cli-only",
                        fault_hook=interrupt,
                    )

            marker_path = (
                fixture.store.root / ".runtime-binding-rebind.json"
            )
            marker = json.loads(marker_path.read_text())
            self.assertEqual(4, marker["schema_version"])
            self.assertEqual("cli-only", marker["bundle_scope"])
            self.assertEqual(
                ["manifest"],
                [entry["role"] for entry in marker["artifacts"]],
            )

            with locked_store_mutation(
                fixture.store,
                operation="recover CLI-only runtime promotion",
            ):
                pass

            self.assertEqual(fixture.old_binary, fixture.bound.read_bytes())
            self.assertEqual(fixture.new_binary, fixture.candidate.read_bytes())
            self.assertFalse(fixture.backup.exists())
            self.assertEqual(
                old_manifest,
                fixture.artifact_paths["manifest"].read_bytes(),
            )
            self.assertFalse(marker_path.exists())

    def test_cli_only_runtime_rebind_rejects_desktop_artifacts_before_marker(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.arrange_runtime_binding_executable_swap(
                Path(temp_dir)
            )
            manifest_artifact = next(
                artifact
                for artifact in fixture.artifacts
                if getattr(artifact, "role") == "manifest"
            )
            launcher_artifact = next(
                artifact
                for artifact in fixture.artifacts
                if getattr(artifact, "role") == "launcher"
            )
            observed_paths = {
                *fixture.artifact_paths.values(),
                fixture.bound,
                fixture.candidate,
                fixture.backup,
            }
            snapshots = {
                path: self.runtime_binding_path_snapshot(path)
                for path in observed_paths
            }

            with self.assertRaisesRegex(
                SwitchError,
                "unexpected targets",
            ):
                with locked_store_mutation(
                    fixture.store,
                    operation="invalid CLI-only runtime promotion",
                ) as locked_store:
                    self.runtime_binding_executable_swap_seams()[
                        "commit_runtime_binding_bundle"
                    ](
                        locked_store,
                        artifacts=(launcher_artifact, manifest_artifact),
                        executable_swap=fixture.swap,
                        bundle_scope="cli-only",
                    )

            self.assertFalse(
                (
                    fixture.store.root
                    / ".runtime-binding-rebind.json"
                ).exists()
            )
            self.assert_runtime_binding_paths_unchanged(snapshots)

    def test_runtime_rebind_executable_swap_rejects_unsafe_paths_modes_and_digests_before_marker(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        for case in (
            "candidate_not_in_sibling_stage",
            "backup_not_sibling",
            "duplicate_backup",
            "candidate_symlink",
            "existing_backup",
            "old_mode",
            "new_mode",
            "old_digest",
            "new_digest",
        ):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                fixture = self.arrange_runtime_binding_executable_swap(root)
                changes: dict[str, object] = {}
                observed_paths = {
                    *fixture.artifact_paths.values(),
                    fixture.bound,
                    fixture.candidate,
                    fixture.backup,
                }
                if case == "candidate_not_in_sibling_stage":
                    outside_candidate = root / "outside-candidate"
                    outside_candidate.write_bytes(fixture.new_binary)
                    outside_candidate.chmod(0o755)
                    changes["candidate_path"] = outside_candidate
                    observed_paths.add(outside_candidate)
                elif case == "backup_not_sibling":
                    outside_backup = root / "outside-backup"
                    changes["backup_path"] = outside_backup
                    observed_paths.add(outside_backup)
                elif case == "duplicate_backup":
                    changes["backup_path"] = fixture.bound
                elif case == "candidate_symlink":
                    fixture.candidate.unlink()
                    fixture.candidate.symlink_to(fixture.bound)
                elif case == "existing_backup":
                    fixture.backup.write_bytes(b"foreign-backup\n")
                    fixture.backup.chmod(0o755)
                elif case == "old_mode":
                    changes["old_mode"] = 0o700
                elif case == "new_mode":
                    changes["new_mode"] = 0o700
                elif case == "old_digest":
                    changes["old_sha256"] = "0" * 64
                else:
                    changes["new_sha256"] = "0" * 64
                snapshots = {
                    path: self.runtime_binding_path_snapshot(path)
                    for path in observed_paths
                }
                marker_path = (
                    fixture.store.root
                    / ".runtime-binding-rebind.json"
                )

                with self.assertRaises(SwitchError):
                    invalid_swap = (
                        self.clone_runtime_binding_executable_swap(
                            fixture.swap,
                            **changes,
                        )
                        if changes
                        else fixture.swap
                    )
                    with locked_store_mutation(
                        fixture.store,
                        operation=f"invalid executable swap {case}",
                    ) as locked_store:
                        self.runtime_binding_executable_swap_seams()[
                            "commit_runtime_binding_bundle"
                        ](
                            locked_store,
                            artifacts=fixture.artifacts,
                            executable_swap=invalid_swap,
                        )

                self.assertFalse(marker_path.exists())
                self.assert_runtime_binding_paths_unchanged(snapshots)

    def test_runtime_rebind_executable_swap_prepared_fault_matrix_rolls_back_binary_and_runtime_bundle(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        phases = (
            "before_bound_to_backup",
            "after_bound_to_backup",
            "before_candidate_to_bound",
            "after_candidate_to_bound",
            "after_manifest",
        )
        for phase in phases:
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                fixture = self.arrange_runtime_binding_executable_swap(root)
                old_artifact_snapshots = {
                    path: self.runtime_binding_path_snapshot(path)
                    for path in fixture.artifact_paths.values()
                }

                marker_path = self.interrupt_runtime_binding_executable_swap(
                    fixture,
                    phase=phase,
                )

                marker = json.loads(marker_path.read_text())
                self.assertEqual("prepared", marker.get("state"))
                if phase == "before_bound_to_backup":
                    expected_binary_states = {
                        fixture.bound: (
                            "file",
                            0o755,
                            fixture.old_binary,
                        ),
                        fixture.candidate: (
                            "file",
                            0o755,
                            fixture.new_binary,
                        ),
                        fixture.backup: ("missing",),
                    }
                elif phase in {
                    "after_bound_to_backup",
                    "before_candidate_to_bound",
                }:
                    expected_binary_states = {
                        fixture.bound: ("missing",),
                        fixture.candidate: (
                            "file",
                            0o755,
                            fixture.new_binary,
                        ),
                        fixture.backup: (
                            "file",
                            0o755,
                            fixture.old_binary,
                        ),
                    }
                else:
                    expected_binary_states = {
                        fixture.bound: (
                            "file",
                            0o755,
                            fixture.new_binary,
                        ),
                        fixture.candidate: ("missing",),
                        fixture.backup: (
                            "file",
                            0o755,
                            fixture.old_binary,
                        ),
                    }
                for path, expected in expected_binary_states.items():
                    self.assertEqual(
                        expected,
                        self.runtime_binding_path_snapshot(path),
                        f"{phase}: {path}",
                    )
                if phase == "after_manifest":
                    by_role = {
                        getattr(artifact, "role"): artifact
                        for artifact in fixture.artifacts
                    }
                    for role, path in fixture.artifact_paths.items():
                        artifact = by_role[role]
                        self.assertEqual(
                            (
                                "file",
                                getattr(artifact, "mode"),
                                getattr(artifact, "payload"),
                            ),
                            self.runtime_binding_path_snapshot(path),
                            role,
                        )
                else:
                    self.assert_runtime_binding_paths_unchanged(
                        old_artifact_snapshots
                    )

                with locked_store_mutation(
                    fixture.store,
                    operation=f"prepared executable recovery {phase}",
                ):
                    pass

                self.assertFalse(marker_path.exists())
                self.assertEqual(
                    ("file", 0o755, fixture.old_binary),
                    self.runtime_binding_path_snapshot(fixture.bound),
                )
                self.assertEqual(
                    ("file", 0o755, fixture.new_binary),
                    self.runtime_binding_path_snapshot(
                        fixture.candidate
                    ),
                )
                self.assertEqual(
                    ("missing",),
                    self.runtime_binding_path_snapshot(fixture.backup),
                )
                self.assert_runtime_binding_paths_unchanged(
                    old_artifact_snapshots
                )

    def test_runtime_rebind_executable_swap_committed_fault_matrix_rolls_forward_binary_and_runtime_bundle(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        for phase in (
            "after_committed_marker",
            "after_marker_retirement",
        ):
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                fixture = self.arrange_runtime_binding_executable_swap(root)

                marker_path = self.interrupt_runtime_binding_executable_swap(
                    fixture,
                    phase=phase,
                )

                if phase == "after_committed_marker":
                    marker = json.loads(marker_path.read_text())
                    self.assertEqual("committed", marker.get("state"))
                else:
                    self.assertFalse(marker_path.exists())
                self.assertEqual(
                    ("file", 0o755, fixture.new_binary),
                    self.runtime_binding_path_snapshot(fixture.bound),
                )
                self.assertEqual(
                    ("missing",),
                    self.runtime_binding_path_snapshot(
                        fixture.candidate
                    ),
                )
                self.assertEqual(
                    ("file", 0o755, fixture.old_binary),
                    self.runtime_binding_path_snapshot(fixture.backup),
                )
                by_role = {
                    getattr(artifact, "role"): artifact
                    for artifact in fixture.artifacts
                }
                for role, path in fixture.artifact_paths.items():
                    artifact = by_role[role]
                    self.assertEqual(
                        (
                            "file",
                            getattr(artifact, "mode"),
                            getattr(artifact, "payload"),
                        ),
                        self.runtime_binding_path_snapshot(path),
                        role,
                    )

                with locked_store_mutation(
                    fixture.store,
                    operation=f"committed executable recovery {phase}",
                ):
                    pass

                self.assertFalse(marker_path.exists())
                self.assertEqual(
                    ("file", 0o755, fixture.new_binary),
                    self.runtime_binding_path_snapshot(fixture.bound),
                )
                self.assertEqual(
                    ("missing",),
                    self.runtime_binding_path_snapshot(
                        fixture.candidate
                    ),
                )
                self.assertEqual(
                    ("file", 0o755, fixture.old_binary),
                    self.runtime_binding_path_snapshot(fixture.backup),
                )

    def test_runtime_rebind_executable_swap_recovery_preserves_foreign_bound_candidate_and_backup_states(
        self,
    ) -> None:
        from codex_switch_transaction import locked_store_mutation

        for marker_state, phase in (
            ("prepared", "after_marker"),
            ("committed", "after_committed_marker"),
        ):
            for target_name in ("bound", "candidate", "backup"):
                with (
                    self.subTest(
                        state=marker_state,
                        target=target_name,
                    ),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    root = Path(temp_dir)
                    fixture = (
                        self.arrange_runtime_binding_executable_swap(
                            root
                        )
                    )
                    marker_path = (
                        self.interrupt_runtime_binding_executable_swap(
                            fixture,
                            phase=phase,
                        )
                    )
                    target = Path(getattr(fixture, target_name))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(
                        f"foreign-{marker_state}-{target_name}\n".encode()
                    )
                    target.chmod(0o711)
                    observed_paths = {
                        *fixture.artifact_paths.values(),
                        fixture.bound,
                        fixture.candidate,
                        fixture.backup,
                        marker_path,
                    }
                    snapshots = {
                        path: self.runtime_binding_path_snapshot(path)
                        for path in observed_paths
                    }

                    with self.assertRaisesRegex(
                        SwitchError,
                        "foreign binary state",
                    ):
                        with locked_store_mutation(
                            fixture.store,
                            operation=(
                                f"foreign {marker_state} "
                                f"executable {target_name}"
                            ),
                        ):
                            pass

                    self.assert_runtime_binding_paths_unchanged(
                        snapshots
                    )

    def test_runtime_rebind_legacy_schema_v1_v2_marker_fixtures_remain_byte_compatible(
        self,
    ) -> None:
        import codex_switch_transaction as transaction
        from codex_switch_transaction import locked_store_mutation

        for schema_version in (1, 2):
            for marker_state in ("prepared", "committed"):
                with (
                    self.subTest(
                        schema=schema_version,
                        state=marker_state,
                    ),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    root = Path(temp_dir)
                    store = self.make_store(root)
                    store.ensure()
                    profile = store.profile_dir("internal")
                    profile.mkdir(parents=True, exist_ok=True)
                    manifest = store.manifest_path("internal")
                    launcher = store.bin_dir / "codex-internal-app"
                    receipt = (
                        store.bin_dir
                        / "codex-internal-app.capabilities.json"
                    )
                    old_manifest = b'{"codex_bin":"legacy-old"}\n'
                    new_manifest = b'{"codex_bin":"legacy-new"}\n'
                    old_launcher = b"#!/bin/sh\n# legacy old\n"
                    new_launcher = b"#!/bin/sh\n# legacy new\n"
                    old_receipt = b'{"schema_version":1,"legacy":"old"}\n'
                    new_receipt = b'{"schema_version":2,"legacy":"new"}\n'
                    unrelated_receipt = b'{"legacy":"unmanaged"}\n'
                    if marker_state == "prepared":
                        manifest.write_bytes(new_manifest)
                        launcher.write_bytes(old_launcher)
                        receipt.write_bytes(
                            old_receipt
                            if schema_version == 2
                            else unrelated_receipt
                        )
                    else:
                        manifest.write_bytes(old_manifest)
                        launcher.write_bytes(new_launcher)
                        receipt.write_bytes(
                            old_receipt
                            if schema_version == 2
                            else unrelated_receipt
                        )
                    manifest.chmod(0o600)
                    launcher.chmod(0o755)
                    receipt.chmod(
                        0o600 if schema_version == 2 else 0o640
                    )
                    marker = {
                        "schema_version": schema_version,
                        "state": marker_state,
                        "manifest_path": str(manifest),
                        "launcher_path": str(launcher),
                        "old_manifest": (
                            self.runtime_rebind_file_state_fixture(
                                old_manifest,
                                0o600,
                            )
                        ),
                        "old_launcher": (
                            self.runtime_rebind_file_state_fixture(
                                old_launcher,
                                0o755,
                            )
                        ),
                        "new_manifest": (
                            self.runtime_rebind_file_state_fixture(
                                new_manifest,
                                0o600,
                            )
                        ),
                        "new_launcher": (
                            self.runtime_rebind_file_state_fixture(
                                new_launcher,
                                0o755,
                            )
                        ),
                    }
                    expected_fields = {
                        "schema_version",
                        "state",
                        "manifest_path",
                        "launcher_path",
                        "old_manifest",
                        "old_launcher",
                        "new_manifest",
                        "new_launcher",
                    }
                    if schema_version == 2:
                        marker.update(
                            {
                                "receipt_path": str(receipt),
                                "old_receipt": (
                                    self.runtime_rebind_file_state_fixture(
                                        old_receipt,
                                        0o600,
                                    )
                                ),
                                "new_receipt": (
                                    self.runtime_rebind_file_state_fixture(
                                        new_receipt,
                                        0o600,
                                    )
                                ),
                            }
                        )
                        expected_fields.update(
                            {
                                "receipt_path",
                                "old_receipt",
                                "new_receipt",
                            }
                        )
                    self.assertEqual(expected_fields, set(marker))
                    self.assertNotIn("artifacts", marker)
                    marker_path = (
                        store.root / ".runtime-binding-rebind.json"
                    )
                    marker_path.write_text(
                        json.dumps(marker, sort_keys=True) + "\n"
                    )
                    marker_path.chmod(0o600)

                    with patch.object(
                        transaction,
                        "_validated_runtime_rebind_bundle_marker",
                        side_effect=AssertionError(
                            "legacy marker entered schema-v3 validation"
                        ),
                    ):
                        with locked_store_mutation(
                            store,
                            operation=(
                                f"legacy schema v{schema_version} "
                                f"{marker_state} recovery"
                            ),
                        ):
                            pass

                    self.assertFalse(marker_path.exists())
                    expected_new = marker_state == "committed"
                    self.assertEqual(
                        new_manifest if expected_new else old_manifest,
                        manifest.read_bytes(),
                    )
                    self.assertEqual(
                        new_launcher if expected_new else old_launcher,
                        launcher.read_bytes(),
                    )
                    self.assertEqual(
                        0o600,
                        stat.S_IMODE(manifest.stat().st_mode),
                    )
                    self.assertEqual(
                        0o755,
                        stat.S_IMODE(launcher.stat().st_mode),
                    )
                    if schema_version == 2:
                        self.assertEqual(
                            new_receipt if expected_new else old_receipt,
                            receipt.read_bytes(),
                        )
                        self.assertEqual(
                            0o600,
                            stat.S_IMODE(receipt.stat().st_mode),
                        )
                    else:
                        self.assertEqual(
                            unrelated_receipt,
                            receipt.read_bytes(),
                        )
                        self.assertEqual(
                            0o640,
                            stat.S_IMODE(receipt.stat().st_mode),
                        )

    def arrange_restore_parent_cleanup_fixture(
        self,
        root: Path,
        *,
        parent_mode: int = 0o751,
    ) -> tuple[Store, Path, Path, Path]:
        store = self.make_store(root)
        parent = store.official_codex_home / "cleanup-parent"
        parent.mkdir(mode=parent_mode)
        parent.chmod(parent_mode)
        target = parent / "config.toml"
        target.write_text("current-before-restore\n")
        target.chmod(0o640)
        historical = store.backups_dir / "historical-parent-cleanup"
        historical.mkdir(parents=True)
        (historical / "backup.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "lifecycle": "committed",
                    "id": historical.name,
                    "operation": "switch",
                    "to_profile": "openai-official",
                    "entries": [
                        {
                            "path": str(target),
                            "before_state": {"kind": "missing"},
                            "committed_after_state": self.file_state(target),
                            "created_parent_paths": [str(parent)],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return store, parent, target, historical

    def arrange_capture_fixture(
        self,
        root: Path,
        *,
        unmanaged: bool = False,
    ) -> tuple[Store, Path, Path, dict[str, object]]:
        store = self.make_store(root)
        profile_dir = store.profile_dir("internal")
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.toml").write_text('model = "before"\n')
        (profile_dir / "auth.json").write_text('{"token":"before"}\n')
        (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
        if unmanaged:
            support_dir = profile_dir / "plugin-support"
            support_dir.mkdir()
            (support_dir / "catalog.json").write_text('{"plugin":"kept"}\n')
        source_home = root / "source"
        source_home.mkdir()
        (source_home / "config.toml").write_text('model = "after"\n')
        (source_home / "auth.json").write_text('{"token":"after"}\n')
        return store, profile_dir, source_home, {
            "source_home": source_home,
            "codex_bin": "/tmp/codex-internal",
            "app_cli_path": "/tmp/codex-internal",
            "allow_missing_auth": False,
            "overwrite": True,
        }

    def arrange_switch_effect_fixture(
        self,
        root: Path,
    ) -> tuple[Store, Path, Path, tuple[Path, ...]]:
        from codex_switch_launch import launch_agent_payload

        store = self.make_store(root)
        store.ensure()
        internal_profile = store.profile_dir("internal")
        official_profile = store.profile_dir("openai-official")
        internal_profile.mkdir()
        official_profile.mkdir()
        target_executable = root / "codex-target"
        prior_executable = root / "codex-prior"
        for executable in (target_executable, prior_executable):
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        (internal_profile / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "internal",
                    "codex_bin": str(target_executable),
                    "app_cli_path": str(target_executable),
                }
            )
            + "\n"
        )
        (official_profile / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "openai-official",
                    "codex_bin": str(prior_executable),
                    "app_cli_path": str(prior_executable),
                }
            )
            + "\n"
        )
        (internal_profile / "config.toml").write_text(
            'model = "internal-after"\n'
            'cli_auth_credentials_store = "file"\n'
        )
        (internal_profile / "auth.json").write_text(
            '{"internal":"after"}\n'
        )
        (official_profile / "config.toml").write_text(
            'model = "official-profile"\n'
        )
        (store.official_codex_home / "config.toml").write_text(
            'model = "official-runtime"\n[features]\nmemory = true\n'
        )
        (store.official_codex_home / "auth.json").write_text(
            '{"official":"before"}\n'
        )
        (store.internal_codex_home / "config.toml").write_text(
            'model = "internal-before"\n'
        )
        (store.internal_codex_home / "internal.config.toml").write_text(
            'model = "internal-layer-before"\n'
        )
        (store.internal_codex_home / "auth.json").write_text(
            '{"internal":"before"}\n'
        )
        shim_path = store.bin_dir / "codex"
        shim_path.write_text("#!/bin/sh\nexec /prior/codex \"$@\"\n")
        shim_path.chmod(0o755)
        store.launch_agent_path.write_bytes(
            launch_agent_payload(store.launch_agent_label, prior_executable)
        )
        store.launch_agent_path.chmod(0o644)
        store.active_path.write_text(
            json.dumps(
                {
                    "profile": "openai-official",
                    "codex_home": str(store.official_codex_home),
                    "live_codex_home": str(store.official_codex_home),
                    "home_mode": "official",
                }
            )
            + "\n"
        )
        observed_paths = (
            store.official_codex_home / "config.toml",
            store.official_codex_home / "auth.json",
            store.internal_codex_home / "config.toml",
            store.internal_codex_home / "internal.config.toml",
            store.internal_codex_home / "auth.json",
            store.manifest_path("internal"),
            shim_path,
            store.launch_agent_path,
            store.active_path,
        )
        return store, target_executable, prior_executable, observed_paths

    def write_explicit_official_active(
        self,
        store: Store,
        official_executable: Path,
    ) -> None:
        store.active_path.write_text(
            json.dumps(
                {
                    "profile": "openai-official",
                    "cli_profile": "openai-official",
                    "app_profile": "openai-official",
                    "codex_home": str(store.official_codex_home),
                    "live_codex_home": str(store.official_codex_home),
                    "home_mode": "official",
                    "shell_cli_path": str(official_executable),
                    "app_cli_path": str(official_executable),
                },
                sort_keys=True,
            )
            + "\n"
        )

    def arrange_pending_switch(
        self,
        root: Path,
    ) -> tuple[Store, Path, Path, dict[str, object]]:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class PendingInterruption(BaseException):
            pass

        class InterruptAfterActive(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise PendingInterruption("pending switch armed")

        store, _, _, _ = self.arrange_switch_effect_fixture(root)
        options: dict[str, object] = {
            "config_mode": "snapshot",
            "shared_config_base": None,
            "clear_missing_auth": False,
            "skip_shim": True,
            "skip_app_cli": True,
            "skip_launchctl": True,
            "filesystem_adapter": InterruptAfterActive(),
        }
        with self.assertRaisesRegex(PendingInterruption, "pending switch armed"):
            execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options=options,
                ),
            )
        marker_path = next(store.root.glob(".pending-transaction-*.json"))
        marker = json.loads(marker_path.read_text())
        backup_dir = store.backups_dir / str(marker["backup_id"])
        return store, marker_path, backup_dir, options

    def write_pre_marker_restore_evidence(
        self,
        store: Store,
        *,
        lifecycle: str = "prepared",
    ) -> Path:
        store.ensure()
        backup_dir = store.backups_dir / f"pre-marker-restore-{lifecycle}"
        backup_dir.mkdir()
        (backup_dir / "backup.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "id": backup_dir.name,
                    "operation": "restore",
                    "lifecycle": lifecycle,
                    "entries": [],
                    "restore_journal": {
                        "schema_version": 1,
                        "state": lifecycle,
                        "effects": [],
                    },
                },
                sort_keys=True,
            )
            + "\n"
        )
        return backup_dir

    def convert_pending_switch_to_legacy_markerless(
        self,
        marker_path: Path,
        backup_dir: Path,
    ) -> None:
        marker_path.unlink()
        manifest_path = backup_dir / "backup.json"
        manifest = json.loads(manifest_path.read_text())
        journal = manifest["switch_journal"]
        for key in (
            "operation",
            "backup_id",
            "transaction_id",
            "marker_name",
            "prepared_journal_sha256",
            "recovery_marker_required",
            "prepared_at",
        ):
            journal.pop(key, None)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")

    def arrange_restorable_file_backup(
        self,
        store: Store,
        *,
        backup_id: str,
    ) -> tuple[Path, Path]:
        from codex_switch_transaction import capture_path_state

        store.ensure()
        target = store.official_codex_home / "config.toml"
        target.write_text('model = "after"\n')
        target.chmod(0o600)
        backup_dir = store.backups_dir / backup_id
        payload = backup_dir / "payloads" / "0000-config.toml"
        payload.parent.mkdir(parents=True)
        payload.write_text('model = "before"\n')
        payload.chmod(0o600)
        before_state = capture_path_state(payload)
        before_state["path"] = str(target)
        committed_after_state = capture_path_state(target)
        (backup_dir / "backup.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "id": backup_id,
                    "operation": "switch",
                    "lifecycle": "committed",
                    "to_profile": "openai-official",
                    "entries": [
                        {
                            "path": str(target),
                            "before_state": before_state,
                            "committed_after_state": committed_after_state,
                            "payload": "payloads/0000-config.toml",
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n"
        )
        return backup_dir, target

    def before_switch_effect_adapter(
        self,
        phase: str,
        callback: object,
        *,
        target: Path | None = None,
    ) -> object:
        from codex_switch_transaction import FilesystemAdapter

        if not callable(callback):
            raise AssertionError("drift callback must be callable")

        class DriftBeforeDependentAction(FilesystemAdapter):
            def __init__(self) -> None:
                self.injected = False

            def before_switch_effect_action(
                self,
                path: Path,
                effect: dict[str, object],
            ) -> None:
                if (
                    self.injected
                    or effect.get("phase") != phase
                    or (target is not None and path != target)
                ):
                    return
                self.injected = True
                callback()

        return DriftBeforeDependentAction()

    def test_atomic_write_fsyncs_file_then_parent_without_chmod_existing_parent(
        self,
    ) -> None:
        from codex_switch_io import atomic_write

        events: list[str] = []
        real_fchmod = os.fchmod
        real_fsync = os.fsync
        real_replace = os.replace

        def tracked_fchmod(descriptor: int, mode: int) -> None:
            events.append("fchmod")
            real_fchmod(descriptor, mode)

        def tracked_fsync(descriptor: int) -> None:
            info = os.fstat(descriptor)
            events.append("fsync-directory" if stat.S_ISDIR(info.st_mode) else "fsync-file")
            real_fsync(descriptor)

        def tracked_replace(source: object, destination: object) -> None:
            events.append("replace")
            real_replace(source, destination)

        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "existing"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            target = parent / "value.json"

            with (
                patch("codex_switch_io.os.fchmod", side_effect=tracked_fchmod),
                patch("codex_switch_io.os.fsync", side_effect=tracked_fsync),
                patch("codex_switch_io.os.replace", side_effect=tracked_replace),
            ):
                atomic_write(target, b"{}\n", mode=0o640)

            self.assertEqual(0o755, parent.stat().st_mode & 0o777)
            self.assertEqual(0o640, target.stat().st_mode & 0o777)
            self.assertEqual(
                ["fchmod", "fsync-file", "replace", "fsync-directory"],
                events,
            )

    def test_descriptor_atomic_write_fsyncs_parent_after_rename(self) -> None:
        from codex_switch_transaction import _atomic_write_at

        events: list[str] = []
        real_fsync = os.fsync

        def tracked_fsync(descriptor: int) -> None:
            info = os.fstat(descriptor)
            events.append("directory" if stat.S_ISDIR(info.st_mode) else "file")
            real_fsync(descriptor)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            descriptor = os.open(root, os.O_RDONLY)
            try:
                with patch(
                    "codex_switch_transaction.os.fsync",
                    side_effect=tracked_fsync,
                ):
                    _atomic_write_at(
                        descriptor,
                        ("value.json",),
                        b"{}\n",
                        mode=0o600,
                    )
            finally:
                os.close(descriptor)

            self.assertEqual(b"{}\n", (root / "value.json").read_bytes())
            self.assertEqual(["file", "directory"], events)

    def write_v2_backup(
        self,
        store: Store,
        backup_id: str,
        entries: list[dict[str, object]],
    ) -> Path:
        backup_dir = store.backups_dir / backup_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "backup.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "lifecycle": "committed",
                    "id": backup_id,
                    "operation": "switch",
                    "to_profile": "internal",
                    "entries": entries,
                }
            )
            + "\n"
        )
        return backup_dir

    def test_shared_internal_switch_preserves_existing_config_and_auth_contract(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            internal_bin = root / "codex-internal"
            official_bin = root / "codex-official"
            for executable in (internal_bin, official_bin):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            (internal_profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "internal",
                        "codex_bin": str(internal_bin),
                        "app_cli_path": str(internal_bin),
                    }
                )
                + "\n"
            )
            (official_profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "openai-official",
                        "codex_bin": str(official_bin),
                        "app_cli_path": str(official_bin),
                    }
                )
                + "\n"
            )
            (internal_profile / "config.toml").write_text(
                'model = "internal-model"\n'
                'model_provider = "internal-provider"\n'
                "\n"
                "[model_providers.internal-provider]\n"
                'name = "Internal"\n'
            )
            (official_profile / "config.toml").write_text(
                'model = "official-model"\n'
            )
            (store.official_codex_home / "config.toml").write_text(
                "[features]\n"
                "memory = true\n"
                "\n"
                "[mcp_servers.shared]\n"
                'command = "shared-mcp"\n'
            )
            (store.official_codex_home / "auth.json").write_text(
                '{"official":"auth"}\n'
            )
            (store.internal_codex_home / "auth.json").write_text(
                '{"stale":"internal"}\n'
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            internal_config = (store.internal_codex_home / "config.toml").read_text()
            self.assertIn("[features]", internal_config)
            self.assertIn("memory = true", internal_config)
            self.assertIn("[mcp_servers.shared]", internal_config)
            self.assertIn('model = "internal-model"', internal_config)
            self.assertFalse((store.internal_codex_home / "auth.json").exists())
            self.assertEqual(
                '{"official":"auth"}\n',
                (store.official_codex_home / "auth.json").read_text(),
            )

    def test_shared_internal_file_auth_survives_switch_without_cross_profile_copy(self):
        from codex_switch_transaction import TransactionRequest, execute_transaction

        variants = (
            'cli_auth_credentials_store="file"',
            'cli_auth_credentials_store="file" # local credentials',
            '"cli_auth_credentials_store"="file"',
            'cli_auth_credentials_store="auto"',
            '',
        )
        cases = (
            ('{"token":"refreshed"}', '{"token":"saved"}', True, '{"token":"refreshed"}'),
            (None, '{"token":"saved"}', True, '{"token":"saved"}'),
            (None, None, True, None),
            ('{"token":"stale"}', '{"token":"saved"}', False, None),
        )
        for storage, (runtime_auth, profile_auth, requires_auth, expected) in product(variants, cases):
            with self.subTest(storage=storage, runtime_auth=runtime_auth, requires_auth=requires_auth), tempfile.TemporaryDirectory() as temp:
                store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp))
                profile = store.profile_dir("internal")
                (profile / "config.toml").write_text(
                    'model="internal-after"\nmodel_provider="internal-provider"\n'
                    + storage + '\n'
                    + '[model_providers.internal-provider]\n'
                    'base_url="https://provider.example/v1"\nwire_api="responses"\n'
                    'requires_openai_auth=' + str(requires_auth).lower() + '\n'
                    + ('' if requires_auth else 'env_key="INTERNAL_TEST_KEY"\n')
                )
                (store.internal_codex_home / "config.toml").write_bytes(
                    (profile / "config.toml").read_bytes()
                )
                for path, payload in ((profile / "auth.json", profile_auth),
                                      (store.internal_codex_home / "auth.json", runtime_auth)):
                    if payload is None:
                        path.unlink()
                    else:
                        path.write_text(payload)
                official_auth = store.official_codex_home / "auth.json"
                official_before = official_auth.read_bytes()
                request = TransactionRequest(operation="switch", profile="internal", options={
                    "config_mode": "shared", "skip_shim": True,
                    "skip_app_cli": True, "skip_launchctl": True,
                })
                for _ in range(2):
                    result = execute_transaction(store, request)
                    self.assertEqual(result.outcome, "committed")
                    active_auth = store.internal_codex_home / "auth.json"
                    if expected is None:
                        self.assertFalse(active_auth.exists())
                    else:
                        self.assertEqual(active_auth.read_text(), expected)
                    self.assertEqual(official_auth.read_bytes(), official_before)

    def test_shared_official_switch_preserves_existing_config_and_auth_contract(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            internal_bin = root / "codex-internal"
            official_bin = root / "codex-official"
            for executable in (internal_bin, official_bin):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            (internal_profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "internal",
                        "codex_bin": str(internal_bin),
                        "app_cli_path": str(internal_bin),
                    }
                )
                + "\n"
            )
            (official_profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "openai-official",
                        "codex_bin": str(official_bin),
                        "app_cli_path": str(official_bin),
                    }
                )
                + "\n"
            )
            (internal_profile / "config.toml").write_text(
                'model = "internal-profile-model"\n'
            )
            (official_profile / "config.toml").write_text(
                'model = "official-profile-model"\n'
                'cli_auth_credentials_store = "file"\n'
            )
            (store.official_codex_home / "config.toml").write_text(
                'model = "official-runtime-model"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            (store.official_codex_home / "auth.json").write_text(
                '{"official":"auth"}\n'
            )
            (store.internal_codex_home / "config.toml").write_text(
                'model = "internal-runtime-model"\n'
                'notify = ["turn-ended"]\n'
                "\n"
                "[features]\n"
                "codex_hooks = true\n"
                "\n"
                "[mcp_servers.internal_shared]\n"
                'command = "internal-mcp"\n'
            )
            (store.internal_codex_home / "auth.json").write_text(
                '{"internal":"auth"}\n'
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="openai-official",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            official_config = (store.official_codex_home / "config.toml").read_text()
            self.assertIn('notify = ["turn-ended"]', official_config)
            self.assertIn("codex_hooks = true", official_config)
            self.assertIn("[mcp_servers.internal_shared]", official_config)
            self.assertNotIn("internal-runtime-model", official_config)
            self.assertEqual(
                '{"official":"auth"}\n',
                (store.official_codex_home / "auth.json").read_text(),
            )
            self.assertEqual(
                '{"internal":"auth"}\n',
                (store.internal_codex_home / "auth.json").read_text(),
            )

    def test_official_shared_dry_run_ignores_profile_local_runtime_sockets(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            internal_bin = root / "codex-internal"
            official_bin = root / "codex-official"
            for executable in (internal_bin, official_bin):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            for profile, executable in (
                (internal_profile, internal_bin),
                (official_profile, official_bin),
            ):
                (profile / "manifest.json").write_text(
                    json.dumps(
                        {
                            "name": profile.name,
                            "codex_bin": str(executable),
                            "app_cli_path": str(executable),
                        }
                    )
                    + "\n"
                )
            (internal_profile / "config.toml").write_text(
                'model = "internal-profile-model"\n'
            )
            (official_profile / "config.toml").write_text(
                'model = "official-profile-model"\n'
            )
            (store.internal_codex_home / "config.toml").write_text(
                'notify = ["turn-ended"]\n'
            )
            (store.official_codex_home / "config.toml").write_text(
                'model = "official-runtime-model"\n'
            )
            runtime_sockets: list[socket.socket] = []
            source_socket_paths: list[Path] = []
            target_socket_paths: list[Path] = []
            try:
                for home, socket_prefix, socket_paths in (
                    (
                        store.internal_codex_home,
                        "i",
                        source_socket_paths,
                    ),
                    (
                        store.official_codex_home,
                        "o",
                        target_socket_paths,
                    ),
                ):
                    for directory_name, socket_name in (
                        ("ipc", f"{socket_prefix}i.sock"),
                        ("mcp-oauth-locks", f"{socket_prefix}o.sock"),
                    ):
                        directory = home / directory_name
                        directory.mkdir(exist_ok=True)
                        socket_path = directory / socket_name
                        runtime_socket = socket.socket(
                            socket.AF_UNIX,
                            socket.SOCK_STREAM,
                        )
                        try:
                            runtime_socket.bind(str(socket_path))
                        except BaseException:
                            runtime_socket.close()
                            raise
                        runtime_sockets.append(runtime_socket)
                        socket_paths.append(socket_path)

                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="openai-official",
                        options={
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                        },
                    ),
                    dry_run=True,
                )
            finally:
                for runtime_socket in runtime_sockets:
                    runtime_socket.close()

            self.assertEqual("dry_run", receipt.outcome)
            self.assertEqual([], list(store.backups_dir.iterdir()))
            self.assertFalse(store.active_path.exists())
            for socket_path in source_socket_paths + target_socket_paths:
                self.assertTrue(stat.S_ISSOCK(os.lstat(socket_path).st_mode))
            for socket_path in source_socket_paths:
                self.assertFalse(
                    (
                        store.official_codex_home
                        / socket_path.relative_to(store.internal_codex_home)
                    ).exists()
                )
            for socket_path in target_socket_paths:
                self.assertFalse(
                    (
                        store.internal_codex_home
                        / socket_path.relative_to(store.official_codex_home)
                    ).exists()
                )

    def test_official_shared_dry_run_ignores_unknown_special_file(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            internal_bin = root / "codex-internal"
            official_bin = root / "codex-official"
            for executable in (internal_bin, official_bin):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            for profile, executable in (
                (internal_profile, internal_bin),
                (official_profile, official_bin),
            ):
                (profile / "manifest.json").write_text(
                    json.dumps(
                        {
                            "name": profile.name,
                            "codex_bin": str(executable),
                            "app_cli_path": str(executable),
                        }
                    )
                    + "\n"
                )
            (internal_profile / "config.toml").write_text(
                'model = "internal-profile-model"\n'
            )
            (official_profile / "config.toml").write_text(
                'model = "official-profile-model"\n'
            )
            (store.internal_codex_home / "config.toml").write_text(
                'notify = ["turn-ended"]\n'
            )
            (store.official_codex_home / "config.toml").write_text(
                'model = "official-runtime-model"\n'
            )
            socket_path = store.internal_codex_home / "unknown-runtime.sock"
            runtime_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                runtime_socket.bind(str(socket_path))
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="openai-official",
                        options={
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                        },
                    ),
                    dry_run=True,
                )
            finally:
                runtime_socket.close()

            self.assertEqual("dry_run", receipt.outcome)
            self.assertTrue(stat.S_ISSOCK(os.lstat(socket_path).st_mode))
            self.assertFalse(
                (store.official_codex_home / socket_path.name).exists()
            )
            self.assertEqual([], list(store.backups_dir.iterdir()))
            self.assertFalse(store.active_path.exists())

    def test_snapshot_switch_creates_schema_v2_restorable_backup(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            internal_bin = root / "codex-internal"
            official_bin = root / "codex-official"
            for executable in (internal_bin, official_bin):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            (internal_profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "internal",
                        "codex_bin": str(internal_bin),
                        "app_cli_path": str(internal_bin),
                    }
                )
                + "\n"
            )
            (official_profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "openai-official",
                        "codex_bin": str(official_bin),
                        "app_cli_path": str(official_bin),
                    }
                )
                + "\n"
            )
            (internal_profile / "config.toml").write_text(
                'model = "internal-profile"\n'
            )
            (official_profile / "config.toml").write_text(
                'model = "official-after"\n'
                'cli_auth_credentials_store = "file"\n'
            )
            (official_profile / "auth.json").write_text(
                '{"official":"after"}\n'
            )
            official_config = store.official_codex_home / "config.toml"
            official_profile_config = (
                store.official_codex_home / "openai-official.config.toml"
            )
            official_auth = store.official_codex_home / "auth.json"
            official_config.write_text('model = "official-before"\n')
            official_auth.write_text('{"official":"before"}\n')
            store.active_path.write_text(
                json.dumps(
                    {
                        "profile": "internal",
                        "codex_home": str(store.internal_codex_home),
                    }
                )
                + "\n"
            )
            before_active = store.active_path.read_bytes()

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="openai-official",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            backup_dir = store.backups_dir / str(receipt.backup_id)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual(2, manifest["schema_version"])
            self.assertEqual("committed", manifest["lifecycle"])
            self.assertTrue(manifest["entries"])
            self.assertTrue(
                all(entry.get("committed_after_state") for entry in manifest["entries"])
            )
            self.assertIn(
                'model = "official-after"',
                official_profile_config.read_text(),
            )
            self.assertEqual('{"official":"after"}\n', official_auth.read_text())

            restore_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": receipt.backup_id, "force": False},
                ),
            )

            self.assertEqual("committed", restore_receipt.outcome)
            self.assertEqual('model = "official-before"\n', official_config.read_text())
            self.assertFalse(official_profile_config.exists())
            self.assertEqual('{"official":"before"}\n', official_auth.read_text())
            self.assertEqual(before_active, store.active_path.read_bytes())

    def test_switch_rechecks_frozen_before_states_before_first_mutation(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, observed_paths = self.arrange_switch_effect_fixture(root)
            target_config = store.internal_codex_home / "config.toml"
            before = {
                path: path.read_bytes()
                for path in observed_paths
                if path != target_config and path.exists()
            }

            def drift_after_backup(message: str) -> None:
                if message == "Applying switch mutations...":
                    target_config.write_text('model = "external-drift"\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "progress_callback": drift_after_backup,
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual('model = "external-drift"\n', target_config.read_text())
            for path, expected in before.items():
                self.assertEqual(expected, path.read_bytes(), str(path))
            backup = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", backup["lifecycle"])
            self.assertIn("changed after backup", backup["failure"])

    def test_shared_switch_uses_frozen_support_entry_set(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            planned_support = store.official_codex_home / "rules"
            planned_support.mkdir()
            (planned_support / "tool.json").write_text("{}\n")

            def add_late_source(message: str) -> None:
                if message == "Applying switch mutations...":
                    late_support = store.official_codex_home / "skills"
                    late_support.mkdir()
                    (late_support / "late.json").write_text("{}\n")

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "progress_callback": add_late_source,
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertFalse(
                (store.internal_codex_home / "rules").exists()
            )
            self.assertFalse(
                (store.internal_codex_home / "skills").exists()
            )
            self.assertTrue(
                (store.official_codex_home / "skills").is_dir()
            )
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            planned_paths = {entry["path"] for entry in manifest["entries"]}
            self.assertIn(
                str(store.internal_codex_home / "rules"),
                planned_paths,
            )
            self.assertNotIn(
                str(store.internal_codex_home / "skills"),
                planned_paths,
            )

    def test_shared_support_allowlist_preserves_ignored_targets(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            allowed_names = ("AGENTS.md", "prompts", "rules", "skills")
            (store.official_codex_home / "AGENTS.md").write_text(
                "# Shared workstation guidance\n"
            )
            for name in allowed_names[1:]:
                source = store.official_codex_home / name
                source.mkdir()
                (source / "source.txt").write_text(f"{name}-source\n")

            ignored_source_names = (
                "worktrees",
                "unknown-support",
                "..codex-global-state.json.tmp-20260810",
                ".codex-global-state.json.backup-20260810",
            )
            for name in ignored_source_names:
                source = store.official_codex_home / name
                source.mkdir()
                (source / "source.txt").write_text(f"{name}-source\n")
            preserved_target = store.internal_codex_home / "worktrees"
            preserved_target.mkdir()
            (preserved_target / "target.txt").write_text("target-owned\n")
            progress: list[str] = []

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "progress_callback": progress.append,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual(
                "# Shared workstation guidance\n",
                (store.internal_codex_home / "AGENTS.md").read_text(),
            )
            for name in allowed_names[1:]:
                target = store.internal_codex_home / name
                self.assertTrue(target.is_symlink(), name)
                self.assertTrue(
                    target.resolve().samefile(store.official_codex_home / name),
                    name,
                )
            self.assertEqual(
                "target-owned\n",
                (preserved_target / "target.txt").read_text(),
            )
            for name in ignored_source_names[1:]:
                self.assertFalse(
                    (store.internal_codex_home / name).exists(),
                    name,
                )

            backup_dir = store.backups_dir / str(receipt.backup_id)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            source_entry_set = next(
                item
                for item in manifest["switch_journal"]["frozen_inputs"]
                if item.get("label") == "shared source entry set"
            )
            source_entries = source_entry_set["before_state"]["entries"]
            self.assertEqual(
                list(allowed_names),
                [entry["name"] for entry in source_entries],
            )
            self.assertTrue(
                all("state" not in entry for entry in source_entries),
                source_entries,
            )
            planned_paths = {
                entry["path"] for entry in manifest["entries"]
            }
            for name in ignored_source_names:
                self.assertNotIn(
                    str(store.internal_codex_home / name),
                    planned_paths,
                )
            self.assertEqual(
                [
                    "Applying shared support [1/4]: AGENTS.md",
                    "Applying shared support [2/4]: prompts",
                    "Applying shared support [3/4]: rules",
                    "Applying shared support [4/4]: skills",
                ],
                [
                    message
                    for message in progress
                    if message.startswith("Applying shared support [")
                ],
            )

    def test_split_running_app_fails_when_rebind_is_required(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter, launch_agent_payload
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, official_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            self.write_explicit_official_active(store, official_executable)
            store.launch_agent_path.write_bytes(
                launch_agent_payload(store.launch_agent_label, target_executable)
            )
            launch_agent_before = store.launch_agent_path.read_bytes()
            active_before = store.active_path.read_bytes()
            runner = _FakeLaunchctlRunner(
                gui_env=str(target_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )
            stopped_probes: list[str] = []

            def observe_running(_store: Store, _selection: object) -> bool:
                stopped_probes.append("running-rebind")
                return True

            with self.assertRaisesRegex(
                SwitchError,
                "fully quit.*keep.*closed",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "app_profile": "openai-official",
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "desktop_binding_adapter": desktop,
                            "split_app_is_running": observe_running,
                        },
                    ),
                )

            self.assertEqual(["running-rebind"], stopped_probes)
            self.assertEqual(
                ["observe:getenv", "observe:service"],
                runner.events,
            )
            self.assertEqual(active_before, store.active_path.read_bytes())
            self.assertEqual(
                launch_agent_before,
                store.launch_agent_path.read_bytes(),
            )
            self.assertEqual([], list(store.backups_dir.iterdir()))
            self.assertEqual(
                [],
                list(store.root.glob(".pending-transaction-*.json")),
            )

    def test_split_running_official_app_preserves_desktop_surface(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, official_executable, _ = self.arrange_switch_effect_fixture(root)
            self.write_explicit_official_active(store, official_executable)
            desktop_source = store.official_codex_home / ".codex-global-state.json"
            desktop_target = store.internal_codex_home / ".codex-global-state.json"
            desktop_source.write_text(
                json.dumps({"appshotHotkey": "official-live"}, sort_keys=True)
                + "\n"
            )
            desktop_target.write_text(
                json.dumps({"appshotHotkey": "internal-preserved"}, sort_keys=True)
                + "\n"
            )
            launch_agent_before = store.launch_agent_path.read_bytes()
            official_files_before = {
                path: path.read_bytes()
                for path in (
                    store.official_codex_home / "config.toml",
                    store.official_codex_home / "auth.json",
                    desktop_source,
                )
            }
            desktop_target_before = desktop_target.read_bytes()
            runner = _FakeLaunchctlRunner(
                gui_env=str(official_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )
            process_observations: list[str] = []

            def observe_running(_store: Store, _selection: object) -> bool:
                process_observations.append("running-official")
                return True

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "app_profile": "openai-official",
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": False,
                        "skip_app_cli": False,
                        "skip_launchctl": False,
                        "desktop_binding_adapter": desktop,
                        "split_app_is_running": observe_running,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual(["running-official"], process_observations)
            self.assertEqual(
                ["observe:getenv", "observe:service"],
                runner.events,
            )
            self.assertEqual(
                launch_agent_before,
                store.launch_agent_path.read_bytes(),
            )
            for path, expected in official_files_before.items():
                self.assertEqual(expected, path.read_bytes(), str(path))
            self.assertEqual(desktop_target_before, desktop_target.read_bytes())
            active = json.loads(store.active_path.read_text())
            self.assertEqual("internal", active["cli_profile"])
            self.assertEqual("openai-official", active["app_profile"])
            self.assertEqual(str(official_executable), active["app_cli_path"])
            backup_dir = store.backups_dir / str(receipt.backup_id)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            entry_paths = {entry["path"] for entry in manifest["entries"]}
            self.assertNotIn(str(store.launch_agent_path), entry_paths)
            self.assertNotIn(str(desktop_source), entry_paths)
            self.assertNotIn(str(desktop_target), entry_paths)
            effect_phases = {
                effect["phase"]
                for effect in manifest["switch_journal"]["effects"]
            }
            self.assertFalse(
                effect_phases
                & {
                    "app_wrapper_write",
                    "desktop_bootout",
                    "desktop_bootstrap",
                    "desktop_global_state_sync",
                    "desktop_setenv",
                    "plist_write",
                }
            )

    def test_split_default_runtime_attestation_preserves_matching_owner(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, official_executable, _ = self.arrange_switch_effect_fixture(root)
            self.write_explicit_official_active(store, official_executable)
            runner = _FakeLaunchctlRunner(
                gui_env=str(official_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )
            processes = [
                SimpleNamespace(
                    kind="desktop",
                    host_kind="chatgpt",
                    app_cli_env=str(official_executable),
                ),
                SimpleNamespace(
                    kind="app-server",
                    command_path=str(official_executable),
                    parent_command="",
                ),
            ]

            with (
                patch(
                    "codex_switch_running_app.is_default_desktop_context",
                    return_value=True,
                ),
                patch(
                    "codex_switch_io.run_quiet",
                    return_value=(0, "fixture process inventory"),
                ),
                patch(
                    "codex_switch_running_app.running_codex_processes",
                    return_value=processes,
                ),
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "app_profile": "openai-official",
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("committed", receipt.outcome)
            self.assertIn("App action: preserve", "\n".join(receipt.preview_lines))
            self.assertEqual(
                ["observe:getenv", "observe:service"],
                runner.events,
            )

    def test_split_default_runtime_attestation_rejects_mismatched_owner(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, internal_executable, official_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            self.write_explicit_official_active(store, official_executable)
            runner = _FakeLaunchctlRunner(
                gui_env=str(official_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )
            processes = [
                SimpleNamespace(
                    kind="app-server",
                    command_path=str(internal_executable),
                    parent_command="",
                ),
            ]

            with (
                patch(
                    "codex_switch_running_app.is_default_desktop_context",
                    return_value=True,
                ),
                patch(
                    "codex_switch_io.run_quiet",
                    return_value=(0, "fixture process inventory"),
                ),
                patch(
                    "codex_switch_running_app.running_codex_processes",
                    return_value=processes,
                ),
                self.assertRaisesRegex(
                    SwitchError,
                    "fully quit.*keep.*closed",
                ),
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "app_profile": "openai-official",
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual([], list(store.backups_dir.iterdir()))
            self.assertEqual(
                ["observe:getenv", "observe:service"],
                runner.events,
            )

    def test_split_preview_reports_preserve_without_stopped_app_probe(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, official_executable, _ = self.arrange_switch_effect_fixture(root)
            self.write_explicit_official_active(store, official_executable)
            active_before = store.active_path.read_bytes()
            launch_agent_before = store.launch_agent_path.read_bytes()
            runner = _FakeLaunchctlRunner(
                gui_env=str(official_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )
            stopped_probes: list[str] = []

            def fail_if_probed(_store: Store, _selection: object) -> bool:
                stopped_probes.append("called")
                raise AssertionError("preserve preview must not require stopped proof")

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "app_profile": "openai-official",
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": False,
                        "skip_app_cli": False,
                        "skip_launchctl": False,
                        "desktop_binding_adapter": desktop,
                        "split_app_is_running": fail_if_probed,
                    },
                ),
                dry_run=True,
            )

            output = "\n".join(receipt.preview_lines)
            self.assertEqual("dry_run", receipt.outcome)
            self.assertIn("App action: preserve", output)
            self.assertNotIn("Stopped-App requirement:", output)
            self.assertEqual([], stopped_probes)
            self.assertEqual(
                ["observe:getenv", "observe:service"],
                runner.events,
            )
            self.assertEqual(active_before, store.active_path.read_bytes())
            self.assertEqual(
                launch_agent_before,
                store.launch_agent_path.read_bytes(),
            )
            self.assertEqual([], list(store.backups_dir.iterdir()))

    def test_split_unreadable_app_inventory_fails_when_rebind_is_required(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter, launch_agent_payload
        from codex_switch_transaction import TransactionRequest, execute_transaction

        def unreadable_inventory(_store: Store, _selection: object) -> bool:
            raise OSError("injected process inventory failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, official_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            self.write_explicit_official_active(store, official_executable)
            store.launch_agent_path.write_bytes(
                launch_agent_payload(store.launch_agent_label, target_executable)
            )
            launch_agent_before = store.launch_agent_path.read_bytes()
            active_before = store.active_path.read_bytes()
            runner = _FakeLaunchctlRunner(
                gui_env=str(target_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Unable to prove.*App.*stopped",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "app_profile": "openai-official",
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "desktop_binding_adapter": desktop,
                            "split_app_is_running": unreadable_inventory,
                        },
                    ),
                )

            self.assertEqual(
                ["observe:getenv", "observe:service"],
                runner.events,
            )
            self.assertEqual(active_before, store.active_path.read_bytes())
            self.assertEqual(
                launch_agent_before,
                store.launch_agent_path.read_bytes(),
            )
            self.assertEqual([], list(store.backups_dir.iterdir()))
            self.assertEqual(
                [],
                list(store.root.glob(".pending-transaction-*.json")),
            )

    def test_split_live_process_probe_fails_closed_before_backup(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        cases = (
            (
                (1, ""),
                [],
                "Unable to prove.*App.*stopped",
            ),
            (
                (0, "fixture process inventory"),
                [SimpleNamespace(kind="app-server")],
                "fully quit.*keep.*closed",
            ),
        )
        for ps_result, processes, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store, _, _, _ = self.arrange_switch_effect_fixture(root)
                active_before = store.active_path.read_bytes()
                with (
                    patch(
                        "codex_switch_running_app.is_default_desktop_context",
                        return_value=True,
                    ),
                    patch("codex_switch_io.run_quiet", return_value=ps_result),
                    patch(
                        "codex_switch_running_app.running_codex_processes",
                        return_value=processes,
                    ),
                    self.assertRaisesRegex(SwitchError, expected),
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options={
                                "app_profile": "openai-official",
                                "config_mode": "shared",
                                "shared_config_base": None,
                                "clear_missing_auth": False,
                                "skip_shim": True,
                                "skip_app_cli": False,
                                "skip_launchctl": True,
                            },
                        ),
                    )

                self.assertEqual(active_before, store.active_path.read_bytes())
                self.assertEqual([], list(store.backups_dir.iterdir()))

    def test_split_dry_run_reports_stopped_app_requirement_without_observing_processes(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            observations: list[str] = []
            active_before = store.active_path.read_bytes()

            def observe_running(_store: Store, _selection: object) -> bool:
                observations.append("called")
                return True

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "app_profile": "openai-official",
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": False,
                        "skip_launchctl": True,
                        "split_app_is_running": observe_running,
                    },
                ),
                dry_run=True,
            )

            self.assertEqual("dry_run", receipt.outcome)
            self.assertEqual([], observations)
            output = "\n".join(receipt.preview_lines)
            self.assertIn("App action: rebind", output)
            self.assertIn(
                "quit ChatGPT/Codex App and keep it closed until the switch completes",
                output,
            )
            self.assertNotIn("Desktop global settings state", output)
            self.assertNotIn(".codex-global-state.json", output)
            self.assertEqual(active_before, store.active_path.read_bytes())
            self.assertEqual([], list(store.backups_dir.iterdir()))

    def test_shared_support_progress_is_counted_and_ordered(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            (store.official_codex_home / "AGENTS.md").write_text("# Shared\n")
            for name in ("prompts", "rules", "skills"):
                source = store.official_codex_home / name
                source.mkdir()
                (source / "source.txt").write_text(f"{name}\n")
            rules_target = store.internal_codex_home / "rules"

            def fail_rules_effect() -> None:
                raise OSError("injected rules sync failure")

            adapter = self.before_switch_effect_adapter(
                "shared_support_sync",
                fail_rules_effect,
                target=rules_target,
            )
            progress: list[str] = []

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                        "progress_callback": progress.append,
                    },
                ),
            )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(
                [
                    "Applying shared support [1/4]: AGENTS.md",
                    "Applying shared support [2/4]: prompts",
                    "Applying shared support [3/4]: rules",
                ],
                [
                    message
                    for message in progress
                    if message.startswith("Applying shared support [")
                ],
            )
            self.assertFalse((store.internal_codex_home / "skills").exists())

    def test_split_late_app_state_drift_rolls_back_after_stopped_preflight(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter, launch_agent_payload
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, prior_executable, _ = self.arrange_switch_effect_fixture(root)
            late_app_cli = self.make_executable(root, "codex-late-app")
            late_launch_agent_payload = launch_agent_payload(
                store.launch_agent_label,
                late_app_cli,
            )
            active_before = store.active_path.read_bytes()
            target_config = store.internal_codex_home / "config.toml"
            target_config_before = target_config.read_bytes()

            def launch_app_write() -> None:
                store.launch_agent_path.write_bytes(
                    late_launch_agent_payload
                )

            filesystem = self.before_switch_effect_adapter(
                "config_write",
                launch_app_write,
                target=target_config,
            )
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "app_profile": "openai-official",
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": False,
                        "skip_launchctl": True,
                        "filesystem_adapter": filesystem,
                        "desktop_binding_adapter": desktop,
                        "split_app_is_running": (
                            lambda _store, _selection: False
                        ),
                    },
                ),
            )

            self.assertTrue(getattr(filesystem, "injected"))
            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(active_before, store.active_path.read_bytes())
            self.assertEqual(
                target_config_before,
                target_config.read_bytes(),
            )
            self.assertEqual(
                late_launch_agent_payload,
                store.launch_agent_path.read_bytes(),
            )
            self.assertIn(
                "Required switch input changed",
                "\n".join(receipt.preview_lines),
            )

    def test_shared_switch_preserves_concurrent_desktop_global_state_after_noop_merge(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        self.maxDiff = None
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            desktop_source = (
                store.official_codex_home / ".codex-global-state.json"
            )
            desktop_target = (
                store.internal_codex_home / ".codex-global-state.json"
            )
            desktop_source.write_text(
                json.dumps({"appshotHotkey": "same"}, sort_keys=True) + "\n"
            )
            desktop_target.write_text(
                json.dumps(
                    {
                        "appOwned": "before",
                        "appshotHotkey": "same",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            concurrent_payload = (
                json.dumps(
                    {
                        "appOwned": "after",
                        "appshotHotkey": "same",
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()

            def replace_desktop_target() -> None:
                replacement = desktop_target.with_name(
                    f".{desktop_target.name}.app-update"
                )
                replacement.write_bytes(concurrent_payload)
                os.replace(replacement, desktop_target)

            target_config = store.internal_codex_home / "config.toml"
            adapter = self.before_switch_effect_adapter(
                "config_write",
                replace_desktop_target,
                target=target_config,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertIsNotNone(receipt.backup_id)
            backup_dir = store.backups_dir / str(receipt.backup_id)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            target_path = str(desktop_target)
            actual = {
                "outcome": receipt.outcome,
                "pending_markers": sorted(
                    path.name
                    for path in store.root.glob(
                        ".pending-transaction-*.json"
                    )
                ),
                "target_payload": desktop_target.read_bytes(),
                "backup_contains_target": any(
                    entry.get("path") == target_path
                    for entry in manifest["entries"]
                ),
                "journal_contains_target": any(
                    effect.get("path") == target_path
                    for effect in manifest["switch_journal"]["effects"]
                ),
                "frozen_inputs_contain_target": any(
                    item.get("path") == target_path
                    for item in manifest["switch_journal"]["frozen_inputs"]
                ),
            }
            self.assertEqual(
                {
                    "outcome": "committed",
                    "pending_markers": [],
                    "target_payload": concurrent_payload,
                    "backup_contains_target": False,
                    "journal_contains_target": False,
                    "frozen_inputs_contain_target": False,
                },
                actual,
            )

    def test_shared_switch_real_desktop_global_state_merge_remains_identity_bound(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            desktop_source = (
                store.official_codex_home / ".codex-global-state.json"
            )
            desktop_target = (
                store.internal_codex_home / ".codex-global-state.json"
            )
            desktop_source.write_text(
                json.dumps(
                    {"appshotHotkey": "source"},
                    sort_keys=True,
                )
                + "\n"
            )
            desktop_target.write_text(
                json.dumps(
                    {
                        "appOwned": "preserve",
                        "appshotHotkey": "target",
                    },
                    sort_keys=True,
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                {
                    "appOwned": "preserve",
                    "appshotHotkey": "source",
                },
                json.loads(desktop_target.read_text()),
            )
            backup_dir = store.backups_dir / str(receipt.backup_id)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            target_path = str(desktop_target)
            self.assertTrue(
                any(
                    entry.get("path") == target_path
                    for entry in manifest["entries"]
                )
            )
            self.assertTrue(
                any(
                    item.get("path") == target_path
                    for item in manifest["switch_journal"]["frozen_inputs"]
                )
            )
            effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect.get("phase") == "desktop_global_state_sync"
            )
            self.assertEqual(target_path, effect["path"])
            self.assertEqual("applied", effect["status"])
            for key in (
                "before_identity",
                "planned_after_state",
                "produced_identity",
                "route_guard",
                "staged_identity",
                "staged_path",
                "staged_route_guard",
                "staged_state",
            ):
                self.assertIn(key, effect)

    def test_legacy_noop_desktop_rollback_failed_marker_recovers_safely(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            _prepared_journal_sha256,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            desktop_source = (
                store.official_codex_home / ".codex-global-state.json"
            )
            desktop_target = (
                store.internal_codex_home / ".codex-global-state.json"
            )
            desktop_source.write_text(
                json.dumps({"appshotHotkey": "source"}, sort_keys=True) + "\n"
            )
            desktop_target.write_text(
                json.dumps(
                    {
                        "appOwned": "before",
                        "appshotHotkey": "target",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            shared_source = store.official_codex_home / "rules"
            shared_target = store.internal_codex_home / "rules"
            for shared_path in (shared_source, shared_target):
                shared_path.mkdir()
                (shared_path / "baseline.json").write_text(
                    '{"state":"same"}\n'
                )
            concurrent_payload = (
                json.dumps(
                    {
                        "appOwned": "after",
                        "appshotHotkey": "source",
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()

            def replace_desktop_target() -> None:
                replacement = desktop_target.with_name(
                    f".{desktop_target.name}.app-update"
                )
                replacement.write_bytes(concurrent_payload)
                os.replace(replacement, desktop_target)

            target_config = store.internal_codex_home / "config.toml"
            adapter = self.before_switch_effect_adapter(
                "config_write",
                replace_desktop_target,
                target=target_config,
            )
            failed = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )
            self.assertEqual("rollback_failed", failed.outcome)
            marker_path = next(
                store.root.glob(".pending-transaction-*.json")
            )
            backup_dir = store.backups_dir / str(failed.backup_id)
            manifest_path = backup_dir / "backup.json"
            manifest = json.loads(manifest_path.read_text())
            journal = manifest["switch_journal"]
            target_path = str(desktop_target)
            desktop_effect = next(
                effect
                for effect in journal["effects"]
                if effect.get("phase") == "desktop_global_state_sync"
            )
            journal["effects"] = journal["effects"][
                : int(desktop_effect["id"]) + 1
            ]
            desktop_effect["status"] = "intent"
            desktop_effect["planned_after_state"] = dict(
                desktop_effect["before_state"]
            )
            desktop_effect["observed_after_state"] = dict(
                desktop_effect["before_state"]
            )
            desktop_effect["produced_identity"] = dict(
                desktop_effect["before_identity"]
            )
            for key in (
                "action_observed_state",
                "staged_identity",
                "staged_path",
                "staged_route_guard",
                "staged_state",
            ):
                desktop_effect.pop(key, None)
            frozen_target = next(
                item
                for item in journal["frozen_inputs"]
                if item.get("path") == target_path
            )
            frozen_target["commit_state"] = dict(
                frozen_target["before_state"]
            )
            frozen_target["commit_replaces_identity"] = True
            manifest["failure"] = (
                "Required switch input changed after "
                "desktop_global_state_sync action: Desktop global-state "
                f"target: {desktop_target}"
            )
            manifest["rollback_failure"] = (
                "Switch rollback refuses ambiguous external drift: "
                f"{desktop_target}"
            )
            digest = _prepared_journal_sha256(journal)
            journal["prepared_journal_sha256"] = digest
            marker = json.loads(marker_path.read_text())
            marker["prepared_journal_sha256"] = digest
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            marker_path.write_text(
                json.dumps(marker, indent=2, sort_keys=True) + "\n"
            )
            shared_effect = next(
                effect
                for effect in journal["effects"]
                if effect.get("path") == str(shared_target)
            )
            self.assertEqual(
                shared_effect["before_state"],
                shared_effect["planned_after_state"],
            )
            self.assertEqual(
                shared_effect["before_state"],
                shared_effect["observed_after_state"],
            )
            later_shared_state = shared_target / "later.json"
            later_shared_state.write_text('{"owner":"app"}\n')

            retry = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": FilesystemAdapter(),
                    },
                ),
            )

            self.assertEqual("committed", retry.outcome)
            self.assertEqual(concurrent_payload, desktop_target.read_bytes())
            self.assertEqual(
                '{"owner":"app"}\n',
                later_shared_state.read_text(),
            )
            self.assertFalse(marker_path.exists())
            recovered = json.loads(manifest_path.read_text())
            self.assertEqual("rolled_back", recovered["lifecycle"])
            self.assertEqual("recovered", recovered["switch_journal"]["state"])
            self.assertEqual(
                "legacy Desktop global-state no-op ownership ignored",
                recovered["recovery_note"],
            )
            recovered_shared_effect = next(
                effect
                for effect in recovered["switch_journal"]["effects"]
                if effect.get("path") == str(shared_target)
            )
            self.assertEqual(
                "preserved App-owned state from legacy no-op",
                recovered_shared_effect["recovery_action"],
            )
            recovered_real_effect = next(
                effect
                for effect in recovered["switch_journal"]["effects"]
                if effect.get("phase") == "home_binding_write"
            )
            self.assertNotEqual(
                recovered_real_effect["before_state"],
                recovered_real_effect["planned_after_state"],
            )
            self.assertNotIn(
                "recovery_action",
                recovered_real_effect,
            )

    def test_legacy_noop_desktop_recovery_rejects_any_evidence_mismatch(
        self,
    ) -> None:
        from codex_switch_transaction import (
            _PendingTransactionEvidence,
            _PendingTransactionMarker,
            _legacy_noop_desktop_recovery_effect_ids,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.internal_codex_home / ".codex-global-state.json"
            target_path = str(target)

            def make_evidence() -> _PendingTransactionEvidence:
                before_state = {
                    "kind": "file",
                    "mode": 0o600,
                    "path": target_path,
                    "sha256": "a" * 64,
                    "size": 7,
                }
                before_identity = {
                    "device": 1,
                    "inode": 2,
                    "kind": "file",
                    "mode": 0o600,
                    "path": target_path,
                }
                effect = {
                    "id": 0,
                    "kind": "filesystem",
                    "phase": "desktop_global_state_sync",
                    "status": "intent",
                    "path": target_path,
                    "before_state": dict(before_state),
                    "planned_after_state": dict(before_state),
                    "observed_after_state": dict(before_state),
                    "before_identity": dict(before_identity),
                    "produced_identity": dict(before_identity),
                    "route_guard": {"schema_version": 1},
                    "recovery_state": "rollback_failed",
                }
                journal = {
                    "state": "rollback_failed",
                    "effects": [effect],
                    "frozen_inputs": [
                        {
                            "label": "Desktop global-state target",
                            "path": target_path,
                            "before_state": dict(before_state),
                            "before_identity": dict(before_identity),
                            "commit_state": dict(before_state),
                            "commit_replaces_identity": True,
                            "capture_kind": "path",
                        }
                    ],
                }
                manifest = {
                    "from_profile": "openai-official",
                    "to_profile": "internal",
                    "failure": (
                        "Required switch input changed after "
                        "desktop_global_state_sync action: Desktop global-state "
                        f"target: {target}"
                    ),
                    "rollback_failure": (
                        "Switch rollback refuses ambiguous external drift: "
                        f"{target}"
                    ),
                    "entries": [
                        {
                            "path": target_path,
                            "before_state": dict(before_state),
                        }
                    ],
                    "switch_journal": journal,
                }
                return _PendingTransactionEvidence(
                    marker=_PendingTransactionMarker(
                        path=store.root / ".pending-transaction-test.json",
                        payload={},
                    ),
                    backup_dir=store.backups_dir / "test",
                    manifest=manifest,
                    journal=journal,
                    operation="switch",
                    lifecycle="rollback_failed",
                )

            baseline = make_evidence()
            self.assertEqual(
                frozenset((0,)),
                _legacy_noop_desktop_recovery_effect_ids(
                    store,
                    baseline,
                ),
            )

            real_config = make_evidence()
            desktop_effect = real_config.journal["effects"][0]
            desktop_effect["id"] = 1
            config_path = str(store.internal_codex_home / "config.toml")
            config_before = {
                "kind": "file",
                "mode": 0o600,
                "path": config_path,
                "sha256": "c" * 64,
                "size": 8,
            }
            config_after = {
                **config_before,
                "sha256": "d" * 64,
                "size": 9,
            }
            real_config.journal["effects"].insert(
                0,
                {
                    "id": 0,
                    "kind": "filesystem",
                    "phase": "config_write",
                    "status": "applied",
                    "path": config_path,
                    "before_state": dict(config_before),
                    "planned_after_state": dict(config_after),
                    "observed_after_state": dict(config_after),
                    "before_identity": {
                        "device": 1,
                        "inode": 3,
                        "kind": "file",
                        "mode": 0o600,
                        "path": config_path,
                    },
                    "produced_identity": {
                        "device": 1,
                        "inode": 4,
                        "kind": "file",
                        "mode": 0o600,
                        "path": config_path,
                    },
                    "route_guard": {"schema_version": 1},
                    "recovery_state": "rollback_failed",
                },
            )
            real_config.manifest["entries"].insert(
                0,
                {
                    "path": config_path,
                    "before_state": dict(config_before),
                },
            )
            self.assertEqual(
                frozenset((1,)),
                _legacy_noop_desktop_recovery_effect_ids(
                    store,
                    real_config,
                ),
            )

            def add_stage(evidence: _PendingTransactionEvidence) -> None:
                evidence.journal["effects"][0]["staged_path"] = "/tmp/stage"

            def change_planned_state(
                evidence: _PendingTransactionEvidence,
            ) -> None:
                evidence.journal["effects"][0]["planned_after_state"][
                    "sha256"
                ] = "b" * 64

            def change_produced_identity(
                evidence: _PendingTransactionEvidence,
            ) -> None:
                evidence.journal["effects"][0]["produced_identity"][
                    "inode"
                ] = 3

            def append_later_effect(
                evidence: _PendingTransactionEvidence,
            ) -> None:
                evidence.journal["effects"].append(
                    {
                        "id": 1,
                        "kind": "filesystem",
                        "phase": "config_write",
                        "status": "intent",
                        "recovery_state": "rollback_failed",
                    }
                )

            def change_profile(evidence: _PendingTransactionEvidence) -> None:
                evidence.manifest["to_profile"] = "openai-official"

            def change_failure(evidence: _PendingTransactionEvidence) -> None:
                evidence.manifest["failure"] = "different failure"

            for name, mutate in {
                "staged": add_stage,
                "planned-state": change_planned_state,
                "produced-identity": change_produced_identity,
                "later-effect": append_later_effect,
                "profile": change_profile,
                "failure": change_failure,
            }.items():
                with self.subTest(case=name):
                    evidence = make_evidence()
                    mutate(evidence)
                    self.assertIsNone(
                        _legacy_noop_desktop_recovery_effect_ids(
                            store,
                            evidence,
                        )
                    )

    def test_switch_rejects_late_active_drift_before_active_action(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            external_active = (
                json.dumps(
                    {
                        "profile": "openai-official",
                        "codex_home": str(store.official_codex_home),
                        "external": "preserve",
                    }
                )
                + "\n"
            ).encode()
            original_inode = store.active_path.stat().st_ino
            adapter = self.before_switch_effect_adapter(
                "active_write",
                lambda: store.active_path.write_bytes(external_active),
                target=store.active_path,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(external_active, store.active_path.read_bytes())
            self.assertEqual(original_inode, store.active_path.stat().st_ino)

    def test_switch_rejects_late_shell_profile_drift_before_shell_action(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            shell_profile = root / "shell-profile"
            shell_profile.write_text("# original shell\n")
            external_shell = b"# external shell drift\n"
            original_inode = shell_profile.stat().st_ino
            adapter = self.before_switch_effect_adapter(
                "shell_bootstrap_write",
                lambda: shell_profile.write_bytes(external_shell),
                target=shell_profile,
            )

            with patch.dict(
                os.environ,
                {
                    "CODEX_SWITCH_SHELL_PROFILE": str(shell_profile),
                    "CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "0",
                },
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": adapter,
                        },
                    ),
                )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(external_shell, shell_profile.read_bytes())
            self.assertEqual(original_inode, shell_profile.stat().st_ino)

    def test_switch_rejects_late_plugin_snapshot_drift_before_snapshot_action(
        self,
    ) -> None:
        from codex_switch_home_sync import plugin_support_snapshot_name
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            snapshot = (
                store.internal_codex_home
                / plugin_support_snapshot_name("internal")
            )
            snapshot.write_text('[mcp_servers.before]\ncommand = "before"\n')
            external_snapshot = b'[mcp_servers.external]\ncommand = "preserve"\n'
            original_inode = snapshot.stat().st_ino
            adapter = self.before_switch_effect_adapter(
                "plugin_snapshot_write",
                lambda: snapshot.write_bytes(external_snapshot),
                target=snapshot,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(external_snapshot, snapshot.read_bytes())
            self.assertEqual(original_inode, snapshot.stat().st_ino)

    def test_switch_rejects_late_auth_source_drift_before_auth_action(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            auth_source = store.profile_dir("internal") / "auth.json"
            auth_target = store.internal_codex_home / "auth.json"
            external_auth = b'{"external":"preserve"}\n'
            target_before = auth_target.read_bytes()
            target_inode = auth_target.stat().st_ino
            adapter = self.before_switch_effect_adapter(
                "auth_write",
                lambda: auth_source.write_bytes(external_auth),
                target=auth_target,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(external_auth, auth_source.read_bytes())
            self.assertEqual(target_before, auth_target.read_bytes())
            self.assertEqual(target_inode, auth_target.stat().st_ino)
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            auth_effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect["phase"] == "auth_write"
            )
            self.assertEqual("intent", auth_effect["status"])
            self.assertNotIn("produced_identity", auth_effect)

    def test_shared_directory_validation_work_is_effect_bounded(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class DirectoryAttestationBudgetAdapter(FilesystemAdapter):
            def __init__(self, source: Path) -> None:
                self.source = source
                self.deep_state_captures = 0

            def capture_state(self, path: Path) -> dict[str, object]:
                if path == self.source:
                    self.deep_state_captures += 1
                return super().capture_state(path)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            shared_source = store.official_codex_home / "skills"
            shared_source.mkdir()
            for index in range(32):
                skill = shared_source / f"skill-{index:02d}"
                skill.mkdir()
                (skill / "SKILL.md").write_text(f"# Skill {index}\n")
            adapter = DirectoryAttestationBudgetAdapter(shared_source)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual(
                "committed",
                receipt.outcome,
                "\n".join(receipt.preview_lines),
            )
            self.assertLessEqual(
                adapter.deep_state_captures,
                8,
                "recursive source attestation must be bounded independently "
                "of unrelated journal effects",
            )

    def test_shared_directory_final_cas_detects_late_drift(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class DriftAtFinalizeIntent(FilesystemAdapter):
            def __init__(self, source_file: Path) -> None:
                self.source_file = source_file
                self.injected = False

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                journal = data.get("switch_journal")
                effects = journal.get("effects") if isinstance(journal, dict) else None
                if (
                    not self.injected
                    and phase == "switch_journal_intent"
                    and isinstance(effects, list)
                    and effects
                    and isinstance(effects[-1], dict)
                    and effects[-1].get("phase") == "backup_finalize"
                ):
                    self.source_file.write_text("external-final-drift\n")
                    self.injected = True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            shared_source = store.official_codex_home / "skills"
            shared_source.mkdir()
            source_file = shared_source / "SKILL.md"
            source_file.write_text("planned\n")
            adapter = DriftAtFinalizeIntent(source_file)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(adapter.injected)
            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual("external-final-drift\n", source_file.read_text())
            self.assertFalse((store.internal_codex_home / "skills").exists())
            self.assertIn("at commit", "\n".join(receipt.preview_lines))

    def test_switch_rejects_late_shared_source_drift_before_shared_action(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            shared_source = store.official_codex_home / "rules"
            shared_source.mkdir()
            source_file = shared_source / "tool.json"
            source_file.write_text('{"source":"planned"}\n')
            external_source = b'{"source":"external-preserved"}\n'
            shared_target = store.internal_codex_home / shared_source.name
            adapter = self.before_switch_effect_adapter(
                "shared_support_sync",
                lambda: source_file.write_bytes(external_source),
                target=shared_target,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(external_source, source_file.read_bytes())
            self.assertFalse(shared_target.exists())
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            shared_effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect["phase"] == "shared_support_sync"
            )
            self.assertEqual("intent", shared_effect["status"])
            self.assertNotIn("produced_identity", shared_effect)

    def test_switch_rejects_late_composite_config_source_drift_before_config_action(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            composite_source = (
                store.official_codex_home / "internal.config.toml"
            )
            composite_source.write_text(
                '[mcp_servers.composite]\ncommand = "planned"\n'
            )
            target = store.internal_codex_home / "config.toml"
            external_source = (
                b'[mcp_servers.composite]\ncommand = "external-preserved"\n'
            )
            target_before = target.read_bytes()
            target_inode = target.stat().st_ino
            adapter = self.before_switch_effect_adapter(
                "config_write",
                lambda: composite_source.write_bytes(external_source),
                target=target,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(getattr(adapter, "injected"))
            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(external_source, composite_source.read_bytes())
            self.assertEqual(target_before, target.read_bytes())
            self.assertEqual(target_inode, target.stat().st_ino)
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            config_effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect["phase"] == "config_write"
            )
            self.assertEqual("intent", config_effect["status"])
            self.assertNotIn("produced_identity", config_effect)

    def test_switch_every_deterministic_filesystem_family_has_planned_after_state(
        self,
    ) -> None:
        import ast
        import inspect

        import codex_switch_transaction as transaction

        tree = ast.parse(inspect.getsource(transaction._execute_switch))
        planned_by_phase: dict[str, bool] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func,
                ast.Attribute,
            ):
                continue
            if node.func.attr != "apply_path":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            phase_node = keywords.get("phase")
            if not isinstance(phase_node, ast.Constant) or not isinstance(
                phase_node.value,
                str,
            ):
                continue
            planned_by_phase[phase_node.value] = (
                "planned_after_state" in keywords
            )

        expected_families = {
            "home_binding_write",
            "target_home_ensure",
            "shared_support_sync",
            "desktop_global_state_sync",
            "stale_runtime_link_remove",
            "config_write",
            "canonical_profile_write",
            "profile_config_write",
            "plugin_snapshot_write",
            "auth_write",
            "auth_remove",
            "shim_write",
            "shell_bootstrap_write",
            "app_capability_receipt_write",
            "app_wrapper_write",
            "plist_write",
            "active_write",
        }
        self.assertEqual(expected_families, set(planned_by_phase))
        self.assertEqual(
            set(),
            {
                phase
                for phase, has_planned_after in planned_by_phase.items()
                if not has_planned_after
            },
        )

    def test_removed_transaction_helpers_have_no_callers_and_compatibility_callers_remain(
        self,
    ) -> None:
        import ast

        scripts_dir = Path(__file__).parent
        removed_by_module = {
            "codex_switch_switching.py": {
                "read_active_profile_home",
                "read_active_profile",
                "official_auth_restore_path",
                "independent_switch_paths",
                "print_independent_dry_run",
                "switch_independent_profile",
            },
            "codex_switch_home_select.py": {"write_home_binding_updates"},
            "codex_switch_restore.py": {
                "remove_existing",
                "restore_entry",
                "copy_path_to_backup",
                "file_sha256",
                "path_exists",
            },
        }
        retained_by_module = {
            "codex_switch_backup.py": {"backup_live_files"},
            "codex_switch_restore.py": {
                "path_state",
                "create_switch_backup",
                "finalize_backup",
                "restore_backup",
            },
            "codex_switch_switching.py": {"read_active_record"},
        }

        for filename, removed in removed_by_module.items():
            tree = ast.parse((scripts_dir / filename).read_text())
            definitions = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.assertEqual(set(), removed & definitions)
        for filename, retained in retained_by_module.items():
            tree = ast.parse((scripts_dir / filename).read_text())
            definitions = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.assertEqual(retained, retained & definitions)

        removed_names = set().union(*removed_by_module.values())
        callers: list[tuple[str, str]] = []
        for path in scripts_dir.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name):
                    called = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    called = node.func.attr
                else:
                    continue
                if called in removed_names:
                    callers.append((path.name, called))
        self.assertEqual([], callers)

    def test_internal_snapshot_targets_internal_home_only(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            internal_bin = root / "codex-internal"
            official_bin = root / "codex-official"
            for executable in (internal_bin, official_bin):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            for profile, executable in (
                (internal_profile, internal_bin),
                (official_profile, official_bin),
            ):
                (profile / "manifest.json").write_text(
                    json.dumps(
                        {
                            "name": profile.name,
                            "codex_bin": str(executable),
                            "app_cli_path": str(executable),
                        }
                    )
                    + "\n"
                )
            (internal_profile / "config.toml").write_text(
                'model = "internal-after"\n'
            )
            (official_profile / "config.toml").write_text(
                'model = "official-profile"\n'
            )
            official_config = store.official_codex_home / "config.toml"
            official_config.write_text(
                'model = "official-runtime"\n[features]\nmemory = true\n'
            )
            internal_config = store.internal_codex_home / "config.toml"
            internal_config.write_text('model = "internal-before"\n')
            official_before = official_config.read_bytes()

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual(official_before, official_config.read_bytes())
            self.assertIn(
                'model = "internal-after"',
                (store.internal_codex_home / "internal.config.toml").read_text(),
            )
            active = json.loads(store.active_path.read_text())
            self.assertEqual("internal", active["profile"])
            self.assertEqual(str(store.internal_codex_home), active["codex_home"])
            preview = "\n".join(receipt.preview_lines)
            self.assertIn(f"target home: {store.internal_codex_home}", preview)
            self.assertNotIn(
                f"backup live files from {store.official_codex_home}",
                preview,
            )

    def test_snapshot_never_copies_official_auth_to_internal(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            internal_bin = root / "codex-internal"
            official_bin = root / "codex-official"
            for executable in (internal_bin, official_bin):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            for profile, executable in (
                (internal_profile, internal_bin),
                (official_profile, official_bin),
            ):
                (profile / "manifest.json").write_text(
                    json.dumps(
                        {
                            "name": profile.name,
                            "codex_bin": str(executable),
                            "app_cli_path": str(executable),
                        }
                    )
                    + "\n"
                )
            (internal_profile / "config.toml").write_text(
                'model = "internal-after"\n'
                'cli_auth_credentials_store = "file"\n'
            )
            (internal_profile / "auth.json").write_text(
                '{"internal":"profile"}\n'
            )
            (official_profile / "config.toml").write_text(
                'model = "official-profile"\n'
            )
            (store.official_codex_home / "config.toml").write_text(
                'model = "official-runtime"\n'
            )
            official_auth = store.official_codex_home / "auth.json"
            internal_auth = store.internal_codex_home / "auth.json"
            official_auth.write_text('{"official":"isolated"}\n')
            internal_auth.write_text('{"internal":"before"}\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('{"official":"isolated"}\n', official_auth.read_text())
            self.assertEqual('{"internal":"profile"}\n', internal_auth.read_text())
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertNotIn(
                str(official_auth),
                {entry["path"] for entry in manifest["entries"]},
            )

    def test_missing_binding_fails_dry_run_before_backup(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        invalid_kinds = (
            "missing",
            "relative",
            "nonexistent",
            "directory",
            "non-executable",
        )
        for field_name in ("codex_bin", "app_cli_path"):
            for invalid_kind in invalid_kinds:
                with self.subTest(field=field_name, invalid_kind=invalid_kind):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        root = Path(temp_dir)
                        store = self.make_store(root)
                        store.ensure()
                        valid_executable = root / "valid-codex"
                        valid_executable.write_text("#!/bin/sh\nexit 0\n")
                        valid_executable.chmod(0o755)
                        if invalid_kind == "missing":
                            invalid_value = ""
                        elif invalid_kind == "relative":
                            invalid_value = "relative-codex"
                        elif invalid_kind == "nonexistent":
                            invalid_value = str(root / "does-not-exist")
                        elif invalid_kind == "directory":
                            invalid_path = root / "binding-directory"
                            invalid_path.mkdir()
                            invalid_value = str(invalid_path)
                        else:
                            invalid_path = root / "non-executable-codex"
                            invalid_path.write_text("#!/bin/sh\nexit 0\n")
                            invalid_path.chmod(0o600)
                            invalid_value = str(invalid_path)
                        internal_profile = store.profile_dir("internal")
                        official_profile = store.profile_dir("openai-official")
                        internal_profile.mkdir()
                        official_profile.mkdir()
                        internal_manifest = {
                            "name": "internal",
                            "codex_bin": str(valid_executable),
                            "app_cli_path": str(valid_executable),
                        }
                        internal_manifest[field_name] = invalid_value
                        (internal_profile / "manifest.json").write_text(
                            json.dumps(internal_manifest) + "\n"
                        )
                        (official_profile / "manifest.json").write_text(
                            json.dumps(
                                {
                                    "name": "openai-official",
                                    "codex_bin": str(valid_executable),
                                    "app_cli_path": str(valid_executable),
                                }
                            )
                            + "\n"
                        )
                        (internal_profile / "config.toml").write_text(
                            'model = "internal"\n'
                        )
                        (official_profile / "config.toml").write_text(
                            'model = "official"\n'
                        )
                        (store.official_codex_home / "config.toml").write_text(
                            'model = "official-runtime"\n'
                        )
                        (store.internal_codex_home / "config.toml").write_text(
                            'model = "internal-before"\n'
                        )
                        before_store = {
                            str(path.relative_to(store.root)): (
                                "directory" if path.is_dir() else path.read_bytes()
                            )
                            for path in store.root.rglob("*")
                        }
                        before_homes = {
                            "official": (
                                store.official_codex_home / "config.toml"
                            ).read_bytes(),
                            "internal": (
                                store.internal_codex_home / "config.toml"
                            ).read_bytes(),
                        }

                        with self.assertRaisesRegex(SwitchError, field_name):
                            execute_transaction(
                                store,
                                TransactionRequest(
                                    operation="switch",
                                    profile="internal",
                                    options={
                                        "config_mode": "snapshot",
                                        "shared_config_base": None,
                                        "clear_missing_auth": False,
                                        "skip_shim": True,
                                        "skip_app_cli": True,
                                        "skip_launchctl": True,
                                    },
                                ),
                                dry_run=True,
                            )

                        self.assertEqual(
                            before_store,
                            {
                                str(path.relative_to(store.root)): (
                                    "directory" if path.is_dir() else path.read_bytes()
                                )
                                for path in store.root.rglob("*")
                            },
                        )
                        self.assertEqual(
                            before_homes["official"],
                            (store.official_codex_home / "config.toml").read_bytes(),
                        )
                        self.assertEqual(
                            before_homes["internal"],
                            (store.internal_codex_home / "config.toml").read_bytes(),
                        )
                        self.assertFalse(any(store.backups_dir.iterdir()))

    def test_switch_rechecks_unchanged_required_bindings_at_commit(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class BindingAuditAdapter(FilesystemAdapter):
            def __init__(self, bindings: tuple[Path, ...]) -> None:
                self.binding_checks = {binding: 0 for binding in bindings}

            def capture_state(self, path: Path) -> dict[str, object]:
                if path in self.binding_checks:
                    self.binding_checks[path] += 1
                return super().capture_state(path)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, codex_bin, _, _ = self.arrange_switch_effect_fixture(root)
            app_cli_path = root / "codex-app-cli"
            app_cli_path.write_text("#!/bin/sh\nexit 0\n")
            app_cli_path.chmod(0o755)
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(app_cli_path)
            manifest_path.write_text(json.dumps(manifest) + "\n")
            filesystem = BindingAuditAdapter((codex_bin, app_cli_path))

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": filesystem,
                        },
                    ),
                )

            self.assertEqual("committed", receipt.outcome)
            self.assertGreaterEqual(filesystem.binding_checks[codex_bin], 3)
            self.assertGreaterEqual(filesystem.binding_checks[app_cli_path], 3)

    def test_codex_bin_deletion_at_last_preterminal_intent_rolls_back(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class DeleteCodexAtLastPreterminalIntentAdapter(FilesystemAdapter):
            def __init__(self, codex_bin: Path) -> None:
                self.codex_bin = codex_bin

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                journal = data.get("switch_journal")
                effects = journal.get("effects") if isinstance(journal, dict) else None
                is_last_preterminal_intent = (
                    phase == "switch_journal_intent"
                    and isinstance(effects, list)
                    and bool(effects)
                    and isinstance(effects[-1], dict)
                    and effects[-1].get("phase") == "backup_finalize"
                )
                if is_last_preterminal_intent:
                    self.codex_bin.unlink()
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, codex_bin, _, _ = self.arrange_switch_effect_fixture(root)
            before_active = store.active_path.read_bytes()
            filesystem = DeleteCodexAtLastPreterminalIntentAdapter(codex_bin)

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": filesystem,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertFalse(codex_bin.exists())
            self.assertEqual(before_active, store.active_path.read_bytes())
            backup = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", backup["lifecycle"])
            self.assertIn("codex_bin", backup["failure"])

    def test_app_cli_drift_at_last_preterminal_intent_rolls_back(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class DriftAppCliAtLastPreterminalIntentAdapter(FilesystemAdapter):
            def __init__(self, app_cli_path: Path) -> None:
                self.app_cli_path = app_cli_path

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                journal = data.get("switch_journal")
                effects = journal.get("effects") if isinstance(journal, dict) else None
                is_last_preterminal_intent = (
                    phase == "switch_journal_intent"
                    and isinstance(effects, list)
                    and bool(effects)
                    and isinstance(effects[-1], dict)
                    and effects[-1].get("phase") == "backup_finalize"
                )
                if is_last_preterminal_intent:
                    self.app_cli_path.write_text("#!/bin/sh\nexit 42\n")
                    self.app_cli_path.chmod(0o755)
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            app_cli_path = root / "codex-app-cli"
            app_cli_path.write_text("#!/bin/sh\nexit 0\n")
            app_cli_path.chmod(0o755)
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(app_cli_path)
            manifest_path.write_text(json.dumps(manifest) + "\n")
            before_active = store.active_path.read_bytes()
            filesystem = DriftAppCliAtLastPreterminalIntentAdapter(app_cli_path)

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": filesystem,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual("#!/bin/sh\nexit 42\n", app_cli_path.read_text())
            self.assertEqual(before_active, store.active_path.read_bytes())
            backup = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", backup["lifecycle"])
            self.assertIn("app_cli_path", backup["failure"])

    def test_split_selected_manifest_drift_preserves_state_per_journal_contract(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class DriftSelectedManifestAtLastIntent(FilesystemAdapter):
            def __init__(self, manifest_path: Path, payload: bytes) -> None:
                self.manifest_path = manifest_path
                self.payload = payload

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                journal = data.get("switch_journal")
                effects = journal.get("effects") if isinstance(journal, dict) else None
                is_last_preterminal_intent = (
                    phase == "switch_journal_intent"
                    and isinstance(effects, list)
                    and bool(effects)
                    and isinstance(effects[-1], dict)
                    and effects[-1].get("phase") == "backup_finalize"
                )
                if is_last_preterminal_intent:
                    self.manifest_path.write_bytes(self.payload)
                    self.manifest_path.chmod(0o600)
                super().write_manifest(path, data, phase=phase)

        for drift_profile in ("internal", "openai-official"):
            with self.subTest(drift_profile=drift_profile):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store, _, _, observed_paths = self.arrange_switch_effect_fixture(root)
                    drift_path = store.manifest_path(drift_profile)
                    external_payload = (
                        json.dumps(
                            {
                                "name": drift_profile,
                                "external_drift": True,
                            },
                            sort_keys=True,
                        ).encode()
                        + b"\n"
                    )
                    protected_paths = tuple(
                        path for path in observed_paths if path != drift_path
                    )
                    before_states = {
                        path: capture_path_state(path) for path in protected_paths
                    }

                    with patch.dict(
                        os.environ,
                        {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                        clear=False,
                    ):
                        receipt = execute_transaction(
                            store,
                            TransactionRequest(
                                operation="switch",
                                profile="internal",
                                options={
                                    "app_profile": "openai-official",
                                    "config_mode": "snapshot",
                                    "shared_config_base": None,
                                    "clear_missing_auth": False,
                                    "skip_shim": False,
                                    "skip_app_cli": False,
                                    "skip_launchctl": True,
                                    "filesystem_adapter": (
                                        DriftSelectedManifestAtLastIntent(
                                            drift_path,
                                            external_payload,
                                        )
                                    ),
                                },
                            ),
                        )

                    expected_outcome = (
                        "rollback_failed"
                        if drift_profile == "internal"
                        else "rolled_back"
                    )
                    self.assertEqual(
                        expected_outcome,
                        receipt.outcome,
                        "\n".join(receipt.preview_lines),
                    )
                    self.assertEqual(external_payload, drift_path.read_bytes())
                    self.assertEqual(
                        before_states,
                        {
                            path: capture_path_state(path)
                            for path in protected_paths
                        },
                    )
                    pending_markers = tuple(
                        store.root.glob(".pending-transaction-*.json")
                    )
                    self.assertEqual(
                        1 if expected_outcome == "rollback_failed" else 0,
                        len(pending_markers),
                    )

    def test_snapshot_auth_source_drift_uses_frozen_payload_and_rolls_back(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class DriftAuthAfterConfigAdapter(FilesystemAdapter):
            def __init__(self, source_auth: Path) -> None:
                self.source_auth = source_auth
                self.auth_payloads: list[bytes] = []

            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "config_write":
                    self.source_auth.write_text('{"external":"drift"}\n')
                elif phase == "auth_write":
                    self.auth_payloads.append(data)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            source_auth = store.profile_dir("internal") / "auth.json"
            target_auth = store.internal_codex_home / "auth.json"
            before_target_auth = target_auth.read_bytes()
            before_target_inode = target_auth.stat().st_ino
            before_active = store.active_path.read_bytes()
            filesystem = DriftAuthAfterConfigAdapter(source_auth)

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": filesystem,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(
                [],
                filesystem.auth_payloads,
            )
            self.assertEqual('{"external":"drift"}\n', source_auth.read_text())
            self.assertEqual(before_target_auth, target_auth.read_bytes())
            self.assertEqual(before_target_inode, target_auth.stat().st_ino)
            self.assertEqual(before_active, store.active_path.read_bytes())
            backup = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", backup["lifecycle"])
            self.assertIn("profile auth", backup["failure"])

    def test_rollback_preserves_untouched_external_auth_change(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class FailAfterConfigWithExternalAuthAdapter(FilesystemAdapter):
            def __init__(self, target_auth: Path) -> None:
                self.target_auth = target_auth

            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "config_write":
                    self.target_auth.write_text('{"external":"keep"}\n')
                    raise OSError("injected failure before auth mutation")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            target_config = store.internal_codex_home / "config.toml"
            target_auth = store.internal_codex_home / "auth.json"
            before_config = capture_path_state(target_config)
            before_active = store.active_path.read_bytes()
            filesystem = FailAfterConfigWithExternalAuthAdapter(target_auth)

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": filesystem,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(before_config, capture_path_state(target_config))
            self.assertEqual('{"external":"keep"}\n', target_auth.read_text())
            self.assertEqual(before_active, store.active_path.read_bytes())

    def test_unreadable_backup_manifest_during_failure_uses_in_memory_rollback(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class CorruptBackupDuringFailureAdapter(FilesystemAdapter):
            def __init__(self, store: Store) -> None:
                self.store = store
                self.corrupted = False

            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "config_write":
                    backup_dirs = tuple(self.store.backups_dir.iterdir())
                    self.assert_single_backup(backup_dirs)
                    (backup_dirs[0] / "backup.json").write_text("not-json\n")
                    self.corrupted = True
                    raise OSError("injected failure with corrupt backup manifest")

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if (
                    self.corrupted
                    and path.name == "backup.json"
                    and phase.startswith("switch_")
                ):
                    raise OSError("backup.json remains unreadable")
                super().write_manifest(path, data, phase=phase)

            @staticmethod
            def assert_single_backup(backups: tuple[Path, ...]) -> None:
                if len(backups) != 1:
                    raise AssertionError(f"expected one backup, found {len(backups)}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            target_config = store.internal_codex_home / "config.toml"
            before_config = capture_path_state(target_config)
            before_active = store.active_path.read_bytes()
            filesystem = CorruptBackupDuringFailureAdapter(store)

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": filesystem,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(before_config, capture_path_state(target_config))
            self.assertEqual(before_active, store.active_path.read_bytes())
            backup_dir = store.backups_dir / str(receipt.backup_id)
            self.assertEqual("not-json\n", (backup_dir / "backup.json").read_text())
            failure = json.loads((backup_dir / "failure.json").read_text())
            self.assertEqual(1, failure["schema_version"])
            self.assertEqual("rolled_back", failure["lifecycle"])
            self.assertEqual(receipt.backup_id, failure["id"])
            self.assertTrue(failure["entries"])
            self.assertTrue(failure["switch_journal"]["effects"])
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            before_retry = capture_path_state(store.root)
            retry_request = TransactionRequest(
                operation="switch",
                profile="internal",
                options={
                    "config_mode": "snapshot",
                    "shared_config_base": None,
                    "clear_missing_auth": False,
                    "skip_shim": True,
                    "skip_app_cli": True,
                    "skip_launchctl": True,
                    "filesystem_adapter": FilesystemAdapter(),
                },
            )

            dry_run = execute_transaction(store, retry_request, dry_run=True)
            self.assertEqual("dry_run", dry_run.outcome)
            self.assertEqual(before_retry, capture_path_state(store.root))
            self.assertTrue(marker_path.exists())

            retry = execute_transaction(store, retry_request)
            self.assertEqual("committed", retry.outcome)
            self.assertFalse(marker_path.exists())

    def test_unreadable_backup_manifest_rejects_unbound_failure_records_without_writes(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            SwitchError,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        for variant in ("mismatched_transaction", "rollback_failed"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store, marker_path, backup_dir, options = self.arrange_pending_switch(
                    root
                )
                marker = json.loads(marker_path.read_text())
                manifest_path = backup_dir / "backup.json"
                manifest = json.loads(manifest_path.read_text())
                journal = dict(manifest["switch_journal"])
                journal["state"] = "recovered"
                failure: dict[str, object] = {
                    "schema_version": 1,
                    "record_kind": "switch_terminal_failure_receipt",
                    "backup_schema_version": 2,
                    "lifecycle": "rolled_back",
                    "id": backup_dir.name,
                    "backup_id": backup_dir.name,
                    "operation": "switch",
                    "failed_at": "2026-07-23T00:00:00Z",
                    "error": "synthetic terminal evidence",
                    "entries": manifest["entries"],
                    "switch_journal": journal,
                    "transaction_id": marker["transaction_id"],
                    "marker_name": marker["marker_name"],
                    "prepared_journal_sha256": marker[
                        "prepared_journal_sha256"
                    ],
                    "recovery_marker_required": True,
                    "rollback_verified": True,
                }
                if variant == "mismatched_transaction":
                    failure["transaction_id"] = "unbound-transaction"
                else:
                    failure["lifecycle"] = "rollback_failed"
                    failure["rollback_verified"] = False
                    journal["state"] = "rollback_failed"
                (backup_dir / "failure.json").write_text(
                    json.dumps(failure, indent=2, sort_keys=True) + "\n"
                )
                manifest_path.write_text("not-json\n")
                before = capture_path_state(store.root)
                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()

                with self.assertRaisesRegex(
                    SwitchError,
                    "bound failure record",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=retry_options,
                        ),
                    )

                self.assertEqual(before, capture_path_state(store.root))
                self.assertTrue(marker_path.exists())

    def test_hard_interruption_after_filesystem_mutation_recovers_before_fresh_transaction(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterConfigAdapter(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "config_write":
                    raise HardInterruption("hard interruption after config write")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            target_config = store.internal_codex_home / "config.toml"
            original_config_state = capture_path_state(target_config)
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": True,
                "skip_app_cli": True,
                "skip_launchctl": True,
                "filesystem_adapter": InterruptAfterConfigAdapter(),
            }

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    HardInterruption,
                    "hard interruption after config write",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                interrupted_backups = tuple(store.backups_dir.iterdir())
                self.assertEqual(1, len(interrupted_backups))
                interrupted_backup = interrupted_backups[0]
                interrupted_manifest = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                self.assertEqual("prepared", interrupted_manifest["lifecycle"])
                config_effects = [
                    effect
                    for effect in interrupted_manifest["switch_journal"]["effects"]
                    if effect.get("phase") == "config_write"
                ]
                self.assertEqual(1, len(config_effects))
                self.assertEqual("intent", config_effects[0]["status"])

                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()
                retry_receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual("committed", retry_receipt.outcome)
            recovered_manifest = json.loads(
                (interrupted_backup / "backup.json").read_text()
            )
            self.assertEqual("rolled_back", recovered_manifest["lifecycle"])
            self.assertEqual("recovered", recovered_manifest["switch_journal"]["state"])
            retry_backup = store.backups_dir / str(retry_receipt.backup_id)
            retry_manifest = json.loads((retry_backup / "backup.json").read_text())
            retry_config_entry = next(
                entry
                for entry in retry_manifest["entries"]
                if entry["path"] == str(target_config)
            )
            self.assertEqual(original_config_state, retry_config_entry["before_state"])

    def test_dry_run_reports_pending_switch_recovery_without_writes(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterConfigAdapter(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "config_write":
                    raise HardInterruption("leave prepared switch")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": True,
                "skip_app_cli": True,
                "skip_launchctl": True,
                "filesystem_adapter": InterruptAfterConfigAdapter(),
            }

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                with self.assertRaises(HardInterruption):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                pending_backup = next(store.backups_dir.iterdir())
                before_store = capture_path_state(store.root)
                before_official = capture_path_state(store.official_codex_home)
                before_internal = capture_path_state(store.internal_codex_home)
                before_manifest = (pending_backup / "backup.json").read_bytes()
                dry_options = dict(options)
                dry_options["filesystem_adapter"] = FilesystemAdapter()
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=dry_options,
                    ),
                    dry_run=True,
                )

            self.assertEqual("dry_run", receipt.outcome)
            self.assertEqual(pending_backup.name, receipt.backup_id)
            self.assertIn(
                "pending switch recovery required",
                "\n".join(receipt.preview_lines),
            )
            self.assertEqual(before_store, capture_path_state(store.root))
            self.assertEqual(before_official, capture_path_state(store.official_codex_home))
            self.assertEqual(before_internal, capture_path_state(store.internal_codex_home))
            self.assertEqual(before_manifest, (pending_backup / "backup.json").read_bytes())

    def test_hard_interruption_after_desktop_effect_recovers_with_fresh_adapter(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptingSetenvRunner(_FakeLaunchctlRunner):
            def __call__(
                self,
                command: list[str],
                env: dict[str, str] | None = None,
            ) -> tuple[int, str]:
                result = super().__call__(command, env)
                if command[1] == "setenv" and self.occurrences["setenv"] == 1:
                    raise HardInterruption("hard interruption after Desktop setenv")
                return result

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            first_runner = InterruptingSetenvRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            first_desktop = _DesktopBindingAdapter(
                store,
                runner=first_runner,
                uid_provider=lambda: 501,
            )
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": False,
                "skip_app_cli": False,
                "skip_launchctl": False,
                "filesystem_adapter": FilesystemAdapter(),
                "desktop_binding_adapter": first_desktop,
            }

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    HardInterruption,
                    "hard interruption after Desktop setenv",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                interrupted_backup = next(store.backups_dir.iterdir())
                interrupted_manifest = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                desktop_effects = [
                    effect
                    for effect in interrupted_manifest["switch_journal"]["effects"]
                    if effect.get("kind") == "desktop"
                ]
                self.assertEqual(1, len(desktop_effects))
                self.assertEqual("desktop_setenv", desktop_effects[0]["phase"])
                self.assertEqual("intent", desktop_effects[0]["status"])
                self.assertEqual(str(target_executable), first_runner.gui_env)

                retry_runner = _FakeLaunchctlRunner(
                    gui_env=first_runner.gui_env,
                    service_loaded=first_runner.service_loaded,
                )
                retry_desktop = _DesktopBindingAdapter(
                    store,
                    runner=retry_runner,
                    uid_provider=lambda: 501,
                )
                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()
                retry_options["desktop_binding_adapter"] = retry_desktop
                retry_receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual("committed", retry_receipt.outcome)
            recovered_manifest = json.loads(
                (interrupted_backup / "backup.json").read_text()
            )
            self.assertEqual("rolled_back", recovered_manifest["lifecycle"])
            retry_manifest = json.loads(
                (
                    store.backups_dir
                    / str(retry_receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual(
                {
                    "gui_env": str(prior_executable),
                    "service_loaded": True,
                },
                retry_manifest["switch_journal"]["desktop_before"],
            )
            self.assertEqual(str(target_executable), retry_runner.gui_env)
            self.assertTrue(retry_runner.service_loaded)

    def test_hard_interruption_after_active_write_recovers_before_fresh_transaction(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterActiveAdapter(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise HardInterruption("hard interruption after active write")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            original_active_state = capture_path_state(store.active_path)
            first_runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            first_desktop = _DesktopBindingAdapter(
                store,
                runner=first_runner,
                uid_provider=lambda: 501,
            )
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": False,
                "skip_app_cli": False,
                "skip_launchctl": False,
                "filesystem_adapter": InterruptAfterActiveAdapter(),
                "desktop_binding_adapter": first_desktop,
            }

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    HardInterruption,
                    "hard interruption after active write",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                interrupted_backup = next(store.backups_dir.iterdir())
                interrupted_manifest = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                active_effect = next(
                    effect
                    for effect in interrupted_manifest["switch_journal"]["effects"]
                    if effect.get("phase") == "active_write"
                )
                self.assertEqual("intent", active_effect["status"])

                retry_runner = _FakeLaunchctlRunner(
                    gui_env=first_runner.gui_env,
                    service_loaded=first_runner.service_loaded,
                )
                retry_desktop = _DesktopBindingAdapter(
                    store,
                    runner=retry_runner,
                    uid_provider=lambda: 501,
                )
                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()
                retry_options["desktop_binding_adapter"] = retry_desktop
                retry_receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual("committed", retry_receipt.outcome)
            recovered_manifest = json.loads(
                (interrupted_backup / "backup.json").read_text()
            )
            self.assertEqual("rolled_back", recovered_manifest["lifecycle"])
            retry_manifest = json.loads(
                (
                    store.backups_dir
                    / str(retry_receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            retry_active_entry = next(
                entry
                for entry in retry_manifest["entries"]
                if entry["path"] == str(store.active_path)
            )
            self.assertEqual(original_active_state, retry_active_entry["before_state"])
            self.assertEqual(
                {
                    "gui_env": str(prior_executable),
                    "service_loaded": True,
                },
                retry_manifest["switch_journal"]["desktop_before"],
            )
            self.assertEqual(str(target_executable), retry_runner.gui_env)
            self.assertTrue(retry_runner.service_loaded)

    def test_hard_interruption_at_backup_finalize_recovers_before_fresh_transaction(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptBeforeFinalizeAdapter(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "backup_finalize":
                    raise HardInterruption("hard interruption at backup finalize")
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            original_active_state = capture_path_state(store.active_path)
            first_runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            first_desktop = _DesktopBindingAdapter(
                store,
                runner=first_runner,
                uid_provider=lambda: 501,
            )
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": False,
                "skip_app_cli": False,
                "skip_launchctl": False,
                "filesystem_adapter": InterruptBeforeFinalizeAdapter(),
                "desktop_binding_adapter": first_desktop,
            }

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    HardInterruption,
                    "hard interruption at backup finalize",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                interrupted_backup = next(store.backups_dir.iterdir())
                interrupted_manifest = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                finalize_effect = next(
                    effect
                    for effect in interrupted_manifest["switch_journal"]["effects"]
                    if effect.get("phase") == "backup_finalize"
                )
                self.assertEqual("finalize", finalize_effect["kind"])
                self.assertEqual("intent", finalize_effect["status"])

                retry_runner = _FakeLaunchctlRunner(
                    gui_env=first_runner.gui_env,
                    service_loaded=first_runner.service_loaded,
                )
                retry_desktop = _DesktopBindingAdapter(
                    store,
                    runner=retry_runner,
                    uid_provider=lambda: 501,
                )
                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()
                retry_options["desktop_binding_adapter"] = retry_desktop
                retry_receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual("committed", retry_receipt.outcome)
            recovered_manifest = json.loads(
                (interrupted_backup / "backup.json").read_text()
            )
            self.assertEqual("rolled_back", recovered_manifest["lifecycle"])
            retry_manifest = json.loads(
                (
                    store.backups_dir
                    / str(retry_receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            retry_active_entry = next(
                entry
                for entry in retry_manifest["entries"]
                if entry["path"] == str(store.active_path)
            )
            self.assertEqual(original_active_state, retry_active_entry["before_state"])
            self.assertEqual(
                {
                    "gui_env": str(prior_executable),
                    "service_loaded": True,
                },
                retry_manifest["switch_journal"]["desktop_before"],
            )
            self.assertEqual(str(target_executable), retry_runner.gui_env)
            self.assertTrue(retry_runner.service_loaded)

    def test_hard_interruption_after_atomic_backup_finalize_stays_committed(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterFinalizeAdapter(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "backup_finalize":
                    raise HardInterruption(
                        "hard interruption after atomic backup finalize"
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            first_runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            first_desktop = _DesktopBindingAdapter(
                store,
                runner=first_runner,
                uid_provider=lambda: 501,
            )
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": False,
                "skip_app_cli": False,
                "skip_launchctl": False,
                "filesystem_adapter": InterruptAfterFinalizeAdapter(),
                "desktop_binding_adapter": first_desktop,
            }

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    HardInterruption,
                    "after atomic backup finalize",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                committed_backup = next(store.backups_dir.iterdir())
                committed_manifest = json.loads(
                    (committed_backup / "backup.json").read_text()
                )
                self.assertEqual("committed", committed_manifest["lifecycle"])
                self.assertEqual(str(target_executable), first_runner.gui_env)
                committed_active_state = capture_path_state(store.active_path)

                retry_runner = _FakeLaunchctlRunner(
                    gui_env=first_runner.gui_env,
                    service_loaded=first_runner.service_loaded,
                )
                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()
                retry_options["desktop_binding_adapter"] = _DesktopBindingAdapter(
                    store,
                    runner=retry_runner,
                    uid_provider=lambda: 501,
                )
                retry_receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual("committed", retry_receipt.outcome)
            self.assertEqual(
                "committed",
                json.loads((committed_backup / "backup.json").read_text())[
                    "lifecycle"
                ],
            )
            retry_manifest = json.loads(
                (
                    store.backups_dir
                    / str(retry_receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            retry_active_entry = next(
                entry
                for entry in retry_manifest["entries"]
                if entry["path"] == str(store.active_path)
            )
            self.assertEqual(
                committed_active_state,
                retry_active_entry["before_state"],
            )
            self.assertEqual(str(target_executable), retry_runner.gui_env)
            self.assertTrue(retry_runner.service_loaded)

    def test_terminal_switch_write_followed_by_catchable_error_stays_committed(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class RaiseAfterTerminalWriteAdapter(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "backup_finalize":
                    raise OSError("injected after terminal switch write")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": RaiseAfterTerminalWriteAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("committed", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("committed", manifest["lifecycle"])
            finalize_effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect.get("phase") == "backup_finalize"
            )
            self.assertEqual("applied", finalize_effect["status"])
            self.assertEqual("committed", manifest["switch_journal"]["state"])
            self.assertEqual(str(target_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            self.assertTrue(all(path.exists() for path in observed_paths))

    def test_switch_publishes_bound_marker_after_durable_backup_before_first_intent(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class OrderingAdapter(FilesystemAdapter):
            def __init__(self, store: Store) -> None:
                self.store = store
                self.events: list[str] = []
                self.marker: dict[str, object] | None = None
                self.prepared_journal: dict[str, object] | None = None

            def sync_tree(
                self,
                path: Path,
                *,
                file_phase: str,
                directory_phase: str,
            ) -> None:
                self.events.append(directory_phase)
                super().sync_tree(
                    path,
                    file_phase=file_phase,
                    directory_phase=directory_phase,
                )

            def sync_directory(self, path: Path, *, phase: str) -> None:
                self.events.append(phase)
                super().sync_directory(path, phase=phase)

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.events.append(phase)
                if phase == "switch_journal_intent":
                    marker_paths = tuple(
                        self.store.root.glob(".pending-transaction-*.json")
                    )
                    if len(marker_paths) == 1:
                        self.marker = json.loads(marker_paths[0].read_text())
                    journal = data.get("switch_journal")
                    if isinstance(journal, dict):
                        self.prepared_journal = dict(journal)
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            adapter = OrderingAdapter(store)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertIsNotNone(adapter.marker)
            self.assertIsNotNone(adapter.prepared_journal)
            marker = adapter.marker or {}
            journal = adapter.prepared_journal or {}
            self.assertEqual("switch", marker["operation"])
            self.assertEqual(receipt.backup_id, marker["backup_id"])
            self.assertEqual(marker["transaction_id"], journal["transaction_id"])
            self.assertEqual(
                marker["prepared_journal_sha256"],
                journal["prepared_journal_sha256"],
            )
            self.assertTrue(journal["recovery_marker_required"])
            self.assertLess(
                adapter.events.index("transaction_backup_directory"),
                adapter.events.index("transaction_backups_directory"),
            )
            self.assertLess(
                adapter.events.index("transaction_backups_directory"),
                adapter.events.index("switch_journal_prepare"),
            )
            self.assertLess(
                adapter.events.index("switch_journal_prepare"),
                adapter.events.index("pending_marker_publish"),
            )
            self.assertLess(
                adapter.events.index("pending_marker_publish"),
                adapter.events.index("switch_journal_intent"),
            )
            self.assertEqual(
                tuple(),
                tuple(store.root.glob(".pending-transaction-*.json")),
            )

    def test_corrupt_pending_marker_blocks_every_supported_mutation_without_writes(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        def snapshot(path: Path) -> dict[str, tuple[object, ...]]:
            result: dict[str, tuple[object, ...]] = {}
            for candidate in sorted(
                (path, *path.rglob("*")),
                key=lambda item: str(item),
            ):
                info = candidate.lstat()
                relative = str(candidate.relative_to(path)) or "."
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISLNK(info.st_mode):
                    result[relative] = ("symlink", mode, os.readlink(candidate))
                elif stat.S_ISREG(info.st_mode):
                    result[relative] = ("file", mode, candidate.read_bytes())
                else:
                    result[relative] = ("directory", mode)
            return result

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            marker = store.root / ".pending-transaction-corrupt-backup.json"
            marker.write_text("{not-json\n")
            before = snapshot(store.root)
            requests = (
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": root / "unused-source",
                        "codex_bin": "/tmp/unused-codex",
                        "app_cli_path": "/tmp/unused-codex",
                        "allow_missing_auth": True,
                        "overwrite": False,
                    },
                ),
                TransactionRequest(
                    operation="restore",
                    profile="",
                    options={"backup_id": "unused-backup", "force": False},
                ),
            )

            for request in requests:
                with self.subTest(operation=request.operation):
                    with self.assertRaisesRegex(
                        SwitchError,
                        "corrupt-backup",
                    ):
                        execute_transaction(store, request)
                    self.assertEqual(before, snapshot(store.root))

    def test_marker_required_switch_without_marker_blocks_cross_operation_capture(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, marker_path, backup_dir, _ = self.arrange_pending_switch(root)
            marker_path.unlink()
            source_home = root / "capture-source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "capture"\n')
            (source_home / "auth.json").write_text('{"token":"capture"}\n')
            before_store = capture_path_state(store.root)

            with self.assertRaisesRegex(SwitchError, backup_dir.name):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="openai-official",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-official",
                            "app_cli_path": "/tmp/codex-official",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": FilesystemAdapter(),
                        },
                    ),
                )

            self.assertEqual(before_store, capture_path_state(store.root))

    def test_effect_free_marker_required_switch_without_marker_is_closed_before_capture(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class MarkerPublished(BaseException):
            pass

        class InterruptAfterMarkerPublish(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "pending_marker_publish":
                    raise MarkerPublished("switch marker published")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            with self.assertRaisesRegex(MarkerPublished, "marker published"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": InterruptAfterMarkerPublish(),
                        },
                    ),
                )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            backup_dir = store.backups_dir / str(marker["backup_id"])
            marker_path.unlink()
            source_home = root / "capture-source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "capture"\n')
            (source_home / "auth.json").write_text('{"token":"capture"}\n')

            capture_request = TransactionRequest(
                operation="capture",
                profile="openai-official",
                options={
                    "source_home": source_home,
                    "codex_bin": "/tmp/codex-official",
                    "app_cli_path": "/tmp/codex-official",
                    "allow_missing_auth": False,
                    "overwrite": True,
                    "filesystem_adapter": FilesystemAdapter(),
                },
            )
            before_dry_run = capture_path_state(store.root)
            dry_run_receipt = execute_transaction(
                store,
                capture_request,
                dry_run=True,
            )
            self.assertEqual("dry_run", dry_run_receipt.outcome)
            self.assertEqual(before_dry_run, capture_path_state(store.root))

            receipt = execute_transaction(
                store,
                capture_request,
            )

            self.assertEqual("committed", receipt.outcome)
            closed = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", closed["lifecycle"])
            self.assertEqual("recovered", closed["switch_journal"]["state"])
            self.assertEqual([], closed["switch_journal"]["effects"])

    def test_pre_marker_restore_blocks_custom_mutation_gate(self) -> None:
        from codex_switch_transaction import custom_switch_mutation_gate

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            backup_dir = self.write_pre_marker_restore_evidence(store)

            with self.assertRaisesRegex(SwitchError, backup_dir.name):
                with custom_switch_mutation_gate(store):
                    self.fail("custom mutation gate admitted unresolved restore")

    def test_pre_marker_restore_blocks_init_before_store_writes(self) -> None:
        from codex_switch_lifecycle import cmd_init
        from codex_switch_transaction import capture_path_state

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            backup_dir = self.write_pre_marker_restore_evidence(store)
            before = capture_path_state(store.root)
            args = argparse.Namespace(
                store_dir=store.root,
                official_codex_home=store.official_codex_home,
                official_codex_home_source="explicit",
                internal_codex_home=store.internal_codex_home,
                internal_codex_home_source="explicit",
                launch_agent_path=store.launch_agent_path,
                launch_agent_label=store.launch_agent_label,
                codex_bin="/tmp/codex-official",
                app_cli_path="/tmp/codex-official",
                capture_current=None,
                overwrite_capture=False,
            )

            with self.assertRaisesRegex(SwitchError, backup_dir.name):
                cmd_init(args)

            self.assertEqual(before, capture_path_state(store.root))

    def test_legacy_markerless_switch_recovers_before_cross_operation_capture(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, marker_path, backup_dir, _ = self.arrange_pending_switch(root)
            self.convert_pending_switch_to_legacy_markerless(
                marker_path,
                backup_dir,
            )
            corrupt_history = store.backups_dir / "corrupt-historical-backup"
            corrupt_history.mkdir()
            (corrupt_history / "backup.json").write_text("{not-json\n")
            source_home = root / "capture-source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "capture"\n')
            (source_home / "auth.json").write_text('{"token":"capture"}\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="openai-official",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-official",
                        "app_cli_path": "/tmp/codex-official",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": FilesystemAdapter(),
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            recovered = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", recovered["lifecycle"])
            self.assertEqual("recovered", recovered["switch_journal"]["state"])
            self.assertEqual(
                "openai-official",
                json.loads(store.active_path.read_text())["profile"],
            )
            self.assertEqual("{not-json\n", (corrupt_history / "backup.json").read_text())

    def test_pre_marker_restore_states_block_every_transaction_operation(self) -> None:
        from codex_switch_transaction import TransactionRequest, capture_path_state, execute_transaction

        requests = (
            TransactionRequest(operation="switch", profile="internal", options={}),
            TransactionRequest(
                operation="capture",
                profile="internal",
                options={
                    "source_home": Path("/unused"),
                    "codex_bin": "/unused",
                    "app_cli_path": "/unused",
                    "allow_missing_auth": False,
                    "overwrite": False,
                },
            ),
            TransactionRequest(
                operation="restore",
                profile="restore",
                options={"backup_id": "unused", "force": False},
            ),
        )
        for lifecycle in ("prepared", "rollback_failed"):
            for request in requests:
                with self.subTest(lifecycle=lifecycle, operation=request.operation):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        store = self.make_store(Path(temp_dir))
                        backup_dir = self.write_pre_marker_restore_evidence(
                            store,
                            lifecycle=lifecycle,
                        )
                        before = capture_path_state(store.root)
                        with self.assertRaisesRegex(SwitchError, backup_dir.name):
                            execute_transaction(store, request)
                        self.assertEqual(before, capture_path_state(store.root))

    def test_corrupt_and_multiple_unmarked_evidence_blocks_without_writes(self) -> None:
        from codex_switch_transaction import TransactionRequest, capture_path_state, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            store.ensure()
            corrupt_dir = store.backups_dir / "corrupt-unmarked"
            corrupt_dir.mkdir()
            (corrupt_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "id": corrupt_dir.name,
                        "operation": "restore",
                        "lifecycle": "prepared",
                        "entries": [],
                    }
                )
                + "\n"
            )
            before = capture_path_state(store.root)
            with self.assertRaisesRegex(SwitchError, "corrupt-unmarked"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "unused", "force": False},
                    ),
                )
            self.assertEqual(before, capture_path_state(store.root))

        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            first = self.write_pre_marker_restore_evidence(
                store,
                lifecycle="prepared",
            )
            second = self.write_pre_marker_restore_evidence(
                store,
                lifecycle="rollback_failed",
            )
            before = capture_path_state(store.root)
            with self.assertRaisesRegex(SwitchError, "Multiple unresolved"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={},
                    ),
                    dry_run=True,
                )
            self.assertEqual(before, capture_path_state(store.root))
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_capture_journal_blocks_custom_and_init_routes_before_writes(self) -> None:
        from codex_switch_lifecycle import cmd_init
        from codex_switch_transaction import capture_path_state, custom_switch_mutation_gate

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            journal = store.profiles_dir / ".internal.capture-journal.json"
            journal.write_text("{corrupt\n")
            before = capture_path_state(store.root)
            with self.assertRaisesRegex(SwitchError, "internal"):
                with custom_switch_mutation_gate(store):
                    self.fail("custom route admitted pending capture")
            self.assertEqual(before, capture_path_state(store.root))

            args = argparse.Namespace(
                store_dir=store.root,
                official_codex_home=store.official_codex_home,
                official_codex_home_source="explicit",
                internal_codex_home=store.internal_codex_home,
                internal_codex_home_source="explicit",
                launch_agent_path=store.launch_agent_path,
                launch_agent_label=store.launch_agent_label,
                codex_bin="/tmp/codex-official",
                app_cli_path="/tmp/codex-official",
                capture_current=None,
                overwrite_capture=False,
            )
            with self.assertRaisesRegex(SwitchError, "internal"):
                cmd_init(args)
            self.assertEqual(before, capture_path_state(store.root))

    def test_effect_free_marker_required_restore_without_marker_closes_before_retry(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class MarkerPublished(BaseException):
            pass

        class InterruptAfterMarkerPublish(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "pending_marker_publish":
                    raise MarkerPublished("restore marker published")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            historical, target = self.arrange_restorable_file_backup(
                store,
                backup_id="historical-effect-free-restore",
            )
            interrupted_request = TransactionRequest(
                operation="restore",
                profile="restore",
                options={
                    "backup_id": historical.name,
                    "force": False,
                    "filesystem_adapter": InterruptAfterMarkerPublish(),
                },
            )
            with self.assertRaisesRegex(MarkerPublished, "marker published"):
                execute_transaction(store, interrupted_request)
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            safety_dir = store.backups_dir / str(marker["backup_id"])
            marker_path.unlink()
            retry_request = TransactionRequest(
                operation="restore",
                profile="restore",
                options={
                    "backup_id": historical.name,
                    "force": False,
                    "filesystem_adapter": FilesystemAdapter(),
                },
            )
            before_dry_run = capture_path_state(store.root)
            dry_run = execute_transaction(store, retry_request, dry_run=True)
            self.assertEqual("dry_run", dry_run.outcome)
            self.assertEqual(before_dry_run, capture_path_state(store.root))

            receipt = execute_transaction(store, retry_request)

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('model = "before"\n', target.read_text())
            closed = json.loads((safety_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", closed["lifecycle"])
            self.assertEqual("recovered", closed["restore_journal"]["state"])
            self.assertEqual([], closed["restore_journal"]["effects"])

    def test_begun_marker_required_restore_without_marker_blocks_before_writes(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class RestoreActionApplied(BaseException):
            pass

        class InterruptAfterRestoreAction(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    raise RestoreActionApplied("restore action applied")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            historical, _ = self.arrange_restorable_file_backup(
                store,
                backup_id="historical-begun-restore",
            )
            with self.assertRaisesRegex(RestoreActionApplied, "action applied"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": historical.name,
                            "force": False,
                            "filesystem_adapter": InterruptAfterRestoreAction(),
                        },
                    ),
                )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            safety_dir = store.backups_dir / str(marker["backup_id"])
            marker_path.unlink()
            safety_manifest = json.loads((safety_dir / "backup.json").read_text())
            self.assertTrue(safety_manifest["restore_journal"]["effects"])
            before = capture_path_state(store.root)

            with self.assertRaisesRegex(SwitchError, safety_dir.name):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": historical.name, "force": True},
                    ),
                    dry_run=True,
                )

            self.assertEqual(before, capture_path_state(store.root))

    def test_unfinished_capture_is_store_wide_gate_and_dry_run_is_read_only(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        def snapshot(path: Path) -> tuple[tuple[str, bytes | None], ...]:
            rows: list[tuple[str, bytes | None]] = []
            for candidate in sorted(path.rglob("*"), key=lambda item: str(item)):
                relative = candidate.relative_to(path).as_posix()
                if candidate.is_symlink():
                    rows.append((relative, os.readlink(candidate).encode()))
                elif candidate.is_file():
                    rows.append((relative, candidate.read_bytes()))
                else:
                    rows.append((relative, None))
            return tuple(rows)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, source_home, _ = self.arrange_capture_fixture(root)
            journal = store.profiles_dir / ".internal.capture-journal.json"
            journal.write_text("{corrupt\n")
            before = snapshot(store.root)
            blocked_requests = (
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
                TransactionRequest(
                    operation="restore",
                    profile="",
                    options={"backup_id": "unused", "force": False},
                ),
                TransactionRequest(
                    operation="capture",
                    profile="openai-official",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex",
                        "app_cli_path": "/tmp/codex",
                        "allow_missing_auth": False,
                        "overwrite": False,
                    },
                ),
            )
            for request in blocked_requests:
                with self.subTest(operation=request.operation):
                    with self.assertRaisesRegex(SwitchError, "internal"):
                        execute_transaction(store, request)
                    self.assertEqual(before, snapshot(store.root))

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex",
                        "app_cli_path": "/tmp/codex",
                        "allow_missing_auth": False,
                        "overwrite": True,
                    },
                ),
                dry_run=True,
            )
            self.assertEqual("dry_run", receipt.outcome)
            self.assertIn("pending capture", " ".join(receipt.preview_lines))
            self.assertEqual(before, snapshot(store.root))

    def test_prepared_switch_missing_later_payload_blocks_before_first_recovery_write(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterActiveAction(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise HardInterruption("after active action")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, observed_paths = self.arrange_switch_effect_fixture(root)
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": True,
                "skip_app_cli": True,
                "skip_launchctl": True,
                "filesystem_adapter": InterruptAfterActiveAction(),
            }
            with self.assertRaisesRegex(HardInterruption, "after active action"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=options,
                    ),
                )

            marker = next(store.root.glob(".pending-transaction-*.json"))
            marker_data = json.loads(marker.read_text())
            backup_dir = store.backups_dir / str(marker_data["backup_id"])
            manifest = json.loads((backup_dir / "backup.json").read_text())
            active_entry = next(
                entry
                for entry in manifest["entries"]
                if entry["path"] == str(store.active_path)
            )
            payload = active_entry["payload"]
            (backup_dir / payload).unlink()
            before_retry = {
                path: capture_path_state(path) for path in observed_paths
            }

            retry_options = dict(options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()
            with self.assertRaisesRegex(SwitchError, "payload"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(
                before_retry,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertTrue(marker.exists())

    def test_mismatched_pending_transaction_id_blocks_without_writes(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, marker_path, backup_dir, options = self.arrange_pending_switch(root)
            manifest_path = backup_dir / "backup.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["switch_journal"]["transaction_id"] = "conflicting-id"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
            before = {
                path.relative_to(store.root).as_posix(): path.read_bytes()
                for path in store.root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            retry_options = dict(options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()

            with self.assertRaisesRegex(SwitchError, str(backup_dir.name)):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(
                before,
                {
                    path.relative_to(store.root).as_posix(): path.read_bytes()
                    for path in store.root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                },
            )
            self.assertTrue(marker_path.exists())

    def test_multiple_pending_markers_block_without_writes(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, marker_path, _, options = self.arrange_pending_switch(root)
            second_marker = store.root / ".pending-transaction-second-backup.json"
            shutil.copy2(marker_path, second_marker)
            before = {
                path.relative_to(store.root).as_posix(): path.read_bytes()
                for path in store.root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            retry_options = dict(options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()

            with self.assertRaisesRegex(SwitchError, "Multiple pending"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(
                before,
                {
                    path.relative_to(store.root).as_posix(): path.read_bytes()
                    for path in store.root.rglob("*")
                    if path.is_file() and not path.is_symlink()
                },
            )

    def test_pending_rollback_failed_evidence_blocks_without_writes(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, marker_path, backup_dir, options = self.arrange_pending_switch(root)
            manifest_path = backup_dir / "backup.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["lifecycle"] = "rollback_failed"
            manifest["switch_journal"]["state"] = "rollback_failed"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
            before_marker = marker_path.read_bytes()
            before_manifest = manifest_path.read_bytes()
            retry_options = dict(options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()

            with self.assertRaisesRegex(SwitchError, "rollback failed"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(before_marker, marker_path.read_bytes())
            self.assertEqual(before_manifest, manifest_path.read_bytes())

    def test_restore_publishes_bound_marker_before_first_intent_and_action(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class OrderingAdapter(FilesystemAdapter):
            def __init__(self, store: Store) -> None:
                self.store = store
                self.events: list[str] = []
                self.marker_at_intent: dict[str, object] | None = None

            def sync_tree(
                self,
                path: Path,
                *,
                file_phase: str,
                directory_phase: str,
            ) -> None:
                self.events.append(directory_phase)
                super().sync_tree(
                    path,
                    file_phase=file_phase,
                    directory_phase=directory_phase,
                )

            def sync_directory(self, path: Path, *, phase: str) -> None:
                self.events.append(phase)
                super().sync_directory(path, phase=phase)

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.events.append(phase)
                if phase == "restore_journal_intent":
                    markers = tuple(
                        self.store.root.glob(".pending-transaction-*.json")
                    )
                    if len(markers) == 1:
                        self.marker_at_intent = json.loads(markers[0].read_text())
                super().write_manifest(path, data, phase=phase)

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.events.append(f"action:{phase}")
                super().materialize(source, destination, state, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            backup_dir = store.backups_dir / "historical"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            payload = payload_dir / "config.toml"
            payload.write_text("historical\n")
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "historical",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": self.file_state(payload),
                                "committed_after_state": self.file_state(target),
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )
            adapter = OrderingAdapter(store)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="",
                    options={
                        "backup_id": "historical",
                        "force": False,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertIsNotNone(adapter.marker_at_intent)
            marker = adapter.marker_at_intent or {}
            self.assertEqual("restore", marker["operation"])
            self.assertEqual(receipt.backup_id, marker["backup_id"])
            self.assertLess(
                adapter.events.index("transaction_backup_directory"),
                adapter.events.index("transaction_backups_directory"),
            )
            self.assertLess(
                adapter.events.index("transaction_backups_directory"),
                adapter.events.index("restore_journal_prepare"),
            )
            self.assertLess(
                adapter.events.index("restore_journal_prepare"),
                adapter.events.index("pending_marker_publish"),
            )
            self.assertLess(
                adapter.events.index("pending_marker_publish"),
                adapter.events.index("restore_journal_intent"),
            )
            self.assertLess(
                adapter.events.index("restore_journal_intent"),
                adapter.events.index("action:apply"),
            )
            self.assertEqual(tuple(), tuple(store.root.glob(".pending-transaction-*.json")))

    def test_restore_recovery_is_idempotent_across_second_hard_interruption(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class HardInterruption(BaseException):
            pass

        class InterruptFirstApply(FilesystemAdapter):
            def __init__(self) -> None:
                self.apply_count = 0

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    self.apply_count += 1
                    if self.apply_count == 1:
                        raise HardInterruption("after first restore target")

        class InterruptFirstRecovery(FilesystemAdapter):
            def __init__(self) -> None:
                self.recovery_count = 0

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase.startswith("restore_recovery_"):
                    self.recovery_count += 1
                    if self.recovery_count == 1:
                        raise HardInterruption("during restore recovery")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            targets = (
                store.official_codex_home / "config.toml",
                store.official_codex_home / "auth.json",
            )
            current_payloads = (b"current-config\n", b"current-auth\n")
            historical_payloads = (b"old-config\n", b"old-auth\n")
            for target, payload in zip(targets, current_payloads):
                target.write_bytes(payload)
            backup_dir = store.backups_dir / "historical-double-interrupt"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            entries: list[dict[str, object]] = []
            for index, (target, payload) in enumerate(
                zip(targets, historical_payloads)
            ):
                staged = payload_dir / f"{index}.bin"
                staged.write_bytes(payload)
                entries.append(
                    {
                        "path": str(target),
                        "before_state": self.file_state(staged),
                        "committed_after_state": self.file_state(target),
                        "payload": f"payloads/{index}.bin",
                    }
                )
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": entries,
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(HardInterruption, "first restore target"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": backup_dir.name,
                            "force": False,
                            "filesystem_adapter": InterruptFirstApply(),
                        },
                    ),
                )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            safety_dir = store.backups_dir / str(marker["backup_id"])

            with self.assertRaisesRegex(HardInterruption, "during restore recovery"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-recovery",
                            "force": False,
                            "filesystem_adapter": InterruptFirstRecovery(),
                        },
                    ),
                )
            self.assertTrue(marker_path.exists())

            with self.assertRaisesRegex(SwitchError, "Backup not found"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-recovery",
                            "force": False,
                            "filesystem_adapter": FilesystemAdapter(),
                        },
                    ),
                )

            self.assertEqual(
                current_payloads,
                tuple(target.read_bytes() for target in targets),
            )
            recovered = json.loads((safety_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", recovered["lifecycle"])
            self.assertEqual("recovered", recovered["restore_journal"]["state"])
            self.assertFalse(marker_path.exists())

    def test_restore_directory_recovery_is_idempotent_after_stage_move(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            SwitchError,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptApply(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    raise HardInterruption("after directory apply")

        class InterruptRecovery(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase.startswith("restore_recovery_"):
                    raise HardInterruption("after directory recovery")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "plugins"
            target.mkdir()
            (target / "current.txt").write_text("current\n")
            backup_dir = store.backups_dir / "historical-directory-interrupt"
            payload = backup_dir / "payloads" / "plugins"
            payload.mkdir(parents=True)
            (payload / "historical.txt").write_text("historical\n")
            before_state = capture_path_state(payload)
            before_state["path"] = str(target)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "id": backup_dir.name,
                        "operation": "switch",
                        "lifecycle": "committed",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": before_state,
                                "committed_after_state": capture_path_state(target),
                                "payload": "payloads/plugins",
                            }
                        ],
                    },
                    sort_keys=True,
                )
                + "\n"
            )

            with self.assertRaisesRegex(HardInterruption, "directory apply"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": backup_dir.name,
                            "force": False,
                            "filesystem_adapter": InterruptApply(),
                        },
                    ),
                )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            safety_dir = store.backups_dir / str(marker["backup_id"])

            with self.assertRaisesRegex(HardInterruption, "directory recovery"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-directory-recovery",
                            "force": False,
                            "filesystem_adapter": InterruptRecovery(),
                        },
                    ),
                )
            self.assertFalse((safety_dir / "payloads" / "0000-plugins").exists())

            with self.assertRaisesRegex(SwitchError, "Backup not found"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-directory-recovery",
                            "force": False,
                            "filesystem_adapter": FilesystemAdapter(),
                        },
                    ),
                )

            self.assertEqual("current\n", (target / "current.txt").read_text())
            self.assertFalse(marker_path.exists())
            recovered = json.loads((safety_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", recovered["lifecycle"])

    def test_restore_recovery_uses_frozen_allowlist_after_manifest_mutation(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class HardInterruption(BaseException):
            pass

        class InterruptBeforeAdoptedHome(FilesystemAdapter):
            def __init__(self, adopted_target: Path) -> None:
                self.adopted_target = adopted_target.resolve()

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "apply" and destination == self.adopted_target:
                    raise HardInterruption("before adopted-home effect")
                super().materialize(source, destination, state, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            internal_profile = store.profile_dir("internal")
            official_profile = store.profile_dir("openai-official")
            internal_profile.mkdir()
            official_profile.mkdir()
            adopted_home = root / "adopted-internal"
            adopted_home.mkdir()
            adopted_target = adopted_home / "config.toml"
            adopted_target.write_text("current-adopted\n")
            internal_manifest_path = store.manifest_path("internal")
            current_manifest = {
                "name": "internal",
                "codex_home": str(adopted_home.resolve()),
            }
            internal_manifest_path.write_text(json.dumps(current_manifest) + "\n")
            (official_profile / "manifest.json").write_text(
                json.dumps({"name": "openai-official"}) + "\n"
            )

            backup_dir = store.backups_dir / "adopted-frozen-authority"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            planned_manifest_payload = payload_dir / "internal-manifest.json"
            planned_manifest_payload.write_text(
                json.dumps(
                    {
                        "name": "internal",
                        "codex_home": str(store.managed_home("internal").resolve()),
                    }
                )
                + "\n"
            )
            adopted_payload = payload_dir / "adopted-config.toml"
            adopted_payload.write_text("historical-adopted\n")
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(internal_manifest_path),
                                "before_state": self.file_state(
                                    planned_manifest_payload
                                ),
                                "committed_after_state": self.file_state(
                                    internal_manifest_path
                                ),
                                "payload": "payloads/internal-manifest.json",
                            },
                            {
                                "path": str(adopted_target),
                                "before_state": self.file_state(adopted_payload),
                                "committed_after_state": self.file_state(
                                    adopted_target
                                ),
                                "payload": "payloads/adopted-config.toml",
                            },
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(HardInterruption, "adopted-home"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": backup_dir.name,
                            "force": False,
                            "filesystem_adapter": InterruptBeforeAdoptedHome(
                                adopted_target
                            ),
                        },
                    ),
                )
            changed_manifest = json.loads(internal_manifest_path.read_text())
            self.assertNotEqual(
                str(adopted_home.resolve()),
                changed_manifest["codex_home"],
            )

            with self.assertRaisesRegex(SwitchError, "Backup not found"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-frozen-recovery",
                            "force": False,
                            "filesystem_adapter": FilesystemAdapter(),
                        },
                    ),
                )

            self.assertEqual(current_manifest, json.loads(internal_manifest_path.read_text()))
            self.assertEqual("current-adopted\n", adopted_target.read_text())
            self.assertEqual(tuple(), tuple(store.root.glob(".pending-transaction-*.json")))

    def test_restore_recovery_preserves_replaced_empty_created_parent(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class HardInterruption(BaseException):
            pass

        class InterruptAfterCreatedParent(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    raise HardInterruption("after created parent")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            created_parent = store.official_codex_home / "nested-created"
            target = created_parent / "config.toml"
            backup_dir = store.backups_dir / "created-parent-identity"
            payload = backup_dir / "payloads" / "config.toml"
            payload.parent.mkdir(parents=True)
            payload.write_text("historical\n")
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": self.file_state(payload),
                                "committed_after_state": {"kind": "missing"},
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )
            with self.assertRaisesRegex(HardInterruption, "created parent"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": backup_dir.name,
                            "force": False,
                            "filesystem_adapter": InterruptAfterCreatedParent(),
                        },
                    ),
                )
            marker = next(store.root.glob(".pending-transaction-*.json"))
            target.unlink()
            created_parent.rmdir()
            created_parent.mkdir()
            replacement_identity = (
                created_parent.stat().st_dev,
                created_parent.stat().st_ino,
            )

            with self.assertRaisesRegex(SwitchError, "identity"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-parent-check",
                            "force": False,
                            "filesystem_adapter": FilesystemAdapter(),
                        },
                    ),
                )

            self.assertTrue(created_parent.is_dir())
            self.assertEqual(
                replacement_identity,
                (created_parent.stat().st_dev, created_parent.stat().st_ino),
            )
            self.assertTrue(marker.exists())

    def test_switch_rejects_unexpected_predecessor_before_overwrite(self) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class DriftAfterMarker(FilesystemAdapter):
            def __init__(self, target: Path) -> None:
                self.target = target

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "pending_marker_publish":
                    self.target.write_text("external-predecessor\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            target = store.bin_dir / "codex"

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": DriftAfterMarker(target),
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual("external-predecessor\n", target.read_text())
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertIn("predecessor", manifest["failure"])

    def test_switch_pinned_parent_prevents_symlink_redirection_after_route_validation(
        self,
    ) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class SwapParentBeforeAction(FilesystemAdapter):
            def __init__(
                self,
                target: Path,
                original_parent: Path,
                parked_parent: Path,
                attacker_parent: Path,
            ) -> None:
                self.target = target
                self.original_parent = original_parent
                self.parked_parent = parked_parent
                self.attacker_parent = attacker_parent
                self.swapped = False

            def before_switch_effect_action(
                self,
                path: Path,
                effect: dict[str, object],
            ) -> None:
                del effect
                if path != self.target or self.swapped:
                    return
                self.swapped = True
                self.original_parent.rename(self.parked_parent)
                self.attacker_parent.mkdir()
                self.original_parent.symlink_to(
                    self.attacker_parent,
                    target_is_directory=True,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            original_parent = store.internal_codex_home
            target = original_parent / "config.toml"
            parked_parent = root / "parked-internal"
            attacker_parent = root / "attacker"
            adapter = SwapParentBeforeAction(
                target,
                original_parent,
                parked_parent,
                attacker_parent,
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(adapter.swapped)
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertFalse((attacker_parent / "config.toml").exists())
            self.assertTrue(original_parent.is_symlink())
            self.assertEqual(attacker_parent.resolve(), original_parent.resolve())
            self.assertTrue((parked_parent / "config.toml").is_file())
            self.assertEqual(1, len(tuple(store.root.glob(".pending-transaction-*.json"))))

    def test_switch_accepts_stable_attested_symlink_ancestor(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            original_home = store.internal_codex_home
            real_parent = root / "real-home-parent"
            real_parent.mkdir()
            real_home = real_parent / "internal"
            original_home.rename(real_home)
            linked_parent = root / "linked-home-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            store.internal_codex_home = linked_parent / "internal"

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertTrue(linked_parent.is_symlink())
            self.assertIn(
                'model = "internal-after"',
                (real_home / "internal.config.toml").read_text(),
            )
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            config_effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect["phase"] == "config_write"
            )
            link_guard = next(
                component
                for component in config_effect["route_guard"]["components"]
                if component["path"] == str(linked_parent)
            )
            self.assertEqual("symlink", link_guard["kind"])
            self.assertEqual(str(real_parent), link_guard["symlink_target"])
            self.assertIsInstance(link_guard["inode"], int)

    def test_switch_rejects_changed_attested_symlink_ancestor_before_overwrite(
        self,
    ) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class RetargetAfterConfigIntent(FilesystemAdapter):
            def __init__(
                self,
                linked_parent: Path,
                attacker_parent: Path,
            ) -> None:
                self.linked_parent = linked_parent
                self.attacker_parent = attacker_parent
                self.retargeted = False

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase != "switch_journal_intent" or self.retargeted:
                    return
                journal = data.get("switch_journal")
                effects = journal.get("effects") if isinstance(journal, dict) else None
                if (
                    not isinstance(effects, list)
                    or not effects
                    or not isinstance(effects[-1], dict)
                    or effects[-1].get("phase") != "config_write"
                ):
                    return
                self.retargeted = True
                self.linked_parent.unlink()
                self.linked_parent.symlink_to(
                    self.attacker_parent,
                    target_is_directory=True,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            original_home = store.internal_codex_home
            real_parent = root / "real-home-parent"
            real_parent.mkdir()
            real_home = real_parent / "internal"
            original_home.rename(real_home)
            linked_parent = root / "linked-home-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            store.internal_codex_home = linked_parent / "internal"
            attacker_parent = root / "attacker-home-parent"
            attacker_home = attacker_parent / "internal"
            attacker_home.mkdir(parents=True)
            attacker_config = attacker_home / "config.toml"
            attacker_config.write_text('model = "attacker-preserved"\n')
            adapter = RetargetAfterConfigIntent(linked_parent, attacker_parent)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertTrue(adapter.retargeted)
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(
                'model = "attacker-preserved"\n',
                attacker_config.read_text(),
            )
            self.assertEqual(
                'model = "internal-before"\n',
                (real_home / "config.toml").read_text(),
            )
            self.assertEqual(1, len(tuple(store.root.glob(".pending-transaction-*.json"))))

    def test_switch_rejects_canonical_parent_swap_between_route_check_and_open(
        self,
    ) -> None:
        import codex_switch_transaction as transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            original_home = store.internal_codex_home
            real_parent = root / "real-home-parent"
            real_parent.mkdir()
            real_home = real_parent / "internal"
            original_home.rename(real_home)
            linked_parent = root / "linked-home-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            store.internal_codex_home = linked_parent / "internal"
            target = store.internal_codex_home / "config.toml"
            parked_home = root / "parked-original-home"
            attacker_home = root / "attacker-home"
            shutil.copytree(real_home, attacker_home, copy_function=os.link)
            attacker_config = attacker_home / "config.toml"
            attacker_before = attacker_config.read_bytes()
            attacker_inode = attacker_config.stat().st_ino
            original_before = (real_home / "config.toml").read_bytes()
            real_parent_identity = (
                real_parent.stat().st_dev,
                real_parent.stat().st_ino,
            )
            armed = False
            injected = False
            real_validate = transaction._validate_route_guard
            real_open = transaction.os.open

            def arm_after_route_validation(
                path: Path,
                guard: dict[str, object],
            ) -> None:
                nonlocal armed
                real_validate(path, guard)
                if path == target:
                    armed = True

            def racing_open(
                path: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal armed, injected
                parent_matches = (
                    dir_fd is not None
                    and (os.fstat(dir_fd).st_dev, os.fstat(dir_fd).st_ino)
                    == real_parent_identity
                )
                if armed and str(path) == real_home.name and parent_matches:
                    armed = False
                    injected = True
                    real_home.rename(parked_home)
                    real_home.symlink_to(attacker_home, target_is_directory=True)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with (
                patch(
                    "codex_switch_transaction._validate_route_guard",
                    side_effect=arm_after_route_validation,
                ),
                patch(
                    "codex_switch_transaction.os.open",
                    side_effect=racing_open,
                ),
            ):
                receipt = transaction.execute_transaction(
                    store,
                    transaction.TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                        },
                    ),
                )

            self.assertTrue(injected)
            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(attacker_before, attacker_config.read_bytes())
            self.assertEqual(attacker_inode, attacker_config.stat().st_ino)
            self.assertEqual(
                original_before,
                (parked_home / "config.toml").read_bytes(),
            )
            self.assertEqual(0, len(tuple(store.root.glob(".pending-transaction-*.json"))))

    def test_switch_recovery_rejects_byte_identical_foreign_inode_after_interruption(
        self,
    ) -> None:
        from codex_switch_transaction import FilesystemAdapter, TransactionRequest, execute_transaction

        class HardInterruption(BaseException):
            pass

        class InterruptAfterReplacement(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "config_write":
                    raise HardInterruption("after replacement before applied")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            target = store.internal_codex_home / "config.toml"
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": True,
                "skip_app_cli": True,
                "skip_launchctl": True,
                "filesystem_adapter": InterruptAfterReplacement(),
            }
            with self.assertRaisesRegex(HardInterruption, "before applied"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=options,
                    ),
                )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(marker["backup_id"])
                    / "backup.json"
                ).read_text()
            )
            config_effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect["phase"] == "config_write"
            )
            self.assertEqual("intent", config_effect["status"])
            self.assertIn("produced_identity", config_effect)
            original_produced_inode = config_effect["produced_identity"]["inode"]
            payload = target.read_bytes()
            mode = stat.S_IMODE(target.stat().st_mode)
            target.unlink()
            target.write_bytes(payload)
            target.chmod(mode)
            foreign_identity = (target.stat().st_dev, target.stat().st_ino)
            self.assertNotEqual(original_produced_inode, foreign_identity[1])

            retry_options = dict(options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()
            with self.assertRaisesRegex(SwitchError, "identity"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(payload, target.read_bytes())
            self.assertEqual(
                foreign_identity,
                (target.stat().st_dev, target.stat().st_ino),
            )
            self.assertTrue(marker_path.exists())

    def test_switch_installs_persisted_stage_and_rejects_identity_change_before_applied(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        for variant in ("persisted-stage", "foreign-after-action"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp_dir:
                class StageIdentityAuditAdapter(FilesystemAdapter):
                    def __init__(self) -> None:
                        self.staged_inode: int | None = None
                        self.produced_inode: int | None = None

                    def before_switch_effect_action(
                        self,
                        path: Path,
                        effect: dict[str, object],
                    ) -> None:
                        if effect.get("phase") == "config_write":
                            staged_identity = effect.get("staged_identity")
                            if isinstance(staged_identity, dict):
                                raw_inode = staged_identity.get("inode")
                                if type(raw_inode) is int:
                                    self.staged_inode = raw_inode
                        super().before_switch_effect_action(path, effect)

                    def write_bytes(
                        self,
                        path: Path,
                        data: bytes,
                        *,
                        mode: int,
                        phase: str,
                    ) -> None:
                        super().write_bytes(
                            path,
                            data,
                            mode=mode,
                            phase=phase,
                        )
                        if phase != "config_write":
                            return
                        if variant == "foreign-after-action":
                            payload = path.read_bytes()
                            path.unlink()
                            path.write_bytes(payload)
                            path.chmod(mode)
                        self.produced_inode = path.stat().st_ino

                root = Path(temp_dir)
                store, _, _, _ = self.arrange_switch_effect_fixture(root)
                target = store.internal_codex_home / "config.toml"
                before = target.read_bytes()
                adapter = StageIdentityAuditAdapter()
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": adapter,
                        },
                    ),
                )

                self.assertIsNotNone(adapter.staged_inode)
                self.assertIsNotNone(adapter.produced_inode)
                if variant == "persisted-stage":
                    self.assertEqual("committed", receipt.outcome)
                    self.assertEqual(
                        adapter.staged_inode,
                        adapter.produced_inode,
                    )
                else:
                    self.assertEqual("rolled_back", receipt.outcome)
                    self.assertEqual(before, target.read_bytes())

    def test_interrupted_repeated_path_effect_chain_recovers(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterSecondPathEffect(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "shell_bootstrap_write":
                    raise HardInterruption("after repeated path action")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            repeated_path = store.bin_dir / "codex"
            original = repeated_path.read_bytes()
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": False,
                "skip_app_cli": True,
                "skip_launchctl": True,
                "filesystem_adapter": InterruptAfterSecondPathEffect(),
            }
            with patch(
                "codex_switch_shell.shell_cli_bootstrap_path",
                return_value=repeated_path,
            ):
                with self.assertRaisesRegex(
                    HardInterruption,
                    "repeated path action",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                marker_path = next(
                    store.root.glob(".pending-transaction-*.json")
                )
                marker = json.loads(marker_path.read_text())
                interrupted_backup = (
                    store.backups_dir / str(marker["backup_id"])
                )
                interrupted = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                repeated_effects = [
                    effect
                    for effect in interrupted["switch_journal"]["effects"]
                    if effect.get("path") == str(repeated_path)
                ]
                self.assertEqual(2, len(repeated_effects))

                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()
                retry = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual("committed", retry.outcome)
            self.assertFalse(marker_path.exists())
            recovered = json.loads(
                (interrupted_backup / "backup.json").read_text()
            )
            self.assertEqual("rolled_back", recovered["lifecycle"])
            original_entry = next(
                entry
                for entry in recovered["entries"]
                if entry["path"] == str(repeated_path)
            )
            payload = interrupted_backup / str(original_entry["payload"])
            self.assertEqual(original, payload.read_bytes())

    def test_every_deterministic_file_effect_recovers_after_action_before_applied(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterPhase(FilesystemAdapter):
            def __init__(self, target_phase: str) -> None:
                self.target_phase = target_phase
                self.interrupted = False

            def interrupt(self, phase: str) -> None:
                if phase == self.target_phase and not self.interrupted:
                    self.interrupted = True
                    raise HardInterruption(f"after {phase} action")

            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                self.interrupt(phase)

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                self.interrupt(phase)

            def remove_path(self, path: Path, *, phase: str) -> None:
                super().remove_path(path, phase=phase)
                self.interrupt(phase)

            def sync_shared_entry(
                self,
                source: Path,
                target: Path,
                *,
                prefer_link: bool,
                phase: str,
            ) -> None:
                super().sync_shared_entry(
                    source,
                    target,
                    prefer_link=prefer_link,
                    phase=phase,
                )
                self.interrupt(phase)

        snapshot_phases = {
            "home_binding_write",
            "config_write",
            "profile_config_write",
            "auth_write",
            "shim_write",
            "shell_bootstrap_write",
            "app_wrapper_write",
            "plist_write",
            "active_write",
        }
        shared_phases = {
            "shared_support_sync",
            "desktop_global_state_sync",
            "stale_runtime_link_remove",
            "canonical_profile_write",
            "plugin_snapshot_write",
            "auth_remove",
        }
        expected_phases = snapshot_phases | shared_phases
        recovered_phases: set[str] = set()

        for target_phase in sorted(expected_phases):
            with (
                self.subTest(phase=target_phase),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, _, _, _ = self.arrange_switch_effect_fixture(root)
                shell_profile = root / "shell-profile"
                shell_profile.write_text("# original shell\n")
                config_mode = (
                    "shared"
                    if target_phase == "app_wrapper_write"
                    or target_phase in shared_phases
                    else "snapshot"
                )
                if config_mode == "shared":
                    shared_file = store.official_codex_home / "AGENTS.md"
                    shared_file.write_text("shared payload\n")
                    (store.official_codex_home / ".codex-global-state.json").write_text(
                        '{"appshotHotkey":"planned"}\n'
                    )
                    stale_link = store.internal_codex_home / "sessions"
                    stale_link.symlink_to(
                        store.official_codex_home / "sessions",
                        target_is_directory=True,
                    )
                if target_phase == "app_wrapper_write":
                    manifest_path = store.manifest_path("internal")
                    manifest = json.loads(manifest_path.read_text())
                    manifest["app_cli_path"] = str(
                        store.bin_dir / "codex-internal-app"
                    )
                    manifest_path.write_text(json.dumps(manifest) + "\n")
                adapter = InterruptAfterPhase(target_phase)
                options: dict[str, object] = {
                    "config_mode": config_mode,
                    "shared_config_base": None,
                    "clear_missing_auth": False,
                    "skip_shim": config_mode == "shared",
                    "skip_app_cli": (
                        config_mode == "shared"
                        and target_phase != "app_wrapper_write"
                    ),
                    "skip_launchctl": True,
                    "filesystem_adapter": adapter,
                }
                with patch.dict(
                    os.environ,
                    {
                        "CODEX_SWITCH_SHELL_PROFILE": str(shell_profile),
                        "CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "0",
                    },
                    clear=False,
                ):
                    with self.assertRaisesRegex(
                        HardInterruption,
                        f"after {target_phase} action",
                    ):
                        execute_transaction(
                            store,
                            TransactionRequest(
                                operation="switch",
                                profile="internal",
                                options=options,
                            ),
                        )

                    self.assertTrue(adapter.interrupted)
                    marker_path = next(
                        store.root.glob(".pending-transaction-*.json")
                    )
                    marker = json.loads(marker_path.read_text())
                    interrupted_backup = (
                        store.backups_dir / str(marker["backup_id"])
                    )
                    interrupted = json.loads(
                        (interrupted_backup / "backup.json").read_text()
                    )
                    interrupted_effect = next(
                        effect
                        for effect in interrupted["switch_journal"]["effects"]
                        if effect.get("phase") == target_phase
                        and "action_observed_state" in effect
                    )
                    self.assertEqual("intent", interrupted_effect["status"])
                    self.assertIn("produced_identity", interrupted_effect)

                    retry_options = dict(options)
                    retry_options["filesystem_adapter"] = FilesystemAdapter()
                    retry = execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=retry_options,
                        ),
                    )

                self.assertEqual("committed", retry.outcome)
                self.assertFalse(marker_path.exists())
                recovered = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                self.assertEqual("rolled_back", recovered["lifecycle"])
                self.assertEqual(
                    "recovered",
                    recovered["switch_journal"]["state"],
                )
                recovered_phases.add(target_phase)

        self.assertEqual(expected_phases, recovered_phases)

    def test_every_deterministic_directory_effect_recovers_after_action_before_applied(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptDirectoryEffect(FilesystemAdapter):
            def __init__(self, target: Path, target_phase: str) -> None:
                self.target = target
                self.target_phase = target_phase

            def ensure_directory(
                self,
                path: Path,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().ensure_directory(path, mode=mode, phase=phase)
                if path == self.target and phase == self.target_phase:
                    raise HardInterruption(f"after {phase} action")

            def sync_shared_entry(
                self,
                source: Path,
                target: Path,
                *,
                prefer_link: bool,
                phase: str,
            ) -> None:
                super().sync_shared_entry(
                    source,
                    target,
                    prefer_link=prefer_link,
                    phase=phase,
                )
                if target == self.target and phase == self.target_phase:
                    raise HardInterruption(f"after {phase} action")

        class RecoveryAuditAdapter(FilesystemAdapter):
            def __init__(self) -> None:
                self.restored_missing: set[Path] = set()

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(
                    source,
                    destination,
                    state,
                    phase=phase,
                )
                if (
                    state.get("kind") == "missing"
                    and self.capture_state(destination).get("kind") == "missing"
                ):
                    self.restored_missing.add(destination)

        for variant in ("target-home-create", "shared-directory-copy"):
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, _, _, _ = self.arrange_switch_effect_fixture(root)
                if variant == "target-home-create":
                    shutil.rmtree(store.internal_codex_home)
                    target = store.internal_codex_home
                    target_phase = "target_home_ensure"
                    profile = "internal"
                    config_mode = "snapshot"
                else:
                    source = store.internal_codex_home / "rules"
                    source.mkdir()
                    (source / "payload.json").write_text('{"stable":true}\n')
                    target = store.official_codex_home / source.name
                    target_phase = "shared_support_sync"
                    profile = "openai-official"
                    config_mode = "shared"
                options: dict[str, object] = {
                    "config_mode": config_mode,
                    "shared_config_base": None,
                    "clear_missing_auth": False,
                    "skip_shim": True,
                    "skip_app_cli": True,
                    "skip_launchctl": True,
                    "filesystem_adapter": InterruptDirectoryEffect(
                        target,
                        target_phase,
                    ),
                }
                with self.assertRaisesRegex(
                    HardInterruption,
                    f"after {target_phase} action",
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile=profile,
                            options=options,
                        ),
                    )

                marker_path = next(
                    store.root.glob(".pending-transaction-*.json")
                )
                marker = json.loads(marker_path.read_text())
                interrupted_backup = (
                    store.backups_dir / str(marker["backup_id"])
                )
                recovery_adapter = RecoveryAuditAdapter()
                retry_options = dict(options)
                retry_options["filesystem_adapter"] = recovery_adapter
                retry = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile=profile,
                        options=retry_options,
                    ),
                )

                self.assertEqual("committed", retry.outcome)
                self.assertIn(
                    target.resolve(),
                    recovery_adapter.restored_missing,
                )
                self.assertFalse(marker_path.exists())
                recovered = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                self.assertEqual("rolled_back", recovered["lifecycle"])
                self.assertEqual(
                    "recovered",
                    recovered["switch_journal"]["state"],
                )

    def test_directory_effect_rejects_identity_change_before_applied(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class ReplaceDirectoryAfterAction(FilesystemAdapter):
            def __init__(self, target: Path) -> None:
                self.target = target

            def sync_shared_entry(
                self,
                source: Path,
                target: Path,
                *,
                prefer_link: bool,
                phase: str,
            ) -> None:
                super().sync_shared_entry(
                    source,
                    target,
                    prefer_link=prefer_link,
                    phase=phase,
                )
                if target == self.target and phase == "shared_support_sync":
                    shutil.rmtree(target)
                    shutil.copytree(source, target, symlinks=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            source = store.internal_codex_home / "rules"
            source.mkdir()
            (source / "payload.json").write_text('{"stable":true}\n')
            target = store.official_codex_home / source.name

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="openai-official",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": ReplaceDirectoryAfterAction(target),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertFalse(target.exists())

    def test_only_planned_managed_wrapper_may_be_missing_at_preflight(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            managed_wrapper = store.bin_dir / "codex-internal-app"
            manifest["app_cli_path"] = str(managed_wrapper)
            manifest_path.write_text(json.dumps(manifest) + "\n")

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": False,
                        "skip_launchctl": True,
                    },
                ),
                dry_run=True,
            )

            self.assertEqual("dry_run", receipt.outcome)
            self.assertFalse(managed_wrapper.exists())
            manifest["app_cli_path"] = str(root / "other-missing-app-cli")
            manifest_path.write_text(json.dumps(manifest) + "\n")
            with self.assertRaisesRegex(SwitchError, "app_cli_path does not exist"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": False,
                            "skip_launchctl": True,
                        },
                    ),
                    dry_run=True,
                )

    def test_internal_switch_commits_receipt_wrapper_and_manifest_together(
        self,
    ) -> None:
        from codex_switch_protocol_adapter import (
            CapabilityReceipt,
            capability_receipt_path_for_launcher,
        )
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, backend, _, _ = self.arrange_switch_effect_fixture(root)
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            launcher = store.bin_dir / "codex-internal-app"
            receipt_path = capability_receipt_path_for_launcher(launcher)
            old_receipt = b'{"legacy":"receipt"}\n'
            receipt_path.write_bytes(old_receipt)
            receipt_path.chmod(0o600)
            manifest["app_cli_path"] = str(launcher)
            manifest["app_capability_receipt_path"] = str(receipt_path)
            manifest["app_capability_receipt_sha256"] = hashlib.sha256(
                old_receipt
            ).hexdigest()
            manifest["app_schema_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest) + "\n")

            result = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": False,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", result.outcome)
            payload = receipt_path.read_bytes()
            receipt = CapabilityReceipt.from_dict(json.loads(payload))
            committed_manifest = json.loads(manifest_path.read_text())
            self.assertEqual(
                hashlib.sha256(backend.read_bytes()).hexdigest(),
                receipt.backend_sha256,
            )
            self.assertEqual(
                str(receipt_path),
                committed_manifest["app_capability_receipt_path"],
            )
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                committed_manifest["app_capability_receipt_sha256"],
            )
            self.assertEqual(
                receipt.schema_sha256,
                committed_manifest["app_schema_sha256"],
            )
            launcher_text = launcher.read_text()
            self.assertIn(str(receipt_path), launcher_text)
            self.assertIn(receipt.schema_sha256, launcher_text)
            backup = json.loads(
                (
                    store.backups_dir
                    / str(result.backup_id)
                    / "backup.json"
                ).read_text()
            )
            receipt_entry = next(
                entry
                for entry in backup["entries"]
                if entry["path"] == str(receipt_path)
            )
            self.assertEqual(
                old_receipt,
                (
                    store.backups_dir
                    / str(result.backup_id)
                    / receipt_entry["payload"]
                ).read_bytes(),
            )

    def test_internal_switch_receipt_write_failure_rolls_back_full_binding(
        self,
    ) -> None:
        from codex_switch_protocol_adapter import capability_receipt_path_for_launcher
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailAfterReceiptWrite(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().write_bytes(path, data, mode=mode, phase=phase)
                if phase == "app_capability_receipt_write":
                    raise OSError("injected capability receipt write failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            manifest_path = store.manifest_path("internal")
            launcher = store.bin_dir / "codex-internal-app"
            receipt_path = capability_receipt_path_for_launcher(launcher)
            old_launcher = b"#!/bin/sh\n# prior launcher\n"
            old_receipt = b'{"legacy":"receipt"}\n'
            launcher.write_bytes(old_launcher)
            launcher.chmod(0o755)
            receipt_path.write_bytes(old_receipt)
            receipt_path.chmod(0o600)
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(launcher)
            manifest["app_capability_receipt_path"] = str(receipt_path)
            manifest["app_capability_receipt_sha256"] = hashlib.sha256(
                old_receipt
            ).hexdigest()
            manifest["app_schema_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest) + "\n")
            old_manifest = manifest_path.read_bytes()

            result = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": False,
                        "skip_launchctl": True,
                        "filesystem_adapter": FailAfterReceiptWrite(),
                    },
                ),
            )

            self.assertEqual("rolled_back", result.outcome)
            self.assertEqual(old_manifest, manifest_path.read_bytes())
            self.assertEqual(old_launcher, launcher.read_bytes())
            self.assertEqual(old_receipt, receipt_path.read_bytes())
            self.assertIn(
                "injected capability receipt write failure",
                "\n".join(result.preview_lines),
            )

    def test_shim_write_failure_rolls_back_complete_switch_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class ShimWriteFailureAdapter(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                if phase == "shim_write":
                    raise OSError("injected shim write failure")
                super().write_bytes(path, data, mode=mode, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            before_states = {
                path: capture_path_state(path) for path in observed_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": ShimWriteFailureAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertIn("injected shim write failure", "\n".join(receipt.preview_lines))

    def test_first_switch_failure_removes_new_target_home(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class ShimWriteFailureAdapter(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                if phase == "shim_write":
                    raise OSError("injected first-switch shim failure")
                super().write_bytes(path, data, mode=mode, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            shutil.rmtree(store.internal_codex_home)
            self.assertFalse(store.internal_codex_home.exists())

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "shared",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": ShimWriteFailureAdapter(),
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertFalse(store.internal_codex_home.exists())

    def test_first_shared_switch_restore_removes_created_target_home(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            shutil.rmtree(store.internal_codex_home)

            switch_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "shared",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", switch_receipt.outcome)
            self.assertTrue(store.internal_codex_home.is_dir())
            restore_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": switch_receipt.backup_id,
                        "force": False,
                    },
                ),
            )

            self.assertEqual("committed", restore_receipt.outcome)
            self.assertFalse(store.internal_codex_home.exists())

    def test_adopted_manifest_home_switch_is_restorable_and_preserves_mode(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            adopted_home = root / "adopted-internal"
            adopted_home.mkdir()
            adopted_home.chmod(0o750)
            original_config = b'model = "adopted-before"\n'
            (adopted_home / "config.toml").write_bytes(original_config)
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest.update(
                {
                    "codex_home": str(adopted_home.resolve()),
                    "home_mode": "custom",
                    "home_selection_confirmed": True,
                }
            )
            manifest_path.write_text(json.dumps(manifest) + "\n")
            store.internal_codex_home = None

            switch_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", switch_receipt.outcome)
            self.assertEqual(0o750, adopted_home.stat().st_mode & 0o777)
            self.assertNotEqual(original_config, (adopted_home / "config.toml").read_bytes())
            restore_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": switch_receipt.backup_id, "force": False},
                ),
            )

            self.assertEqual("committed", restore_receipt.outcome)
            self.assertEqual(original_config, (adopted_home / "config.toml").read_bytes())
            self.assertEqual(0o750, adopted_home.stat().st_mode & 0o777)

    def test_nested_missing_manifest_home_records_and_removes_full_created_chain(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            nested_root = root / "created-root"
            nested_home = nested_root / "parent" / "home"
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest.update(
                {
                    "codex_home": str(nested_home.resolve()),
                    "home_mode": "custom",
                    "home_selection_confirmed": True,
                }
            )
            manifest_path.write_text(json.dumps(manifest) + "\n")
            store.internal_codex_home = None

            switch_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                    },
                ),
            )

            self.assertEqual("committed", switch_receipt.outcome)
            self.assertEqual(0o700, nested_home.stat().st_mode & 0o777)
            backup = json.loads(
                (
                    store.backups_dir
                    / str(switch_receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            config_entry = next(
                entry
                for entry in backup["entries"]
                if entry["path"] == str((nested_home / "config.toml").resolve())
            )
            self.assertEqual(
                [
                    str(nested_home.resolve()),
                    str(nested_home.parent.resolve()),
                    str(nested_root.resolve()),
                ],
                config_entry["created_parent_paths"],
            )
            restore_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": switch_receipt.backup_id, "force": False},
                ),
            )

            self.assertEqual("committed", restore_receipt.outcome)
            self.assertFalse(nested_root.exists())

    def test_nested_missing_manifest_home_recovers_before_retry(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterActiveWrite(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise HardInterruption("nested home switch interrupted")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            nested_root = root / "created-recovery-root"
            nested_home = nested_root / "parent" / "home"
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest.update(
                {
                    "codex_home": str(nested_home.resolve()),
                    "home_mode": "custom",
                    "home_selection_confirmed": True,
                }
            )
            manifest_path.write_text(json.dumps(manifest) + "\n")
            store.internal_codex_home = None
            options: dict[str, object] = {
                "config_mode": "snapshot",
                "shared_config_base": None,
                "clear_missing_auth": False,
                "skip_shim": True,
                "skip_app_cli": True,
                "skip_launchctl": True,
                "filesystem_adapter": InterruptAfterActiveWrite(),
            }

            with self.assertRaisesRegex(
                HardInterruption,
                "nested home switch interrupted",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=options,
                    ),
                )

            retry_options = dict(options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options=retry_options,
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual(0o700, stat.S_IMODE(nested_home.stat().st_mode))
            self.assertFalse(any(store.root.glob(".pending-transaction-*.json")))

    def test_plist_write_failure_rolls_back_complete_switch_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class PlistWriteFailureAdapter(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                if phase == "plist_write":
                    raise OSError("injected plist write failure")
                super().write_bytes(path, data, mode=mode, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            before_states = {
                path: capture_path_state(path) for path in observed_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": PlistWriteFailureAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertIn("injected plist write failure", "\n".join(receipt.preview_lines))

    def test_split_official_desktop_failure_rolls_back_both_surfaces(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class PlistWriteFailureAdapter(FilesystemAdapter):
            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                if phase == "plist_write":
                    raise OSError("injected split plist write failure")
                super().write_bytes(path, data, mode=mode, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, internal_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            official_executable = self.make_executable(root, "codex-official-target")
            official_manifest_path = store.manifest_path("openai-official")
            official_manifest = json.loads(official_manifest_path.read_text())
            official_manifest["codex_bin"] = str(official_executable)
            official_manifest["app_cli_path"] = str(official_executable)
            official_manifest_path.write_text(json.dumps(official_manifest) + "\n")
            protected_paths = (*observed_paths, official_manifest_path)
            before_states = {
                path: capture_path_state(path) for path in protected_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "app_profile": "openai-official",
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": PlistWriteFailureAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in protected_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            self.assertNotEqual(str(internal_executable), runner.gui_env)
            self.assertIn(
                "injected split plist write failure",
                "\n".join(receipt.preview_lines),
            )

    def test_gui_setenv_failure_rolls_back_complete_switch_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            before_states = {
                path: capture_path_state(path) for path in observed_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
                fail_on_occurrence={"setenv": 1},
                mutate_before_failure_on_occurrence={"setenv": 1},
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": FilesystemAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            self.assertIn(f"setenv:{target_executable}", runner.events)
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertIn("injected setenv failure", "\n".join(receipt.preview_lines))

    def test_bootout_failure_rolls_back_complete_switch_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            before_states = {
                path: capture_path_state(path) for path in observed_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
                fail_on_occurrence={"bootout": 1},
                mutate_before_failure_on_occurrence={"bootout": 1},
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": FilesystemAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            self.assertIn("bootout", runner.events)
            self.assertEqual(1, runner.events.count("bootstrap"))
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertIn("injected bootout failure", "\n".join(receipt.preview_lines))

    def test_bootstrap_failure_rolls_back_complete_switch_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            before_states = {
                path: capture_path_state(path) for path in observed_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
                fail_on_occurrence={"bootstrap": 1},
                mutate_before_failure_on_occurrence={"bootstrap": 1},
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": FilesystemAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            self.assertEqual(2, runner.events.count("bootstrap"))
            self.assertEqual(2, runner.events.count("bootout"))
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertIn("injected bootstrap failure", "\n".join(receipt.preview_lines))

    def test_active_write_failure_rolls_back_complete_switch_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class ActiveWriteFailureAdapter(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise OSError("injected active write failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            before_states = {
                path: capture_path_state(path) for path in observed_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": ActiveWriteFailureAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            self.assertEqual(2, runner.events.count("bootout"))
            self.assertEqual(2, runner.events.count("bootstrap"))
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertIn("injected active write failure", "\n".join(receipt.preview_lines))

    def test_switch_rollback_publishes_atomic_terminal_manifest(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailAfterActiveWrite(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise OSError("injected terminal rollback trigger")

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": FailAfterActiveWrite(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            backup_dir = store.backups_dir / str(receipt.backup_id)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertEqual("recovered", manifest["switch_journal"]["state"])
            self.assertEqual(
                tuple(),
                tuple(store.root.glob(".pending-transaction-*.json")),
            )

    def test_rolled_back_switch_marker_cleanup_warning_names_actual_outcome(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class FailApplyAndMarkerCleanup(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise OSError("injected rollback trigger")

            def durable_unlink(self, path: Path, *, phase: str) -> None:
                if phase == "pending_marker_remove":
                    raise OSError("injected marker cleanup failure")
                super().durable_unlink(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": FailApplyAndMarkerCleanup(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIn(
                "rolled_back; pending recovery marker retained",
                "\n".join(receipt.preview_lines),
            )
            self.assertEqual(
                1,
                len(tuple(store.root.glob(".pending-transaction-*.json"))),
            )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            retry_request = TransactionRequest(
                operation="switch",
                profile="internal",
                options={
                    "config_mode": "snapshot",
                    "shared_config_base": None,
                    "clear_missing_auth": False,
                    "skip_shim": True,
                    "skip_app_cli": True,
                    "skip_launchctl": True,
                    "filesystem_adapter": FilesystemAdapter(),
                },
            )
            before_dry_run = capture_path_state(store.root)

            dry_run = execute_transaction(store, retry_request, dry_run=True)

            self.assertEqual("dry_run", dry_run.outcome)
            self.assertEqual(before_dry_run, capture_path_state(store.root))
            self.assertTrue(marker_path.exists())

            retry = execute_transaction(store, retry_request)
            self.assertEqual("committed", retry.outcome)
            self.assertFalse(marker_path.exists())

    def test_committed_marker_cleanup_warning_is_rendered_by_switch_cli(
        self,
    ) -> None:
        from codex_switch_switching import switch_profile
        from codex_switch_transaction import FilesystemAdapter

        original_unlink = FilesystemAdapter.durable_unlink

        def fail_marker_cleanup(
            adapter: FilesystemAdapter,
            path: Path,
            *,
            phase: str,
        ) -> None:
            if phase == "pending_marker_remove":
                raise OSError("injected committed marker cleanup failure")
            original_unlink(adapter, path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))
            output = io.StringIO()

            with patch.object(
                FilesystemAdapter,
                "durable_unlink",
                fail_marker_cleanup,
            ), redirect_stdout(output):
                switch_profile(
                    store,
                    "internal",
                    dry_run=False,
                    clear_missing_auth=False,
                    config_mode="snapshot",
                    shared_config_base=None,
                    skip_shim=True,
                    skip_app_cli=True,
                    skip_launchctl=True,
                )

            rendered = output.getvalue()
            self.assertIn("Switched to profile internal\n", rendered)
            self.assertIn("Backup: ", rendered)
            self.assertIn(
                "committed; pending recovery marker retained at ",
                rendered,
            )
            self.assertIn(
                "the next applying command will retry cleanup",
                rendered,
            )
            self.assertEqual(
                1,
                len(tuple(store.root.glob(".pending-transaction-*.json"))),
            )

    def test_restore_committed_marker_cleanup_failure_is_retryable_and_retires_marker(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailMarkerCleanup(FilesystemAdapter):
            def durable_unlink(self, path: Path, *, phase: str) -> None:
                if phase == "pending_marker_remove":
                    raise OSError("injected restore marker cleanup failure")
                super().durable_unlink(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, historical = self.arrange_restore_parent_cleanup_fixture(
                Path(temp_dir)
            )
            request = TransactionRequest(
                operation="restore",
                profile="",
                options={
                    "backup_id": historical.name,
                    "force": False,
                    "filesystem_adapter": FailMarkerCleanup(),
                },
            )

            receipt = execute_transaction(store, request)

            self.assertEqual("committed", receipt.outcome)
            self.assertIn(
                "committed; pending recovery marker retained",
                "\n".join(receipt.preview_lines),
            )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))

            retry = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="",
                    options={
                        "backup_id": historical.name,
                        "force": True,
                        "filesystem_adapter": FilesystemAdapter(),
                    },
                ),
            )

            self.assertEqual("committed", retry.outcome)
            self.assertFalse(marker_path.exists())

    def test_supported_switch_contention_is_byte_identical(self) -> None:
        from codex_switch_switching import switch_profile
        from codex_switch_transaction import capture_path_state

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, observed_paths = self.arrange_switch_effect_fixture(
                Path(temp_dir)
            )
            before_store = capture_path_state(store.root)
            before_paths = {
                path: capture_path_state(path) for path in observed_paths
            }
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_directory_lock,
                args=(str(store.root), ready, release),
            )
            holder.start()
            self.assertTrue(ready.wait(10), "lock holder did not become ready")
            try:
                with self.assertRaisesRegex(SwitchError, "profile store is busy"):
                    with redirect_stdout(io.StringIO()):
                        switch_profile(
                            store,
                            "internal",
                            dry_run=False,
                            clear_missing_auth=False,
                            config_mode="snapshot",
                            shared_config_base=None,
                            skip_shim=True,
                            skip_app_cli=True,
                            skip_launchctl=True,
                        )
            finally:
                release.set()
                holder.join(10)
                if holder.is_alive():
                    holder.terminate()
                    holder.join()

            self.assertEqual(0, holder.exitcode)
            self.assertEqual(before_store, capture_path_state(store.root))
            self.assertEqual(
                before_paths,
                {path: capture_path_state(path) for path in observed_paths},
            )

    def test_pending_capture_blocks_custom_before_write(self) -> None:
        from codex_switch_switching import switch_profile
        from codex_switch_transaction import capture_path_state

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            (store.official_codex_home / "config.toml").write_text(
                'model = "before"\n'
            )
            journal = store.profiles_dir / ".internal.capture-journal.json"
            journal.write_text("{corrupt\n")
            before_store = capture_path_state(store.root)
            before_home = capture_path_state(store.official_codex_home)

            with self.assertRaisesRegex(
                SwitchError,
                "Pending capture recovery blocks custom switch for profile internal",
            ):
                with redirect_stdout(io.StringIO()):
                    switch_profile(
                        store,
                        "custom",
                        dry_run=False,
                        clear_missing_auth=False,
                        config_mode="snapshot",
                        shared_config_base=None,
                        skip_shim=True,
                        skip_app_cli=True,
                        skip_launchctl=True,
                    )

            self.assertEqual(before_store, capture_path_state(store.root))
            self.assertEqual(before_home, capture_path_state(store.official_codex_home))
            self.assertFalse(any(store.root.glob(".pending-transaction-*.json")))

    def test_custom_gate_retires_valid_committed_terminal_marker(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            custom_switch_mutation_gate,
            execute_transaction,
        )

        class FailMarkerCleanup(FilesystemAdapter):
            def durable_unlink(self, path: Path, *, phase: str) -> None:
                if phase == "pending_marker_remove":
                    raise OSError("injected marker cleanup failure")
                super().durable_unlink(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": FailMarkerCleanup(),
                    },
                ),
            )
            self.assertEqual("committed", receipt.outcome)
            marker_path = next(store.root.glob(".pending-transaction-*.json"))

            before_dry_run = capture_path_state(store.root)
            with custom_switch_mutation_gate(store, dry_run=True):
                self.assertTrue(marker_path.exists())
            self.assertEqual(before_dry_run, capture_path_state(store.root))

            with custom_switch_mutation_gate(store):
                self.assertFalse(marker_path.exists())

    def test_switch_rollback_effects_are_durable_before_rolled_back_terminal_write(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class RollbackDurabilityAdapter(FilesystemAdapter):
            def __init__(self) -> None:
                self.events: list[tuple[str, str, str]] = []

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(
                    source,
                    destination,
                    state,
                    phase=phase,
                )
                self.events.append(("materialize", phase, str(destination)))

            def sync_file(self, path: Path, *, phase: str) -> None:
                self.events.append(("sync_file", phase, str(path)))
                super().sync_file(path, phase=phase)

            def sync_tree(
                self,
                path: Path,
                *,
                file_phase: str,
                directory_phase: str,
            ) -> None:
                self.events.append(("sync_tree", directory_phase, str(path)))
                super().sync_tree(
                    path,
                    file_phase=file_phase,
                    directory_phase=directory_phase,
                )

            def sync_directory(self, path: Path, *, phase: str) -> None:
                self.events.append(("sync_directory", phase, str(path)))
                super().sync_directory(path, phase=phase)

            def durable_unlink(self, path: Path, *, phase: str) -> None:
                self.events.append(("durable_unlink", phase, str(path)))
                super().durable_unlink(path, phase=phase)

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.events.append(("manifest", phase, str(path)))
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise OSError("injected rollback durability trigger")

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))
            adapter = RollbackDurabilityAdapter()

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            terminal_index = adapter.events.index(
                (
                    "manifest",
                    "switch_rolled_back_finalize",
                    str(store.backups_dir / str(receipt.backup_id) / "backup.json"),
                )
            )
            rollback_writes = [
                (index, phase, path)
                for index, (kind, phase, path) in enumerate(adapter.events)
                if kind == "materialize" and phase.startswith("switch_rollback_")
            ]
            self.assertTrue(rollback_writes)
            for materialize_index, phase, path in rollback_writes:
                parent_sync = (
                    "sync_directory",
                    f"{phase}_parent",
                    str(Path(path).parent),
                )
                self.assertIn(parent_sync, adapter.events)
                self.assertLess(materialize_index, adapter.events.index(parent_sync))
                self.assertLess(adapter.events.index(parent_sync), terminal_index)
            terminal_file_sync = (
                "sync_file",
                "switch_rolled_back_terminal_manifest",
                str(store.backups_dir / str(receipt.backup_id) / "backup.json"),
            )
            terminal_dir_sync = (
                "sync_directory",
                "switch_rolled_back_terminal_backup",
                str(store.backups_dir / str(receipt.backup_id)),
            )
            cleanup_index = next(
                index
                for index, event in enumerate(adapter.events)
                if event[0:2] == ("durable_unlink", "pending_marker_remove")
            )
            self.assertLess(terminal_index, adapter.events.index(terminal_file_sync))
            self.assertLess(
                adapter.events.index(terminal_file_sync),
                adapter.events.index(terminal_dir_sync),
            )
            self.assertLess(adapter.events.index(terminal_dir_sync), cleanup_index)
            terminal_manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertTrue(terminal_manifest["switch_journal"]["effects"])
            self.assertTrue(
                all(
                    effect.get("recovery_state") == "recovered"
                    for effect in terminal_manifest["switch_journal"]["effects"]
                )
            )

    def test_prepared_switch_recovery_effects_are_durable_before_recovered_terminal_write(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class RecoveryDurabilityAdapter(FilesystemAdapter):
            def __init__(self) -> None:
                self.events: list[tuple[str, str, str]] = []

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(
                    source,
                    destination,
                    state,
                    phase=phase,
                )
                self.events.append(("materialize", phase, str(destination)))

            def sync_file(self, path: Path, *, phase: str) -> None:
                self.events.append(("sync_file", phase, str(path)))
                super().sync_file(path, phase=phase)

            def sync_tree(
                self,
                path: Path,
                *,
                file_phase: str,
                directory_phase: str,
            ) -> None:
                self.events.append(("sync_tree", directory_phase, str(path)))
                super().sync_tree(
                    path,
                    file_phase=file_phase,
                    directory_phase=directory_phase,
                )

            def sync_directory(self, path: Path, *, phase: str) -> None:
                self.events.append(("sync_directory", phase, str(path)))
                super().sync_directory(path, phase=phase)

            def durable_unlink(self, path: Path, *, phase: str) -> None:
                self.events.append(("durable_unlink", phase, str(path)))
                super().durable_unlink(path, phase=phase)

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.events.append(("manifest", phase, str(path)))
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, backup_dir, options = self.arrange_pending_switch(root)
            adapter = RecoveryDurabilityAdapter()
            retry_options = dict(options)
            retry_options["filesystem_adapter"] = adapter

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options=retry_options,
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            terminal_event = (
                "manifest",
                "switch_recovery_finalize",
                str(backup_dir / "backup.json"),
            )
            terminal_index = adapter.events.index(terminal_event)
            recovery_writes = [
                (index, phase, path)
                for index, (kind, phase, path) in enumerate(adapter.events)
                if kind == "materialize" and phase.startswith("switch_recovery_")
            ]
            self.assertTrue(recovery_writes)
            for materialize_index, phase, path in recovery_writes:
                parent_sync = (
                    "sync_directory",
                    f"{phase}_parent",
                    str(Path(path).parent),
                )
                self.assertIn(parent_sync, adapter.events)
                self.assertLess(materialize_index, adapter.events.index(parent_sync))
                self.assertLess(adapter.events.index(parent_sync), terminal_index)
            terminal_file_sync = (
                "sync_file",
                "switch_recovery_terminal_manifest",
                str(backup_dir / "backup.json"),
            )
            terminal_dir_sync = (
                "sync_directory",
                "switch_recovery_terminal_backup",
                str(backup_dir),
            )
            cleanup_index = next(
                index
                for index, event in enumerate(adapter.events)
                if event[0:2] == ("durable_unlink", "pending_marker_remove")
            )
            self.assertLess(terminal_index, adapter.events.index(terminal_file_sync))
            self.assertLess(
                adapter.events.index(terminal_file_sync),
                adapter.events.index(terminal_dir_sync),
            )
            self.assertLess(adapter.events.index(terminal_dir_sync), cleanup_index)
            recovered_manifest = json.loads((backup_dir / "backup.json").read_text())
            self.assertTrue(recovered_manifest["switch_journal"]["effects"])
            self.assertTrue(
                all(
                    effect.get("recovery_state") == "recovered"
                    for effect in recovered_manifest["switch_journal"]["effects"]
                )
            )

    def test_switch_rollback_marker_cleanup_failure_is_retryable_and_retires_marker(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailAfterMarkerUnlink(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise OSError("injected rollback trigger")

            def durable_unlink(self, path: Path, *, phase: str) -> None:
                if phase == "pending_marker_remove":
                    path.unlink()
                    raise OSError("injected marker parent-sync failure")
                super().durable_unlink(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": FailAfterMarkerUnlink(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIn(
                "rolled_back; pending recovery marker retained",
                "\n".join(receipt.preview_lines),
            )
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            retry = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": FilesystemAdapter(),
                    },
                ),
            )

            self.assertEqual("committed", retry.outcome)
            self.assertFalse(marker_path.exists())

    def test_prepared_switch_recovery_preflights_target_home_ensure_identity_before_any_write(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            SwitchError,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterActive(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise HardInterruption("pending missing-home switch")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            shutil.rmtree(store.internal_codex_home)
            request = TransactionRequest(
                operation="switch",
                profile="internal",
                options={
                    "config_mode": "snapshot",
                    "shared_config_base": None,
                    "clear_missing_auth": False,
                    "skip_shim": True,
                    "skip_app_cli": True,
                    "skip_launchctl": True,
                    "filesystem_adapter": InterruptAfterActive(),
                },
            )
            with self.assertRaisesRegex(
                HardInterruption,
                "pending missing-home switch",
            ):
                execute_transaction(store, request)

            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            backup_dir = store.backups_dir / str(marker["backup_id"])
            manifest_path = backup_dir / "backup.json"
            manifest = json.loads(manifest_path.read_text())
            ensure_effect = next(
                effect
                for effect in manifest["switch_journal"]["effects"]
                if effect.get("phase") == "target_home_ensure"
                and effect.get("path") == str(store.internal_codex_home)
            )
            self.assertEqual("missing", ensure_effect["before_state"]["kind"])
            ensure_effect["produced_identity"]["inode"] += 1
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            before = capture_path_state(root)
            retry_options = dict(request.options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()

            with self.assertRaisesRegex(SwitchError, "directory identity changed"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(before, capture_path_state(root))
            self.assertTrue(marker_path.exists())

    def test_prepared_switch_recovery_accepts_desktop_already_restored_without_reconcile(
        self,
    ) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            SwitchError,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterSetenvRunner(_FakeLaunchctlRunner):
            def __call__(
                self,
                command: list[str],
                env: dict[str, str] | None = None,
            ) -> tuple[int, str]:
                result = super().__call__(command, env)
                if command[1] == "setenv" and self.occurrences["setenv"] == 1:
                    raise HardInterruption("pending Desktop switch")
                return result

        class CountingDesktopAdapter(_DesktopBindingAdapter):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                self.reconcile_calls = 0

            def reconcile(self, observation: object, *, skip_launchctl: bool) -> None:
                self.reconcile_calls += 1
                super().reconcile(observation, skip_launchctl=skip_launchctl)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, prior_executable, _ = self.arrange_switch_effect_fixture(root)
            first_runner = InterruptAfterSetenvRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            first_desktop = _DesktopBindingAdapter(
                store,
                runner=first_runner,
                uid_provider=lambda: 501,
            )
            with self.assertRaisesRegex(HardInterruption, "pending Desktop switch"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": FilesystemAdapter(),
                            "desktop_binding_adapter": first_desktop,
                        },
                    ),
                )

            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            interrupted_backup = store.backups_dir / str(marker["backup_id"])
            retry_runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            retry_desktop = CountingDesktopAdapter(
                store,
                runner=retry_runner,
                uid_provider=lambda: 501,
            )

            with self.assertRaisesRegex(SwitchError, "cannot capture"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": root / "missing-source",
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": True,
                            "overwrite": True,
                            "filesystem_adapter": FilesystemAdapter(),
                            "desktop_binding_adapter": retry_desktop,
                        },
                    ),
                )

            self.assertEqual(0, retry_desktop.reconcile_calls)
            self.assertEqual(str(prior_executable), retry_runner.gui_env)
            self.assertTrue(retry_runner.service_loaded)
            self.assertFalse(marker_path.exists())
            recovered = json.loads((interrupted_backup / "backup.json").read_text())
            self.assertEqual("rolled_back", recovered["lifecycle"])
            self.assertEqual("recovered", recovered["switch_journal"]["state"])

    def test_switch_prepared_marker_intent_and_applied_interruptions_recover(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        for interruption_point in ("prepared", "marker", "intent", "applied"):
            with (
                self.subTest(interruption_point=interruption_point),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                class InterruptAtCheckpoint(FilesystemAdapter):
                    def write_manifest(
                        self,
                        path: Path,
                        data: dict[str, object],
                        *,
                        phase: str,
                    ) -> None:
                        super().write_manifest(path, data, phase=phase)
                        journal = data.get("switch_journal")
                        effects = (
                            journal.get("effects")
                            if isinstance(journal, dict)
                            else None
                        )
                        latest = (
                            effects[-1]
                            if isinstance(effects, list)
                            and effects
                            and isinstance(effects[-1], dict)
                            else None
                        )
                        should_interrupt = (
                            interruption_point == "prepared"
                            and phase == "switch_journal_prepare"
                        ) or (
                            interruption_point == "marker"
                            and phase == "pending_marker_publish"
                        ) or (
                            interruption_point == "intent"
                            and phase == "switch_journal_intent"
                            and isinstance(latest, dict)
                            and latest.get("status") == "intent"
                        ) or (
                            interruption_point == "applied"
                            and phase == "switch_journal_applied"
                            and isinstance(latest, dict)
                            and latest.get("status") == "applied"
                        )
                        if should_interrupt:
                            raise HardInterruption(
                                f"interrupted after {interruption_point} checkpoint"
                            )

                root = Path(temp_dir)
                store, _, _, _ = self.arrange_switch_effect_fixture(root)
                options: dict[str, object] = {
                    "config_mode": "snapshot",
                    "shared_config_base": None,
                    "clear_missing_auth": False,
                    "skip_shim": True,
                    "skip_app_cli": True,
                    "skip_launchctl": True,
                    "filesystem_adapter": InterruptAtCheckpoint(),
                }
                with self.assertRaisesRegex(
                    HardInterruption,
                    interruption_point,
                ):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options=options,
                        ),
                    )

                interrupted_backup = next(store.backups_dir.iterdir())
                interrupted = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                self.assertEqual("prepared", interrupted["lifecycle"])
                if interruption_point == "prepared":
                    self.assertEqual(
                        tuple(),
                        tuple(store.root.glob(".pending-transaction-*.json")),
                    )
                else:
                    self.assertEqual(
                        1,
                        len(tuple(store.root.glob(".pending-transaction-*.json"))),
                    )

                retry_options = dict(options)
                retry_options["filesystem_adapter"] = FilesystemAdapter()
                retry = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=retry_options,
                    ),
                )

                self.assertEqual("committed", retry.outcome)
                recovered = json.loads(
                    (interrupted_backup / "backup.json").read_text()
                )
                self.assertEqual("rolled_back", recovered["lifecycle"])
                self.assertEqual("recovered", recovered["switch_journal"]["state"])
                self.assertEqual(
                    tuple(),
                    tuple(store.root.glob(".pending-transaction-*.json")),
                )

    def test_prepared_switch_recovery_durability_interruption_is_idempotent(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterRecoverySync(FilesystemAdapter):
            def sync_directory(self, path: Path, *, phase: str) -> None:
                super().sync_directory(path, phase=phase)
                if phase == "switch_recovery_0_parent":
                    raise HardInterruption(
                        "interrupted after first durable recovery effect"
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, marker_path, backup_dir, options = self.arrange_pending_switch(root)
            interrupted_options = dict(options)
            interrupted_options["filesystem_adapter"] = (
                InterruptAfterRecoverySync()
            )
            with self.assertRaisesRegex(
                HardInterruption,
                "durable recovery effect",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options=interrupted_options,
                    ),
                )

            self.assertTrue(marker_path.exists())
            interrupted = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual("prepared", interrupted["lifecycle"])
            retry_options = dict(options)
            retry_options["filesystem_adapter"] = FilesystemAdapter()

            retry = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options=retry_options,
                ),
            )

            self.assertEqual("committed", retry.outcome)
            self.assertFalse(marker_path.exists())
            recovered = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", recovered["lifecycle"])
            self.assertEqual("recovered", recovered["switch_journal"]["state"])

    def test_switch_rollback_durability_failure_retains_rollback_failed_evidence(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailRollbackDurability(FilesystemAdapter):
            def __init__(self) -> None:
                self.failed_sync = False

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise OSError("injected switch failure")

            def sync_directory(self, path: Path, *, phase: str) -> None:
                super().sync_directory(path, phase=phase)
                if phase.startswith("switch_rollback_") and not self.failed_sync:
                    self.failed_sync = True
                    raise OSError("injected rollback durability failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": FailRollbackDurability(),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            self.assertTrue(marker_path.exists())
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rollback_failed", manifest["lifecycle"])
            self.assertEqual(
                "rollback_failed",
                manifest["switch_journal"]["state"],
            )
            self.assertTrue(
                all(
                    effect.get("recovery_state") == "rollback_failed"
                    for effect in manifest["switch_journal"]["effects"]
                )
            )

    def test_restore_parent_cleanup_effect_is_journaled_and_durable_before_commit(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class CleanupAuditAdapter(FilesystemAdapter):
            def __init__(self) -> None:
                self.events: list[str] = []

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                journal = data.get("restore_journal")
                effects = (
                    journal.get("effects") if isinstance(journal, dict) else None
                )
                latest = (
                    effects[-1]
                    if isinstance(effects, list)
                    and effects
                    and isinstance(effects[-1], dict)
                    else None
                )
                if (
                    isinstance(latest, dict)
                    and latest.get("phase") == "restore_parent_cleanup"
                ):
                    self.events.append(
                        f"journal:{phase}:{latest.get('status')}"
                    )
                if phase == "committed_manifest":
                    self.events.append("terminal")
                super().write_manifest(path, data, phase=phase)

            def remove_empty_dir(self, path: Path, *, phase: str) -> None:
                self.events.append(f"remove:{phase}")
                super().remove_empty_dir(path, phase=phase)

            def sync_directory(self, path: Path, *, phase: str) -> None:
                self.events.append(f"sync:{phase}")
                super().sync_directory(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            store, parent, target, historical = (
                self.arrange_restore_parent_cleanup_fixture(Path(temp_dir))
            )
            adapter = CleanupAuditAdapter()
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="",
                    options={
                        "backup_id": historical.name,
                        "force": False,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertFalse(target.exists())
            self.assertFalse(parent.exists())
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            cleanup_effect = next(
                effect
                for effect in manifest["restore_journal"]["effects"]
                if effect.get("phase") == "restore_parent_cleanup"
            )
            self.assertEqual("filesystem", cleanup_effect["kind"])
            self.assertEqual("applied", cleanup_effect["status"])
            self.assertEqual(str(parent.resolve()), cleanup_effect["path"])
            self.assertEqual(0o751, cleanup_effect["before_state"]["mode"])
            self.assertEqual("missing", cleanup_effect["planned_after_state"]["kind"])
            self.assertEqual("missing", cleanup_effect["observed_after_state"]["kind"])
            self.assertEqual(
                str(parent.resolve()),
                cleanup_effect["before_identity"]["path"],
            )
            intent_index = adapter.events.index(
                "journal:restore_journal_intent:intent"
            )
            remove_index = adapter.events.index("remove:restore_parent_cleanup")
            sync_index = adapter.events.index("sync:restore_parent_cleanup_parent")
            applied_index = adapter.events.index(
                "journal:restore_journal_applied:applied"
            )
            terminal_index = adapter.events.index("terminal")
            self.assertLess(intent_index, remove_index)
            self.assertLess(remove_index, sync_index)
            self.assertLess(sync_index, applied_index)
            self.assertLess(applied_index, terminal_index)

    def test_restore_parent_cleanup_failure_restores_removed_parent_and_prior_mode(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailAfterParentRemoval(FilesystemAdapter):
            def remove_empty_dir(self, path: Path, *, phase: str) -> None:
                super().remove_empty_dir(path, phase=phase)
                if phase in {"apply_parent_cleanup", "restore_parent_cleanup"}:
                    raise OSError("injected cleanup failure after removal")

        with tempfile.TemporaryDirectory() as temp_dir:
            store, parent, target, historical = (
                self.arrange_restore_parent_cleanup_fixture(Path(temp_dir))
            )
            before_target = target.read_bytes()

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="",
                    options={
                        "backup_id": historical.name,
                        "force": False,
                        "filesystem_adapter": FailAfterParentRemoval(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(before_target, target.read_bytes())
            self.assertEqual(0o751, stat.S_IMODE(parent.stat().st_mode))
            self.assertEqual(
                tuple(),
                tuple(store.root.glob(".pending-transaction-*.json")),
            )
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            cleanup_effect = next(
                effect
                for effect in manifest["restore_journal"]["effects"]
                if effect.get("phase") == "restore_parent_cleanup"
            )
            self.assertEqual("recovered", cleanup_effect["recovery_state"])

    def test_restore_parent_cleanup_hard_interruption_recovers_idempotently(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptAfterParentRemoval(FilesystemAdapter):
            def remove_empty_dir(self, path: Path, *, phase: str) -> None:
                super().remove_empty_dir(path, phase=phase)
                if phase in {"apply_parent_cleanup", "restore_parent_cleanup"}:
                    raise HardInterruption("after parent cleanup removal")

        class InterruptAfterParentRecreation(FilesystemAdapter):
            def ensure_directory(
                self,
                path: Path,
                *,
                mode: int,
                phase: str,
            ) -> None:
                super().ensure_directory(path, mode=mode, phase=phase)
                if phase.startswith("restore_recovery_parent_"):
                    raise HardInterruption("after parent cleanup recreation")

        with tempfile.TemporaryDirectory() as temp_dir:
            store, parent, target, historical = (
                self.arrange_restore_parent_cleanup_fixture(Path(temp_dir))
            )
            before_target = target.read_bytes()
            with self.assertRaisesRegex(HardInterruption, "cleanup removal"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": historical.name,
                            "force": False,
                            "filesystem_adapter": InterruptAfterParentRemoval(),
                        },
                    ),
                )

            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            safety_dir = store.backups_dir / str(marker["backup_id"])
            self.assertFalse(parent.exists())
            with self.assertRaisesRegex(HardInterruption, "cleanup recreation"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-first-recovery",
                            "force": False,
                            "filesystem_adapter": InterruptAfterParentRecreation(),
                        },
                    ),
                )

            self.assertTrue(marker_path.exists())
            self.assertTrue(parent.is_dir())
            self.assertEqual(0o751, stat.S_IMODE(parent.stat().st_mode))
            with self.assertRaisesRegex(SwitchError, "Backup not found"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-second-recovery",
                            "force": False,
                            "filesystem_adapter": FilesystemAdapter(),
                        },
                    ),
                )

            self.assertEqual(before_target, target.read_bytes())
            self.assertEqual(0o751, stat.S_IMODE(parent.stat().st_mode))
            self.assertFalse(marker_path.exists())
            recovered = json.loads((safety_dir / "backup.json").read_text())
            self.assertEqual("rolled_back", recovered["lifecycle"])
            self.assertEqual("recovered", recovered["restore_journal"]["state"])

    def test_prepared_restore_recovery_preflights_parent_cleanup_identity_before_any_write(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            SwitchError,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class HardInterruption(BaseException):
            pass

        class InterruptBeforeParentCleanup(FilesystemAdapter):
            def remove_empty_dir(self, path: Path, *, phase: str) -> None:
                if phase in {"apply_parent_cleanup", "restore_parent_cleanup"}:
                    raise HardInterruption("before parent cleanup removal")
                super().remove_empty_dir(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, parent, target, historical = (
                self.arrange_restore_parent_cleanup_fixture(root)
            )
            with self.assertRaisesRegex(HardInterruption, "cleanup removal"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": historical.name,
                            "force": False,
                            "filesystem_adapter": InterruptBeforeParentCleanup(),
                        },
                    ),
                )

            self.assertFalse(target.exists())
            self.assertTrue(parent.is_dir())
            marker_path = next(store.root.glob(".pending-transaction-*.json"))
            marker = json.loads(marker_path.read_text())
            safety_dir = store.backups_dir / str(marker["backup_id"])
            manifest_path = safety_dir / "backup.json"
            manifest = json.loads(manifest_path.read_text())
            cleanup_effect = next(
                effect
                for effect in manifest["restore_journal"]["effects"]
                if effect.get("phase") == "restore_parent_cleanup"
            )
            cleanup_effect["before_identity"]["inode"] += 1
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            before = capture_path_state(root)

            with self.assertRaisesRegex(
                SwitchError,
                "parent cleanup identity changed",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": "missing-after-preflight-rejection",
                            "force": False,
                            "filesystem_adapter": FilesystemAdapter(),
                        },
                    ),
                )

            self.assertEqual(before, capture_path_state(root))
            self.assertTrue(marker_path.exists())

    def test_planning_reads_are_frozen_atomically_for_every_switch_input(
        self,
    ) -> None:
        from codex_switch_home_sync import (
            desktop_global_state_path,
            plugin_support_snapshot_name,
            shared_support_entries,
            stale_runtime_links,
        )
        from codex_switch_launch import validate_executable_path
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        text_cases = (
            "manifest",
            "active",
            "profile_config",
            "base_config",
            "target_config",
            "plugin_snapshot",
            "composite_config",
            "desktop_source",
            "desktop_target",
            "shell_profile",
        )
        for case in text_cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store, _, _, _ = (
                    self.arrange_switch_effect_fixture(root)
                )
                config_mode = "shared" if case in {
                    "plugin_snapshot",
                    "composite_config",
                    "desktop_source",
                    "desktop_target",
                } else "snapshot"
                watched: Path
                replacement: str
                extra_context = nullcontext()
                skip_shim = True
                if case == "manifest":
                    watched = store.manifest_path("internal")
                    manifest = json.loads(watched.read_text())
                    manifest["external_generation"] = 2
                    replacement = json.dumps(manifest, sort_keys=True) + "\n"
                elif case == "active":
                    watched = store.active_path
                    active = json.loads(watched.read_text())
                    active["external_generation"] = 2
                    replacement = json.dumps(active, sort_keys=True) + "\n"
                elif case == "profile_config":
                    watched = store.profile_dir("internal") / "config.toml"
                    replacement = (
                        'model = "external-profile"\n'
                        'cli_auth_credentials_store = "file"\n'
                    )
                elif case == "base_config":
                    watched = store.official_codex_home / "config.toml"
                    replacement = (
                        'model = "external-base"\n[features]\nmemory = false\n'
                    )
                elif case == "target_config":
                    watched = store.internal_codex_home / "config.toml"
                    replacement = 'model = "external-target"\n'
                elif case == "plugin_snapshot":
                    watched = (
                        store.profile_dir("internal")
                        / plugin_support_snapshot_name("internal")
                    )
                    watched.write_text('[features]\napps = true\n')
                    replacement = '[features]\napps = false\n'
                elif case == "composite_config":
                    watched = store.internal_codex_home / "internal.config.toml"
                    replacement = 'model = "external-composite"\n'
                elif case in {"desktop_source", "desktop_target"}:
                    desktop_source = desktop_global_state_path(
                        store.official_codex_home
                    )
                    desktop_target = desktop_global_state_path(
                        store.internal_codex_home
                    )
                    desktop_source.write_text(
                        json.dumps({"appshotHotkey": "source-old"}) + "\n"
                    )
                    desktop_target.write_text(
                        json.dumps(
                            {
                                "appshotHotkey": "target-old",
                                "unrelated": "preserve-me",
                            }
                        )
                        + "\n"
                    )
                    watched = (
                        desktop_source
                        if case == "desktop_source"
                        else desktop_target
                    )
                    replacement = (
                        json.dumps(
                            {
                                "appshotHotkey": "source-new",
                                "external_generation": 2,
                            }
                        )
                        + "\n"
                        if case == "desktop_source"
                        else json.dumps(
                            {
                                "appshotHotkey": "target-new",
                                "unrelated": "newer-target-value",
                            }
                        )
                        + "\n"
                    )
                else:
                    watched = root / "shell-profile"
                    watched.write_text("# shell-before\n")
                    replacement = "# shell-external-new\n"
                    skip_shim = False
                    extra_context = patch(
                        "codex_switch_shell.shell_cli_bootstrap_path",
                        return_value=watched,
                    )

                original_read_text = Path.read_text
                changed = False

                def mutate_after_read(
                    path: Path,
                    *args: object,
                    **kwargs: object,
                ) -> str:
                    nonlocal changed
                    result = original_read_text(path, *args, **kwargs)
                    if path == watched and not changed:
                        changed = True
                        path.write_text(replacement)
                    return result

                options: dict[str, object] = {
                    "config_mode": config_mode,
                    "shared_config_base": None,
                    "clear_missing_auth": False,
                    "skip_shim": skip_shim,
                    "skip_app_cli": True,
                    "skip_launchctl": True,
                    "filesystem_adapter": FilesystemAdapter(),
                }
                with extra_context, patch.object(
                    Path,
                    "read_text",
                    new=mutate_after_read,
                ):
                    with self.assertRaises(SwitchError):
                        execute_transaction(
                            store,
                            TransactionRequest(
                                operation="switch",
                                profile="internal",
                                options=options,
                            ),
                        )

                self.assertTrue(changed)
                self.assertEqual(replacement, original_read_text(watched))
                self.assertEqual(tuple(), tuple(store.backups_dir.iterdir()))

        with self.subTest(case="auth"), tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            watched = store.profile_dir("internal") / "auth.json"
            replacement = b'{"internal":"external-new"}\n'
            original_read_bytes = Path.read_bytes
            changed = False

            def mutate_after_bytes(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> bytes:
                nonlocal changed
                result = original_read_bytes(path, *args, **kwargs)
                if path == watched and not changed:
                    changed = True
                    path.write_bytes(replacement)
                return result

            with patch.object(Path, "read_bytes", new=mutate_after_bytes):
                with self.assertRaises(SwitchError):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options={
                                "config_mode": "snapshot",
                                "shared_config_base": None,
                                "clear_missing_auth": False,
                                "skip_shim": True,
                                "skip_app_cli": True,
                                "skip_launchctl": True,
                                "filesystem_adapter": FilesystemAdapter(),
                            },
                        ),
                    )

            self.assertTrue(changed)
            self.assertEqual(replacement, original_read_bytes(watched))
            self.assertEqual(tuple(), tuple(store.backups_dir.iterdir()))

        with self.subTest(case="shared_entry_set"), tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            added = store.official_codex_home / "rules"
            changed = False

            def mutate_after_enumeration(home: Path) -> list[Path]:
                nonlocal changed
                entries = shared_support_entries(home)
                if home == store.official_codex_home and not changed:
                    added.mkdir()
                    (added / "external.json").write_text('{"external":true}\n')
                    changed = True
                return entries

            with patch(
                "codex_switch_home_sync.shared_support_entries",
                side_effect=mutate_after_enumeration,
            ):
                with self.assertRaises(SwitchError):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options={
                                "config_mode": "shared",
                                "shared_config_base": None,
                                "clear_missing_auth": False,
                                "skip_shim": True,
                                "skip_app_cli": True,
                                "skip_launchctl": True,
                                "filesystem_adapter": FilesystemAdapter(),
                            },
                        ),
                    )
            self.assertEqual(
                '{"external":true}\n',
                (added / "external.json").read_text(),
            )
            self.assertEqual(tuple(), tuple(store.backups_dir.iterdir()))

        with self.subTest(case="stale_link"), tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            stale = store.internal_codex_home / "history.jsonl"
            stale.symlink_to(store.official_codex_home / "history.jsonl")
            replacement_target = root / "external-history.jsonl"
            replacement_target.write_text("external\n")

            def mutate_after_stale_read(home: Path, source_home: Path) -> list[Path]:
                links = stale_runtime_links(home, source_home)
                if stale in links:
                    stale.unlink()
                    stale.symlink_to(replacement_target)
                return links

            with patch(
                "codex_switch_home_sync.stale_runtime_links",
                side_effect=mutate_after_stale_read,
            ):
                with self.assertRaises(SwitchError):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options={
                                "config_mode": "shared",
                                "shared_config_base": None,
                                "clear_missing_auth": False,
                                "skip_shim": True,
                                "skip_app_cli": True,
                                "skip_launchctl": True,
                                "filesystem_adapter": FilesystemAdapter(),
                            },
                        ),
                    )
            self.assertTrue(stale.is_symlink())
            self.assertEqual(str(replacement_target), os.readlink(stale))
            self.assertEqual(tuple(), tuple(store.backups_dir.iterdir()))

        with self.subTest(case="binding"), tempfile.TemporaryDirectory() as temp_dir:
            from codex_switch_paths import (
                resolve_internal_codex_bin as resolve_backend,
            )

            root = Path(temp_dir)
            store, target_executable, _, _ = self.arrange_switch_effect_fixture(root)
            changed = False

            def mutate_after_resolution(raw_path: str | None) -> str:
                nonlocal changed
                path = Path(resolve_backend(raw_path))
                if not changed:
                    changed = True
                    path.write_text("#!/bin/sh\nexit 7\n")
                    path.chmod(0o755)
                return str(path)

            with patch(
                "codex_switch_transaction.resolve_internal_codex_bin",
                side_effect=mutate_after_resolution,
            ):
                with self.assertRaises(SwitchError):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options={
                                "config_mode": "snapshot",
                                "shared_config_base": None,
                                "clear_missing_auth": False,
                                "skip_shim": True,
                                "skip_app_cli": True,
                                "skip_launchctl": True,
                                "filesystem_adapter": FilesystemAdapter(),
                            },
                        ),
                    )
            self.assertTrue(changed)
            self.assertEqual("#!/bin/sh\nexit 7\n", target_executable.read_text())
            self.assertEqual(tuple(), tuple(store.backups_dir.iterdir()))

    def test_restore_terminal_reread_rejects_unbound_or_incomplete_committed_manifest_without_marker_cleanup(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        for variant in ("unbound", "incomplete"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp_dir:
                class CorruptCommittedTerminal(FilesystemAdapter):
                    def write_manifest(
                        self,
                        path: Path,
                        data: dict[str, object],
                        *,
                        phase: str,
                    ) -> None:
                        if phase != "committed_manifest":
                            super().write_manifest(path, data, phase=phase)
                            return
                        if variant == "unbound":
                            corrupted = json.loads(json.dumps(data))
                            corrupted["restore_journal"]["transaction_id"] = (
                                "unbound-transaction"
                            )
                        else:
                            corrupted = {
                                "schema_version": 2,
                                "lifecycle": "committed",
                                "id": path.parent.name,
                                "operation": "restore",
                                "restore_journal": {
                                    "schema_version": 1,
                                    "state": "committed",
                                },
                            }
                        super().write_manifest(path, corrupted, phase=phase)
                        raise OSError("injected corrupt terminal write")

                root = Path(temp_dir)
                store, _, _, historical = (
                    self.arrange_restore_parent_cleanup_fixture(root)
                )
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="",
                        options={
                            "backup_id": historical.name,
                            "force": False,
                            "filesystem_adapter": CorruptCommittedTerminal(),
                        },
                    ),
                )

                self.assertNotEqual("committed", receipt.outcome)
                self.assertEqual(
                    1,
                    len(tuple(store.root.glob(".pending-transaction-*.json"))),
                )

    def test_switch_terminal_reread_rejects_unbound_committed_manifest(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        for variant in ("unbound", "incomplete", "staged_state_mismatch"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp_dir:
                class CorruptCommittedTerminal(FilesystemAdapter):
                    def write_manifest(
                        self,
                        path: Path,
                        data: dict[str, object],
                        *,
                        phase: str,
                    ) -> None:
                        if phase != "backup_finalize":
                            super().write_manifest(path, data, phase=phase)
                            return
                        if variant == "unbound":
                            corrupted = json.loads(json.dumps(data))
                            corrupted["switch_journal"]["transaction_id"] = (
                                "unbound-transaction"
                            )
                        elif variant == "staged_state_mismatch":
                            corrupted = json.loads(json.dumps(data))
                            staged_effect = next(
                                effect
                                for effect in corrupted["switch_journal"]["effects"]
                                if isinstance(effect.get("staged_state"), dict)
                                and effect["staged_state"].get("kind") == "file"
                            )
                            staged_effect["staged_state"]["sha256"] = "0" * 64
                        else:
                            corrupted = {
                                "schema_version": 2,
                                "lifecycle": "committed",
                                "id": path.parent.name,
                                "operation": "switch",
                                "entries": [],
                                "switch_journal": {
                                    "schema_version": 1,
                                    "state": "committed",
                                    "effects": [
                                        {
                                            "id": 0,
                                            "kind": "finalize",
                                            "phase": "backup_finalize",
                                            "status": "applied",
                                            "observed_after_state": {
                                                "lifecycle": "committed"
                                            },
                                        }
                                    ],
                                },
                            }
                        super().write_manifest(path, corrupted, phase=phase)
                        raise OSError("injected corrupt switch terminal write")

                store, _, _, _ = self.arrange_switch_effect_fixture(
                    Path(temp_dir)
                )
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": CorruptCommittedTerminal(),
                        },
                    ),
                )

                self.assertNotEqual("committed", receipt.outcome)
                self.assertEqual(
                    1,
                    len(tuple(store.root.glob(".pending-transaction-*.json"))),
                )

    def test_switch_terminal_reread_rejects_silent_corruption_before_marker_cleanup(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class SilentlyCorruptCommittedTerminal(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase != "backup_finalize":
                    super().write_manifest(path, data, phase=phase)
                    return
                corrupted = json.loads(json.dumps(data))
                corrupted["switch_journal"]["transaction_id"] = (
                    "silently-unbound-transaction"
                )
                super().write_manifest(path, corrupted, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            store, _, _, _ = self.arrange_switch_effect_fixture(Path(temp_dir))

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={
                        "config_mode": "snapshot",
                        "shared_config_base": None,
                        "clear_missing_auth": False,
                        "skip_shim": True,
                        "skip_app_cli": True,
                        "skip_launchctl": True,
                        "filesystem_adapter": SilentlyCorruptCommittedTerminal(),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(
                1,
                len(tuple(store.root.glob(".pending-transaction-*.json"))),
            )

    def test_active_is_restored_after_filesystem_and_desktop_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        order: list[str] = []

        class OrderedRunner(_FakeLaunchctlRunner):
            def __call__(
                self,
                command: list[str],
                env: dict[str, str] | None = None,
            ) -> tuple[int, str]:
                if "failure" in order and command[1] in {
                    "setenv",
                    "unsetenv",
                    "bootout",
                    "bootstrap",
                }:
                    order.append(f"desktop:{command[1]}")
                return super().__call__(command, env)

        class OrderedActiveFailureAdapter(FilesystemAdapter):
            def __init__(self, active_path: Path) -> None:
                self.active_path = active_path

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    order.append("failure")
                    raise OSError("injected active write failure")

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                order.append(
                    "active"
                    if destination == self.active_path.resolve(strict=False)
                    else f"filesystem:{destination}"
                )
                super().materialize(source, destination, state, phase=phase)

            def remove_empty_dir(self, path: Path, *, phase: str) -> None:
                order.append("directory-cleanup")
                super().remove_empty_dir(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, prior_executable, _ = self.arrange_switch_effect_fixture(root)
            new_internal_home = root / "new-internal-parent" / "home"
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest.update(
                {
                    "codex_home": str(new_internal_home.resolve()),
                    "home_mode": "custom",
                    "home_selection_confirmed": True,
                }
            )
            manifest_path.write_text(json.dumps(manifest) + "\n")
            store.internal_codex_home = None
            runner = OrderedRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )
            filesystem = OrderedActiveFailureAdapter(store.active_path)

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": filesystem,
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            rollback_order = order[order.index("failure") + 1 :]
            self.assertTrue(
                any(event.startswith("filesystem:") for event in rollback_order)
            )
            self.assertIn("directory-cleanup", rollback_order)
            self.assertTrue(
                any(event.startswith("desktop:") for event in rollback_order)
            )
            self.assertEqual("active", rollback_order[-1], rollback_order)

    def test_backup_finalize_failure_rolls_back_complete_switch_state(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class BackupFinalizeFailureAdapter(FilesystemAdapter):
            def __init__(self) -> None:
                self.phases: list[str] = []

            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                self.phases.append(phase)
                super().write_bytes(path, data, mode=mode, phase=phase)

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.phases.append(phase)
                if phase == "backup_finalize":
                    raise OSError("injected backup finalize failure")
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            before_states = {
                path: capture_path_state(path) for path in observed_paths
            }
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )
            filesystem = BackupFinalizeFailureAdapter()

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": filesystem,
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            self.assertEqual(
                before_states,
                {path: capture_path_state(path) for path in observed_paths},
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            finalize_index = filesystem.phases.index("backup_finalize")
            self.assertIn("active_write", filesystem.phases[:finalize_index])
            self.assertEqual(
                "switch_journal_intent",
                filesystem.phases[finalize_index - 1],
            )
            manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("rolled_back", manifest["lifecycle"])
            self.assertIn(
                "injected backup finalize failure",
                "\n".join(receipt.preview_lines),
            )

    def test_deep_switch_transaction_rejects_cli_only_internal_app_before_backup(
        self,
    ) -> None:
        from codex_switch_transaction import (
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _target, _prior, observed_paths = (
                self.arrange_switch_effect_fixture(root)
            )
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest.update(
                {
                    "internal_cli_generation": {
                        "schema_version": 1,
                        "scope": "cli-only",
                        "backend_sha256": "a" * 64,
                        "backend_version": "2.0.0",
                    },
                    "internal_app_readiness": "unverified",
                }
            )
            manifest_path.write_text(json.dumps(manifest) + "\n")
            protected = {
                path: self.runtime_binding_path_snapshot(path)
                for path in {*observed_paths, manifest_path}
            }
            before_backups = tuple(store.backups_dir.iterdir())

            with self.assertRaisesRegex(
                SwitchError,
                "internal.app_readiness.unverified",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                        },
                    ),
                    dry_run=True,
                )

            self.assertEqual(
                before_backups,
                tuple(store.backups_dir.iterdir()),
            )
            self.assert_runtime_binding_paths_unchanged(protected)

    def test_switch_rollback_failure_preserves_material_and_backup_id(self) -> None:
        from codex_switch_launch import _DesktopBindingAdapter
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class RollbackFailureAdapter(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "active_write":
                    raise OSError("injected active write failure")

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase.startswith("switch_rollback_"):
                    raise OSError("injected switch rollback failure")
                super().materialize(
                    source,
                    destination,
                    state,
                    phase=phase,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, target_executable, prior_executable, _ = (
                self.arrange_switch_effect_fixture(root)
            )
            runner = _FakeLaunchctlRunner(
                gui_env=str(prior_executable),
                service_loaded=True,
            )
            desktop = _DesktopBindingAdapter(
                store,
                runner=runner,
                uid_provider=lambda: 501,
            )

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": False,
                            "skip_launchctl": False,
                            "filesystem_adapter": RollbackFailureAdapter(),
                            "desktop_binding_adapter": desktop,
                        },
                    ),
                )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            backup_dir = store.backups_dir / str(receipt.backup_id)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual("rollback_failed", manifest["lifecycle"])
            self.assertIn(
                "injected switch rollback failure",
                manifest["rollback_failure"],
            )
            payload_entries = [
                entry for entry in manifest["entries"] if entry.get("payload")
            ]
            self.assertTrue(payload_entries)
            self.assertTrue(
                all((backup_dir / entry["payload"]).exists() for entry in payload_entries)
            )
            self.assertEqual(str(prior_executable), runner.gui_env)
            self.assertTrue(runner.service_loaded)
            self.assertEqual(2, runner.events.count("bootout"))
            self.assertEqual(2, runner.events.count("bootstrap"))
            self.assertIn(
                "injected switch rollback failure",
                "\n".join(receipt.preview_lines),
            )

    def test_switch_rollback_preserves_later_change_to_already_produced_target(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class ChangeProducedConfigBeforeLaterFailure(FilesystemAdapter):
            def __init__(self, produced_target: Path) -> None:
                self.produced_target = produced_target

            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                if phase == "shim_write":
                    self.produced_target.write_bytes(
                        b'model = "external-after-config"\n'
                    )
                    raise OSError("injected failure after external target change")
                super().write_bytes(path, data, mode=mode, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            target = store.internal_codex_home / "config.toml"
            store.internal_codex_home.chmod(0o750)
            adapter = ChangeProducedConfigBeforeLaterFailure(target)

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
                clear=False,
            ):
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": False,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                            "filesystem_adapter": adapter,
                        },
                    ),
                )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(
                b'model = "external-after-config"\n',
                target.read_bytes(),
            )
            self.assertEqual(
                0o750,
                stat.S_IMODE(store.internal_codex_home.stat().st_mode),
            )
            self.assertEqual(
                1,
                len(tuple(store.root.glob(".pending-transaction-*.json"))),
            )

    def test_malformed_active_record_blocks_transaction_without_writes(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        malformed_records = {
            "invalid-json": b"not-json\n",
            "non-object": b"[]\n",
            "missing-required-fields": b"{}\n",
            "invalid-profile": json.dumps(
                {"profile": 7, "codex_home": "/tmp/internal"}
            ).encode()
            + b"\n",
            "invalid-home": json.dumps(
                {"profile": "internal", "codex_home": 7}
            ).encode()
            + b"\n",
            "relative-home": json.dumps(
                {"profile": "internal", "codex_home": "relative/home"}
            ).encode()
            + b"\n",
        }
        for malformed_kind, payload in malformed_records.items():
            with self.subTest(malformed_kind=malformed_kind):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store, _, _, observed_paths = self.arrange_switch_effect_fixture(root)
                    store.active_path.write_bytes(payload)
                    before_store = {
                        str(path.relative_to(store.root)): (
                            "directory" if path.is_dir() else path.read_bytes()
                        )
                        for path in store.root.rglob("*")
                    }
                    before_paths = {path: path.read_bytes() for path in observed_paths}

                    with self.assertRaisesRegex(SwitchError, "active"):
                        execute_transaction(
                            store,
                            TransactionRequest(
                                operation="switch",
                                profile="internal",
                                options={
                                    "config_mode": "snapshot",
                                    "shared_config_base": None,
                                    "clear_missing_auth": False,
                                    "skip_shim": True,
                                    "skip_app_cli": True,
                                    "skip_launchctl": True,
                                },
                            ),
                        )

                    self.assertEqual(
                        before_store,
                        {
                            str(path.relative_to(store.root)): (
                                "directory" if path.is_dir() else path.read_bytes()
                            )
                            for path in store.root.rglob("*")
                        },
                    )
                    self.assertEqual(
                        before_paths,
                        {path: path.read_bytes() for path in observed_paths},
                    )
                    self.assertFalse(any(store.backups_dir.iterdir()))

    def test_concurrent_transaction_returns_busy_before_backup_or_read(self) -> None:
        try:
            from codex_switch_transaction import TransactionRequest, execute_transaction
        except ModuleNotFoundError:
            self.fail("public transaction seam is missing")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            broken_backup = store.backups_dir / "broken"
            broken_backup.mkdir(parents=True)
            (broken_backup / "backup.json").write_text("not-json\n")
            target = store.official_codex_home / "config.toml"
            target.write_text("before\n")
            before_paths = sorted(
                str(path.relative_to(store.root)) for path in store.root.rglob("*")
            )

            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_directory_lock,
                args=(str(store.root), ready, release),
            )
            holder.start()
            self.assertTrue(ready.wait(10), "lock holder did not become ready")
            try:
                with self.assertRaisesRegex(SwitchError, "profile store is busy"):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="restore",
                            profile="restore",
                            options={"backup_id": "broken", "force": False},
                        ),
                    )
            finally:
                release.set()
                holder.join(10)
                if holder.is_alive():
                    holder.terminate()
                    holder.join()

            self.assertEqual(0, holder.exitcode)
            self.assertEqual("before\n", target.read_text())
            self.assertEqual(
                before_paths,
                sorted(str(path.relative_to(store.root)) for path in store.root.rglob("*")),
            )

    def test_transaction_apply_executes_under_python39(self) -> None:
        python = shutil.which("python3")
        if python is None:
            self.skipTest("python3 is unavailable")
        version = subprocess.run(
            [python, "--version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout.strip()
        if not version.startswith("Python 3.9."):
            self.skipTest(f"python3 is not Python 3.9: {version}")
        script = """
import json
import tempfile
from pathlib import Path

from codex_switch_store import Store
from codex_switch_transaction import TransactionRequest, execute_transaction

with tempfile.TemporaryDirectory() as temp_dir:
    root = Path(temp_dir)
    store_root = root / "store"
    official = root / "official"
    internal = root / "internal"
    store_root.mkdir()
    official.mkdir()
    internal.mkdir()
    store = Store(
        root=store_root,
        official_codex_home=official,
        internal_codex_home=internal,
        launch_agent_path=root / "agent.plist",
        launch_agent_label="test.codex-switch",
    )
    target = official / "absent.toml"
    backup = store.backups_dir / "python39"
    backup.mkdir(parents=True)
    (backup / "backup.json").write_text(json.dumps({
        "schema_version": 2,
        "lifecycle": "committed",
        "id": "python39",
        "operation": "switch",
        "to_profile": "internal",
        "entries": [{
            "path": str(target),
            "before_state": {"kind": "missing"},
            "committed_after_state": {"kind": "missing"},
        }],
    }) + "\\n")
    receipt = execute_transaction(
        store,
        TransactionRequest(
            operation="restore",
            profile="restore",
            options={"backup_id": "python39", "force": False},
        ),
    )
    print(receipt.outcome)
"""
        environment = dict(os.environ)
        scripts_dir = str(Path(__file__).resolve().parent)
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (scripts_dir, environment.get("PYTHONPATH", "")) if part
        )

        result = subprocess.run(
            [python, "-c", script],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("committed", result.stdout.strip())

    def test_store_directory_lock_is_released_after_failure(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            missing_request = TransactionRequest(
                operation="restore",
                profile="restore",
                options={"backup_id": "missing", "force": False},
            )

            with self.assertRaisesRegex(SwitchError, "Backup not found: missing"):
                execute_transaction(store, missing_request)

            target = store.official_codex_home / "config.toml"
            backup_dir = store.backups_dir / "ready"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "id": "ready",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "pre_state": {"kind": "missing"},
                                "post_state": {"kind": "missing"},
                            }
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": "ready", "force": False},
                ),
                dry_run=True,
            )

            self.assertEqual("dry_run", receipt.outcome)
            self.assertEqual(
                ("restore backup ready", f"- restore {target.resolve()}"),
                receipt.preview_lines,
            )

    def test_switch_dry_run_rejects_active_home_collision_without_writes(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _, _, _ = self.arrange_switch_effect_fixture(root)
            active_home = root / "active-official-home"
            active_home.mkdir()
            (active_home / "config.toml").write_text('model = "active"\n')
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest.update(
                {
                    "codex_home": str(active_home),
                    "home_mode": "custom",
                    "home_selection_confirmed": True,
                }
            )
            manifest_path.write_text(json.dumps(manifest) + "\n")
            store.internal_codex_home = None
            active = json.loads(store.active_path.read_text())
            active["codex_home"] = str(active_home)
            active["live_codex_home"] = str(active_home)
            store.active_path.write_text(json.dumps(active) + "\n")
            active_before = store.active_path.read_bytes()
            manifest_before = manifest_path.read_bytes()
            backups_before = tuple(store.backups_dir.iterdir())

            with self.assertRaisesRegex(
                SwitchError,
                "Refusing to switch from openai-official to internal with the same",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="switch",
                        profile="internal",
                        options={
                            "config_mode": "snapshot",
                            "shared_config_base": None,
                            "clear_missing_auth": False,
                            "skip_shim": True,
                            "skip_app_cli": True,
                            "skip_launchctl": True,
                        },
                    ),
                    dry_run=True,
                )

            self.assertEqual(active_before, store.active_path.read_bytes())
            self.assertEqual(manifest_before, manifest_path.read_bytes())
            self.assertEqual(backups_before, tuple(store.backups_dir.iterdir()))

    def test_dry_run_performs_no_store_write(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "absent.toml"
            backup_dir = store.backups_dir / "v2-preview"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "v2-preview",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": {"kind": "missing"},
                            }
                        ],
                    }
                )
                + "\n"
            )
            before = {
                str(path.relative_to(store.root)): (
                    "directory" if path.is_dir() else path.read_bytes()
                )
                for path in store.root.rglob("*")
            }

            try:
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "v2-preview", "force": False},
                    ),
                    dry_run=True,
                )
            except SwitchError as exc:
                self.fail(f"schema-v2 dry run was rejected: {exc}")

            self.assertEqual("dry_run", receipt.outcome)
            self.assertEqual(
                before,
                {
                    str(path.relative_to(store.root)): (
                        "directory" if path.is_dir() else path.read_bytes()
                    )
                    for path in store.root.rglob("*")
                },
            )
            self.assertFalse(target.exists())

    def test_restore_detects_changed_directory_descendant(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "support"
            target.mkdir(mode=0o700)
            child = target / "plugin.json"
            child.write_text('{"version": 2}\n')
            child.chmod(0o600)
            backup_dir = store.backups_dir / "directory-conflict"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "directory-conflict",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": {
                                    "kind": "directory",
                                    "mode": 0o700,
                                    "entry_count": 0,
                                    "tree_sha256": "0" * 64,
                                },
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Current path changed since backup was committed",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "directory-conflict", "force": False},
                    ),
                    dry_run=True,
                )

            self.assertEqual('{"version": 2}\n', child.read_text())
            self.assertEqual(0o600, child.stat().st_mode & 0o777)

    def test_restore_rejects_unsupported_object_kind_before_mutation(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "unsupported"
            backup_dir = store.backups_dir / "unsupported-kind"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "unsupported-kind",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "fifo"},
                                "committed_after_state": {"kind": "missing"},
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Unsupported backup state kind: fifo",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "unsupported-kind", "force": False},
                    ),
                    dry_run=True,
                )

            self.assertFalse(target.exists())

    def test_restore_rejects_payload_escape(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            escaped_payload = store.backups_dir / "escaped.toml"
            escaped_payload.parent.mkdir(parents=True)
            escaped_payload.write_text("historical\n")
            backup_dir = store.backups_dir / "payload-escape"
            backup_dir.mkdir()
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "payload-escape",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {
                                    "kind": "file",
                                    "mode": 0o600,
                                    "size": 11,
                                    "sha256": "4" * 64,
                                },
                                "committed_after_state": {"kind": "missing"},
                                "payload": "../escaped.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "payload escapes backup directory",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "payload-escape", "force": False},
                    ),
                    dry_run=True,
                )

            self.assertFalse(target.exists())
            self.assertEqual("historical\n", escaped_payload.read_text())

    def test_restore_rejects_unapproved_absolute_target(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = root / "unapproved" / "config.toml"
            backup_dir = store.backups_dir / "unapproved-target"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "unapproved-target",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": {"kind": "missing"},
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Restore target is not approved",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "unapproved-target", "force": False},
                    ),
                    dry_run=True,
                )

            self.assertFalse(target.exists())

    def test_restore_does_not_allow_arbitrary_store_root_target(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.root / "unapproved.json"
            self.write_v2_backup(
                store,
                "store-root-target",
                [
                    {
                        "path": str(target),
                        "before_state": {"kind": "missing"},
                        "committed_after_state": {"kind": "missing"},
                    }
                ],
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Restore target is not approved",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "store-root-target",
                            "force": False,
                        },
                    ),
                    dry_run=True,
                )

            self.assertFalse(target.exists())

    def test_restore_allows_current_shell_bootstrap_target(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            shell_profile = root / "shell" / ".zshrc"
            backup_dir = store.backups_dir / "shell-bootstrap"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "shell-bootstrap",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(shell_profile),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": {"kind": "missing"},
                            }
                        ],
                    }
                )
                + "\n"
            )

            with patch.dict(
                os.environ,
                {
                    "CODEX_SWITCH_SHELL_PROFILE": str(shell_profile),
                    "CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "",
                },
            ):
                try:
                    receipt = execute_transaction(
                        store,
                        TransactionRequest(
                            operation="restore",
                            profile="restore",
                            options={"backup_id": "shell-bootstrap", "force": False},
                        ),
                        dry_run=True,
                    )
                except SwitchError as exc:
                    self.fail(f"current shell bootstrap target was rejected: {exc}")

            self.assertEqual("dry_run", receipt.outcome)
            self.assertFalse(shell_profile.exists())

    def test_restore_uses_canonical_destination_after_preflight(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class RecordApplyDestination(FilesystemAdapter):
            def __init__(self) -> None:
                self.applied: list[Path] = []

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "apply":
                    self.applied.append(destination)
                super().materialize(source, destination, state, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            nested = store.official_codex_home / "nested"
            nested.mkdir()
            raw_target = nested / ".." / "config.toml"
            canonical_target = store.official_codex_home.resolve() / "config.toml"
            self.write_v2_backup(
                store,
                "canonical-target",
                [
                    {
                        "path": str(raw_target),
                        "before_state": {"kind": "missing"},
                        "committed_after_state": {"kind": "missing"},
                    }
                ],
            )
            adapter = RecordApplyDestination()

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": "canonical-target",
                        "force": False,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual([canonical_target], adapter.applied)
            self.assertEqual(
                f"- restore {canonical_target}",
                receipt.preview_lines[1],
            )

    def test_restore_rejects_parent_symlink_swap_before_materialize(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class SwapParentBeforeApply(FilesystemAdapter):
            def __init__(self, parent: Path, outside: Path) -> None:
                self.parent = parent
                self.outside = outside
                self.swapped = False

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "apply" and not self.swapped:
                    self.swapped = True
                    self.parent.rename(self.parent.with_name("managed-moved"))
                    self.parent.symlink_to(self.outside, target_is_directory=True)
                super().materialize(source, destination, state, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            managed_parent = store.official_codex_home / "managed"
            managed_parent.mkdir()
            target = managed_parent / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            outside = root / "outside"
            outside.mkdir()
            outside_target = outside / "config.toml"
            outside_target.write_text("outside\n")
            outside_target.chmod(0o600)
            backup_dir = store.backups_dir / "parent-swap"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": self.file_state(target),
                            }
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": backup_dir.name,
                        "force": False,
                        "filesystem_adapter": SwapParentBeforeApply(
                            managed_parent,
                            outside,
                        ),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual("outside\n", outside_target.read_text())
            self.assertEqual(
                "current\n",
                (store.official_codex_home / "managed-moved" / "config.toml").read_text(),
            )

    def test_restore_installs_the_attested_staged_file_inode(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class StageInodeAudit(FilesystemAdapter):
            def __init__(self, store: Store) -> None:
                self.store = store
                self.stage_inode: int | None = None
                self.destination_inode: int | None = None

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "apply":
                    self.assert_stage(source)
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    self.destination_inode = destination.stat().st_ino

            def assert_stage(self, source: Path | None) -> None:
                if source is None:
                    raise AssertionError("restore file has no staged source")
                self.stage_inode = source.stat().st_ino

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            historical = store.backups_dir / "attested-stage-source"
            payload = historical / "payloads" / "config.toml"
            payload.parent.mkdir(parents=True)
            payload.write_text("historical\n")
            payload.chmod(0o600)
            self.write_v2_backup(
                store,
                historical.name,
                [
                    {
                        "path": str(target),
                        "before_state": self.file_state(payload),
                        "committed_after_state": self.file_state(target),
                        "payload": "payloads/config.toml",
                    }
                ],
            )
            adapter = StageInodeAudit(store)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": historical.name,
                        "force": False,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertIsNotNone(adapter.stage_inode)
            self.assertEqual(adapter.stage_inode, adapter.destination_inode)

    def test_restore_rejects_changed_staged_identity_before_target_write(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class ReplaceStageBeforeApply(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "apply" and source is not None:
                    payload = source.read_bytes()
                    mode = stat.S_IMODE(source.stat().st_mode)
                    source.unlink()
                    source.write_bytes(payload)
                    source.chmod(mode)
                super().materialize(source, destination, state, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            before = target.read_bytes()
            historical = store.backups_dir / "changed-stage-source"
            payload = historical / "payloads" / "config.toml"
            payload.parent.mkdir(parents=True)
            payload.write_text("historical\n")
            payload.chmod(0o600)
            self.write_v2_backup(
                store,
                historical.name,
                [
                    {
                        "path": str(target),
                        "before_state": self.file_state(payload),
                        "committed_after_state": self.file_state(target),
                        "payload": "payloads/config.toml",
                    }
                ],
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": historical.name,
                        "force": False,
                        "filesystem_adapter": ReplaceStageBeforeApply(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(before, target.read_bytes())

    def test_restore_attests_stable_lexical_symlink_route_and_rejects_change(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        for variant in ("stable", "changed"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp_dir:
                class ChangeLexicalRouteBeforeApply(FilesystemAdapter):
                    def __init__(
                        self,
                        alias: Path,
                        outside: Path,
                    ) -> None:
                        self.alias = alias
                        self.outside = outside

                    def materialize(
                        self,
                        source: Path | None,
                        destination: Path,
                        state: dict[str, object],
                        *,
                        phase: str,
                    ) -> None:
                        if phase == "apply" and variant == "changed":
                            self.alias.unlink()
                            self.alias.symlink_to(
                                self.outside,
                                target_is_directory=True,
                            )
                        super().materialize(
                            source,
                            destination,
                            state,
                            phase=phase,
                        )

                root = Path(temp_dir)
                store = self.make_store(root)
                alias = root / "official-alias"
                alias.symlink_to(
                    store.official_codex_home,
                    target_is_directory=True,
                )
                target = store.official_codex_home / "config.toml"
                target.write_text("current\n")
                target.chmod(0o600)
                outside = root / "outside"
                outside.mkdir()
                outside_target = outside / "config.toml"
                outside_target.write_text("outside\n")
                historical = store.backups_dir / f"lexical-route-{variant}"
                payload = historical / "payloads" / "config.toml"
                payload.parent.mkdir(parents=True)
                payload.write_text("historical\n")
                payload.chmod(0o600)
                self.write_v2_backup(
                    store,
                    historical.name,
                    [
                        {
                            "path": str(alias / "config.toml"),
                            "before_state": self.file_state(payload),
                            "committed_after_state": self.file_state(target),
                            "payload": "payloads/config.toml",
                        }
                    ],
                )

                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": historical.name,
                            "force": False,
                            "filesystem_adapter": ChangeLexicalRouteBeforeApply(
                                alias,
                                outside,
                            ),
                        },
                    ),
                )

                if variant == "stable":
                    self.assertEqual("committed", receipt.outcome)
                    self.assertEqual("historical\n", target.read_text())
                    safety = json.loads(
                        (
                            store.backups_dir
                            / str(receipt.backup_id)
                            / "backup.json"
                        ).read_text()
                    )
                    effect = safety["restore_journal"]["effects"][0]
                    self.assertEqual(
                        "symlink",
                        next(
                            component
                            for component in effect["route_guard"]["components"]
                            if component["path"] == str(alias)
                        )["kind"],
                    )
                else:
                    self.assertEqual("rolled_back", receipt.outcome)
                    self.assertEqual("current\n", target.read_text())
                    self.assertEqual("outside\n", outside_target.read_text())

    def test_restore_parent_cleanup_preserves_foreign_empty_replacement(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class ReplaceParentInsideCleanup(FilesystemAdapter):
            def __init__(self, parent: Path, moved: Path) -> None:
                self.parent = parent
                self.moved = moved

            def remove_empty_dir(self, path: Path, *, phase: str) -> None:
                if phase == "restore_parent_cleanup":
                    path.rename(self.moved)
                    path.mkdir(mode=0o700)
                super().remove_empty_dir(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, parent, _, historical = (
                self.arrange_restore_parent_cleanup_fixture(root)
            )
            moved = parent.with_name("cleanup-parent-owned")

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": historical.name,
                        "force": False,
                        "filesystem_adapter": ReplaceParentInsideCleanup(
                            parent,
                            moved,
                        ),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertTrue(parent.is_dir())
            self.assertTrue(moved.is_dir())

    def test_restore_rejects_terminal_parent_component(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            raw_target = store.official_codex_home / "child" / ".." / ".."
            self.write_v2_backup(
                store,
                "terminal-parent",
                [
                    {
                        "path": str(raw_target),
                        "before_state": {"kind": "missing"},
                        "committed_after_state": {"kind": "missing"},
                    }
                ],
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Restore target is not approved",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "terminal-parent", "force": True},
                    ),
                    dry_run=True,
                )

    def test_restore_rejects_duplicate_targets_before_safety_backup(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            backup_dir = store.backups_dir / "duplicate-target"
            backup_dir.mkdir(parents=True)
            entry = {
                "path": str(target),
                "before_state": {"kind": "missing"},
                "committed_after_state": {"kind": "missing"},
            }
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "duplicate-target",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [entry, dict(entry)],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "duplicate or overlapping targets",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "duplicate-target", "force": False},
                    ),
                )

            self.assertFalse(target.exists())
            self.assertEqual(
                ["duplicate-target"],
                sorted(path.name for path in store.backups_dir.iterdir()),
            )

    def test_restore_rejects_backup_id_escape_before_read(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            outside = store.root / "outside"
            outside.mkdir()
            (outside / "backup.json").write_text("not-json\n")

            with self.assertRaisesRegex(
                SwitchError,
                "Backup id is not contained in the store",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "../outside", "force": False},
                    ),
                    dry_run=True,
                )

            self.assertEqual("not-json\n", (outside / "backup.json").read_text())

    def test_restore_rejects_v0_files_manifest(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            backup_dir = store.backups_dir / "legacy-v0"
            backup_dir.mkdir(parents=True)
            (backup_dir / "config.toml").write_text("historical\n")
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "profile": "internal",
                        "live_codex_home": str(store.official_codex_home),
                        "files": ["config.toml"],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "legacy v0 files manifest.*manual recovery",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "legacy-v0", "force": True},
                    ),
                )

            self.assertEqual("current\n", target.read_text())
            self.assertTrue((backup_dir / "config.toml").exists())

    def test_restore_rejects_non_integer_schema_version(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            backup_dir = store.backups_dir / "float-schema"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2.0,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Unsupported backup schema version.*2.0",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": backup_dir.name, "force": False},
                    ),
                    dry_run=True,
                )

    def test_restore_rejects_v1_directory_even_with_force(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "support"
            target.mkdir()
            (target / "current.txt").write_text("current\n")
            backup_dir = store.backups_dir / "legacy-v1-directory"
            payload = backup_dir / "0-support"
            payload.mkdir(parents=True)
            (payload / "historical.txt").write_text("historical\n")
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "id": "legacy-v1-directory",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "pre_state": {"kind": "directory", "mode": 0o700},
                                "post_state": {"kind": "missing"},
                                "backup_rel": "0-support",
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "v1 directory.*not recursively attested.*manual recovery",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "legacy-v1-directory",
                            "force": True,
                        },
                    ),
                    dry_run=True,
                )

            self.assertEqual("current\n", (target / "current.txt").read_text())
            self.assertFalse((target / "historical.txt").exists())

    def test_restore_rejects_unattested_v1_symlink_before_mutation(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "current-link"
            backup_dir = store.backups_dir / "unattested-v1-symlink"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "id": "unattested-v1-symlink",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "pre_state": {"kind": "symlink"},
                                "post_state": {"kind": "missing"},
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Symlink backup state has no attested target",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "unattested-v1-symlink",
                            "force": True,
                        },
                    ),
                )

            self.assertFalse(target.exists())
            self.assertEqual(
                ["unattested-v1-symlink"],
                sorted(path.name for path in store.backups_dir.iterdir()),
            )

    def test_force_does_not_bypass_malformed_file_attestation(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            backup_dir = store.backups_dir / "malformed-attestation"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "malformed-attestation",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": {
                                    "kind": "file",
                                    "mode": 0o600,
                                    "sha256": "not-a-digest",
                                },
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "File backup state has no valid SHA-256 attestation",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "malformed-attestation",
                            "force": True,
                        },
                    ),
                )

            self.assertEqual("current\n", target.read_text())
            self.assertEqual(
                ["malformed-attestation"],
                sorted(path.name for path in store.backups_dir.iterdir()),
            )

    def test_force_does_not_bypass_malformed_directory_attestation(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "support"
            target.mkdir()
            (target / "current.txt").write_text("current\n")
            backup_dir = store.backups_dir / "malformed-directory"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "malformed-directory",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": {
                                    "kind": "directory",
                                    "mode": 0o700,
                                    "entry_count": 1,
                                    "tree_sha256": "not-a-tree-digest",
                                },
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Directory backup state has no valid tree SHA-256 attestation",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "malformed-directory",
                            "force": True,
                        },
                    ),
                )

            self.assertEqual("current\n", (target / "current.txt").read_text())

    def test_restore_rejects_empty_v1_and_v2_entries_before_safety_backup_even_with_force(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store = self.make_store(root)
                    store.ensure()
                    backup_id = f"empty-v{schema_version}"
                    backup_dir = store.backups_dir / backup_id
                    backup_dir.mkdir()
                    manifest: dict[str, object] = {
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [],
                    }
                    if schema_version == 2:
                        manifest.update(
                            {
                                "schema_version": 2,
                                "lifecycle": "committed",
                                "id": backup_id,
                            }
                        )
                    (backup_dir / "backup.json").write_text(
                        json.dumps(manifest) + "\n"
                    )
                    before_children = tuple(
                        sorted(path.name for path in store.backups_dir.iterdir())
                    )
                    before_manifest = (backup_dir / "backup.json").read_bytes()

                    with self.assertRaisesRegex(SwitchError, "entries"):
                        execute_transaction(
                            store,
                            TransactionRequest(
                                operation="restore",
                                profile="restore",
                                options={"backup_id": backup_id, "force": True},
                            ),
                        )

                    self.assertEqual(
                        before_children,
                        tuple(
                            sorted(
                                path.name
                                for path in store.backups_dir.iterdir()
                            )
                        ),
                    )
                    self.assertEqual(
                        before_manifest,
                        (backup_dir / "backup.json").read_bytes(),
                    )

    def test_restore_rejects_invalid_recorded_modes_before_safety_backup_even_with_force(
        self,
    ) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        for schema_version in (1, 2):
            for invalid_mode in (-1, True, 0o10000):
                with self.subTest(
                    schema_version=schema_version,
                    invalid_mode=invalid_mode,
                ):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        root = Path(temp_dir)
                        store = self.make_store(root)
                        store.ensure()
                        target = store.internal_codex_home / "config.toml"
                        target.write_text('model = "current"\n')
                        current = self.file_state(target)
                        before_state = dict(current)
                        before_state["mode"] = invalid_mode
                        backup_id = f"invalid-mode-v{schema_version}-{invalid_mode}"
                        backup_dir = store.backups_dir / backup_id
                        backup_dir.mkdir()
                        payload = backup_dir / "payload.bin"
                        payload.write_bytes(target.read_bytes())
                        if schema_version == 1:
                            entry = {
                                "path": str(target),
                                "pre_state": before_state,
                                "post_state": current,
                                "backup_rel": payload.name,
                            }
                            manifest = {
                                "operation": "switch",
                                "to_profile": "internal",
                                "entries": [entry],
                            }
                        else:
                            entry = {
                                "path": str(target),
                                "before_state": before_state,
                                "committed_after_state": current,
                                "payload": payload.name,
                            }
                            manifest = {
                                "schema_version": 2,
                                "lifecycle": "committed",
                                "id": backup_id,
                                "operation": "switch",
                                "to_profile": "internal",
                                "entries": [entry],
                            }
                        (backup_dir / "backup.json").write_text(
                            json.dumps(manifest) + "\n"
                        )
                        before_children = tuple(
                            sorted(
                                path.name
                                for path in store.backups_dir.iterdir()
                            )
                        )
                        before_target = target.read_bytes()

                        with self.assertRaisesRegex(
                            SwitchError,
                            "permission mode",
                        ):
                            execute_transaction(
                                store,
                                TransactionRequest(
                                    operation="restore",
                                    profile="restore",
                                    options={"backup_id": backup_id, "force": True},
                                ),
                            )

                        self.assertEqual(before_target, target.read_bytes())
                        self.assertEqual(
                            before_children,
                            tuple(
                                sorted(
                                    path.name
                                    for path in store.backups_dir.iterdir()
                                )
                            ),
                        )

    def test_recorded_mode_and_directory_entry_count_validation_matrix(self) -> None:
        from codex_switch_restore import RestoreManifest, RestoreManifestEntry
        from codex_switch_transaction import (
            TransactionRequest,
            _preflight_manifest_states,
            capture_path_state,
            execute_transaction,
        )

        def state(kind: str, mode: object = 0o600) -> dict[str, object]:
            if kind == "missing":
                return {"kind": "missing", "mode": mode}
            if kind == "file":
                return {
                    "kind": "file",
                    "mode": mode,
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            if kind == "symlink":
                return {
                    "kind": "symlink",
                    "mode": mode,
                    "symlink_target": "target",
                }
            return {
                "kind": "directory",
                "mode": mode,
                "entry_count": 0,
                "tree_sha256": hashlib.sha256(b"[]").hexdigest(),
            }

        for schema_version in (1, 2):
            for position in ("before", "committed_after"):
                for kind in ("missing", "file", "symlink", "directory"):
                    for invalid_mode in (True, -1, 0o10000):
                        with self.subTest(
                            schema_version=schema_version,
                            position=position,
                            kind=kind,
                            invalid_mode=invalid_mode,
                        ):
                            before = state("file")
                            after = state("file")
                            selected = state(kind, invalid_mode)
                            if position == "before":
                                before = selected
                            else:
                                after = selected
                            manifest = RestoreManifest(
                                backup_id="mode-matrix",
                                schema_version=schema_version,
                                lifecycle="committed",
                                operation="switch",
                                profile="internal",
                                entries=(
                                    RestoreManifestEntry(
                                        path="/tmp/config.toml",
                                        before_state=before,
                                        committed_after_state=after,
                                        payload=None,
                                    ),
                                ),
                            )
                            with self.assertRaisesRegex(
                                SwitchError,
                                "permission mode",
                            ):
                                _preflight_manifest_states(manifest)

        for schema_version in (1, 2):
            for position in ("before", "committed_after"):
                for kind in ("missing", "file", "symlink", "directory"):
                    with self.subTest(
                        special_bits_schema=schema_version,
                        position=position,
                        kind=kind,
                    ):
                        before = state("file")
                        after = state("file")
                        selected = state(kind, 0o7777)
                        if position == "before":
                            before = selected
                        else:
                            after = selected
                        manifest = RestoreManifest(
                            backup_id="special-mode-matrix",
                            schema_version=schema_version,
                            lifecycle="committed",
                            operation="switch",
                            profile="internal",
                            entries=(
                                RestoreManifestEntry(
                                    path="/tmp/config.toml",
                                    before_state=before,
                                    committed_after_state=after,
                                    payload=None,
                                ),
                            ),
                        )
                        if schema_version == 1 and kind == "directory":
                            with self.assertRaisesRegex(
                                SwitchError,
                                "v1 directory",
                            ):
                                _preflight_manifest_states(manifest)
                        else:
                            _preflight_manifest_states(manifest)

        for position in ("before_state", "committed_after_state"):
            with self.subTest(directory_entry_count_position=position):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store = self.make_store(root)
                    target = store.official_codex_home / "support"
                    target.mkdir()
                    (target / "current.txt").write_text("current\n")
                    backup_dir = store.backups_dir / f"bad-count-{position}"
                    payload = backup_dir / "payloads" / "support"
                    payload.mkdir(parents=True)
                    (payload / "historical.txt").write_text("historical\n")
                    before_state = capture_path_state(payload)
                    before_state["path"] = str(target.resolve())
                    after_state = capture_path_state(target)
                    selected = before_state if position == "before_state" else after_state
                    selected["entry_count"] = int(selected["entry_count"]) + 1
                    (backup_dir / "backup.json").write_text(
                        json.dumps(
                            {
                                "schema_version": 2,
                                "lifecycle": "committed",
                                "id": backup_dir.name,
                                "operation": "switch",
                                "to_profile": "openai-official",
                                "entries": [
                                    {
                                        "path": str(target.resolve()),
                                        "before_state": before_state,
                                        "committed_after_state": after_state,
                                        "payload": "payloads/support",
                                    }
                                ],
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    before_target = capture_path_state(target)
                    before_backups = tuple(
                        sorted(path.name for path in store.backups_dir.iterdir())
                    )

                    with self.assertRaisesRegex(
                        SwitchError,
                        "entry count|digest or state mismatch",
                    ):
                        execute_transaction(
                            store,
                            TransactionRequest(
                                operation="restore",
                                profile="restore",
                                options={
                                    "backup_id": backup_dir.name,
                                    "force": True,
                                },
                            ),
                        )

                    self.assertEqual(before_target, capture_path_state(target))
                    self.assertEqual(
                        before_backups,
                        tuple(
                            sorted(path.name for path in store.backups_dir.iterdir())
                        ),
                    )

    def test_supported_adopted_home_authority_matrix(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        for profile in ("internal", "openai-official"):
            with self.subTest(profile=profile, authority="valid"):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store, _, _, _ = self.arrange_switch_effect_fixture(root)
                    adopted_home = (root / f"adopted-{profile}").resolve()
                    adopted_home.mkdir()
                    adopted_home.chmod(0o750)
                    original = f'marker = "{profile}-before"\n'.encode()
                    (adopted_home / "config.toml").write_bytes(original)
                    manifest_path = store.manifest_path(profile)
                    manifest = json.loads(manifest_path.read_text())
                    manifest.update(
                        {
                            "codex_home": str(adopted_home),
                            "home_mode": "adopted",
                            "home_selection_confirmed": True,
                        }
                    )
                    manifest_path.write_text(json.dumps(manifest) + "\n")
                    if profile == "internal":
                        store.internal_codex_home = None

                    switch_receipt = execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile=profile,
                            options={
                                "config_mode": "snapshot",
                                "shared_config_base": None,
                                "clear_missing_auth": False,
                                "skip_shim": True,
                                "skip_app_cli": True,
                                "skip_launchctl": True,
                            },
                        ),
                    )
                    self.assertEqual("committed", switch_receipt.outcome)
                    restore_receipt = execute_transaction(
                        store,
                        TransactionRequest(
                            operation="restore",
                            profile="restore",
                            options={
                                "backup_id": switch_receipt.backup_id,
                                "force": False,
                            },
                        ),
                    )
                    self.assertEqual("committed", restore_receipt.outcome)
                    self.assertEqual(original, (adopted_home / "config.toml").read_bytes())
                    self.assertEqual(0o750, stat.S_IMODE(adopted_home.stat().st_mode))

        for profile in ("internal", "openai-official"):
            for invalid_kind in (
                "relative",
                "dotdot",
                "symlink",
                "backup-contained",
            ):
                with self.subTest(profile=profile, authority=invalid_kind):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        root = Path(temp_dir)
                        store = self.make_store(root)
                        backup_dir, target = self.arrange_restorable_file_backup(
                            store,
                            backup_id=f"authority-{profile}-{invalid_kind}",
                        )
                        profile_dir = store.profile_dir(profile)
                        profile_dir.mkdir(parents=True, exist_ok=True)
                        canonical_home = (root / "adopted-home").resolve()
                        canonical_home.mkdir(exist_ok=True)
                        if invalid_kind == "relative":
                            raw_home = "relative/home"
                        elif invalid_kind == "dotdot":
                            raw_home = str(
                                canonical_home / "child" / ".."
                            )
                        elif invalid_kind == "symlink":
                            alias = root / "adopted-alias"
                            alias.symlink_to(canonical_home, target_is_directory=True)
                            raw_home = str(alias)
                        else:
                            raw_home = str(
                                (store.backups_dir / "adopted-home").resolve()
                            )
                        (profile_dir / "manifest.json").write_text(
                            json.dumps(
                                {
                                    "name": profile,
                                    "codex_home": raw_home,
                                    "home_selection_confirmed": True,
                                }
                            )
                            + "\n"
                        )
                        before_target = target.read_bytes()
                        before_backups = tuple(
                            sorted(path.name for path in store.backups_dir.iterdir())
                        )

                        with self.assertRaisesRegex(SwitchError, "home"):
                            execute_transaction(
                                store,
                                TransactionRequest(
                                    operation="restore",
                                    profile="restore",
                                    options={
                                        "backup_id": backup_dir.name,
                                        "force": True,
                                    },
                                ),
                            )

                        self.assertEqual(before_target, target.read_bytes())
                        self.assertEqual(
                            before_backups,
                            tuple(
                                sorted(
                                    path.name for path in store.backups_dir.iterdir()
                                )
                            ),
                        )

    def test_failed_nested_home_cleanup_preserves_changed_or_non_empty_parent(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailAfterParentEdit(FilesystemAdapter):
            def __init__(
                self,
                created_root: Path,
                moved_root: Path,
                variant: str,
            ) -> None:
                self.created_root = created_root
                self.moved_root = moved_root
                self.variant = variant
                self.replacement_identity: tuple[int, int] | None = None

            def write_bytes(
                self,
                path: Path,
                data: bytes,
                *,
                mode: int,
                phase: str,
            ) -> None:
                if phase == "shim_write":
                    if self.variant == "non_empty":
                        (self.created_root / "foreign.txt").write_text("foreign\n")
                    elif self.variant == "changed":
                        self.created_root.rename(self.moved_root)
                        self.created_root.mkdir()
                        info = self.created_root.stat()
                        self.replacement_identity = (info.st_dev, info.st_ino)
                    raise OSError(f"injected nested-home failure: {self.variant}")
                super().write_bytes(path, data, mode=mode, phase=phase)

        for variant in ("unchanged", "non_empty", "changed"):
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    store, _, _, _ = self.arrange_switch_effect_fixture(root)
                    created_root = (root / f"created-{variant}").resolve()
                    nested_home = created_root / "parent" / "home"
                    moved_root = created_root.with_name(
                        f"{created_root.name}-transaction-owned"
                    )
                    manifest_path = store.manifest_path("internal")
                    manifest = json.loads(manifest_path.read_text())
                    manifest.update(
                        {
                            "codex_home": str(nested_home),
                            "home_mode": "adopted",
                            "home_selection_confirmed": True,
                        }
                    )
                    manifest_path.write_text(json.dumps(manifest) + "\n")
                    store.internal_codex_home = None
                    adapter = FailAfterParentEdit(
                        created_root,
                        moved_root,
                        variant,
                    )

                    receipt = execute_transaction(
                        store,
                        TransactionRequest(
                            operation="switch",
                            profile="internal",
                            options={
                                "config_mode": "snapshot",
                                "shared_config_base": None,
                                "clear_missing_auth": False,
                                "skip_shim": False,
                                "skip_app_cli": True,
                                "skip_launchctl": True,
                                "filesystem_adapter": adapter,
                            },
                        ),
                    )

                    if variant == "unchanged":
                        self.assertEqual("rolled_back", receipt.outcome)
                        self.assertFalse(created_root.exists())
                    elif variant == "non_empty":
                        self.assertEqual("rollback_failed", receipt.outcome)
                        self.assertEqual(
                            "foreign\n",
                            (created_root / "foreign.txt").read_text(),
                        )
                    else:
                        self.assertEqual("rollback_failed", receipt.outcome)
                        self.assertTrue(created_root.is_dir())
                        self.assertTrue(moved_root.is_dir())
                        self.assertIsNotNone(adapter.replacement_identity)
                        info = created_root.stat()
                        self.assertEqual(
                            adapter.replacement_identity,
                            (info.st_dev, info.st_ino),
                        )

    def test_restore_preflights_later_missing_payload_before_first_mutation(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            first = store.official_codex_home / "first.toml"
            second = store.official_codex_home / "second.toml"
            first.write_text("current-first\n")
            second.write_text("current-second\n")
            first.chmod(0o600)
            second.chmod(0o600)
            backup_dir = store.backups_dir / "later-missing"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            first_payload = payload_dir / "000-first.toml"
            first_payload.write_text("old-first\n")
            first_payload.chmod(0o600)

            def file_state(path: Path) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "kind": "file",
                    "mode": path.stat().st_mode & 0o777,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "later-missing",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(first),
                                "before_state": file_state(first_payload),
                                "committed_after_state": file_state(first),
                                "payload": "payloads/000-first.toml",
                            },
                            {
                                "path": str(second),
                                "before_state": {
                                    "kind": "file",
                                    "mode": 0o600,
                                    "size": 11,
                                    "sha256": "a" * 64,
                                },
                                "committed_after_state": file_state(second),
                                "payload": "payloads/001-second.toml",
                            },
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Backup payload is missing.*second.toml",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "later-missing", "force": False},
                    ),
                )

            self.assertEqual("current-first\n", first.read_text())
            self.assertEqual("current-second\n", second.read_text())
            self.assertEqual(["later-missing"], sorted(path.name for path in store.backups_dir.iterdir()))

    def test_restore_rejects_target_change_after_initial_preflight(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class MutateAfterInitialCapture(FilesystemAdapter):
            def __init__(self, target: Path) -> None:
                self.target = target.resolve()
                self.mutated = False

            def capture_state(self, path: Path) -> dict[str, object]:
                state = super().capture_state(path)
                if path == self.target and not self.mutated:
                    self.target.write_text("changed-after-preflight\n")
                    self.mutated = True
                return state

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            backup_dir = store.backups_dir / "preflight-race"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            payload = payload_dir / "config.toml"
            payload.write_text("historical\n")
            payload.chmod(0o600)

            def state(path: Path) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "kind": "file",
                    "mode": path.stat().st_mode & 0o777,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "preflight-race",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": state(payload),
                                "committed_after_state": state(target),
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "changed after initial preflight",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "preflight-race",
                            "force": False,
                            "filesystem_adapter": MutateAfterInitialCapture(target),
                        },
                    ),
                )

            self.assertEqual("changed-after-preflight\n", target.read_text())
            self.assertEqual(
                ["preflight-race"],
                sorted(path.name for path in store.backups_dir.iterdir()),
            )

    def test_create_switch_backup_rejects_unattested_payload_copy(self) -> None:
        from codex_switch_restore import create_switch_backup
        from codex_switch_transaction import FilesystemAdapter

        class CorruptBackupCopy(FilesystemAdapter):
            def copy_material(
                self,
                source: Path,
                destination: Path,
                kind: object,
                *,
                phase: str,
            ) -> None:
                super().copy_material(
                    source,
                    destination,
                    kind,
                    phase=phase,
                )
                if phase == "switch_backup":
                    destination.write_text("corrupted-copy\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            source = store.official_codex_home / "config.toml"
            source.write_text("current\n")
            source.chmod(0o600)

            with self.assertRaisesRegex(
                SwitchError,
                "Backup payload copy does not match captured state",
            ) as raised:
                create_switch_backup(
                    store=store,
                    operation="switch",
                    from_profile="openai-official",
                    to_profile="internal",
                    paths=[source],
                    filesystem_adapter=CorruptBackupCopy(),
                )

            self.assertEqual("current\n", source.read_text())
            backup_dirs = list(store.backups_dir.iterdir())
            self.assertEqual(1, len(backup_dirs))
            failed_backup = backup_dirs[0]
            self.assertIn(failed_backup.name, str(raised.exception))
            self.assertFalse((failed_backup / "backup.json").exists())
            failure = json.loads((failed_backup / "failure.json").read_text())
            self.assertEqual("rollback_failed", failure["lifecycle"])
            self.assertEqual(str(source), failure["failed_path"])
            self.assertTrue((failed_backup / "payloads").is_dir())

    def test_create_switch_backup_rejects_corrupt_directory_payload(self) -> None:
        from codex_switch_restore import create_switch_backup
        from codex_switch_transaction import FilesystemAdapter

        class CorruptDirectoryCopy(FilesystemAdapter):
            def copy_material(
                self,
                source: Path,
                destination: Path,
                kind: object,
                *,
                phase: str,
            ) -> None:
                super().copy_material(source, destination, kind, phase=phase)
                if phase == "switch_backup" and kind == "directory":
                    (destination / "nested.txt").write_text("corrupted\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            source = store.official_codex_home / "support"
            source.mkdir(mode=0o750)
            nested = source / "nested.txt"
            nested.write_text("original\n")
            nested.chmod(0o640)

            with self.assertRaisesRegex(
                SwitchError,
                "Backup payload copy does not match captured state",
            ):
                create_switch_backup(
                    store=store,
                    operation="switch",
                    from_profile="openai-official",
                    to_profile="internal",
                    paths=[source],
                    filesystem_adapter=CorruptDirectoryCopy(),
                )

            self.assertEqual("original\n", nested.read_text())
            failed_backup = next(store.backups_dir.iterdir())
            self.assertFalse((failed_backup / "backup.json").exists())
            self.assertTrue((failed_backup / "failure.json").is_file())

    def test_finalize_backup_rejects_payload_tampering(self) -> None:
        from codex_switch_restore import create_switch_backup, finalize_backup

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            source = store.official_codex_home / "config.toml"
            source.write_text("before\n")
            source.chmod(0o600)
            backup_dir = create_switch_backup(
                store=store,
                operation="switch",
                from_profile="openai-official",
                to_profile="internal",
                paths=[source],
            )
            manifest_path = backup_dir / "backup.json"
            prepared = json.loads(manifest_path.read_text())
            payload = backup_dir / prepared["entries"][0]["payload"]
            payload.write_text("tampered\n")

            with self.assertRaisesRegex(
                SwitchError,
                "payload no longer matches captured state",
            ):
                finalize_backup(backup_dir)

            unchanged = json.loads(manifest_path.read_text())
            self.assertEqual("prepared", unchanged["lifecycle"])
            self.assertEqual({}, unchanged["entries"][0]["committed_after_state"])

    def test_successful_custom_switch_backup_is_committed_and_restorable(
        self,
    ) -> None:
        from codex_switch_switching import switch_profile
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            executable = root / "codex-custom"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            profile_dir = store.profile_dir("custom")
            profile_dir.mkdir()
            (profile_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "custom",
                        "codex_bin": str(executable),
                        "app_cli_path": str(executable),
                    }
                )
                + "\n"
            )
            (profile_dir / "config.toml").write_text('model = "custom"\n')
            live_config = store.live_codex_home / "config.toml"
            original_config = b'[features]\nmemory = true\n'
            live_config.write_bytes(original_config)

            with redirect_stdout(io.StringIO()):
                switch_profile(
                    store,
                    "custom",
                    dry_run=False,
                    clear_missing_auth=False,
                    config_mode="snapshot",
                    shared_config_base=None,
                    skip_shim=True,
                    skip_app_cli=True,
                    skip_launchctl=True,
                )

            backups = tuple(store.backups_dir.iterdir())
            self.assertEqual(1, len(backups))
            backup_dir = backups[0]
            manifest = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual("committed", manifest["lifecycle"])
            custom_config = store.live_codex_home / "custom.config.toml"
            self.assertTrue(custom_config.is_file())

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": backup_dir.name, "force": False},
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual(original_config, live_config.read_bytes())
            self.assertFalse(custom_config.exists())

    def test_custom_apply_respects_common_store_lock_without_marker(self) -> None:
        from codex_switch_switching import switch_profile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            executable = root / "codex-custom"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            profile_dir = store.profile_dir("custom")
            profile_dir.mkdir()
            (profile_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "custom",
                        "codex_bin": str(executable),
                        "app_cli_path": str(executable),
                    }
                )
                + "\n"
            )
            (profile_dir / "config.toml").write_text('model = "custom"\n')
            (store.official_codex_home / "config.toml").write_text(
                'model = "official"\n'
            )
            before = {
                str(path.relative_to(store.root)): path.read_bytes()
                for path in store.root.rglob("*")
                if path.is_file()
            }
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_directory_lock,
                args=(str(store.root), ready, release),
            )
            holder.start()
            self.assertTrue(ready.wait(10), "lock holder did not become ready")
            try:
                with self.assertRaisesRegex(SwitchError, "profile store is busy"):
                    with redirect_stdout(io.StringIO()):
                        switch_profile(
                            store,
                            "custom",
                            dry_run=False,
                            clear_missing_auth=False,
                            config_mode="snapshot",
                            shared_config_base=None,
                            skip_shim=True,
                            skip_app_cli=True,
                            skip_launchctl=True,
                        )
            finally:
                release.set()
                holder.join(10)
                if holder.is_alive():
                    holder.terminate()
                    holder.join()

            self.assertEqual(0, holder.exitcode)
            self.assertEqual(
                before,
                {
                    str(path.relative_to(store.root)): path.read_bytes()
                    for path in store.root.rglob("*")
                    if path.is_file()
                },
            )
            self.assertFalse(any(store.root.glob(".pending-transaction-*.json")))

    def test_finalize_backup_rejects_non_integer_schema_version(self) -> None:
        from codex_switch_restore import create_switch_backup, finalize_backup

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            source = store.official_codex_home / "config.toml"
            source.write_text("before\n")
            source.chmod(0o600)
            backup_dir = create_switch_backup(
                store=store,
                operation="switch",
                from_profile="openai-official",
                to_profile="internal",
                paths=[source],
            )
            manifest_path = backup_dir / "backup.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["schema_version"] = 2.0
            manifest_path.write_text(json.dumps(manifest) + "\n")

            with self.assertRaisesRegex(
                SwitchError,
                "Unsupported backup schema version.*2.0",
            ):
                finalize_backup(backup_dir)

            self.assertEqual(
                "prepared",
                json.loads(manifest_path.read_text())["lifecycle"],
            )

    def test_restore_rejects_safety_payload_copy_mismatch(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class CorruptSafetyCopy(FilesystemAdapter):
            def copy_material(
                self,
                source: Path,
                destination: Path,
                kind: object,
                *,
                phase: str,
            ) -> None:
                super().copy_material(
                    source,
                    destination,
                    kind,
                    phase=phase,
                )
                if phase == "safety_backup":
                    destination.write_text("corrupted-safety-copy\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            backup_dir = self.write_v2_backup(
                store,
                "safety-copy-mismatch",
                [],
            )
            payload = backup_dir / "payloads" / "config.toml"
            payload.parent.mkdir()
            payload.write_text("historical\n")
            payload.chmod(0o600)
            manifest = json.loads((backup_dir / "backup.json").read_text())
            manifest["entries"] = [
                {
                    "path": str(target),
                    "before_state": self.file_state(payload),
                    "committed_after_state": self.file_state(target),
                    "payload": "payloads/config.toml",
                }
            ]
            (backup_dir / "backup.json").write_text(json.dumps(manifest) + "\n")

            with self.assertRaisesRegex(
                SwitchError,
                "Safety backup payload does not match initial state",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "safety-copy-mismatch",
                            "force": False,
                            "filesystem_adapter": CorruptSafetyCopy(),
                        },
                    ),
                )

            self.assertEqual("current\n", target.read_text())
            safety_dirs = [
                path
                for path in store.backups_dir.iterdir()
                if path.name != "safety-copy-mismatch"
            ]
            self.assertEqual(1, len(safety_dirs))
            self.assertFalse((safety_dirs[0] / "backup.json").exists())
            self.assertTrue((safety_dirs[0] / "payloads").is_dir())

    def test_restore_rejects_corrupt_payload_before_first_mutation(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o640)
            backup_dir = store.backups_dir / "corrupt-payload"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            payload = payload_dir / "config.toml"
            payload.write_text("tampered\n")
            payload.chmod(0o600)
            current_data = target.read_bytes()
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "corrupt-payload",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {
                                    "kind": "file",
                                    "mode": 0o600,
                                    "size": 11,
                                    "sha256": hashlib.sha256(b"historical\n").hexdigest(),
                                },
                                "committed_after_state": {
                                    "kind": "file",
                                    "mode": 0o640,
                                    "size": len(current_data),
                                    "sha256": hashlib.sha256(current_data).hexdigest(),
                                },
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            with self.assertRaisesRegex(
                SwitchError,
                "payload digest or state mismatch",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "corrupt-payload", "force": False},
                    ),
                )

            self.assertEqual(b"current\n", target.read_bytes())
            self.assertEqual(0o640, target.stat().st_mode & 0o777)
            self.assertEqual(
                ["corrupt-payload"],
                sorted(path.name for path in store.backups_dir.iterdir()),
            )

    def test_restore_accepts_attested_v1_file_and_symlink(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target_file = store.official_codex_home / "config.toml"
            target_link = store.official_codex_home / "current-link"
            target_file.write_text("current\n")
            target_file.chmod(0o640)
            target_link.symlink_to("current-destination")
            backup_dir = store.backups_dir / "attested-v1"
            backup_dir.mkdir(parents=True)
            payload = backup_dir / "0-config.toml"
            payload.write_text("historical\n")
            payload.chmod(0o600)

            def v1_file_state(path: Path) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "kind": "file",
                    "path": str(path),
                    "mode": path.stat().st_mode & 0o777,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "id": "attested-v1",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target_file),
                                "pre_state": v1_file_state(payload),
                                "post_state": v1_file_state(target_file),
                                "backup_rel": "0-config.toml",
                            },
                            {
                                "path": str(target_link),
                                "pre_state": {
                                    "kind": "symlink",
                                    "symlink_target": "historical-destination",
                                },
                                "post_state": {
                                    "kind": "symlink",
                                    "symlink_target": "current-destination",
                                },
                            },
                        ],
                    }
                )
                + "\n"
            )

            try:
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={"backup_id": "attested-v1", "force": False},
                    ),
                )
            except SwitchError as exc:
                self.fail(f"attested v1 restore was rejected: {exc}")

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual("historical\n", target_file.read_text())
            self.assertEqual(0o600, target_file.stat().st_mode & 0o777)
            self.assertTrue(target_link.is_symlink())
            self.assertEqual("historical-destination", os.readlink(target_link))
            self.assertIsNotNone(receipt.backup_id)
            safety_manifest = json.loads(
                (store.backups_dir / str(receipt.backup_id) / "backup.json").read_text()
            )
            self.assertEqual(2, safety_manifest["schema_version"])
            self.assertEqual("committed", safety_manifest["lifecycle"])

    def test_terminal_restore_write_followed_by_catchable_error_stays_committed(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class RaiseAfterTerminalWriteAdapter(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "committed_manifest":
                    raise OSError("injected after terminal restore write")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            current_state = self.file_state(target)
            backup_dir = store.backups_dir / "terminal-restore-source"
            payload = backup_dir / "payloads" / "0000-config.toml"
            payload.parent.mkdir(parents=True)
            payload.write_text("historical\n")
            payload.chmod(0o600)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": self.file_state(payload),
                                "committed_after_state": current_state,
                                "payload": "payloads/0000-config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": backup_dir.name,
                        "force": False,
                        "filesystem_adapter": RaiseAfterTerminalWriteAdapter(),
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual("historical\n", target.read_text())
            safety_manifest = json.loads(
                (
                    store.backups_dir
                    / str(receipt.backup_id)
                    / "backup.json"
                ).read_text()
            )
            self.assertEqual("committed", safety_manifest["lifecycle"])
            self.assertEqual(
                "committed",
                safety_manifest["restore_journal"]["state"],
            )

    def test_failed_restore_rolls_back_applied_entries(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailAfterFirstMaterialize(FilesystemAdapter):
            def __init__(self, fail_target: Path) -> None:
                self.fail_target = fail_target.resolve()
                self.events: list[tuple[str, str]] = []

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.events.append((phase, destination.name))
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply" and destination == self.fail_target:
                    raise OSError("injected apply failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            first = store.official_codex_home / "first.toml"
            second = store.official_codex_home / "second.toml"
            first.write_text("current-first\n")
            second.write_text("current-second\n")
            first.chmod(0o600)
            second.chmod(0o600)
            backup_dir = store.backups_dir / "rollback-applied"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            old_first = payload_dir / "first.toml"
            old_second = payload_dir / "second.toml"
            old_first.write_text("old-first\n")
            old_second.write_text("old-second\n")
            old_first.chmod(0o600)
            old_second.chmod(0o600)

            def state(path: Path) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "kind": "file",
                    "mode": path.stat().st_mode & 0o777,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "rollback-applied",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(first),
                                "before_state": state(old_first),
                                "committed_after_state": state(first),
                                "payload": "payloads/first.toml",
                            },
                            {
                                "path": str(second),
                                "before_state": state(old_second),
                                "committed_after_state": state(second),
                                "payload": "payloads/second.toml",
                            },
                        ],
                    }
                )
                + "\n"
            )

            adapter = FailAfterFirstMaterialize(second)
            try:
                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": "rollback-applied",
                            "force": False,
                            "filesystem_adapter": adapter,
                        },
                    ),
                )
            except SwitchError as exc:
                self.fail(f"restore returned no rollback receipt: {exc}")

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual("current-first\n", first.read_text())
            self.assertEqual("current-second\n", second.read_text())
            self.assertEqual(
                [
                    ("apply", "first.toml"),
                    ("apply", "second.toml"),
                    ("rollback", "second.toml"),
                    ("rollback", "first.toml"),
                ],
                adapter.events,
            )
            self.assertIsNotNone(receipt.backup_id)
            safety_manifest = json.loads(
                (store.backups_dir / str(receipt.backup_id) / "backup.json").read_text()
            )
            self.assertEqual("rolled_back", safety_manifest["lifecycle"])

    def test_failed_restore_removes_parents_created_by_apply(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailAfterApply(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    raise OSError("injected apply failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "new" / "nested" / "config.toml"
            backup_dir = store.backups_dir / "rollback-new-parents"
            payload = backup_dir / "payloads" / "config.toml"
            payload.parent.mkdir(parents=True)
            payload.write_text("historical\n")
            payload.chmod(0o600)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": self.file_state(payload),
                                "committed_after_state": {"kind": "missing"},
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": backup_dir.name,
                        "force": False,
                        "filesystem_adapter": FailAfterApply(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertFalse(target.exists())
            self.assertFalse((store.official_codex_home / "new").exists())

    def test_rollback_keeps_parent_created_by_another_writer(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class ExternalParentThenFail(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "apply":
                    destination.parent.mkdir(parents=True, exist_ok=True)
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    raise OSError("injected apply failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            external_parent = store.official_codex_home / "external"
            target = external_parent / "config.toml"
            backup_dir = store.backups_dir / "external-parent"
            payload = backup_dir / "payloads" / "config.toml"
            payload.parent.mkdir(parents=True)
            payload.write_text("historical\n")
            payload.chmod(0o600)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": self.file_state(payload),
                                "committed_after_state": {"kind": "missing"},
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": backup_dir.name,
                        "force": False,
                        "filesystem_adapter": ExternalParentThenFail(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertFalse(target.exists())
            self.assertTrue(external_parent.is_dir())

    def test_restore_rechecks_each_target_immediately_before_apply(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class MutateSecondAfterFirst(FilesystemAdapter):
            def __init__(self, first: Path, second: Path) -> None:
                self.first = first.resolve()
                self.second = second.resolve()

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply" and destination == self.first:
                    self.second.write_text("external-second\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            first = store.official_codex_home / "first.toml"
            second = store.official_codex_home / "second.toml"
            first.write_text("current-first\n")
            second.write_text("current-second\n")
            first.chmod(0o600)
            second.chmod(0o600)
            backup_dir = store.backups_dir / "per-target-recheck"
            payloads = backup_dir / "payloads"
            payloads.mkdir(parents=True)
            old_first = payloads / "first.toml"
            old_second = payloads / "second.toml"
            old_first.write_text("old-first\n")
            old_second.write_text("old-second\n")
            old_first.chmod(0o600)
            old_second.chmod(0o600)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(first),
                                "before_state": self.file_state(old_first),
                                "committed_after_state": self.file_state(first),
                                "payload": "payloads/first.toml",
                            },
                            {
                                "path": str(second),
                                "before_state": self.file_state(old_second),
                                "committed_after_state": self.file_state(second),
                                "payload": "payloads/second.toml",
                            },
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": backup_dir.name,
                        "force": False,
                        "filesystem_adapter": MutateSecondAfterFirst(first, second),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual("current-first\n", first.read_text())
            self.assertEqual("external-second\n", second.read_text())

    def test_restore_late_external_drift_fails_closed_without_reverse_writes(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class MutateFirstAfterSecond(FilesystemAdapter):
            def __init__(self, first: Path, second: Path) -> None:
                self.first = first.resolve()
                self.second = second.resolve()

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply" and destination == self.second:
                    self.first.write_text("external-after-apply\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            first = store.official_codex_home / "first.toml"
            second = store.official_codex_home / "second.toml"
            first.write_text("current-first\n")
            second.write_text("current-second\n")
            first.chmod(0o600)
            second.chmod(0o600)
            backup_dir = store.backups_dir / "precommit-recheck"
            payloads = backup_dir / "payloads"
            payloads.mkdir(parents=True)
            old_first = payloads / "first.toml"
            old_second = payloads / "second.toml"
            old_first.write_text("old-first\n")
            old_second.write_text("old-second\n")
            old_first.chmod(0o600)
            old_second.chmod(0o600)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": backup_dir.name,
                        "operation": "switch",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(first),
                                "before_state": self.file_state(old_first),
                                "committed_after_state": self.file_state(first),
                                "payload": "payloads/first.toml",
                            },
                            {
                                "path": str(second),
                                "before_state": self.file_state(old_second),
                                "committed_after_state": self.file_state(second),
                                "payload": "payloads/second.toml",
                            },
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": backup_dir.name,
                        "force": False,
                        "filesystem_adapter": MutateFirstAfterSecond(first, second),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual("external-after-apply\n", first.read_text())
            self.assertEqual("old-second\n", second.read_text())
            self.assertIsNotNone(receipt.backup_id)
            safety_dir = store.backups_dir / str(receipt.backup_id)
            safety_manifest = json.loads((safety_dir / "backup.json").read_text())
            self.assertEqual("rollback_failed", safety_manifest["lifecycle"])
            self.assertTrue((safety_dir / "payloads").is_dir())
            self.assertTrue((safety_dir / "restore-stage").is_dir())
            markers = tuple(store.root.glob(".pending-transaction-*.json"))
            self.assertEqual(1, len(markers))
            self.assertEqual(
                str(receipt.backup_id),
                str(json.loads(markers[0].read_text())["backup_id"]),
            )

    def test_restore_creates_reversible_safety_backup(self) -> None:
        from codex_switch_restore import restore_backup
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            backup_dir = store.backups_dir / "historical"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            payload = payload_dir / "config.toml"
            payload.write_text("historical\n")
            payload.chmod(0o600)

            def state(path: Path) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "kind": "file",
                    "mode": path.stat().st_mode & 0o777,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "historical",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": state(payload),
                                "committed_after_state": state(target),
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            first_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": "historical", "force": False},
                ),
            )
            self.assertEqual("historical\n", target.read_text())
            self.assertIsNotNone(first_receipt.backup_id)
            safety_id = str(first_receipt.backup_id)
            safety_manifest_path = store.backups_dir / safety_id / "backup.json"
            safety_manifest = json.loads(safety_manifest_path.read_text())
            self.assertEqual("committed", safety_manifest["lifecycle"])

            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    restore_backup(
                        store,
                        safety_id,
                        dry_run=False,
                        apply=True,
                        force=False,
                    )
            except SwitchError as exc:
                self.fail(f"schema-v2 safety backup was not reversible: {exc}")

            self.assertEqual("current\n", target.read_text())
            self.assertIn(f"restore backup {safety_id}", output.getvalue())
            self.assertIn(f"Restored backup {safety_id}", output.getvalue())
            self.assertTrue(safety_manifest_path.exists())

    def test_successful_restore_safety_backup_removes_created_parents(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "new" / "nested" / "config.toml"
            historical_dir = store.backups_dir / "historical-new-parents"
            payload = historical_dir / "payloads" / "config.toml"
            payload.parent.mkdir(parents=True)
            payload.write_text("historical\n")
            payload.chmod(0o600)
            (historical_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": historical_dir.name,
                        "operation": "switch",
                        "to_profile": "openai-official",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": self.file_state(payload),
                                "committed_after_state": {"kind": "missing"},
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": historical_dir.name,
                        "force": False,
                    },
                ),
            )
            self.assertEqual("committed", receipt.outcome)
            self.assertTrue(target.is_file())
            self.assertIsNotNone(receipt.backup_id)

            reverse_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": str(receipt.backup_id),
                        "force": False,
                    },
                ),
            )

            self.assertEqual("committed", reverse_receipt.outcome)
            self.assertFalse(target.exists())
            self.assertFalse((store.official_codex_home / "new").exists())

    def test_rollback_failure_preserves_material_and_backup_id(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailApplyAndRollback(FilesystemAdapter):
            def __init__(self, apply_target: Path, rollback_target: Path) -> None:
                self.apply_target = apply_target.resolve()
                self.rollback_target = rollback_target.resolve()

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "rollback" and destination == self.rollback_target:
                    raise OSError("injected rollback failure")
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply" and destination == self.apply_target:
                    raise OSError("injected apply failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            first = store.official_codex_home / "first.toml"
            second = store.official_codex_home / "second.toml"
            first.write_text("current-first\n")
            second.write_text("current-second\n")
            first.chmod(0o600)
            second.chmod(0o600)
            backup_dir = store.backups_dir / "rollback-failure"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            old_first = payload_dir / "first.toml"
            old_second = payload_dir / "second.toml"
            old_first.write_text("old-first\n")
            old_second.write_text("old-second\n")
            old_first.chmod(0o600)
            old_second.chmod(0o600)

            def state(path: Path) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "kind": "file",
                    "mode": path.stat().st_mode & 0o777,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "rollback-failure",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(first),
                                "before_state": state(old_first),
                                "committed_after_state": state(first),
                                "payload": "payloads/first.toml",
                            },
                            {
                                "path": str(second),
                                "before_state": state(old_second),
                                "committed_after_state": state(second),
                                "payload": "payloads/second.toml",
                            },
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": "rollback-failure",
                        "force": False,
                        "filesystem_adapter": FailApplyAndRollback(second, first),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            safety_dir = store.backups_dir / str(receipt.backup_id)
            safety_manifest = json.loads((safety_dir / "backup.json").read_text())
            self.assertEqual("rollback_failed", safety_manifest["lifecycle"])
            self.assertIn("injected rollback failure", safety_manifest["error"])
            self.assertTrue((safety_dir / "payloads").is_dir())
            self.assertTrue((safety_dir / "restore-stage").is_dir())
            self.assertTrue((backup_dir / "backup.json").exists())
            self.assertEqual("old-first\n", first.read_text())
            self.assertEqual("current-second\n", second.read_text())

    def test_rollback_finalization_failure_returns_durable_receipt(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailRollbackFinalization(FilesystemAdapter):
            def __init__(self, failure: str) -> None:
                self.failure = failure

            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().materialize(source, destination, state, phase=phase)
                if phase == "apply":
                    raise OSError("injected apply failure")

            def remove_tree(self, path: Path, *, phase: str) -> None:
                if self.failure == "cleanup" and phase == "rollback_stage_cleanup":
                    raise OSError("injected rollback cleanup failure")
                super().remove_tree(path, phase=phase)

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if self.failure == "manifest" and phase == "rolled_back_manifest":
                    raise OSError("injected rolled-back manifest failure")
                super().write_manifest(path, data, phase=phase)

        for failure in ("cleanup", "manifest"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                target = store.official_codex_home / "config.toml"
                target.write_text("current\n")
                target.chmod(0o600)
                backup_dir = self.write_v2_backup(
                    store,
                    f"rollback-finalize-{failure}",
                    [],
                )
                payload = backup_dir / "payloads" / "config.toml"
                payload.parent.mkdir()
                payload.write_text("historical\n")
                payload.chmod(0o600)
                manifest = json.loads((backup_dir / "backup.json").read_text())
                manifest["entries"] = [
                    {
                        "path": str(target),
                        "before_state": self.file_state(payload),
                        "committed_after_state": self.file_state(target),
                        "payload": "payloads/config.toml",
                    }
                ]
                (backup_dir / "backup.json").write_text(
                    json.dumps(manifest) + "\n"
                )

                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="restore",
                        profile="restore",
                        options={
                            "backup_id": f"rollback-finalize-{failure}",
                            "force": False,
                            "filesystem_adapter": FailRollbackFinalization(failure),
                        },
                    ),
                )

                self.assertEqual("rollback_failed", receipt.outcome)
                self.assertIsNotNone(receipt.backup_id)
                self.assertEqual("current\n", target.read_text())
                safety_dir = store.backups_dir / str(receipt.backup_id)
                safety_manifest = json.loads(
                    (safety_dir / "backup.json").read_text()
                )
                self.assertEqual("rollback_failed", safety_manifest["lifecycle"])
                self.assertTrue((safety_dir / "payloads").is_dir())
                self.assertEqual(
                    failure == "cleanup",
                    (safety_dir / "restore-stage").exists(),
                )

    def test_silent_rollback_mismatch_is_reported_as_rollback_failure(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class SilentRollbackMismatch(FilesystemAdapter):
            def materialize(
                self,
                source: Path | None,
                destination: Path,
                state: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "rollback":
                    return
                super().materialize(source, destination, state, phase=phase)
                raise OSError("injected apply failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "config.toml"
            target.write_text("current\n")
            target.chmod(0o600)
            backup_dir = store.backups_dir / "silent-rollback-mismatch"
            payload_dir = backup_dir / "payloads"
            payload_dir.mkdir(parents=True)
            payload = payload_dir / "config.toml"
            payload.write_text("historical\n")
            payload.chmod(0o600)

            def state(path: Path) -> dict[str, object]:
                data = path.read_bytes()
                return {
                    "kind": "file",
                    "mode": path.stat().st_mode & 0o777,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "silent-rollback-mismatch",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": state(payload),
                                "committed_after_state": state(target),
                                "payload": "payloads/config.toml",
                            }
                        ],
                    }
                )
                + "\n"
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={
                        "backup_id": "silent-rollback-mismatch",
                        "force": False,
                        "filesystem_adapter": SilentRollbackMismatch(),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertIsNotNone(receipt.backup_id)
            safety_dir = store.backups_dir / str(receipt.backup_id)
            safety_manifest = json.loads((safety_dir / "backup.json").read_text())
            self.assertEqual("rollback_failed", safety_manifest["lifecycle"])
            self.assertIn("state mismatch", safety_manifest["error"])
            self.assertEqual("historical\n", target.read_text())

    def test_invalid_capture_toml_preserves_existing_profile(self) -> None:
        from codex_switch_capture import capture_profile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            before = {
                path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in profile_dir.iterdir()
            }
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text("[broken\n")
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            codex_bin = self.make_executable(root)

            with self.assertRaisesRegex(SwitchError, "Invalid TOML"):
                capture_profile(
                    store=store,
                    name="internal",
                    source_home=source_home,
                    codex_bin=str(codex_bin),
                    app_cli_path=str(codex_bin),
                    allow_missing_auth=False,
                    overwrite=True,
                )

            self.assertEqual(
                before,
                {
                    path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                    for path in profile_dir.iterdir()
                },
            )

    def test_capture_dry_run_overwrite_existing_profile_is_read_only(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class RecordFilesystemMutations(FilesystemAdapter):
            def __init__(self) -> None:
                self.mutations: list[str] = []

            def create_store_root(self, path: Path) -> None:
                self.mutations.append("create_store_root")
                super().create_store_root(path)

            def copy_material(
                self,
                source: Path,
                destination: Path,
                kind: object,
                *,
                phase: str,
            ) -> None:
                self.mutations.append(f"copy_material:{phase}")
                super().copy_material(source, destination, kind, phase=phase)

            def make_directory(
                self,
                path: Path,
                *,
                mode: int,
                phase: str,
            ) -> tuple[int, int]:
                self.mutations.append(f"make_directory:{phase}")
                return super().make_directory(path, mode=mode, phase=phase)

            def chmod(self, path: Path, mode: int, *, phase: str) -> None:
                self.mutations.append(f"chmod:{phase}")
                super().chmod(path, mode, phase=phase)

            def remove_path(self, path: Path, *, phase: str) -> None:
                self.mutations.append(f"remove_path:{phase}")
                super().remove_path(path, phase=phase)

            def remove_tree(self, path: Path, *, phase: str) -> None:
                self.mutations.append(f"remove_tree:{phase}")
                super().remove_tree(path, phase=phase)

            def remove_file(self, path: Path, *, phase: str) -> None:
                self.mutations.append(f"remove_file:{phase}")
                super().remove_file(path, phase=phase)

            def rename(
                self,
                source: Path,
                destination: Path,
                *,
                phase: str,
            ) -> None:
                self.mutations.append(f"rename:{phase}")
                super().rename(source, destination, phase=phase)

            def sync_file(self, path: Path, *, phase: str) -> None:
                self.mutations.append(f"sync_file:{phase}")
                super().sync_file(path, phase=phase)

            def sync_directory(self, path: Path, *, phase: str) -> None:
                self.mutations.append(f"sync_directory:{phase}")
                super().sync_directory(path, phase=phase)

            def sync_tree(
                self,
                path: Path,
                *,
                file_phase: str,
                directory_phase: str,
            ) -> None:
                self.mutations.append(
                    f"sync_tree:{file_phase}:{directory_phase}"
                )
                super().sync_tree(
                    path,
                    file_phase=file_phase,
                    directory_phase=directory_phase,
                )

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                self.mutations.append(f"write_manifest:{phase}")
                super().write_manifest(path, data, phase=phase)

        def snapshot_tree(path: Path) -> dict[str, tuple[object, ...]]:
            snapshot: dict[str, tuple[object, ...]] = {}

            def visit(current: Path) -> None:
                info = current.lstat()
                relative = (
                    "." if current == path else current.relative_to(path).as_posix()
                )
                mode = info.st_mode & 0o777
                if current.is_symlink():
                    snapshot[relative] = (
                        "symlink",
                        mode,
                        info.st_mtime_ns,
                        os.readlink(current),
                    )
                    return
                if current.is_dir():
                    snapshot[relative] = ("directory", mode, info.st_mtime_ns)
                    for child in sorted(current.iterdir(), key=lambda item: item.name):
                        visit(child)
                    return
                snapshot[relative] = (
                    "file",
                    mode,
                    info.st_mtime_ns,
                    current.read_bytes(),
                )

            visit(path)
            return snapshot

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _, options = self.arrange_capture_fixture(
                root,
                unmanaged=True,
            )
            support_dir = profile_dir / "plugin-support"
            support_dir.chmod(0o750)
            support_file = support_dir / "catalog.json"
            support_file.chmod(0o640)
            (support_dir / "current").symlink_to("catalog.json")
            adapter = RecordFilesystemMutations()
            request = TransactionRequest(
                operation="capture",
                profile="internal",
                options={**options, "filesystem_adapter": adapter},
            )
            artifact_paths = (
                store.profiles_dir / ".internal.capture-stage",
                store.profiles_dir / ".internal.capture-previous",
                store.profiles_dir / ".internal.capture-journal.json",
            )
            before_digest = capture_path_state(store.root)
            before_tree = snapshot_tree(store.root)

            try:
                receipt = execute_transaction(store, request, dry_run=True)
            except SwitchError as exc:
                self.fail(f"existing-profile capture dry run was rejected: {exc}")

            self.assertEqual("capture", receipt.operation)
            self.assertEqual("dry_run", receipt.outcome)
            self.assertEqual(
                ("Captured profile internal: config.toml, auth.json",),
                receipt.preview_lines,
            )
            self.assertEqual(before_digest, capture_path_state(store.root))
            self.assertEqual(before_tree, snapshot_tree(store.root))
            self.assertTrue(all(not path.exists() for path in artifact_paths))
            self.assertEqual([], adapter.mutations)

    def test_capture_manifest_write_config_corruption_is_rejected_before_journal(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class CorruptConfigAfterManifestWrite(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "capture_manifest":
                    (path.parent / "config.toml").write_text("[broken\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            before_files = {
                path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in profile_dir.iterdir()
            }
            before_tree = capture_path_state(profile_dir)
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            with self.assertRaisesRegex(
                SwitchError,
                "Staged capture config|Invalid TOML",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": CorruptConfigAfterManifestWrite(),
                        },
                    ),
                )

            self.assertEqual(
                before_files,
                {
                    path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                    for path in profile_dir.iterdir()
                },
            )
            self.assertEqual(before_tree, capture_path_state(profile_dir))
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-stage").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-journal.json").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

    def test_capture_post_read_config_corruption_is_rejected_before_journal(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class CorruptConfigAfterValidatedRead(FilesystemAdapter):
            def read_text(self, path: Path) -> str:
                text = super().read_text(path)
                path.write_text("[broken\n")
                return text

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            before_files = {
                path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in profile_dir.iterdir()
            }
            before_tree = capture_path_state(profile_dir)
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            with self.assertRaisesRegex(
                SwitchError,
                "Staged capture state changed",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": CorruptConfigAfterValidatedRead(),
                        },
                    ),
                )

            self.assertEqual(
                before_files,
                {
                    path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                    for path in profile_dir.iterdir()
                },
            )
            self.assertEqual(before_tree, capture_path_state(profile_dir))
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_required_auth_capture_failure_preserves_existing_profile(self) -> None:
        from codex_switch_capture import capture_profile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            before = {
                path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in profile_dir.iterdir()
            }
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            codex_bin = self.make_executable(root)

            with self.assertRaisesRegex(SwitchError, "allow-missing-auth"):
                capture_profile(
                    store=store,
                    name="internal",
                    source_home=source_home,
                    codex_bin=str(codex_bin),
                    app_cli_path=str(codex_bin),
                    allow_missing_auth=False,
                    overwrite=True,
                )

            self.assertEqual(
                before,
                {
                    path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
                    for path in profile_dir.iterdir()
                },
            )

    def test_allowed_missing_auth_removes_stale_auth(self) -> None:
        from codex_switch_capture import capture_profile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"stale"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            codex_bin = self.make_executable(root)

            capture_profile(
                store=store,
                name="internal",
                source_home=source_home,
                codex_bin=str(codex_bin),
                app_cli_path=str(codex_bin),
                allow_missing_auth=True,
                overwrite=True,
            )

            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            self.assertFalse((profile_dir / "auth.json").exists())
            self.assertEqual(
                "internal",
                json.loads((profile_dir / "manifest.json").read_text())["name"],
            )

    def test_capture_preserves_unmanaged_plugin_support_files(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            support_dir = profile_dir / "plugin-support"
            support_dir.mkdir(parents=True)
            support_file = support_dir / "catalog.json"
            support_file.write_text('{"plugin":"kept"}\n')
            support_file.chmod(0o640)
            (support_dir / "current").symlink_to("catalog.json")
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('{"plugin":"kept"}\n', support_file.read_text())
            self.assertEqual(0o640, support_file.stat().st_mode & 0o777)
            self.assertTrue((support_dir / "current").is_symlink())
            self.assertEqual("catalog.json", os.readlink(support_dir / "current"))
            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            self.assertEqual('{"token":"after"}\n', (profile_dir / "auth.json").read_text())

    def test_capture_cloned_managed_symlinks_cannot_escape_stage(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            external_config = root / "external-config.toml"
            external_auth = root / "external-auth.json"
            external_manifest = root / "external-manifest.json"
            external_config.write_text('sentinel = "config"\n')
            external_auth.write_text('{"sentinel":"auth"}\n')
            external_manifest.write_text('{"sentinel":"manifest"}\n')
            (profile_dir / "config.toml").symlink_to(external_config)
            (profile_dir / "auth.json").symlink_to(external_auth)
            (profile_dir / "manifest.json").symlink_to(external_manifest)
            (profile_dir / "plugin-support.json").write_text('{"kept":true}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('sentinel = "config"\n', external_config.read_text())
            self.assertEqual('{"sentinel":"auth"}\n', external_auth.read_text())
            self.assertEqual(
                '{"sentinel":"manifest"}\n', external_manifest.read_text()
            )
            self.assertFalse((profile_dir / "config.toml").is_symlink())
            self.assertFalse((profile_dir / "auth.json").is_symlink())
            self.assertFalse((profile_dir / "manifest.json").is_symlink())
            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            self.assertEqual('{"token":"after"}\n', (profile_dir / "auth.json").read_text())
            self.assertEqual(
                '{"kept":true}\n',
                (profile_dir / "plugin-support.json").read_text(),
            )

    def test_capture_config_symlink_injected_before_copy_cannot_escape_stage(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            external_config = root / "external.toml"
            external_config.write_text('sentinel = "unchanged"\n')
            external_before = external_config.read_bytes()

            class InjectConfigSymlinkBeforeCopy(FilesystemAdapter):
                def copy_material(
                    self,
                    source: Path,
                    destination: Path,
                    kind: object,
                    *,
                    phase: str,
                ) -> None:
                    if phase == "capture_config":
                        destination.symlink_to(external_config)
                    super().copy_material(
                        source,
                        destination,
                        kind,
                        phase=phase,
                    )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": InjectConfigSymlinkBeforeCopy(),
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual(external_before, external_config.read_bytes())
            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            self.assertEqual('{"token":"after"}\n', (profile_dir / "auth.json").read_text())

    def test_required_auth_disappearance_after_preflight_preserves_profile(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            source_auth = source_home / "auth.json"
            (source_home / "config.toml").write_text('model = "after"\n')
            source_auth.write_text('{"token":"after"}\n')

            class RemoveRequiredAuthAfterPreflight(FilesystemAdapter):
                def copy_material(
                    self,
                    source: Path,
                    destination: Path,
                    kind: object,
                    *,
                    phase: str,
                ) -> None:
                    super().copy_material(source, destination, kind, phase=phase)
                    if phase == "capture_config":
                        source_auth.unlink()

            with self.assertRaisesRegex(SwitchError, "auth.*changed|auth.*missing"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": RemoveRequiredAuthAfterPreflight(),
                        },
                    ),
                )

            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_allowed_missing_auth_appearance_after_preflight_preserves_profile(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            source_auth = source_home / "auth.json"
            (source_home / "config.toml").write_text('model = "after"\n')

            class AddAllowedAuthAfterPreflight(FilesystemAdapter):
                def copy_material(
                    self,
                    source: Path,
                    destination: Path,
                    kind: object,
                    *,
                    phase: str,
                ) -> None:
                    super().copy_material(source, destination, kind, phase=phase)
                    if phase == "capture_config":
                        source_auth.write_text('{"token":"appeared"}\n')

            with self.assertRaisesRegex(SwitchError, "auth source changed"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": True,
                            "overwrite": True,
                            "filesystem_adapter": AddAllowedAuthAfterPreflight(),
                        },
                    ),
                )

            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_first_capture_busy_race_does_not_chmod_store_root_before_lock(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class CreateAndLockStoreRoot(FilesystemAdapter):
            def __init__(self) -> None:
                self.descriptor: int | None = None

            def create_store_root(self, path: Path) -> None:
                path.mkdir(mode=0o755)
                path.chmod(0o755)
                self.descriptor = os.open(path, os.O_RDONLY)
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def close(self) -> None:
                if self.descriptor is not None:
                    fcntl.flock(self.descriptor, fcntl.LOCK_UN)
                    os.close(self.descriptor)
                    self.descriptor = None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(
                root=root / "store",
                official_codex_home=root / "official",
                internal_codex_home=root / "internal",
                launch_agent_path=root / "agent.plist",
                launch_agent_label="test.codex-switch",
            )
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            adapter = CreateAndLockStoreRoot()

            try:
                with self.assertRaisesRegex(SwitchError, "profile store is busy"):
                    execute_transaction(
                        store,
                        TransactionRequest(
                            operation="capture",
                            profile="internal",
                            options={
                                "source_home": source_home,
                                "codex_bin": "/tmp/codex-internal",
                                "app_cli_path": "/tmp/codex-internal",
                                "allow_missing_auth": False,
                                "overwrite": True,
                                "filesystem_adapter": adapter,
                            },
                        ),
                    )
            finally:
                adapter.close()

            self.assertEqual(0o755, store.root.stat().st_mode & 0o777)
            self.assertEqual([], list(store.root.iterdir()))

    def test_capture_rejects_outward_profiles_directory_symlink(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            outside = root / "outside-profiles"
            outside.mkdir(mode=0o755)
            outside.chmod(0o755)
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside must stay unchanged\n")
            store.profiles_dir.symlink_to(outside, target_is_directory=True)
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            with self.assertRaisesRegex(SwitchError, "profiles.*symlink|profiles.*directory"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                        },
                    ),
                )

            self.assertEqual("outside must stay unchanged\n", sentinel.read_text())
            self.assertEqual(0o755, outside.stat().st_mode & 0o777)
            self.assertEqual(["sentinel.txt"], [path.name for path in outside.iterdir()])

    def test_capture_rejects_replaced_profiles_parent_before_artifact_write(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            outside = root / "outside-profiles"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("outside must stay unchanged\n")
            detached = root / "detached-profiles"

            class ReplaceProfilesParent(FilesystemAdapter):
                def __init__(self) -> None:
                    self.replaced = False

                def capture_parent_checkpoint(self, path: Path, *, phase: str) -> None:
                    if phase == "capture_before_recovery" and not self.replaced:
                        path.rename(detached)
                        path.symlink_to(outside, target_is_directory=True)
                        self.replaced = True

            with self.assertRaisesRegex(SwitchError, "profiles.*changed"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": ReplaceProfilesParent(),
                        },
                    ),
                )

            self.assertEqual("outside must stay unchanged\n", sentinel.read_text())
            self.assertEqual(["sentinel.txt"], [path.name for path in outside.iterdir()])
            self.assertEqual(
                'model = "before"\n',
                (detached / "internal" / "config.toml").read_text(),
            )

    def test_capture_does_not_delete_unowned_stage_created_before_mkdir(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            stage_dir = store.profiles_dir / ".internal.capture-stage"

            class CreateUnownedStageBeforeMkdir(FilesystemAdapter):
                def __init__(self) -> None:
                    self.created = False

                def capture_parent_checkpoint(self, path: Path, *, phase: str) -> None:
                    if phase == "capture_before_stage_write" and not self.created:
                        stage_dir.mkdir()
                        (stage_dir / "sentinel.txt").write_text("must remain\n")
                        self.created = True

            raised: BaseException | None = None
            try:
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": CreateUnownedStageBeforeMkdir(),
                        },
                    ),
                )
            except BaseException as exc:
                raised = exc

            self.assertIsInstance(raised, SwitchError)
            self.assertIn("not owned", str(raised))
            self.assertEqual("must remain\n", (stage_dir / "sentinel.txt").read_text())
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-journal.json").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

    def test_capture_clone_parent_swap_cannot_write_outside_pinned_workspace(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            before_profile = capture_path_state(profile_dir)
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            outside = root / "outside-profiles"
            outside_profile = outside / "internal"
            outside_profile.mkdir(parents=True)
            (outside_profile / "config.toml").write_text(
                'model = "outside-sentinel"\n'
            )
            outside_state = capture_path_state(outside)
            detached = root / "detached-profiles"

            class SwapParentInsideClone(FilesystemAdapter):
                def __init__(self) -> None:
                    self.replaced = False

                def copy_material(
                    self,
                    source: Path,
                    destination: Path,
                    kind: object,
                    *,
                    phase: str,
                ) -> None:
                    if phase == "capture_clone" and not self.replaced:
                        destination.parent.rename(detached)
                        destination.parent.symlink_to(
                            outside,
                            target_is_directory=True,
                        )
                        self.replaced = True
                    super().copy_material(
                        source,
                        destination,
                        kind,
                        phase=phase,
                    )

            with self.assertRaisesRegex(SwitchError, "profiles.*changed"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": SwapParentInsideClone(),
                        },
                    ),
                )

            self.assertEqual(outside_state, capture_path_state(outside))
            detached_profile_state = capture_path_state(detached / "internal")
            self.assertEqual(
                {
                    key: before_profile[key]
                    for key in ("kind", "mode", "entry_count", "tree_sha256")
                },
                {
                    key: detached_profile_state[key]
                    for key in ("kind", "mode", "entry_count", "tree_sha256")
                },
            )
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in detached.iterdir()),
            )
            self.assertTrue(store.profiles_dir.is_symlink())

    def test_capture_second_rename_failure_restores_previous_profile(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailSecondCaptureRename(FilesystemAdapter):
            def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                if phase == "capture_stage_to_destination":
                    raise OSError("injected second capture rename failure")
                super().rename(source, destination, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            (profile_dir / "plugin-support.json").write_text('{"kept":true}\n')
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": FailSecondCaptureRename(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_capture_replaced_previous_is_rejected_before_rollback_install(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            previous_dir = store.profiles_dir / ".internal.capture-previous"

            class ReplacePreviousBeforeRollback(FilesystemAdapter):
                def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                    if phase == "capture_stage_to_destination":
                        shutil.rmtree(previous_dir)
                        previous_dir.mkdir()
                        (previous_dir / "config.toml").write_text(
                            'model = "attacker"\n'
                        )
                        (previous_dir / "auth.json").write_text(
                            '{"token":"attacker"}\n'
                        )
                        (previous_dir / "manifest.json").write_text(
                            '{"name":"attacker"}\n'
                        )
                        raise OSError("injected second rename failure")
                    super().rename(source, destination, phase=phase)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": ReplacePreviousBeforeRollback(),
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertFalse(profile_dir.exists())
            self.assertEqual(
                'model = "attacker"\n',
                (previous_dir / "config.toml").read_text(),
            )
            self.assertTrue(
                (store.profiles_dir / ".internal.capture-stage").is_dir()
            )
            self.assertTrue(
                (store.profiles_dir / ".internal.capture-journal.json").is_file()
            )

    def test_capture_rollback_destination_drift_is_rejected_before_mutation(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            displaced_destination = root / "displaced-new-profile"

            class ReplaceDestinationAtRollbackCheckpoint(FilesystemAdapter):
                def __init__(self) -> None:
                    self.replaced = False
                    self.rollback_renames: list[str] = []

                def write_manifest(
                    self,
                    path: Path,
                    data: dict[str, object],
                    *,
                    phase: str,
                ) -> None:
                    if phase == "capture_finalize":
                        raise OSError("injected capture finalize failure")
                    super().write_manifest(path, data, phase=phase)

                def capture_parent_checkpoint(self, path: Path, *, phase: str) -> None:
                    if (
                        phase == "capture_before_rollback_destination_to_stage"
                        and not self.replaced
                    ):
                        destination = path / "internal"
                        destination.rename(displaced_destination)
                        destination.mkdir()
                        (destination / "config.toml").write_text(
                            'model = "attacker"\n'
                        )
                        self.replaced = True

                def rename(
                    self,
                    source: Path,
                    destination: Path,
                    *,
                    phase: str,
                ) -> None:
                    if phase.startswith("capture_rollback_"):
                        self.rollback_renames.append(phase)
                    super().rename(source, destination, phase=phase)

            adapter = ReplaceDestinationAtRollbackCheckpoint()
            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual([], adapter.rollback_renames)
            self.assertEqual(
                'model = "attacker"\n',
                (profile_dir / "config.toml").read_text(),
            )
            self.assertEqual(
                'model = "after"\n',
                (displaced_destination / "config.toml").read_text(),
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-stage").exists()
            )
            self.assertEqual(
                'model = "before"\n',
                (
                    store.profiles_dir
                    / ".internal.capture-previous"
                    / "config.toml"
                ).read_text(),
            )
            self.assertTrue(
                (store.profiles_dir / ".internal.capture-journal.json").is_file()
            )

    def test_capture_staged_state_attestation_failure_removes_unjournaled_stage(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            stage_dir = store.profiles_dir / ".internal.capture-stage"

            class FailStagedStateAttestation(FilesystemAdapter):
                def __init__(self) -> None:
                    self.failed = False

                def capture_state(self, path: Path) -> dict[str, object]:
                    if (
                        path == stage_dir
                        and (path / "manifest.json").is_file()
                        and not self.failed
                    ):
                        self.failed = True
                        raise OSError("injected staged-state attestation failure")
                    return super().capture_state(path)

            with self.assertRaisesRegex(OSError, "staged-state attestation"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": FailStagedStateAttestation(),
                        },
                    ),
                )

            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertFalse(stage_dir.exists())
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-journal.json").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

    def test_capture_real_unsupported_stage_entry_is_cleaned_without_journal(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class AddUnsupportedEntryAfterClone(FilesystemAdapter):
            def copy_material(
                self,
                source: Path,
                destination: Path,
                kind: object,
                *,
                phase: str,
            ) -> None:
                super().copy_material(source, destination, kind, phase=phase)
                if phase == "capture_clone":
                    os.mkfifo(destination / "late.fifo")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            with self.assertRaisesRegex(
                SwitchError,
                "Unsupported filesystem object kind.*late.fifo",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": AddUnsupportedEntryAfterClone(),
                        },
                    ),
                )

            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-stage").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-journal.json").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

    def test_capture_clone_then_raise_cleans_owned_unsupported_stage_without_journal(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class CloneUnsupportedEntryThenRaise(FilesystemAdapter):
            def copy_material(
                self,
                source: Path,
                destination: Path,
                kind: object,
                *,
                phase: str,
            ) -> None:
                super().copy_material(source, destination, kind, phase=phase)
                if phase == "capture_clone":
                    os.mkfifo(destination / "late.fifo")
                    raise OSError("injected clone completion failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            raised: BaseException | None = None
            try:
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": CloneUnsupportedEntryThenRaise(),
                        },
                    ),
                )
            except BaseException as exc:
                raised = exc

            self.assertIsInstance(raised, OSError)
            self.assertIn("injected clone completion failure", str(raised))
            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-stage").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-journal.json").exists()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

    def test_capture_finalize_failure_restores_previous_profile(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailCaptureFinalize(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "capture_finalize":
                    raise OSError("injected capture finalize failure")
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": FailCaptureFinalize(),
                    },
                ),
            )

            self.assertEqual("rolled_back", receipt.outcome)
            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_capture_committed_cleanup_failure_returns_committed_with_durable_journal(
        self,
    ) -> None:
        from codex_switch_capture import capture_profile
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailCommittedPreviousCleanup(FilesystemAdapter):
            def remove_tree(self, path: Path, *, phase: str) -> None:
                if phase == "capture_previous_cleanup":
                    raise OSError("injected committed cleanup failure")
                super().remove_tree(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": FailCommittedPreviousCleanup(),
                    },
                ),
            )

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            previous_dir = store.profiles_dir / ".internal.capture-previous"
            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            self.assertEqual('model = "before"\n', (previous_dir / "config.toml").read_text())
            self.assertEqual(
                "committed",
                json.loads(journal_path.read_text())["lifecycle"],
            )

            output = io.StringIO()
            codex_bin = self.make_executable(root)
            with patch(
                "codex_switch_capture.execute_transaction",
                return_value=receipt,
            ), redirect_stdout(output):
                capture_profile(
                    store=store,
                    name="internal",
                    source_home=source_home,
                    codex_bin=str(codex_bin),
                    app_cli_path=str(codex_bin),
                    allow_missing_auth=False,
                    overwrite=True,
                )
            self.assertEqual(
                "Captured profile internal: config.toml, auth.json\n",
                output.getvalue(),
            )

    def test_capture_profile_rollback_error_preserves_causal_details(self) -> None:
        from codex_switch_capture import capture_profile
        from codex_switch_transaction import TransactionReceipt

        receipt = TransactionReceipt(
            operation="capture",
            outcome="rolled_back",
            preview_lines=(
                "Captured profile internal: config.toml, auth.json",
                "capture failed: injected second rename failure",
                "rollback completed",
            ),
            backup_id=None,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            codex_bin = self.make_executable(root)
            with patch(
                "codex_switch_capture.execute_transaction",
                return_value=receipt,
            ), self.assertRaises(SwitchError) as raised:
                capture_profile(
                    store=store,
                    name="internal",
                    source_home=root / "source",
                    codex_bin=str(codex_bin),
                    app_cli_path=str(codex_bin),
                    allow_missing_auth=False,
                    overwrite=True,
                )

        message = str(raised.exception)
        self.assertIn("capture failed: injected second rename failure", message)
        self.assertIn("rollback completed", message)

    def test_capture_recovers_or_rejects_incomplete_journal(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class InterruptAfterFirstCaptureRename(FilesystemAdapter):
            def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                super().rename(source, destination, phase=phase)
                if phase == "capture_destination_to_previous":
                    raise KeyboardInterrupt("injected capture interruption")

        def arrange(root: Path) -> tuple[Store, TransactionRequest]:
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text(
                '{"name":"internal","description":"before"}\n'
            )
            (profile_dir / "plugin-support.json").write_text('{"kept":true}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            return store, TransactionRequest(
                operation="capture",
                profile="internal",
                options={
                    "source_home": source_home,
                    "codex_bin": "/tmp/codex-internal",
                    "app_cli_path": "/tmp/codex-internal",
                    "allow_missing_auth": False,
                    "overwrite": True,
                },
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, request = arrange(root)
            interrupted_options = dict(request.options)
            interrupted_options["filesystem_adapter"] = (
                InterruptAfterFirstCaptureRename()
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation=request.operation,
                        profile=request.profile,
                        options=interrupted_options,
                    ),
                )

            receipt = execute_transaction(store, request)

            profile_dir = store.profile_dir("internal")
            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            self.assertEqual('{"token":"after"}\n', (profile_dir / "auth.json").read_text())
            self.assertEqual(
                '{"kept":true}\n',
                (profile_dir / "plugin-support.json").read_text(),
            )
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, request = arrange(root)
            interrupted_options = dict(request.options)
            interrupted_options["filesystem_adapter"] = (
                InterruptAfterFirstCaptureRename()
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation=request.operation,
                        profile=request.profile,
                        options=interrupted_options,
                    ),
                )
            journal_path = next(
                store.profiles_dir.glob(".*.capture-journal.json")
            )
            journal_path.write_text("not-json\n")
            interrupted_state = capture_path_state(store.profiles_dir)

            with self.assertRaisesRegex(SwitchError, "Invalid JSON"):
                execute_transaction(store, request)

            self.assertEqual(
                interrupted_state,
                capture_path_state(store.profiles_dir),
            )

    def test_capture_rejects_non_integer_journal_schema_without_mutation(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class InterruptAfterFirstCaptureRename(FilesystemAdapter):
            def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                super().rename(source, destination, phase=phase)
                if phase == "capture_destination_to_previous":
                    raise KeyboardInterrupt("injected capture interruption")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            options: dict[str, object] = {
                "source_home": source_home,
                "codex_bin": "/tmp/codex-internal",
                "app_cli_path": "/tmp/codex-internal",
                "allow_missing_auth": False,
                "overwrite": True,
                "filesystem_adapter": InterruptAfterFirstCaptureRename(),
            }
            request = TransactionRequest(
                operation="capture",
                profile="internal",
                options=options,
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_transaction(store, request)

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            journal = json.loads(journal_path.read_text())
            journal["schema_version"] = 1.0
            journal_path.write_text(json.dumps(journal))
            interrupted_state = capture_path_state(store.profiles_dir)
            retry_options = dict(options)
            retry_options.pop("filesystem_adapter")

            with self.assertRaisesRegex(
                SwitchError,
                "Unsupported capture journal schema",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(
                interrupted_state,
                capture_path_state(store.profiles_dir),
            )

    def test_capture_prepared_journal_tampering_is_not_adopted(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class TamperPreparedJournalAfterWrite(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "capture_prepare":
                    path.write_text('{"tampered":true}\n')

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            before = capture_path_state(profile_dir)
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')

            with self.assertRaisesRegex(
                SwitchError,
                "Prepared capture journal does not match transaction",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options={
                            "source_home": source_home,
                            "codex_bin": "/tmp/codex-internal",
                            "app_cli_path": "/tmp/codex-internal",
                            "allow_missing_auth": False,
                            "overwrite": True,
                            "filesystem_adapter": TamperPreparedJournalAfterWrite(),
                        },
                    ),
                )

            self.assertEqual(before, capture_path_state(profile_dir))
            self.assertTrue(
                (store.profiles_dir / ".internal.capture-stage").is_dir()
            )
            self.assertEqual(
                '{"tampered":true}\n',
                (
                    store.profiles_dir / ".internal.capture-journal.json"
                ).read_text(),
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

    def test_capture_unmanaged_manifest_phase_drift_is_not_adopted(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class DriftUnmanagedDuringManifest(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "capture_manifest":
                    (path.parent / "plugin-support" / "catalog.json").write_text(
                        '{"plugin":"injected"}\n'
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root, unmanaged=True)
            )
            before = capture_path_state(profile_dir)
            injected_options = dict(options)
            injected_options["filesystem_adapter"] = (
                DriftUnmanagedDuringManifest()
            )

            with self.assertRaisesRegex(
                SwitchError,
                "Staged capture unmanaged artifacts changed",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=injected_options,
                    ),
                )

            self.assertEqual(before, capture_path_state(profile_dir))
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_capture_manifest_argument_mutation_is_not_adopted(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class MutateManifestArgument(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                if phase == "capture_manifest":
                    data["name"] = "injected"
                super().write_manifest(path, data, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root)
            )
            before = capture_path_state(profile_dir)
            injected_options = dict(options)
            injected_options["filesystem_adapter"] = MutateManifestArgument()

            with self.assertRaisesRegex(
                SwitchError,
                "Staged capture manifest does not match transaction",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=injected_options,
                    ),
                )

            self.assertEqual(before, capture_path_state(profile_dir))
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_capture_prepared_journal_argument_mutation_is_not_adopted(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class MutatePreparedJournalArgument(FilesystemAdapter):
            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "capture_prepare":
                    data["profile"] = "injected"

            def remove_tree(self, path: Path, *, phase: str) -> None:
                if phase == "capture_previous_cleanup":
                    raise OSError("retain committed journal")
                super().remove_tree(path, phase=phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root)
            )
            injected_options = dict(options)
            injected_options["filesystem_adapter"] = (
                MutatePreparedJournalArgument()
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=injected_options,
                ),
            )

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            self.assertEqual("committed", receipt.outcome)
            self.assertEqual("internal", json.loads(journal_path.read_text())["profile"])

    def test_capture_destination_finalize_drift_retains_recovery_evidence(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root)
            )

            class DriftDestinationDuringFinalize(FilesystemAdapter):
                def write_manifest(
                    self,
                    path: Path,
                    data: dict[str, object],
                    *,
                    phase: str,
                ) -> None:
                    super().write_manifest(path, data, phase=phase)
                    if phase == "capture_finalize":
                        (profile_dir / "config.toml").write_text(
                            'model = "injected"\n'
                        )

            injected_options = dict(options)
            injected_options["filesystem_adapter"] = (
                DriftDestinationDuringFinalize()
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=injected_options,
                ),
            )

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            previous_dir = store.profiles_dir / ".internal.capture-previous"
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertTrue(journal_path.is_file())
            self.assertTrue(previous_dir.is_dir())
            self.assertEqual(
                "prepared",
                json.loads(journal_path.read_text())["lifecycle"],
            )
            self.assertEqual(
                'model = "injected"\n',
                (profile_dir / "config.toml").read_text(),
            )

    def test_capture_previous_cleanup_drift_retains_committed_journal(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root)
            )

            class DriftDestinationDuringPreviousCleanup(FilesystemAdapter):
                def remove_tree(self, path: Path, *, phase: str) -> None:
                    super().remove_tree(path, phase=phase)
                    if phase == "capture_previous_cleanup":
                        (profile_dir / "config.toml").write_text(
                            'model = "injected"\n'
                        )

            injected_options = dict(options)
            injected_options["filesystem_adapter"] = (
                DriftDestinationDuringPreviousCleanup()
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=injected_options,
                ),
            )

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertTrue(journal_path.is_file())
            self.assertEqual(
                "committed",
                json.loads(journal_path.read_text())["lifecycle"],
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

    def test_capture_previous_cleanup_corrupt_journal_is_rebuilt_and_retryable(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root)
            )
            journal_path = store.profiles_dir / ".internal.capture-journal.json"

            class CorruptJournalAfterPreviousCleanup(FilesystemAdapter):
                def remove_tree(self, path: Path, *, phase: str) -> None:
                    super().remove_tree(path, phase=phase)
                    if phase == "capture_previous_cleanup":
                        journal_path.write_text('{"corrupt":true}\n')

            injected_options = dict(options)
            injected_options["filesystem_adapter"] = (
                CorruptJournalAfterPreviousCleanup()
            )

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=injected_options,
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            rebuilt_journal = json.loads(journal_path.read_text())
            self.assertEqual("committed", rebuilt_journal["lifecycle"])
            self.assertEqual("internal", rebuilt_journal["profile"])
            self.assertEqual(
                ("Captured profile internal: config.toml, auth.json",),
                receipt.preview_lines,
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )

            retry = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=options,
                ),
            )

            self.assertEqual("committed", retry.outcome)
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_capture_stage_drift_after_previous_rename_restores_live_profile(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root, unmanaged=True)
            )
            stage_dir = store.profiles_dir / ".internal.capture-stage"
            before = capture_path_state(profile_dir)

            class DriftStageAfterPreviousRenameSync(FilesystemAdapter):
                def __init__(self) -> None:
                    self.drifted_stage_state: dict[str, object] | None = None
                    self.rename_phases: list[str] = []

                def sync_directory(self, path: Path, *, phase: str) -> None:
                    super().sync_directory(path, phase=phase)
                    if phase == "capture_destination_to_previous_parent":
                        (stage_dir / "config.toml").write_text(
                            'model = "drifted"\n'
                        )
                        self.drifted_stage_state = capture_path_state(stage_dir)

                def rename(
                    self,
                    source: Path,
                    destination: Path,
                    *,
                    phase: str,
                ) -> None:
                    super().rename(source, destination, phase=phase)
                    self.rename_phases.append(phase)

            adapter = DriftStageAfterPreviousRenameSync()
            injected_options = dict(options)
            injected_options["filesystem_adapter"] = adapter

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=injected_options,
                ),
            )

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(before, capture_path_state(profile_dir))
            self.assertIsNotNone(adapter.drifted_stage_state)
            self.assertEqual(
                adapter.drifted_stage_state,
                capture_path_state(stage_dir),
            )
            self.assertTrue(journal_path.is_file())
            self.assertEqual(
                "prepared",
                json.loads(journal_path.read_text())["lifecycle"],
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )
            self.assertEqual(
                [
                    "capture_destination_to_previous",
                    "capture_rollback_previous_to_destination",
                ],
                adapter.rename_phases,
            )
            self.assertTrue(
                any(
                    "Capture stage changed before rename" in line
                    for line in receipt.preview_lines
                )
            )
            self.assertTrue(
                any(
                    "rollback retained changed stage" in line
                    for line in receipt.preview_lines
                )
            )

    def test_capture_stage_drift_after_rollback_move_restores_live_profile(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root, unmanaged=True)
            )
            stage_dir = store.profiles_dir / ".internal.capture-stage"
            before = capture_path_state(profile_dir)

            class DriftStageAfterRollbackMove(FilesystemAdapter):
                def __init__(self) -> None:
                    self.drifted_stage_state: dict[str, object] | None = None
                    self.rename_phases: list[str] = []

                def write_manifest(
                    self,
                    path: Path,
                    data: dict[str, object],
                    *,
                    phase: str,
                ) -> None:
                    if phase == "capture_finalize":
                        raise OSError("injected capture finalize failure")
                    super().write_manifest(path, data, phase=phase)

                def sync_directory(self, path: Path, *, phase: str) -> None:
                    super().sync_directory(path, phase=phase)
                    if phase == "capture_rollback_destination_to_stage_parent":
                        (stage_dir / "config.toml").write_text(
                            'model = "drifted-after-move"\n'
                        )
                        self.drifted_stage_state = capture_path_state(stage_dir)

                def rename(
                    self,
                    source: Path,
                    destination: Path,
                    *,
                    phase: str,
                ) -> None:
                    super().rename(source, destination, phase=phase)
                    self.rename_phases.append(phase)

            adapter = DriftStageAfterRollbackMove()
            injected_options = dict(options)
            injected_options["filesystem_adapter"] = adapter

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=injected_options,
                ),
            )

            self.assertEqual("rollback_failed", receipt.outcome)
            self.assertEqual(before, capture_path_state(profile_dir))
            self.assertIsNotNone(adapter.drifted_stage_state)
            self.assertEqual(
                adapter.drifted_stage_state,
                capture_path_state(stage_dir),
            )
            self.assertTrue(
                (store.profiles_dir / ".internal.capture-journal.json").is_file()
            )
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )
            self.assertEqual(
                [
                    "capture_destination_to_previous",
                    "capture_stage_to_destination",
                    "capture_rollback_destination_to_stage",
                    "capture_rollback_previous_to_destination",
                ],
                adapter.rename_phases,
            )

    def test_capture_rollback_terminal_vector_retains_recreated_artifacts(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        for injection in (
            "rollback_parent_sync_previous",
            "journal_cleanup_stage",
        ):
            with self.subTest(injection=injection), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store, profile_dir, _source_home, options = (
                    self.arrange_capture_fixture(root, unmanaged=True)
                )
                before = capture_path_state(profile_dir)
                stage_dir = store.profiles_dir / ".internal.capture-stage"
                previous_dir = store.profiles_dir / ".internal.capture-previous"
                journal_path = store.profiles_dir / ".internal.capture-journal.json"
                residual_path = (
                    previous_dir
                    if injection == "rollback_parent_sync_previous"
                    else stage_dir
                )

                class RecreateRollbackArtifact(FilesystemAdapter):
                    def write_manifest(
                        self,
                        path: Path,
                        data: dict[str, object],
                        *,
                        phase: str,
                    ) -> None:
                        if phase == "capture_finalize":
                            raise OSError("injected capture finalize failure")
                        super().write_manifest(path, data, phase=phase)

                    def sync_directory(self, path: Path, *, phase: str) -> None:
                        super().sync_directory(path, phase=phase)
                        if (
                            injection == "rollback_parent_sync_previous"
                            and phase
                            == "capture_rollback_previous_to_destination_parent"
                        ):
                            previous_dir.mkdir()
                            (previous_dir / "sentinel").write_text("recreated\n")

                    def remove_file(self, path: Path, *, phase: str) -> None:
                        super().remove_file(path, phase=phase)
                        if (
                            injection == "journal_cleanup_stage"
                            and phase == "capture_rollback_journal_cleanup"
                        ):
                            stage_dir.mkdir()
                            (stage_dir / "sentinel").write_text("recreated\n")

                injected_options = dict(options)
                injected_options["filesystem_adapter"] = (
                    RecreateRollbackArtifact()
                )

                receipt = execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=injected_options,
                    ),
                )

                self.assertEqual("rollback_failed", receipt.outcome)
                self.assertEqual(before, capture_path_state(profile_dir))
                self.assertTrue(residual_path.is_dir())
                self.assertEqual(
                    "recreated\n",
                    (residual_path / "sentinel").read_text(),
                )
                self.assertTrue(journal_path.is_file())
                retained_journal = json.loads(journal_path.read_text())
                self.assertEqual("prepared", retained_journal["lifecycle"])
                self.assertEqual("internal", retained_journal["profile"])
                self.assertTrue(
                    any("rollback failed" in line for line in receipt.preview_lines)
                )

    def test_capture_post_durable_checkpoint_failure_never_enters_rollback(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailPostDurableCheckpoint(FilesystemAdapter):
            def __init__(self) -> None:
                self.failed = False
                self.rename_phases: list[str] = []

            def capture_parent_checkpoint(self, path: Path, *, phase: str) -> None:
                if phase == "capture_after_finalize_journal" and not self.failed:
                    self.failed = True
                    raise OSError("injected post-durable checkpoint failure")

            def rename(
                self,
                source: Path,
                destination: Path,
                *,
                phase: str,
            ) -> None:
                super().rename(source, destination, phase=phase)
                self.rename_phases.append(phase)
                if phase == "capture_rollback_destination_to_stage":
                    raise KeyboardInterrupt("rollback must not start after commit")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root)
            )
            adapter = FailPostDurableCheckpoint()
            injected_options = dict(options)
            injected_options["filesystem_adapter"] = adapter

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=injected_options,
                ),
            )

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            self.assertEqual("committed", receipt.outcome)
            self.assertEqual('model = "after"\n', (profile_dir / "config.toml").read_text())
            self.assertEqual(
                "committed",
                json.loads(journal_path.read_text())["lifecycle"],
            )
            self.assertEqual(
                [
                    "capture_destination_to_previous",
                    "capture_stage_to_destination",
                ],
                adapter.rename_phases,
            )

            retry = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=options,
                ),
            )

            self.assertEqual("committed", retry.outcome)
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_capture_predurable_committed_bytes_are_downgraded_before_rollback(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class FailFinalizeSyncAndInterruptRollback(FilesystemAdapter):
            def __init__(self) -> None:
                self.failed = False
                self.events: list[str] = []

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase == "capture_retain_journal":
                    self.events.append(f"write:{data.get('lifecycle')}")

            def sync_file(self, path: Path, *, phase: str) -> None:
                super().sync_file(path, phase=phase)
                if phase == "capture_finalize_journal" and not self.failed:
                    self.failed = True
                    raise OSError("injected pre-durable finalize sync failure")
                if phase == "capture_retain_journal":
                    self.events.append("sync-file:prepared")

            def sync_directory(self, path: Path, *, phase: str) -> None:
                super().sync_directory(path, phase=phase)
                if phase == "capture_retain_journal_parent":
                    self.events.append("sync-parent:prepared")

            def rename(
                self,
                source: Path,
                destination: Path,
                *,
                phase: str,
            ) -> None:
                super().rename(source, destination, phase=phase)
                if phase == "capture_rollback_destination_to_stage":
                    self.events.append("rename:rollback-destination-to-stage")
                    raise KeyboardInterrupt("injected rollback interruption")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root)
            )
            adapter = FailFinalizeSyncAndInterruptRollback()
            injected_options = dict(options)
            injected_options["filesystem_adapter"] = adapter

            with self.assertRaises(KeyboardInterrupt):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=injected_options,
                    ),
                )

            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            self.assertEqual(
                "prepared",
                json.loads(journal_path.read_text())["lifecycle"],
            )
            self.assertEqual(
                [
                    "write:prepared",
                    "sync-file:prepared",
                    "sync-parent:prepared",
                    "rename:rollback-destination-to-stage",
                ],
                adapter.events,
            )

            retry = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options=options,
                ),
            )

            self.assertEqual("committed", retry.outcome)
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_capture_prepared_recovery_restores_previous_before_stage_drift_error(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class InterruptAfterFirstCaptureRename(FilesystemAdapter):
            def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                super().rename(source, destination, phase=phase)
                if phase == "capture_destination_to_previous":
                    raise KeyboardInterrupt("injected capture interruption")

        class RecordRecoveryRenames(FilesystemAdapter):
            def __init__(self) -> None:
                self.rename_phases: list[str] = []

            def rename(
                self,
                source: Path,
                destination: Path,
                *,
                phase: str,
            ) -> None:
                super().rename(source, destination, phase=phase)
                self.rename_phases.append(phase)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, profile_dir, _source_home, options = (
                self.arrange_capture_fixture(root, unmanaged=True)
            )
            before = capture_path_state(profile_dir)
            interrupted_options = dict(options)
            interrupted_options["filesystem_adapter"] = (
                InterruptAfterFirstCaptureRename()
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=interrupted_options,
                    ),
                )

            stage_dir = store.profiles_dir / ".internal.capture-stage"
            journal_path = store.profiles_dir / ".internal.capture-journal.json"
            (stage_dir / "config.toml").write_text(
                'model = "drifted-recovery"\n'
            )
            drifted_stage = capture_path_state(stage_dir)
            journal_bytes = journal_path.read_bytes()
            adapter = RecordRecoveryRenames()
            retry_options = dict(options)
            retry_options["filesystem_adapter"] = adapter

            with self.assertRaisesRegex(
                SwitchError,
                "Prepared capture recovery retained changed stage",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(before, capture_path_state(profile_dir))
            self.assertEqual(drifted_stage, capture_path_state(stage_dir))
            self.assertEqual(journal_bytes, journal_path.read_bytes())
            self.assertFalse(
                (store.profiles_dir / ".internal.capture-previous").exists()
            )
            self.assertEqual(
                ["capture_recover_previous_to_destination"],
                adapter.rename_phases,
            )

    def test_capture_recovery_read_json_cannot_replace_journal_state(self) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            capture_path_state,
            execute_transaction,
        )

        class InterruptAfterFirstCaptureRename(FilesystemAdapter):
            def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                super().rename(source, destination, phase=phase)
                if phase == "capture_destination_to_previous":
                    raise KeyboardInterrupt("injected capture interruption")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _profile_dir, source_home, options = (
                self.arrange_capture_fixture(root, unmanaged=True)
            )
            interrupted_options = dict(options)
            interrupted_options["filesystem_adapter"] = (
                InterruptAfterFirstCaptureRename()
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=interrupted_options,
                    ),
                )

            stage_dir = store.profiles_dir / ".internal.capture-stage"
            (stage_dir / "plugin-support" / "catalog.json").write_text(
                '{"plugin":"replacement"}\n'
            )
            forged_staged_state = capture_path_state(stage_dir)
            interrupted_state = capture_path_state(store.profiles_dir)
            (source_home / "config.toml").write_text("[broken\n")

            class ForgeRecoveredJournalState(FilesystemAdapter):
                def read_json(self, path: Path) -> dict[str, object]:
                    data = super().read_json(path)
                    if path.name == ".internal.capture-journal.json":
                        data["staged_state"] = forged_staged_state
                    return data

            retry_options = dict(options)
            retry_options["filesystem_adapter"] = ForgeRecoveredJournalState()
            with self.assertRaisesRegex(
                SwitchError,
                "Capture journal content does not match file state",
            ):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=retry_options,
                    ),
                )

            self.assertEqual(
                interrupted_state,
                capture_path_state(store.profiles_dir),
            )

    def test_capture_fsyncs_stage_and_prepared_journal_before_both_renames(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class RecordCaptureDurability(FilesystemAdapter):
            def __init__(self) -> None:
                self.events: list[str] = []

            def write_manifest(
                self,
                path: Path,
                data: dict[str, object],
                *,
                phase: str,
            ) -> None:
                super().write_manifest(path, data, phase=phase)
                if phase in {"capture_prepare", "capture_finalize"}:
                    self.events.append(f"write:{phase}")

            def sync_file(self, path: Path, *, phase: str) -> None:
                super().sync_file(path, phase=phase)
                self.events.append(f"sync-file:{phase}:{path.name}")

            def sync_directory(self, path: Path, *, phase: str) -> None:
                super().sync_directory(path, phase=phase)
                self.events.append(f"sync-directory:{phase}:{path.name}")

            def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                super().rename(source, destination, phase=phase)
                self.events.append(f"rename:{phase}")

            def remove_tree(self, path: Path, *, phase: str) -> None:
                super().remove_tree(path, phase=phase)
                self.events.append(f"remove-tree:{phase}")

            def remove_file(self, path: Path, *, phase: str) -> None:
                super().remove_file(path, phase=phase)
                self.events.append(f"remove-file:{phase}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            (profile_dir / "plugin-support.json").write_text('{"kept":true}\n')
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            adapter = RecordCaptureDurability()

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="capture",
                    profile="internal",
                    options={
                        "source_home": source_home,
                        "codex_bin": "/tmp/codex-internal",
                        "app_cli_path": "/tmp/codex-internal",
                        "allow_missing_auth": False,
                        "overwrite": True,
                        "filesystem_adapter": adapter,
                    },
                ),
            )

            self.assertEqual("committed", receipt.outcome)
            self.assertEqual(
                [
                    "sync-file:capture_stage_data:auth.json",
                    "sync-file:capture_stage_data:config.toml",
                    "sync-file:capture_stage_data:manifest.json",
                    "sync-file:capture_stage_data:plugin-support.json",
                    "sync-directory:capture_stage_directory:.internal.capture-stage",
                    "write:capture_prepare",
                    "sync-file:capture_prepare_journal:.internal.capture-journal.json",
                    "sync-directory:capture_prepare_parent:profiles",
                    "rename:capture_destination_to_previous",
                    "sync-directory:capture_destination_to_previous_parent:profiles",
                    "rename:capture_stage_to_destination",
                    "sync-directory:capture_stage_to_destination_parent:profiles",
                    "write:capture_finalize",
                    "sync-file:capture_finalize_journal:.internal.capture-journal.json",
                    "sync-directory:capture_finalize_parent:profiles",
                    "remove-tree:capture_previous_cleanup",
                    "sync-directory:capture_previous_cleanup_parent:profiles",
                    "remove-file:capture_journal_cleanup",
                    "sync-directory:capture_journal_cleanup_parent:profiles",
                ],
                adapter.events,
            )

    def test_capture_recovers_prepared_journal_before_rejecting_invalid_retry_source(
        self,
    ) -> None:
        from codex_switch_transaction import (
            FilesystemAdapter,
            TransactionRequest,
            execute_transaction,
        )

        class InterruptAfterFirstCaptureRename(FilesystemAdapter):
            def rename(self, source: Path, destination: Path, *, phase: str) -> None:
                super().rename(source, destination, phase=phase)
                if phase == "capture_destination_to_previous":
                    raise KeyboardInterrupt("injected capture interruption")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            profile_dir = store.profile_dir("internal")
            profile_dir.mkdir(parents=True)
            (profile_dir / "config.toml").write_text('model = "before"\n')
            (profile_dir / "auth.json").write_text('{"token":"before"}\n')
            (profile_dir / "manifest.json").write_text('{"name":"internal"}\n')
            before = {
                path.name: path.read_bytes()
                for path in profile_dir.iterdir()
                if path.is_file()
            }
            source_home = root / "source"
            source_home.mkdir()
            source_config = source_home / "config.toml"
            source_config.write_text('model = "after"\n')
            (source_home / "auth.json").write_text('{"token":"after"}\n')
            request_options: dict[str, object] = {
                "source_home": source_home,
                "codex_bin": "/tmp/codex-internal",
                "app_cli_path": "/tmp/codex-internal",
                "allow_missing_auth": False,
                "overwrite": True,
            }
            interrupted_options = dict(request_options)
            interrupted_options["filesystem_adapter"] = (
                InterruptAfterFirstCaptureRename()
            )
            with self.assertRaises(KeyboardInterrupt):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=interrupted_options,
                    ),
                )
            source_config.write_text("[broken\n")

            with self.assertRaisesRegex(SwitchError, "Invalid TOML"):
                execute_transaction(
                    store,
                    TransactionRequest(
                        operation="capture",
                        profile="internal",
                        options=request_options,
                    ),
                )

            self.assertEqual(
                before,
                {
                    path.name: path.read_bytes()
                    for path in profile_dir.iterdir()
                    if path.is_file()
                },
            )
            self.assertEqual(
                ["internal"],
                sorted(path.name for path in store.profiles_dir.iterdir()),
            )

    def test_cmd_capture_preserves_success_output_contract(self) -> None:
        from codex_switch_capture import cmd_capture

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store_root = root / "store"
            source_home = root / "source"
            source_home.mkdir()
            (source_home / "config.toml").write_text('model = "internal"\n')
            (source_home / "auth.json").write_text('{"token":"internal"}\n')
            codex_bin = self.make_executable(root)
            args = argparse.Namespace(
                store_dir=store_root,
                official_codex_home=root / "official",
                official_codex_home_source="explicit",
                internal_codex_home=root / "internal",
                internal_codex_home_source="explicit",
                launch_agent_path=root / "agent.plist",
                launch_agent_label="test.codex-switch",
                name="internal",
                from_codex_home=source_home,
                codex_bin=str(codex_bin),
                app_cli_path=str(codex_bin),
                allow_missing_auth=False,
                overwrite=False,
            )
            output = io.StringIO()

            with redirect_stdout(output):
                cmd_capture(args)

            self.assertEqual(
                "Captured profile internal: config.toml, auth.json\n",
                output.getvalue(),
            )

    def test_cmd_init_indirect_capture_preserves_success_output_contract(self) -> None:
        from codex_switch_lifecycle import cmd_init

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store_root = root / "store"
            store_root.mkdir()
            official_home = root / "official"
            official_home.mkdir()
            (official_home / "config.toml").write_text('model = "internal"\n')
            (official_home / "auth.json").write_text('{"token":"internal"}\n')
            codex_bin = self.make_executable(root)
            args = argparse.Namespace(
                store_dir=store_root,
                official_codex_home=official_home,
                official_codex_home_source="explicit",
                internal_codex_home=root / "internal",
                internal_codex_home_source="explicit",
                launch_agent_path=root / "agent.plist",
                launch_agent_label="test.codex-switch",
                codex_bin=str(codex_bin),
                app_cli_path="/tmp/codex-official",
                capture_current="internal",
                overwrite_capture=False,
            )
            output = io.StringIO()

            with redirect_stdout(output):
                cmd_init(args)

            self.assertEqual(
                "Captured profile internal: config.toml, auth.json\n"
                f"Initialized Codex switch store: {store_root}\n"
                f"Shim directory: {store_root / 'bin'}\n",
                output.getvalue(),
            )

    def test_init_capture_busy_and_pending_are_byte_identical(self) -> None:
        from codex_switch_lifecycle import cmd_init
        from codex_switch_transaction import capture_path_state

        def init_args(store: Store, root: Path) -> argparse.Namespace:
            return argparse.Namespace(
                store_dir=store.root,
                official_codex_home=store.official_codex_home,
                official_codex_home_source="explicit",
                internal_codex_home=store.internal_codex_home,
                internal_codex_home_source="explicit",
                launch_agent_path=store.launch_agent_path,
                launch_agent_label=store.launch_agent_label,
                codex_bin="/tmp/codex-internal",
                app_cli_path="/tmp/codex-official",
                capture_current="internal",
                overwrite_capture=False,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            (store.official_codex_home / "config.toml").write_text(
                'model = "busy"\n'
            )
            before = capture_path_state(store.root)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_directory_lock,
                args=(str(store.root), ready, release),
            )
            holder.start()
            self.assertTrue(ready.wait(10), "lock holder did not become ready")
            try:
                with self.assertRaisesRegex(
                    SwitchError,
                    f"profile store is busy: {store.root}",
                ):
                    cmd_init(init_args(store, root))
            finally:
                release.set()
                holder.join(10)
                if holder.is_alive():
                    holder.terminate()
                    holder.join()
            self.assertEqual(0, holder.exitcode)
            self.assertEqual(before, capture_path_state(store.root))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            (store.official_codex_home / "config.toml").write_text(
                'model = "pending"\n'
            )
            journal = store.profiles_dir / ".internal.capture-journal.json"
            journal.write_text("{corrupt\n")
            before = capture_path_state(store.root)

            with self.assertRaisesRegex(
                SwitchError,
                "Pending capture blocks init: internal",
            ):
                cmd_init(init_args(store, root))

            self.assertEqual(before, capture_path_state(store.root))

    def test_init_capture_uses_one_store_lock_and_preserves_exact_output(
        self,
    ) -> None:
        import codex_switch_transaction as transaction
        from codex_switch_lifecycle import cmd_init

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store_root = root / "store"
            store_root.mkdir()
            official_home = root / "official"
            official_home.mkdir()
            (official_home / "config.toml").write_text('model = "internal"\n')
            (official_home / "auth.json").write_text('{"token":"internal"}\n')
            codex_bin = self.make_executable(root)
            args = argparse.Namespace(
                store_dir=store_root,
                official_codex_home=official_home,
                official_codex_home_source="explicit",
                internal_codex_home=root / "internal",
                internal_codex_home_source="explicit",
                launch_agent_path=root / "agent.plist",
                launch_agent_label="test.codex-switch",
                codex_bin=str(codex_bin),
                app_cli_path="/tmp/codex-official",
                capture_current="internal",
                overwrite_capture=False,
            )
            lock_entries: list[Path] = []
            original_enter = transaction._StoreLock.__enter__

            def enter_once(lock: object) -> object:
                lock_entries.append(lock.root)
                return original_enter(lock)

            output = io.StringIO()
            with patch.object(transaction._StoreLock, "__enter__", enter_once):
                with redirect_stdout(output):
                    cmd_init(args)

            self.assertEqual([store_root], lock_entries)
            self.assertEqual(
                "Captured profile internal: config.toml, auth.json\n"
                f"Initialized Codex switch store: {store_root}\n"
                f"Shim directory: {store_root / 'bin'}\n",
                output.getvalue(),
            )

    def test_cmd_init_capture_failure_restores_pre_init_store_and_preserves_stdout_contract(
        self,
    ) -> None:
        from codex_switch_lifecycle import cmd_init
        from codex_switch_transaction import capture_path_state

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            (store.root / "sentinel.txt").write_text("preserve\n")
            (store.official_codex_home / "config.toml").write_text(
                'model = ["unterminated"\n'
            )
            args = argparse.Namespace(
                store_dir=store.root,
                official_codex_home=store.official_codex_home,
                official_codex_home_source="explicit",
                internal_codex_home=store.internal_codex_home,
                internal_codex_home_source="explicit",
                launch_agent_path=store.launch_agent_path,
                launch_agent_label=store.launch_agent_label,
                codex_bin="/tmp/codex-internal",
                app_cli_path="/tmp/codex-official",
                capture_current="internal",
                overwrite_capture=False,
            )
            before_store = capture_path_state(store.root)
            output = io.StringIO()

            with self.assertRaises(SwitchError), redirect_stdout(output):
                cmd_init(args)

            self.assertEqual("", output.getvalue())
            self.assertEqual(before_store, capture_path_state(store.root))

    def test_recursive_file_symlink_mode_tree_state_round_trips(self) -> None:
        from codex_switch_transaction import TransactionRequest, execute_transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            target = store.official_codex_home / "support"
            nested = target / "nested"
            nested.mkdir(parents=True)
            target.chmod(0o750)
            nested.chmod(0o700)
            config = nested / "plugin.json"
            config.write_text('{"enabled": true}\n')
            config.chmod(0o640)
            (target / "current").symlink_to("nested/plugin.json")
            backup_dir = store.backups_dir / "remove-tree"
            backup_dir.mkdir(parents=True)
            (backup_dir / "backup.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "lifecycle": "committed",
                        "id": "remove-tree",
                        "operation": "switch",
                        "to_profile": "internal",
                        "entries": [
                            {
                                "path": str(target),
                                "before_state": {"kind": "missing"},
                                "committed_after_state": {
                                    "kind": "directory",
                                    "mode": 0o750,
                                    "entry_count": 0,
                                    "tree_sha256": "0" * 64,
                                },
                            }
                        ],
                    }
                )
                + "\n"
            )

            removal_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": "remove-tree", "force": True},
                ),
            )
            self.assertFalse(target.exists())
            self.assertIsNotNone(removal_receipt.backup_id)
            safety_id = str(removal_receipt.backup_id)
            safety_manifest = json.loads(
                (store.backups_dir / safety_id / "backup.json").read_text()
            )
            directory_state = safety_manifest["entries"][0]["before_state"]
            self.assertEqual("directory", directory_state["kind"])
            self.assertEqual(3, directory_state["entry_count"])
            self.assertEqual(64, len(directory_state["tree_sha256"]))

            restore_receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="restore",
                    profile="restore",
                    options={"backup_id": safety_id, "force": False},
                ),
            )

            self.assertEqual("committed", restore_receipt.outcome)
            self.assertEqual(0o750, target.stat().st_mode & 0o777)
            self.assertEqual(0o700, nested.stat().st_mode & 0o777)
            self.assertEqual(0o640, config.stat().st_mode & 0o777)
            self.assertEqual('{"enabled": true}\n', config.read_text())
            self.assertTrue((target / "current").is_symlink())
            self.assertEqual("nested/plugin.json", os.readlink(target / "current"))


if __name__ == "__main__":
    unittest.main()
