#!/usr/bin/env python3

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable, Dict, Iterable
from unittest import mock

try:
    import release_auto
except ModuleNotFoundError:
    from scripts import release_auto


MODULE_PATH = Path(__file__).with_name("codex_switch_release_bundle.py")
PROMOTION_MODULE_PATH = Path(__file__).with_name("codex_switch_promotion.py")
UPDATE_POLICY_MODULE_PATH = Path(__file__).with_name(
    "codex_switch_update_policy.py"
)
OFFICIAL_RELEASE_MODULE_PATH = Path(__file__).with_name(
    "codex_switch_official_release.py"
)
PROFILE_SWITCH_MODULE_PATH = Path(__file__).with_name("codex_profile_switch.py")
PACKAGE_SCRIPT = Path(__file__).with_name("package-release.sh")
REPO_ROOT = Path(__file__).parents[1]
INSTALLER = REPO_ROOT / "install.sh"
REMOTE_RUNNER = REPO_ROOT / "run.sh"
ENV_SETUP = REPO_ROOT / "scripts" / "codex_env_setup"
WRAPPER = REPO_ROOT / "scripts" / "codex-switch"

MANIFEST_NAME = "bundle-manifest.json"
MANIFEST_SCHEMA = "codex-switch.release-bundle"
MANIFEST_CLASSIFICATION = "codex-switch-release-bundle"
STAGING_PREFIX = ".codex-switch-stage-"
BACKUP_PREFIX = ".codex-switch-backup-"

FIXED_FILES = ["README.md", "SKILL.md", "VERSION", "run.sh"]
FIXED_DIRECTORIES = ["agents", "docs", "evals", "scripts"]
REQUIRED_PATHS = [
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
    "scripts/codex_switch_selection.py",
    "scripts/codex_switch_shared_configuration.py",
    "scripts/package-release.sh",
    MANIFEST_NAME,
]
REQUIRED_PYTHON_MODULES = [
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
]
EXECUTABLE_EXPECTATIONS = {
    "run.sh": "0755",
    "scripts/codex-switch": "0755",
    "scripts/package-release.sh": "0755",
}


def load_bundle_module() -> ModuleType:
    if not MODULE_PATH.is_file():
        raise AssertionError(
            "release bundle module is missing; rejection tests require the "
            "BundleError contract, not a missing-command failure"
        )
    spec = importlib.util.spec_from_file_location(
        "codex_switch_release_bundle_under_test",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load release bundle module: {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_promotion_module() -> ModuleType:
    if not PROMOTION_MODULE_PATH.is_file():
        raise AssertionError(
            "promotion module is missing; immutable promotion tests require "
            "the PromotionError contract"
        )
    spec = importlib.util.spec_from_file_location(
        "codex_switch_promotion_under_test",
        PROMOTION_MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(
            f"could not load promotion module: {PROMOTION_MODULE_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_update_policy_module() -> ModuleType:
    if not UPDATE_POLICY_MODULE_PATH.is_file():
        raise AssertionError(
            "update policy module is missing; ordered internal-update tests "
            "require the structured decision contract"
        )
    spec = importlib.util.spec_from_file_location(
        "codex_switch_update_policy_under_test",
        UPDATE_POLICY_MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(
            f"could not load update policy module: {UPDATE_POLICY_MODULE_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(paths: Iterable[Path]) -> Dict[Path, bytes]:
    return {path: path.read_bytes() for path in paths}


def tree_snapshot(root: Path) -> Dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def filesystem_snapshot(root: Path) -> Dict[str, bytes]:
    if not os.path.lexists(str(root)):
        return {".": b"missing"}
    paths = [root, *sorted(root.rglob("*"))]
    result: Dict[str, bytes] = {}
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        mode = f"{stat.S_IMODE(info.st_mode):04o}".encode()
        if path.is_symlink():
            payload = b"symlink:" + mode + b":" + os.readlink(path).encode()
        elif path.is_file():
            payload = b"file:" + mode + b":" + path.read_bytes()
        elif path.is_dir():
            payload = b"directory:" + mode
        else:
            payload = b"other:" + mode
        result[relative] = payload
    return result


def write_required_python_modules(scripts_dir: Path) -> None:
    shutil.copy2(MODULE_PATH, scripts_dir / MODULE_PATH.name)
    shutil.copy2(PROMOTION_MODULE_PATH, scripts_dir / PROMOTION_MODULE_PATH.name)
    shutil.copy2(
        UPDATE_POLICY_MODULE_PATH,
        scripts_dir / UPDATE_POLICY_MODULE_PATH.name,
    )
    (scripts_dir / OFFICIAL_RELEASE_MODULE_PATH.name).write_text(
        "VALUE = 1\n"
    )
    (scripts_dir / PROFILE_SWITCH_MODULE_PATH.name).write_text("VALUE = 1\n")
    for name in (
        "codex_switch_parity.py",
        "codex_switch_runtime_binding.py",
        "codex_switch_app_proxy.py",
        "codex_switch_home_sync.py",
        "codex_switch_selection.py",
        "codex_switch_shared_configuration.py",
    ):
        (scripts_dir / name).write_text("VALUE = 1\n")


class CodexUpdateReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.repo = self.workspace / "codex-switch"
        self.output = self.root / "output"
        self._write_source_repo(self.repo)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_source_repo(self, repo: Path) -> None:
        (repo / "agents").mkdir(parents=True)
        (repo / "docs" / "troubleshooting").mkdir(parents=True)
        (repo / "evals").mkdir(parents=True)
        (repo / "scripts" / "__pycache__").mkdir(parents=True)

        (repo / "README.md").write_text("repository sentinel\n")
        (repo / "SKILL.md").write_text("skill\n")
        (repo / "VERSION").write_text("1.2.3\n")
        (repo / "run.sh").write_text("#!/usr/bin/env bash\necho runner\n")
        (repo / "run.sh").chmod(0o700)
        (repo / "agents" / "openai.yaml").write_text("name: codex-switch\n")
        (
            repo
            / "docs"
            / "troubleshooting"
            / "internal-azure-responses-resource-stickiness.md"
        ).write_text("troubleshooting sentinel\n")
        (repo / "evals" / "evals.json").write_text('{"evals": []}\n')
        (repo / "scripts" / "codex-switch").write_text(
            "#!/usr/bin/env bash\necho codex-switch\n"
        )
        (repo / "scripts" / "codex-switch").chmod(0o700)
        (repo / "scripts" / "package-release.sh").write_text(
            "#!/usr/bin/env bash\necho package\n"
        )
        (repo / "scripts" / "package-release.sh").chmod(0o700)
        write_required_python_modules(repo / "scripts")
        (repo / "scripts" / "helper.py").write_text("VALUE = 1\n")
        (repo / "scripts" / "__pycache__" / "helper.pyc").write_bytes(b"cache")
        (repo / "NOT_SHIPPED.txt").write_text("outside fixed allowlist\n")

    def _repo_sentinel(self) -> Path:
        return self.repo / "README.md"

    def _assert_bundle_error(
        self,
        reason: str,
        operation: Callable[[ModuleType], object],
    ) -> object:
        module = load_bundle_module()
        with self.assertRaises(module.BundleError) as caught:
            operation(module)
        self.assertEqual(reason, caught.exception.reason)
        return caught.exception

    def _assert_no_release_workdirs(self, output: Path) -> None:
        if not output.exists():
            return
        residual = sorted(
            path.name
            for path in output.iterdir()
            if path.name.startswith(STAGING_PREFIX)
            or path.name.startswith(BACKUP_PREFIX)
        )
        self.assertEqual([], residual)

    def _public_output_paths(self) -> list[Path]:
        return [
            self.output / "codex-switch" / "VERSION",
            self.output / "codex-switch" / MANIFEST_NAME,
            self.output / "run.sh",
            self.output / "codex-switch.tar.gz",
        ]

    def _prepare_commit_bound_legacy_release(
        self,
    ) -> tuple[ModuleType, object, str]:
        module = load_bundle_module()
        shutil.rmtree(self.repo / "scripts" / "__pycache__")
        (self.repo / "VERSION").write_text("0.1.13\n")
        (self.repo / "install.sh").write_text("#!/usr/bin/env sh\nexit 0\n")
        (self.repo / "install.sh").chmod(0o755)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "legacy release"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(["git", "tag", "v0.1.13"], cwd=self.repo, check=True)
        receipt = module.build_release_bundle(self.repo, self.output)
        receipt.manifest.unlink()
        module._create_archive(receipt.package_dir, receipt.archive)
        return module, receipt, release_auto.resolve_commit(self.repo, "HEAD")

    def _initialize_commit_bound_repo(
        self,
        repo: Path,
        *,
        tag: str = "v1.2.3",
    ) -> str:
        shutil.rmtree(repo / "scripts" / "__pycache__")
        (repo / "install.sh").write_text("#!/usr/bin/env sh\nexit 0\n")
        (repo / "install.sh").chmod(0o755)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "release source"],
            cwd=repo,
            check=True,
        )
        subprocess.run(["git", "tag", tag], cwd=repo, check=True)
        return release_auto.resolve_commit(repo, "HEAD")

    def test_release_bundle_requires_independent_selection_module(self) -> None:
        module = load_bundle_module()

        self.assertIn(
            "codex_switch_selection.py",
            module.REQUIRED_PYTHON_MODULES,
        )
        self.assertIn(
            "codex_switch_shared_configuration.py",
            module.REQUIRED_PYTHON_MODULES,
        )

    def test_rejects_output_root_equal_repository_without_mutation(self) -> None:
        sentinel = self._repo_sentinel()
        before = sentinel.read_bytes()

        self._assert_bundle_error(
            "repository_output_root",
            lambda module: module.validate_package_destination(
                self.repo,
                self.repo,
                self.repo / "dist" / "codex-switch",
            ),
        )

        self.assertEqual(before, sentinel.read_bytes())

    def test_rejects_package_destination_equal_repository_without_mutation(
        self,
    ) -> None:
        sentinel = self._repo_sentinel()
        before = sentinel.read_bytes()

        self._assert_bundle_error(
            "repository_package_destination",
            lambda module: module.validate_package_destination(
                self.repo,
                self.repo / "dist",
                self.repo,
            ),
        )

        self.assertEqual(before, sentinel.read_bytes())

    def test_rejects_output_root_equal_repository_ancestor_without_mutation(
        self,
    ) -> None:
        sentinel = self._repo_sentinel()
        ancestor_sentinel = self.workspace / "ancestor-sentinel.bin"
        ancestor_sentinel.write_bytes(b"ancestor")
        before = snapshot([sentinel, ancestor_sentinel])

        self._assert_bundle_error(
            "repository_ancestor_output_root",
            lambda module: module.validate_package_destination(
                self.repo,
                self.workspace,
                self.workspace / "unrelated-package",
            ),
        )

        self.assertEqual(before, snapshot(before))

    def test_rejects_package_destination_equal_repository_ancestor_without_mutation(
        self,
    ) -> None:
        sentinel = self._repo_sentinel()
        ancestor_sentinel = self.workspace / "ancestor-sentinel.bin"
        ancestor_sentinel.write_bytes(b"ancestor")
        before = snapshot([sentinel, ancestor_sentinel])

        self._assert_bundle_error(
            "repository_ancestor_package_destination",
            lambda module: module.validate_package_destination(
                self.repo,
                self.repo / "dist",
                self.workspace,
            ),
        )

        self.assertEqual(before, snapshot(before))

    def test_rejects_filesystem_root_for_output_or_package_without_mutation(
        self,
    ) -> None:
        sentinel = self._repo_sentinel()
        before = sentinel.read_bytes()
        filesystem_root = Path(self.repo.anchor)

        cases = [
            (
                "filesystem_root_output_root",
                filesystem_root,
                self.repo / "dist" / "codex-switch",
            ),
            (
                "filesystem_root_package_destination",
                self.repo / "dist",
                filesystem_root,
            ),
        ]
        for reason, output_root, package_dir in cases:
            with self.subTest(reason=reason):
                self._assert_bundle_error(
                    reason,
                    lambda module, output_root=output_root, package_dir=package_dir: (
                        module.validate_package_destination(
                            self.repo,
                            output_root,
                            package_dir,
                        )
                    ),
                )
                self.assertEqual(before, sentinel.read_bytes())

    def test_rejects_output_root_symlink_without_mutation(self) -> None:
        sentinel = self._repo_sentinel()
        output_target = self.root / "output-target"
        output_target.mkdir()
        destination_sentinel = output_target / "destination-sentinel.bin"
        destination_sentinel.write_bytes(b"destination")
        output_link = self.root / "output-link"
        output_link.symlink_to(output_target, target_is_directory=True)
        before = snapshot([sentinel, destination_sentinel])

        self._assert_bundle_error(
            "symlink_output_root",
            lambda module: module.build_release_bundle(self.repo, output_link),
        )

        self.assertEqual(before, snapshot(before))

    def test_rejects_package_destination_symlink_without_mutation(self) -> None:
        sentinel = self._repo_sentinel()
        destination_target = self.root / "destination-target"
        destination_target.mkdir()
        destination_sentinel = destination_target / "destination-sentinel.bin"
        destination_sentinel.write_bytes(b"destination")
        self.output.mkdir()
        (self.output / "codex-switch").symlink_to(
            destination_target,
            target_is_directory=True,
        )
        before = snapshot([sentinel, destination_sentinel])

        self._assert_bundle_error(
            "symlink_package_destination",
            lambda module: module.build_release_bundle(self.repo, self.output),
        )

        self.assertEqual(before, snapshot(before))

    def test_rejects_unrelated_existing_directory_without_mutation(self) -> None:
        sentinel = self._repo_sentinel()
        package_dir = self.output / "codex-switch"
        package_dir.mkdir(parents=True)
        destination_sentinel = package_dir / "unrelated.bin"
        destination_sentinel.write_bytes(b"unrelated")
        (package_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema": "foreign.release-bundle",
                    "schema_version": 1,
                    "classification": "foreign-output",
                }
            )
        )
        before = snapshot([sentinel, destination_sentinel])

        self._assert_bundle_error(
            "unclassified_destination",
            lambda module: module.build_release_bundle(self.repo, self.output),
        )

        self.assertEqual(before, snapshot(before))

    def test_release_archive_is_deterministic_across_output_roots(self) -> None:
        module = load_bundle_module()
        first_output = self.root / "first-output"
        second_output = self.root / "second-output"

        first = module.build_release_bundle(self.repo, first_output)
        for path in sorted(self.repo.rglob("*")):
            if path.is_symlink():
                continue
            os.utime(path, (1_900_000_000, 1_900_000_000))
        second = module.build_release_bundle(self.repo, second_output)

        self.assertEqual(sha256(first.archive), sha256(second.archive))

    def test_env_setup_runtime_dependency_is_validated_in_release_package(self) -> None:
        helper = "codex_switch_internal_runtime.py"
        (self.repo / "scripts/codex_env_setup").write_text(
            '#!/bin/sh\npython3 "$SCRIPT_DIR/' + helper + '"\n'
        )
        module = load_bundle_module()
        with self.assertRaises(module.BundleError) as caught:
            module.build_release_bundle(self.repo, self.output)
        self.assertIn("missing payload module", str(caught.exception))
        shutil.copy2(REPO_ROOT / "scripts" / helper, self.repo / "scripts" / helper)
        receipt = module.build_release_bundle(self.repo, self.root / "complete-output")
        self.assertEqual(
            (REPO_ROOT / "scripts" / helper).read_bytes(),
            (receipt.package_dir / "scripts" / helper).read_bytes(),
        )

    def test_strict_bundle_protects_nested_manifest_named_payload(self) -> None:
        nested_manifest = self.repo / "scripts" / MANIFEST_NAME
        nested_manifest.write_text('{"payload": true}\n')
        module = load_bundle_module()

        receipt = module.build_release_bundle(self.repo, self.output)
        manifest = json.loads(receipt.manifest.read_text())
        protected_paths = {entry["path"] for entry in manifest["files"]}

        self.assertIn(f"scripts/{MANIFEST_NAME}", protected_paths)
        nested_packaged = receipt.package_dir / "scripts" / MANIFEST_NAME
        nested_packaged.write_text('{"payload": false}\n')
        with self.assertRaises(module.BundleError) as caught:
            module.validate_release_outputs(receipt.package_dir)
        self.assertEqual("manifest_invalid", caught.exception.reason)

    def test_strict_bundle_rejects_special_file_before_archive_validation(
        self,
    ) -> None:
        module = load_bundle_module()
        receipt = module.build_release_bundle(self.repo, self.output)
        special = receipt.package_dir / "scripts" / "blocked.fifo"
        os.mkfifo(special)

        with self.assertRaises(module.BundleError) as caught:
            module.validate_release_outputs(receipt.package_dir)

        self.assertEqual("manifest_invalid", caught.exception.reason)
        self.assertIn("special file", str(caught.exception))

    def test_strict_bundle_rejects_unsafe_package_root_mode(self) -> None:
        module = load_bundle_module()
        receipt = module.build_release_bundle(self.repo, self.output)
        receipt.package_dir.chmod(0o777)

        with self.assertRaises(module.BundleError) as caught:
            module.validate_release_outputs(receipt.package_dir)

        self.assertEqual("manifest_invalid", caught.exception.reason)
        self.assertIn("root mode", str(caught.exception))

    def test_release_assets_require_manifest_without_explicit_legacy_mode(
        self,
    ) -> None:
        _module, _receipt, commit = self._prepare_commit_bound_legacy_release()

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.collect_release_assets(
                self.repo,
                self.output,
                "v0.1.13",
                commit,
            )

        self.assertIn(
            "manifest is required outside explicit historical reconciliation",
            str(caught.exception),
        )

    def test_release_assets_accept_commit_bound_legacy_bundle(self) -> None:
        _module, _receipt, commit = self._prepare_commit_bound_legacy_release()

        assets = release_auto.collect_release_assets(
            self.repo,
            self.output,
            "v0.1.13",
            commit,
            allow_legacy=True,
        )

        self.assertEqual(
            list(release_auto.REQUIRED_RELEASE_ASSETS),
            [asset.name for asset in assets],
        )

    def test_assets_cli_enables_explicit_supported_legacy_mode(self) -> None:
        _module, _receipt, commit = self._prepare_commit_bound_legacy_release()
        manifest = self.root / "legacy-assets.json"
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "release_auto.py"),
                "--repo",
                str(self.repo),
                "assets",
                "--tag",
                "v0.1.13",
                "--commit",
                commit,
                "--dist-dir",
                str(self.output),
                "--manifest",
                str(manifest),
                "--require-tag",
                "--allow-legacy",
                "--json",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("assets_validated", payload["outcome"])
        self.assertEqual("v0.1.13", payload["tag"])
        self.assertEqual(commit, payload["commit"])
        self.assertEqual(
            list(release_auto.REQUIRED_RELEASE_ASSETS),
            [asset["name"] for asset in payload["assets"]],
        )
        self.assertTrue(manifest.is_file())

    def test_release_assets_accept_manifest_bearing_v0_1_14_only_in_legacy_mode(
        self,
    ) -> None:
        historical_repo = self.root / "historical-v0.1.14"
        subprocess.run(
            ["git", "clone", "-q", "--no-local", str(REPO_ROOT), str(historical_repo)],
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", "--detach", "v0.1.14"],
            cwd=historical_repo,
            check=True,
        )
        historical_output = self.root / "historical-v0.1.14-output"
        env = {
            **os.environ,
            "CODEX_SWITCH_DIST_DIR": str(historical_output),
            "CODEX_SWITCH_PYTHON": sys.executable,
        }
        subprocess.run(
            [str(historical_repo / "scripts" / "package-release.sh")],
            cwd=historical_repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        commit = release_auto.resolve_commit(historical_repo, "HEAD")

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.collect_release_assets(
                historical_repo,
                historical_output,
                "v0.1.14",
                commit,
            )

        self.assertIn("required paths mismatch", str(caught.exception))

        assets = release_auto.collect_release_assets(
            historical_repo,
            historical_output,
            "v0.1.14",
            commit,
            allow_legacy=True,
        )

        self.assertEqual(
            list(release_auto.REQUIRED_RELEASE_ASSETS),
            [asset.name for asset in assets],
        )

    def test_release_assets_reject_legacy_package_content_drift(self) -> None:
        module, receipt, commit = self._prepare_commit_bound_legacy_release()
        (receipt.package_dir / "README.md").write_text("drifted package\n")
        module._create_archive(receipt.package_dir, receipt.archive)

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.collect_release_assets(
                self.repo,
                self.output,
                "v0.1.13",
                commit,
                allow_legacy=True,
            )

        self.assertIn(
            "Release bundle differs from commit-bound source: README.md",
            str(caught.exception),
        )

    def test_release_assets_reject_legacy_runner_mismatch(self) -> None:
        _module, receipt, commit = self._prepare_commit_bound_legacy_release()
        receipt.runner.write_text("#!/usr/bin/env bash\necho drifted runner\n")
        receipt.runner.chmod(0o755)

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.collect_release_assets(
                self.repo,
                self.output,
                "v0.1.13",
                commit,
                allow_legacy=True,
            )

        self.assertIn(
            "Legacy top-level runner differs from package run.sh",
            str(caught.exception),
        )

    def test_release_assets_reject_legacy_package_symlink(self) -> None:
        _module, receipt, commit = self._prepare_commit_bound_legacy_release()
        helper = receipt.package_dir / "scripts" / "helper.py"
        helper.unlink()
        helper.symlink_to(self.repo / "scripts" / "helper.py")

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.collect_release_assets(
                self.repo,
                self.output,
                "v0.1.13",
                commit,
                allow_legacy=True,
            )

        self.assertIn(
            "Legacy release package contains a symlink",
            str(caught.exception),
        )

    def test_release_assets_reject_legacy_executable_mode(self) -> None:
        module, receipt, commit = self._prepare_commit_bound_legacy_release()
        packaged_command = receipt.package_dir / "scripts" / "codex-switch"
        packaged_command.chmod(0o644)
        module._create_archive(receipt.package_dir, receipt.archive)

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.collect_release_assets(
                self.repo,
                self.output,
                "v0.1.13",
                commit,
                allow_legacy=True,
            )

        self.assertIn(
            "Legacy release executable mode mismatch: scripts/codex-switch",
            str(caught.exception),
        )

    def test_release_assets_reject_legacy_archive_corruption(self) -> None:
        _module, receipt, commit = self._prepare_commit_bound_legacy_release()
        receipt.archive.write_bytes(b"not a tar archive")

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.collect_release_assets(
                self.repo,
                self.output,
                "v0.1.13",
                commit,
                allow_legacy=True,
            )

        self.assertIn(
            "Release archive could not be validated",
            str(caught.exception),
        )

    def test_legacy_archive_is_canonicalized_for_reruns(self) -> None:
        _module, receipt, commit = self._prepare_commit_bound_legacy_release()

        def write_historical_archive(mtime: int) -> str:
            for path in receipt.package_dir.rglob("*"):
                if not path.is_symlink():
                    os.utime(path, (mtime, mtime))
            with tarfile.open(receipt.archive, "w:gz") as archive:
                archive.add(receipt.package_dir, arcname="codex-switch")
            return sha256(receipt.archive)

        first_raw = write_historical_archive(1_700_000_000)
        first_assets = release_auto.collect_release_assets(
            self.repo,
            self.output,
            "v0.1.13",
            commit,
            allow_legacy=True,
        )
        first_canonical = {
            asset.name: asset.sha256 for asset in first_assets
        }["codex-switch.tar.gz"]
        second_raw = write_historical_archive(1_800_000_000)
        second_assets = release_auto.collect_release_assets(
            self.repo,
            self.output,
            "v0.1.13",
            commit,
            allow_legacy=True,
        )
        second_canonical = {
            asset.name: asset.sha256 for asset in second_assets
        }["codex-switch.tar.gz"]

        self.assertNotEqual(first_raw, second_raw)
        self.assertEqual(first_canonical, second_canonical)

    def test_release_assets_accept_supported_v0_1_12_layout(self) -> None:
        historical_repo = self.root / "historical-v0.1.12"
        subprocess.run(
            ["git", "clone", "-q", "--no-local", str(REPO_ROOT), str(historical_repo)],
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", "--detach", "v0.1.12"],
            cwd=historical_repo,
            check=True,
        )
        historical_output = self.root / "historical-output"
        env = os.environ.copy()
        env["CODEX_SWITCH_DIST_DIR"] = str(historical_output)
        subprocess.run(
            [str(historical_repo / "scripts" / "package-release.sh")],
            cwd=historical_repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        commit = release_auto.resolve_commit(historical_repo, "HEAD")

        assets = release_auto.collect_release_assets(
            historical_repo,
            historical_output,
            "v0.1.12",
            commit,
            allow_legacy=True,
        )

        self.assertEqual(
            list(release_auto.REQUIRED_RELEASE_ASSETS),
            [asset.name for asset in assets],
        )

    def test_supported_historical_tag_retries_reproduce_archive_hashes(
        self,
    ) -> None:
        for tag, first_mtime, second_mtime in (
            ("v0.1.12", 1_700_000_000, 1_800_000_000),
            ("v0.1.13", 1_710_000_000, 1_810_000_000),
        ):
            with self.subTest(tag=tag):
                historical_repo = self.root / f"historical-{tag}"
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "-q",
                        "--no-local",
                        str(REPO_ROOT),
                        str(historical_repo),
                    ],
                    check=True,
                )
                subprocess.run(
                    ["git", "checkout", "-q", "--detach", tag],
                    cwd=historical_repo,
                    check=True,
                )
                first_output = self.root / f"{tag}-first"
                env = os.environ.copy()
                env["CODEX_SWITCH_DIST_DIR"] = str(first_output)
                subprocess.run(
                    [str(historical_repo / "scripts" / "package-release.sh")],
                    cwd=historical_repo,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                second_output = self.root / f"{tag}-second"
                shutil.copytree(first_output, second_output)
                commit = release_auto.resolve_commit(historical_repo, "HEAD")

                canonical_hashes: list[str] = []
                raw_hashes: list[str] = []
                for output, mtime in (
                    (first_output, first_mtime),
                    (second_output, second_mtime),
                ):
                    package_dir = output / "codex-switch"
                    for path in [package_dir, *package_dir.rglob("*")]:
                        if not path.is_symlink():
                            os.utime(path, (mtime, mtime))
                    archive_path = output / "codex-switch.tar.gz"
                    with tarfile.open(archive_path, "w:gz") as archive:
                        archive.add(package_dir, arcname="codex-switch")
                    raw_hashes.append(sha256(archive_path))
                    assets = release_auto.collect_release_assets(
                        historical_repo,
                        output,
                        tag,
                        commit,
                        allow_legacy=True,
                    )
                    canonical_hashes.append(
                        {
                            asset.name: asset.sha256 for asset in assets
                        }["codex-switch.tar.gz"]
                    )

                self.assertNotEqual(raw_hashes[0], raw_hashes[1])
                self.assertEqual(canonical_hashes[0], canonical_hashes[1])

    def test_release_assets_reject_unsupported_legacy_layout(self) -> None:
        _module, _receipt, commit = self._prepare_commit_bound_legacy_release()

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.collect_release_assets(
                self.repo,
                self.output,
                "v0.1.14",
                commit,
                allow_legacy=True,
            )

        self.assertIn(
            "Unsupported historical release layout: v0.1.14",
            str(caught.exception),
        )

    def test_release_assets_use_commit_tree_despite_hidden_content_drift(
        self,
    ) -> None:
        cases = (
            ("assume-unchanged", "--assume-unchanged", "README.md"),
            ("skip-worktree", "--skip-worktree", "SKILL.md"),
        )
        for label, flag, relative in cases:
            with self.subTest(flag=label):
                repo = self.root / f"hidden-{label}"
                output = self.root / f"hidden-{label}-output"
                self._write_source_repo(repo)
                commit = self._initialize_commit_bound_repo(repo)
                subprocess.run(
                    ["git", "update-index", flag, relative],
                    cwd=repo,
                    check=True,
                )
                (repo / relative).write_text(f"hidden drift: {label}\n")
                load_bundle_module().build_release_bundle(repo, output)

                with self.assertRaises(release_auto.ReleaseConflict) as caught:
                    release_auto.collect_release_assets(
                        repo,
                        output,
                        "v1.2.3",
                        commit,
                    )

                self.assertIn(
                    f"Release source differs from commit tree: {relative}",
                    str(caught.exception),
                )

    def test_release_assets_use_commit_tree_file_modes(self) -> None:
        repo = self.root / "hidden-mode"
        output = self.root / "hidden-mode-output"
        self._write_source_repo(repo)
        commit = self._initialize_commit_bound_repo(repo)
        relative = "scripts/helper.py"
        subprocess.run(
            ["git", "update-index", "--skip-worktree", relative],
            cwd=repo,
            check=True,
        )
        (repo / relative).chmod(0o755)
        load_bundle_module().build_release_bundle(repo, output)

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.collect_release_assets(
                repo,
                output,
                "v1.2.3",
                commit,
            )

        self.assertIn(
            f"Release source mode differs from commit tree: {relative}",
            str(caught.exception),
        )

    def test_rejects_existing_destination_without_build_marker(self) -> None:
        sentinel = self._repo_sentinel()
        package_dir = self.output / "codex-switch"
        package_dir.mkdir(parents=True)
        destination_sentinel = package_dir / "VERSION"
        destination_sentinel.write_bytes(b"foreign-version\n")
        before = snapshot([sentinel, destination_sentinel])

        self._assert_bundle_error(
            "missing_build_marker",
            lambda module: module.build_release_bundle(self.repo, self.output),
        )

        self.assertEqual(before, snapshot(before))

    def test_rejects_boolean_schema_build_marker_without_mutation(self) -> None:
        sentinel = self._repo_sentinel()
        package_dir = self.output / "codex-switch"
        package_dir.mkdir(parents=True)
        destination_sentinel = package_dir / "VERSION"
        destination_sentinel.write_bytes(b"foreign-version\n")
        (package_dir / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema": MANIFEST_SCHEMA,
                    "schema_version": True,
                    "classification": MANIFEST_CLASSIFICATION,
                }
            )
        )
        before = snapshot([sentinel, destination_sentinel])

        self._assert_bundle_error(
            "invalid_build_marker",
            lambda module: module.build_release_bundle(self.repo, self.output),
        )

        self.assertEqual(before, snapshot(before))

    def test_copy_failure_preserves_previous_bundle_and_sentinels(self) -> None:
        module = load_bundle_module()
        module.build_release_bundle(self.repo, self.output)
        repository_sentinel = self._repo_sentinel()
        output_sentinel = self.output / "unrelated-output.bin"
        output_sentinel.write_bytes(b"unrelated-output")
        before = snapshot(
            [
                repository_sentinel,
                output_sentinel,
                *self._public_output_paths(),
            ]
        )
        (
            self.repo
            / "docs"
            / "troubleshooting"
            / "internal-azure-responses-resource-stickiness.md"
        ).write_text("candidate update\n")

        def fail_copy(source: Path, destination: Path) -> None:
            raise OSError(f"injected copy failure: {source} -> {destination}")

        with self.assertRaises(module.BundleError) as caught:
            module.build_release_bundle(
                self.repo,
                self.output,
                copy_path=fail_copy,
            )

        self.assertEqual("copy_failed", caught.exception.reason)
        self.assertEqual(before, snapshot(before))
        self._assert_no_release_workdirs(self.output)

    def test_release_bundle_rejects_missing_required_python_modules(self) -> None:
        module = load_bundle_module()

        for module_name in REQUIRED_PYTHON_MODULES:
            with self.subTest(module=module_name):
                source = self.root / f"missing-{Path(module_name).stem}"
                output = self.root / f"missing-output-{Path(module_name).stem}"
                self._write_source_repo(source)
                missing = source / "scripts" / module_name
                missing.unlink()
                sentinel = source / "README.md"
                before = sentinel.read_bytes()

                with self.assertRaises(module.BundleError) as caught:
                    module.build_release_bundle(source, output)

                self.assertEqual("source_invalid", caught.exception.reason)
                self.assertEqual(before, sentinel.read_bytes())
                self.assertFalse((output / "codex-switch").exists())

    def test_release_bundle_rejects_missing_transitive_runtime_import(
        self,
    ) -> None:
        module = load_bundle_module()
        (self.repo / "scripts" / "codex_switch_parity.py").write_text(
            "import codex_switch_missing_transitive_runtime\n"
        )

        with self.assertRaises(module.BundleError) as caught:
            module.build_release_bundle(self.repo, self.output)

        self.assertEqual("runtime_import_invalid", caught.exception.reason)
        self.assertFalse((self.output / "codex-switch").exists())

    def test_release_bundle_rejects_runtime_import_payload_mutation(
        self,
    ) -> None:
        module = load_bundle_module()
        (self.repo / "scripts" / "codex_switch_parity.py").write_text(
            "from pathlib import Path\n"
            "Path(__file__).with_name('import-mutation.txt').write_text('bad')\n"
        )

        with self.assertRaises(module.BundleError) as caught:
            module.build_release_bundle(self.repo, self.output)

        self.assertEqual("runtime_import_mutated", caught.exception.reason)
        self.assertFalse((self.output / "codex-switch").exists())

    def test_release_bundle_rejects_unresolved_generated_script_reference(
        self,
    ) -> None:
        module = load_bundle_module()
        (self.repo / "scripts" / "codex_switch_app_wrapper.py").write_text(
            'SCRIPT = "$SWITCH_SCRIPTS/codex_switch_missing_launcher.py"\n'
        )

        with self.assertRaises(module.BundleError) as caught:
            module.build_release_bundle(self.repo, self.output)

        self.assertEqual("runtime_reference_invalid", caught.exception.reason)
        self.assertFalse((self.output / "codex-switch").exists())

    def test_finalization_reclassifies_destination_swapped_after_preflight(
        self,
    ) -> None:
        module = load_bundle_module()
        module.build_release_bundle(self.repo, self.output)
        public_package = self.output / "codex-switch"
        saved_package = self.output / "saved-valid-package"
        public_runner = self.output / "run.sh"
        public_archive = self.output / "codex-switch.tar.gz"
        repository_sentinel = self._repo_sentinel()
        prior_package = tree_snapshot(public_package)
        prior_public = snapshot(
            [repository_sentinel, public_runner, public_archive]
        )
        unmarked_sentinel = public_package / "unmarked-sentinel.bin"
        sentinel_bytes = b"must-not-be-removed"
        swaps = []
        (self.repo / "VERSION").write_text("2.0.0\n")
        (
            self.repo
            / "docs"
            / "troubleshooting"
            / "internal-azure-responses-resource-stickiness.md"
        ).write_text("candidate update\n")

        def swap_public_package_after_preflight(
            source: Path,
            destination: Path,
        ) -> None:
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            if not swaps:
                os.replace(public_package, saved_package)
                public_package.mkdir()
                unmarked_sentinel.write_bytes(sentinel_bytes)
                swaps.append((public_package, saved_package))

        with self.assertRaises(module.BundleError) as caught:
            module.build_release_bundle(
                self.repo,
                self.output,
                copy_path=swap_public_package_after_preflight,
            )

        self.assertEqual("missing_build_marker", caught.exception.reason)
        self.assertEqual(1, len(swaps))
        self.assertEqual(sentinel_bytes, unmarked_sentinel.read_bytes())
        self.assertEqual(prior_package, tree_snapshot(saved_package))
        self.assertEqual(prior_public, snapshot(prior_public))
        self._assert_no_release_workdirs(self.output)

    def test_successful_bundle_has_manifest_modes_digests_and_archive(
        self,
    ) -> None:
        module = load_bundle_module()
        receipt = module.build_release_bundle(self.repo, self.output)
        package_dir = self.output / "codex-switch"
        manifest_path = package_dir / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())

        self.assertEqual(package_dir.resolve(), receipt.package_dir)
        self.assertEqual((self.output / "run.sh").resolve(), receipt.runner)
        self.assertEqual(
            (self.output / "codex-switch.tar.gz").resolve(),
            receipt.archive,
        )
        self.assertEqual(MANIFEST_SCHEMA, manifest["schema"])
        self.assertEqual(1, manifest["schema_version"])
        self.assertEqual(MANIFEST_CLASSIFICATION, manifest["classification"])
        self.assertEqual("1.2.3", manifest["version"])
        self.assertEqual(
            {"files": FIXED_FILES, "directories": FIXED_DIRECTORIES},
            manifest["allowlist"],
        )
        self.assertEqual(REQUIRED_PATHS, manifest["required_paths"])
        self.assertEqual(
            EXECUTABLE_EXPECTATIONS,
            {
                item["path"]: item["mode"]
                for item in manifest["executable_expectations"]
            },
        )

        file_entries = {item["path"]: item for item in manifest["files"]}
        actual_files = {
            path.relative_to(package_dir).as_posix()
            for path in package_dir.rglob("*")
            if path.is_file() and path.name != MANIFEST_NAME
        }
        self.assertEqual(actual_files, set(file_entries))
        for relative, entry in file_entries.items():
            path = package_dir / relative
            self.assertEqual(sha256(path), entry["sha256"])
            self.assertEqual(f"{stat.S_IMODE(path.stat().st_mode):04o}", entry["mode"])

        for relative, expected_mode in EXECUTABLE_EXPECTATIONS.items():
            path = package_dir / relative
            self.assertEqual(
                expected_mode,
                f"{stat.S_IMODE(path.stat().st_mode):04o}",
            )
        self.assertEqual(
            "0755",
            f"{stat.S_IMODE((self.output / 'run.sh').stat().st_mode):04o}",
        )
        self.assertEqual(
            manifest["top_level_runner"]["sha256"],
            sha256(self.output / "run.sh"),
        )
        self.assertEqual(
            manifest["payload_sha256"],
            manifest["archive"]["payload_sha256"],
        )
        self.assertFalse((package_dir / "NOT_SHIPPED.txt").exists())
        self.assertFalse((package_dir / "scripts" / "__pycache__").exists())
        self.assertTrue(
            (
                package_dir
                / "docs"
                / "troubleshooting"
                / "internal-azure-responses-resource-stickiness.md"
            ).is_file()
        )

        with tarfile.open(self.output / "codex-switch.tar.gz", "r:gz") as archive:
            members = {
                member.name.rstrip("/")
                for member in archive.getmembers()
                if member.name.rstrip("/")
            }
        expected_members = {"codex-switch"}
        expected_members.update(
            f"codex-switch/{path.relative_to(package_dir).as_posix()}"
            for path in package_dir.rglob("*")
        )
        self.assertEqual(expected_members, members)
        self.assertFalse(any("__pycache__" in member for member in members))
        self.assertFalse(any("codex-switch-stage" in member for member in members))

        validated = module.validate_release_outputs(
            package_dir,
            self.output / "run.sh",
            self.output / "codex-switch.tar.gz",
        )
        self.assertEqual(manifest["payload_sha256"], validated["payload_sha256"])
        self._assert_no_release_workdirs(self.output)

    def test_partial_finalization_failure_rolls_back_all_outputs(self) -> None:
        module = load_bundle_module()
        module.build_release_bundle(self.repo, self.output)
        before = snapshot(self._public_output_paths())
        (self.repo / "VERSION").write_text("2.0.0\n")
        (
            self.repo
            / "docs"
            / "troubleshooting"
            / "internal-azure-responses-resource-stickiness.md"
        ).write_text("updated candidate\n")
        injected_promotions = []

        def fail_after_package(source: Path, destination: Path) -> None:
            source = Path(source)
            destination = Path(destination)
            if (
                source.name == "run.sh"
                and source.parent.name.startswith(STAGING_PREFIX)
                and destination.resolve(strict=False)
                == (self.output / "run.sh").resolve(strict=False)
            ):
                injected_promotions.append((source, destination))
                raise OSError("injected finalization failure")
            os.replace(source, destination)

        with self.assertRaises(module.BundleError) as caught:
            module.build_release_bundle(
                self.repo,
                self.output,
                replace_path=fail_after_package,
            )

        self.assertEqual("finalization_failed", caught.exception.reason)
        self.assertEqual(1, len(injected_promotions))
        self.assertEqual(before, snapshot(before))
        module.validate_release_outputs(
            self.output / "codex-switch",
            self.output / "run.sh",
            self.output / "codex-switch.tar.gz",
        )
        self._assert_no_release_workdirs(self.output)

    def test_package_release_adapter_prints_tarball_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "dist"
            env = os.environ.copy()
            env["CODEX_SWITCH_DIST_DIR"] = str(output)
            env["PYTHONDONTWRITEBYTECODE"] = "1"

            result = subprocess.run(
                [str(PACKAGE_SCRIPT)],
                cwd=REPO_ROOT,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                f"{(output / 'codex-switch.tar.gz').resolve()}\n",
                result.stdout,
            )
            self.assertTrue((output / "codex-switch" / MANIFEST_NAME).is_file())
            self.assertTrue((output / "run.sh").is_file())
            self.assertTrue((output / "codex-switch.tar.gz").is_file())
            package = output / "codex-switch"
            manifest = json.loads((package / MANIFEST_NAME).read_text())
            self.assertEqual(REQUIRED_PATHS, manifest["required_paths"])
            self.assertTrue((package / "scripts" / "codex_switch_parity.py").is_file())
            packaged_paths = {
                path.relative_to(package).as_posix()
                for path in package.rglob("*")
            }
            self.assertNotIn(
                "testdata/parity/retained-v2-probe-redacted.json",
                packaged_paths,
            )
            self.assertFalse(
                any(
                    path.startswith((".planning/", "openspec/", "testdata/"))
                    or path.endswith("/config.toml")
                    or path.endswith("/auth.json")
                    for path in packaged_paths
                )
            )
            manifest_files = {
                item["path"] for item in manifest["files"]
            }
            actual_files = {
                path.relative_to(package).as_posix()
                for path in package.rglob("*")
                if path.is_file() and path.name != MANIFEST_NAME
            }
            self.assertEqual(actual_files, manifest_files)
            self.assertFalse((package / "scripts" / "__pycache__").exists())
            self._assert_no_release_workdirs(output)


class FakeReleaseGitHub:
    def __init__(
        self,
        *,
        exists: bool,
        assets: Dict[str, bytes],
        draft: bool = False,
        fail_publish_once: bool = False,
    ) -> None:
        self.exists = exists
        self.assets = dict(assets)
        self.draft = draft
        self.fail_publish_once = fail_publish_once
        self.calls: list[tuple[str, str]] = []

    def inspect_release(self, tag: str):
        self.calls.append(("inspect", tag))
        return release_auto.ReleaseSnapshot(
            exists=self.exists,
            assets=tuple(sorted(self.assets)),
            draft=self.draft,
        )

    def create_release(self, tag: str) -> None:
        self.calls.append(("create", tag))
        if self.exists:
            raise AssertionError("release already exists")
        self.exists = True
        self.draft = True

    def download_asset(self, tag: str, name: str, destination: Path) -> None:
        self.calls.append(("download", name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.assets[name])

    def upload_asset(self, tag: str, path: Path) -> None:
        self.calls.append(("upload", path.name))
        if path.name in self.assets:
            raise AssertionError(f"asset already exists: {path.name}")
        self.assets[path.name] = path.read_bytes()

    def publish_release(self, tag: str) -> None:
        self.calls.append(("publish", tag))
        if self.fail_publish_once:
            self.fail_publish_once = False
            raise release_auto.ReleaseError("injected publish failure")
        self.draft = False


class HiddenStarterReleaseGitHub(FakeReleaseGitHub):
    def __init__(self, *, state: str = "starter", size: int = 0) -> None:
        super().__init__(exists=True, assets={}, draft=False)
        self.hidden_assets = {
            "install.sh": SimpleNamespace(
                asset_id=901,
                name="install.sh",
                state=state,
                size=size,
            )
        }

    def inspect_release(self, tag: str):
        snapshot = super().inspect_release(tag)
        object.__setattr__(
            snapshot,
            "starter_assets",
            tuple(self.hidden_assets.values()),
        )
        return snapshot

    def delete_asset(self, tag: str, asset: object) -> None:
        name = str(getattr(asset, "name"))
        self.calls.append(("delete", name))
        hidden = self.hidden_assets.get(name)
        if hidden is None or hidden.asset_id != getattr(asset, "asset_id"):
            raise AssertionError(f"unexpected hidden asset deletion: {name}")
        del self.hidden_assets[name]

    def upload_asset(self, tag: str, path: Path) -> None:
        if path.name in self.hidden_assets:
            raise release_auto.ReleaseError(
                f"asset under the same name already exists: [{path.name}]"
            )
        super().upload_asset(tag, path)


class DisappearingStarterReleaseGitHub(HiddenStarterReleaseGitHub):
    def __init__(
        self,
        *,
        recreate_result: str = "draft",
        starter_names: tuple[str, ...] = ("install.sh",),
    ) -> None:
        super().__init__()
        if recreate_result not in {
            "draft",
            "failure",
            "missing",
            "published",
            "nonempty",
        }:
            raise ValueError(f"unsupported recreate result: {recreate_result}")
        if (
            not starter_names
            or len(starter_names) != len(set(starter_names))
            or any(
                name not in release_auto.REQUIRED_RELEASE_ASSETS
                for name in starter_names
            )
        ):
            raise ValueError("starter names must be unique canonical assets")
        self.recreate_result = recreate_result
        self.hidden_assets = {
            name: SimpleNamespace(
                asset_id=901 + index,
                name=name,
                state="starter",
                size=0,
            )
            for index, name in enumerate(starter_names)
        }

    def delete_asset(self, tag: str, asset: object) -> None:
        super().delete_asset(tag, asset)
        self.hidden_assets.clear()
        self.exists = False
        self.draft = False

    def create_release(self, tag: str) -> None:
        if self.recreate_result == "failure":
            self.calls.append(("create", tag))
            raise release_auto.ReleaseError("injected recreation failure")
        super().create_release(tag)
        if self.recreate_result == "missing":
            self.exists = False
        elif self.recreate_result == "published":
            self.draft = False
        elif self.recreate_result == "nonempty":
            self.assets["unexpected.bin"] = b"unexpected"


class RetryingDisappearingStarterReleaseGitHub(
    DisappearingStarterReleaseGitHub
):
    def __init__(
        self,
        *,
        retryable_create_failures: int = 0,
        post_success_missing_reads: int = 0,
    ) -> None:
        super().__init__()
        self.retryable_create_failures = retryable_create_failures
        self.post_success_missing_reads = post_success_missing_reads
        self.create_succeeded = False

    def create_release(self, tag: str) -> None:
        self.calls.append(("create", tag))
        if self.retryable_create_failures > 0:
            self.retryable_create_failures -= 1
            raise release_auto.ReleaseCreateRetryable(
                "tag_name_exists",
                "Release.tag_name already exists",
            )
        if self.exists:
            raise AssertionError("release already exists")
        self.exists = True
        self.draft = True
        self.create_succeeded = True

    def inspect_release(self, tag: str):
        if self.create_succeeded and self.post_success_missing_reads > 0:
            self.calls.append(("inspect", tag))
            self.post_success_missing_reads -= 1
            return release_auto.ReleaseSnapshot(
                exists=False,
                assets=(),
                draft=False,
            )
        return super().inspect_release(tag)


class CodexReleasePlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "scripts").mkdir()
        (self.repo / "VERSION").write_text("1.0.0\n")
        (self.repo / "README.md").write_text("base\n")
        (self.repo / "scripts" / "tool.py").write_text("VALUE = 1\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        self._git("tag", "v1.0.0")
        self.base_commit = release_auto.resolve_commit(self.repo, "HEAD")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def _commit_release_change(self) -> str:
        (self.repo / "scripts" / "tool.py").write_text("VALUE = 2\n")
        self._git("add", "scripts/tool.py")
        self._git("commit", "-q", "-m", "release change")
        return release_auto.resolve_commit(self.repo, "HEAD")

    def _asset_fixture(self) -> tuple[tuple[object, ...], Path]:
        asset_root = self.root / "assets"
        asset_root.mkdir()
        paths = {
            "install.sh": asset_root / "install.sh",
            "run.sh": asset_root / "run.sh",
            "codex-switch.tar.gz": asset_root / "codex-switch.tar.gz",
        }
        for name, path in paths.items():
            path.write_bytes(f"{name}-bytes\n".encode())
        assets = tuple(
            release_auto.build_asset_evidence(name, paths[name])
            for name in release_auto.REQUIRED_RELEASE_ASSETS
        )
        manifest = self.root / "asset-manifest.json"
        release_auto.write_asset_manifest(
            manifest,
            tag="v1.0.1",
            commit=self.base_commit,
            assets=assets,
        )
        return assets, manifest

    def test_non_ancestor_latest_tag_is_rejected(self) -> None:
        self._git("checkout", "-q", "--orphan", "unrelated")
        (self.repo / "README.md").write_text("unrelated\n")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "unrelated root")

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.build_plan(self.repo, "HEAD")

        self.assertIn("is not an ancestor", str(caught.exception))

    def test_existing_tag_on_different_commit_is_rejected(self) -> None:
        candidate_commit = self._commit_release_change()

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.validate_prepare_state(
                source_commit=self.base_commit,
                remote_main_commit=self.base_commit,
                candidate_commit=candidate_commit,
                existing_tag_commit="f" * 40,
            )

        self.assertIn(
            "release tag points at a different commit",
            str(caught.exception),
        )

    def test_latest_tag_missing_asset_selects_reconciliation(self) -> None:
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                "install.sh": b"install",
                "run.sh": b"run",
            },
        )

        plan = release_auto.build_plan(self.repo, "HEAD", github=github)

        self.assertEqual("reconcile", plan["release_action"])
        self.assertEqual("v1.0.0", plan["target_tag"])
        self.assertEqual(self.base_commit, plan["target_commit"])
        self.assertEqual(["codex-switch.tar.gz"], plan["missing_assets"])

    def test_reconciliation_preserves_pending_release_in_same_plan(self) -> None:
        source_commit = self._commit_release_change()
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                "install.sh": b"install",
                "run.sh": b"run",
            },
        )

        plan = release_auto.build_plan(self.repo, "HEAD", github=github)
        github_output = self.root / "github-output"
        release_auto.write_github_output(github_output, plan)
        outputs = dict(
            line.split("=", 1)
            for line in github_output.read_text().splitlines()
        )

        self.assertEqual("reconcile_then_prepare", plan["release_action"])
        self.assertTrue(plan["reconcile_required"])
        self.assertTrue(plan["prepare_required"])
        self.assertEqual("v1.0.0", plan["target_tag"])
        self.assertEqual(self.base_commit, plan["target_commit"])
        self.assertEqual(source_commit, plan["source_commit"])
        self.assertEqual("v1.0.1", plan["next_tag"])
        self.assertEqual("true", outputs["reconcile_required"])
        self.assertEqual("true", outputs["prepare_required"])
        self.assertEqual(source_commit, outputs["source_commit"])

    def test_abandoned_latest_tag_prepares_replacement_without_inspection(
        self,
    ) -> None:
        source_commit = self._commit_release_change()
        github = FakeReleaseGitHub(
            exists=True,
            assets={},
            draft=True,
        )

        plan = release_auto.build_plan(
            self.repo,
            "HEAD",
            github=github,
            abandoned_tags=("v1.0.0",),
        )
        github_output = self.root / "github-output"
        release_auto.write_github_output(github_output, plan)
        outputs = dict(
            line.split("=", 1)
            for line in github_output.read_text().splitlines()
        )

        self.assertEqual([], github.calls)
        self.assertEqual("prepare", plan["release_action"])
        self.assertFalse(plan["reconcile_required"])
        self.assertTrue(plan["prepare_required"])
        self.assertEqual(source_commit, plan["source_commit"])
        self.assertEqual("v1.0.1", plan["target_tag"])
        self.assertEqual("v1.0.1", plan["next_tag"])
        self.assertEqual("v1.0.0", plan["abandoned_tag"])
        self.assertEqual("v1.0.0", outputs["abandoned_tag"])

    def test_abandoned_latest_tag_requires_release_relevant_replacement(
        self,
    ) -> None:
        github = FakeReleaseGitHub(
            exists=True,
            assets={},
            draft=True,
        )

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.build_plan(
                self.repo,
                "HEAD",
                github=github,
                abandoned_tags=("v1.0.0",),
            )

        self.assertEqual([], github.calls)
        self.assertIn(
            "without a release-relevant replacement",
            str(caught.exception),
        )

    def test_older_abandoned_tag_does_not_skip_latest_release_inspection(
        self,
    ) -> None:
        self._commit_release_change()
        self._git("tag", "v1.0.1")
        (self.repo / "scripts" / "tool.py").write_text("VALUE = 3\n")
        self._git("add", "scripts/tool.py")
        self._git("commit", "-q", "-m", "next release change")
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                name: name.encode()
                for name in release_auto.REQUIRED_RELEASE_ASSETS
            },
            draft=False,
        )

        plan = release_auto.build_plan(
            self.repo,
            "HEAD",
            github=github,
            abandoned_tags=("v1.0.0",),
        )

        self.assertEqual([("inspect", "v1.0.1")], github.calls)
        self.assertEqual("", plan["abandoned_tag"])
        self.assertEqual("v1.0.2", plan["next_tag"])

    def test_matching_complete_latest_tag_requires_no_action(self) -> None:
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                name: name.encode()
                for name in release_auto.REQUIRED_RELEASE_ASSETS
            },
            draft=False,
        )

        plan = release_auto.build_plan(self.repo, "HEAD", github=github)

        self.assertEqual("none", plan["release_action"])
        self.assertFalse(plan["release_required"])
        self.assertEqual("", plan["target_tag"])

    def test_complete_latest_release_with_changes_selects_prepare(self) -> None:
        source_commit = self._commit_release_change()
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                name: name.encode()
                for name in release_auto.REQUIRED_RELEASE_ASSETS
            },
            draft=False,
        )

        plan = release_auto.build_plan(self.repo, "HEAD", github=github)

        self.assertEqual("prepare", plan["release_action"])
        self.assertFalse(plan["reconcile_required"])
        self.assertTrue(plan["prepare_required"])
        self.assertEqual(source_commit, plan["source_commit"])
        self.assertEqual("v1.0.1", plan["target_tag"])
        self.assertEqual("", plan["target_commit"])
        self.assertEqual("v1.0.1", plan["next_tag"])

    def test_remote_main_race_is_rejected_before_ref_creation(self) -> None:
        candidate_commit = self._commit_release_change()

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.validate_prepare_state(
                source_commit=self.base_commit,
                remote_main_commit="e" * 40,
                candidate_commit=candidate_commit,
                existing_tag_commit=None,
            )

        self.assertIn("remote main moved", str(caught.exception))

    def test_asset_checksum_drift_is_rejected_after_validation(self) -> None:
        _assets, manifest = self._asset_fixture()
        (self.root / "assets" / "run.sh").write_text("changed\n")

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.load_asset_manifest(
                manifest,
                expected_tag="v1.0.1",
                expected_commit=self.base_commit,
            )

        self.assertIn(
            "Release asset changed after validation: run.sh",
            str(caught.exception),
        )

    def test_publish_failure_rerun_reuses_matching_uploaded_assets(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = FakeReleaseGitHub(
            exists=False,
            assets={},
            fail_publish_once=True,
        )

        with self.assertRaises(release_auto.ReleaseError):
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
            )

        first_uploads = [
            call for call in github.calls if call[0] == "upload"
        ]
        receipt = release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
        )
        all_uploads = [
            call for call in github.calls if call[0] == "upload"
        ]

        self.assertEqual("published", receipt["outcome"])
        self.assertEqual(first_uploads, all_uploads)
        self.assertEqual(
            sorted(release_auto.REQUIRED_RELEASE_ASSETS),
            sorted(github.assets),
        )
        self.assertFalse(github.draft)

    def test_reconcile_reads_back_new_draft_before_upload(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = FakeReleaseGitHub(exists=False, assets={})

        release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
        )

        create_index = github.calls.index(("create", "v1.0.1"))
        upload_index = github.calls.index(("upload", "install.sh"))
        self.assertEqual(
            ("inspect", "v1.0.1"),
            github.calls[create_index + 1],
        )
        self.assertLess(create_index, upload_index)

    def test_reconcile_uploads_only_the_missing_asset(self) -> None:
        assets, _manifest = self._asset_fixture()
        asset_bytes = {
            asset.name: asset.path.read_bytes()
            for asset in assets
        }
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                "install.sh": asset_bytes["install.sh"],
                "run.sh": asset_bytes["run.sh"],
            },
            draft=False,
        )

        receipt = release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
        )

        self.assertEqual("reconciled", receipt["outcome"])
        self.assertEqual(
            [("upload", "codex-switch.tar.gz")],
            [call for call in github.calls if call[0] == "upload"],
        )
        self.assertNotIn(("create", "v1.0.1"), github.calls)
        self.assertNotIn(("publish", "v1.0.1"), github.calls)

    def test_reconcile_recovers_hidden_zero_byte_starter_asset(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = HiddenStarterReleaseGitHub()

        receipt = release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
        )

        self.assertEqual("reconciled", receipt["outcome"])
        self.assertEqual(
            ["install.sh"],
            receipt["recovered_starter_assets"],
        )
        self.assertEqual(
            sorted(release_auto.REQUIRED_RELEASE_ASSETS),
            sorted(github.assets),
        )
        delete_index = github.calls.index(("delete", "install.sh"))
        upload_index = github.calls.index(("upload", "install.sh"))
        self.assertLess(delete_index, upload_index)
        self.assertIn(
            ("inspect", "v1.0.1"),
            github.calls[delete_index + 1 : upload_index],
        )

    def test_reconcile_recreates_draft_when_starter_delete_removes_release(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        github = DisappearingStarterReleaseGitHub()

        receipt = release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
        )

        self.assertEqual("published", receipt["outcome"])
        self.assertTrue(
            receipt["recreated_release_after_starter_recovery"]
        )
        self.assertEqual(
            ["install.sh"],
            receipt["recovered_starter_assets"],
        )
        self.assertEqual(
            sorted(release_auto.REQUIRED_RELEASE_ASSETS),
            sorted(github.assets),
        )
        self.assertFalse(github.draft)
        delete_index = github.calls.index(("delete", "install.sh"))
        create_index = github.calls.index(("create", "v1.0.1"))
        upload_index = github.calls.index(("upload", "install.sh"))
        self.assertLess(delete_index, create_index)
        self.assertIn(
            ("inspect", "v1.0.1"),
            github.calls[delete_index + 1 : create_index],
        )
        self.assertEqual(
            ("inspect", "v1.0.1"),
            github.calls[create_index + 1],
        )
        self.assertLess(create_index, upload_index)
        self.assertIn(("publish", "v1.0.1"), github.calls)

    def test_reconcile_stops_using_stale_starter_ids_when_release_disappears(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        github = DisappearingStarterReleaseGitHub(
            starter_names=("install.sh", "run.sh")
        )

        receipt = release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
        )

        self.assertEqual(
            [("delete", "install.sh")],
            [call for call in github.calls if call[0] == "delete"],
        )
        self.assertEqual(
            ["install.sh", "run.sh"],
            receipt["recovered_starter_assets"],
        )
        self.assertTrue(
            receipt["recreated_release_after_starter_recovery"]
        )
        self.assertEqual(
            sorted(release_auto.REQUIRED_RELEASE_ASSETS),
            sorted(github.assets),
        )

    def test_reconcile_stops_when_starter_release_recreation_fails(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        github = DisappearingStarterReleaseGitHub(
            recreate_result="failure"
        )

        with self.assertRaises(release_auto.ReleaseError) as caught:
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
            )

        self.assertIn("injected recreation failure", str(caught.exception))
        self.assertIn(("create", "v1.0.1"), github.calls)
        self.assertFalse(
            any(call[0] in {"upload", "publish"} for call in github.calls)
        )

    def test_reconcile_retries_typed_tag_name_exists_recreation(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = RetryingDisappearingStarterReleaseGitHub(
            retryable_create_failures=1
        )
        checks = 0

        def check_tag_identity() -> None:
            nonlocal checks
            checks += 1

        with mock.patch("time.sleep") as sleep:
            receipt = release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
                tag_identity_check=check_tag_identity,
            )

        self.assertEqual("published", receipt["outcome"])
        self.assertEqual(
            [("create", "v1.0.1"), ("create", "v1.0.1")],
            [call for call in github.calls if call[0] == "create"],
        )
        self.assertEqual([mock.call(1.0)], sleep.call_args_list)
        self.assertGreaterEqual(checks, 4)

    def test_reconcile_accepts_empty_draft_after_retryable_create_error(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()

        class AcceptedButErroredGitHub(
            RetryingDisappearingStarterReleaseGitHub
        ):
            def create_release(inner_self, tag: str) -> None:
                inner_self.calls.append(("create", tag))
                inner_self.exists = True
                inner_self.draft = True
                raise release_auto.ReleaseCreateRetryable(
                    "transport_error",
                    "connection force closed after server accepted release",
                )

        github = AcceptedButErroredGitHub()

        with mock.patch("time.sleep") as sleep:
            receipt = release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
            )

        self.assertEqual("published", receipt["outcome"])
        self.assertEqual(
            [("create", "v1.0.1")],
            [call for call in github.calls if call[0] == "create"],
        )
        sleep.assert_not_called()

    def test_reconcile_waits_for_successful_create_visibility_without_recreating(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        github = RetryingDisappearingStarterReleaseGitHub(
            post_success_missing_reads=2
        )

        with mock.patch("time.sleep") as sleep:
            receipt = release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
            )

        self.assertEqual("published", receipt["outcome"])
        self.assertEqual(
            [("create", "v1.0.1")],
            [call for call in github.calls if call[0] == "create"],
        )
        self.assertEqual(
            [mock.call(1.0), mock.call(2.0)],
            sleep.call_args_list,
        )

    def test_reconcile_rechecks_tag_before_retrying_recreation(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = RetryingDisappearingStarterReleaseGitHub(
            retryable_create_failures=1
        )
        checks = 0

        def check_tag_identity() -> None:
            nonlocal checks
            checks += 1
            if checks == 4:
                raise release_auto.ReleaseConflict("remote tag moved")

        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(release_auto.ReleaseConflict) as caught:
                release_auto.reconcile_release_assets(
                    tag="v1.0.1",
                    release_commit=self.base_commit,
                    tag_commit=self.base_commit,
                    assets=assets,
                    github=github,
                    tag_identity_check=check_tag_identity,
                )

        self.assertIn("remote tag moved", str(caught.exception))
        self.assertEqual(
            [("create", "v1.0.1")],
            [call for call in github.calls if call[0] == "create"],
        )
        self.assertEqual([mock.call(1.0)], sleep.call_args_list)
        self.assertFalse(
            any(call[0] in {"upload", "publish"} for call in github.calls)
        )

    def test_reconcile_rechecks_tag_during_successful_create_readback(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        github = RetryingDisappearingStarterReleaseGitHub(
            post_success_missing_reads=2
        )
        checks = 0

        def check_tag_identity() -> None:
            nonlocal checks
            checks += 1
            if checks == 4:
                raise release_auto.ReleaseConflict("remote tag moved")

        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(release_auto.ReleaseConflict) as caught:
                release_auto.reconcile_release_assets(
                    tag="v1.0.1",
                    release_commit=self.base_commit,
                    tag_commit=self.base_commit,
                    assets=assets,
                    github=github,
                    tag_identity_check=check_tag_identity,
                )

        self.assertIn("remote tag moved", str(caught.exception))
        self.assertEqual(
            [("create", "v1.0.1")],
            [call for call in github.calls if call[0] == "create"],
        )
        self.assertEqual([mock.call(1.0)], sleep.call_args_list)
        self.assertFalse(
            any(call[0] in {"upload", "publish"} for call in github.calls)
        )

    def test_reconcile_rejects_untyped_recreation_failure_without_retry(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        github = DisappearingStarterReleaseGitHub(
            recreate_result="failure"
        )

        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(release_auto.ReleaseError) as caught:
                release_auto.reconcile_release_assets(
                    tag="v1.0.1",
                    release_commit=self.base_commit,
                    tag_commit=self.base_commit,
                    assets=assets,
                    github=github,
                )

        self.assertIn("injected recreation failure", str(caught.exception))
        self.assertEqual(
            [("create", "v1.0.1")],
            [call for call in github.calls if call[0] == "create"],
        )
        sleep.assert_not_called()

    def test_reconcile_stops_after_recreation_retry_exhaustion(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = RetryingDisappearingStarterReleaseGitHub(
            retryable_create_failures=10
        )

        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(release_auto.ReleaseError) as caught:
                release_auto.reconcile_release_assets(
                    tag="v1.0.1",
                    release_commit=self.base_commit,
                    tag_commit=self.base_commit,
                    assets=assets,
                    github=github,
                )

        self.assertIn("after 5 attempts", str(caught.exception))
        self.assertEqual(
            5,
            len([call for call in github.calls if call[0] == "create"]),
        )
        self.assertEqual(
            [mock.call(1.0), mock.call(2.0), mock.call(4.0), mock.call(8.0)],
            sleep.call_args_list,
        )
        self.assertFalse(
            any(call[0] in {"upload", "publish"} for call in github.calls)
        )

    def test_reconcile_requires_empty_draft_after_starter_recreation(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        cases = (
            ("missing", release_auto.ReleaseError, "missing after draft creation"),
            ("published", release_auto.ReleaseConflict, "not a draft"),
            ("nonempty", release_auto.ReleaseConflict, "not empty"),
        )

        for recreate_result, error_type, message in cases:
            with self.subTest(recreate_result=recreate_result):
                github = DisappearingStarterReleaseGitHub(
                    recreate_result=recreate_result
                )

                with mock.patch("time.sleep"):
                    with self.assertRaises(error_type) as caught:
                        release_auto.reconcile_release_assets(
                            tag="v1.0.1",
                            release_commit=self.base_commit,
                            tag_commit=self.base_commit,
                            assets=assets,
                            github=github,
                        )

                self.assertIn(message, str(caught.exception))
                self.assertFalse(
                    any(
                        call[0] in {"upload", "publish"}
                        for call in github.calls
                    )
                )

    def test_reconcile_rechecks_tag_before_recreating_starter_release(
        self,
    ) -> None:
        assets, _manifest = self._asset_fixture()
        github = DisappearingStarterReleaseGitHub()
        checks = 0

        def check_tag_identity() -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise release_auto.ReleaseConflict("remote tag moved")

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
                tag_identity_check=check_tag_identity,
            )

        self.assertIn("remote tag moved", str(caught.exception))
        self.assertEqual(2, checks)
        self.assertNotIn(("create", "v1.0.1"), github.calls)
        self.assertFalse(
            any(call[0] in {"upload", "publish"} for call in github.calls)
        )

    def test_reconcile_rejects_nonempty_starter_asset(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = HiddenStarterReleaseGitHub(size=17)

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
            )

        self.assertIn("non-empty starter", str(caught.exception))
        self.assertFalse(
            any(call[0] in {"delete", "upload"} for call in github.calls)
        )

    def test_reconcile_rejects_unsupported_hidden_asset_state(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = HiddenStarterReleaseGitHub(state="processing")

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
            )

        self.assertIn("unsupported release asset state", str(caught.exception))
        self.assertFalse(
            any(call[0] in {"delete", "upload"} for call in github.calls)
        )

    def test_reconcile_rechecks_tag_identity_before_starter_delete(self) -> None:
        assets, _manifest = self._asset_fixture()
        events: list[str] = []

        class RecordingGitHub(HiddenStarterReleaseGitHub):
            def delete_asset(inner_self, tag: str, asset: object) -> None:
                events.append("delete")
                super().delete_asset(tag, asset)

            def upload_asset(inner_self, tag: str, path: Path) -> None:
                events.append(f"upload:{path.name}")
                super().upload_asset(tag, path)

        def check_tag_identity() -> None:
            events.append("check")

        release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=RecordingGitHub(),
            tag_identity_check=check_tag_identity,
        )

        delete_index = events.index("delete")
        self.assertGreater(delete_index, 0)
        self.assertEqual("check", events[delete_index - 1])

    def test_reconcile_complete_same_tag_is_read_only(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                asset.name: asset.path.read_bytes()
                for asset in assets
            },
            draft=False,
        )

        receipt = release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
        )

        self.assertEqual("complete", receipt["outcome"])
        self.assertFalse(
            any(
                call[0] in {"create", "upload", "publish"}
                for call in github.calls
            )
        )

    def test_reconcile_existing_checksum_conflict_does_not_mutate(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = FakeReleaseGitHub(
            exists=True,
            assets={
                "install.sh": b"conflicting bytes",
            },
            draft=True,
        )

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
            )

        self.assertIn(
            "Existing release asset checksum mismatch: v1.0.1/install.sh",
            str(caught.exception),
        )
        self.assertFalse(
            any(
                call[0] in {"create", "upload", "publish"}
                for call in github.calls
            )
        )

    def test_reconcile_tag_commit_conflict_stops_before_github_calls(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = FakeReleaseGitHub(
            exists=True,
            assets={},
            draft=True,
        )

        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit="f" * 40,
                assets=assets,
                github=github,
            )

        self.assertIn(
            "release tag v1.0.1 points at a different commit",
            str(caught.exception),
        )
        self.assertEqual([], github.calls)

    def test_resolve_remote_semantic_tag_requires_exact_remote_tag(self) -> None:
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self._git("remote", "add", "origin", str(remote))
        self._git("push", "-q", "origin", "refs/tags/v1.0.0")

        resolved = release_auto.resolve_remote_semantic_tag(
            self.repo,
            "origin",
            "v1.0.0",
        )

        self.assertEqual(self.base_commit, resolved)
        for invalid in (
            "main",
            "v1.0",
            "refs/tags/v1.0.0",
            "v1.0.0^{commit}",
            "v1.0.0\nrefs/heads/main",
        ):
            with self.subTest(tag=invalid):
                with self.assertRaises(
                    (ValueError, release_auto.ReleaseError)
                ):
                    release_auto.resolve_remote_semantic_tag(
                        self.repo,
                        "origin",
                        invalid,
                    )

    def test_reconcile_rechecks_tag_identity_around_every_mutation(self) -> None:
        assets, _manifest = self._asset_fixture()
        events: list[str] = []

        class RecordingGitHub(FakeReleaseGitHub):
            def create_release(inner_self, tag: str) -> None:
                events.append("create")
                super().create_release(tag)

            def upload_asset(inner_self, tag: str, path: Path) -> None:
                events.append(f"upload:{path.name}")
                super().upload_asset(tag, path)

            def publish_release(inner_self, tag: str) -> None:
                events.append("publish")
                super().publish_release(tag)

        github = RecordingGitHub(exists=False, assets={})

        def check_tag_identity() -> None:
            events.append("check")

        release_auto.reconcile_release_assets(
            tag="v1.0.1",
            release_commit=self.base_commit,
            tag_commit=self.base_commit,
            assets=assets,
            github=github,
            tag_identity_check=check_tag_identity,
        )

        mutation_indices = [
            index
            for index, event in enumerate(events)
            if event == "create"
            or event == "publish"
            or event.startswith("upload:")
        ]
        self.assertTrue(mutation_indices)
        for index in mutation_indices:
            self.assertGreater(index, 0)
            self.assertEqual("check", events[index - 1])
        self.assertEqual("check", events[-1])
        self.assertGreaterEqual(
            events.count("check"),
            len(mutation_indices) + 2,
        )

    def test_reconcile_tag_movement_aborts_before_later_mutations(self) -> None:
        assets, _manifest = self._asset_fixture()
        github = FakeReleaseGitHub(exists=False, assets={})
        checks = 0

        def check_tag_identity() -> None:
            nonlocal checks
            checks += 1
            if checks == 3:
                raise release_auto.ReleaseConflict("remote tag moved")

        with self.assertRaises(release_auto.ReleaseConflict):
            release_auto.reconcile_release_assets(
                tag="v1.0.1",
                release_commit=self.base_commit,
                tag_commit=self.base_commit,
                assets=assets,
                github=github,
                tag_identity_check=check_tag_identity,
            )

        self.assertEqual(
            [],
            [call for call in github.calls if call[0] == "upload"],
        )
        self.assertNotIn(("publish", "v1.0.1"), github.calls)

    def test_github_release_inspection_finds_draft_via_release_listing(
        self,
    ) -> None:
        first_page = [
            {
                "tag_name": f"v0.0.{index}",
            }
            for index in range(100)
        ]
        responses = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps(
                    {
                        "message": "Not Found",
                        "status": "404",
                    }
                ),
                stderr="gh: Not Found (HTTP 404)",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(first_page),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "id": 77,
                            "tag_name": "v0.1.14",
                            "draft": True,
                            "assets": [],
                        }
                    ]
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="[]",
                stderr="",
            ),
        ]

        with mock.patch.object(
            release_auto,
            "_run",
            side_effect=responses,
        ) as run:
            snapshot = release_auto.GitHubCliAdapter(
                "owner/repo"
            ).inspect_release("v0.1.14")

        self.assertTrue(snapshot.exists)
        self.assertTrue(snapshot.draft)
        self.assertEqual((), snapshot.assets)
        self.assertEqual(
            [
                [
                    "gh",
                    "api",
                    "repos/owner/repo/releases/tags/v0.1.14",
                ],
                [
                    "gh",
                    "api",
                    "repos/owner/repo/releases?per_page=100&page=1",
                ],
                [
                    "gh",
                    "api",
                    "repos/owner/repo/releases?per_page=100&page=2",
                ],
                [
                    "gh",
                    "api",
                    "repos/owner/repo/releases/77/assets?per_page=100&page=1",
                ],
            ],
            [call.args[0] for call in run.call_args_list],
        )

    def test_github_release_inspection_preserves_missing_without_exact_match(
        self,
    ) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps(
                    {
                        "message": "Not Found",
                        "status": "404",
                    }
                ),
                stderr="gh: Not Found (HTTP 404)",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps([{"tag_name": "v0.1.140"}]),
                stderr="",
            ),
        ]

        with mock.patch.object(
            release_auto,
            "_run",
            side_effect=responses,
        ) as run:
            snapshot = release_auto.GitHubCliAdapter(
                "owner/repo"
            ).inspect_release("v0.1.14")

        self.assertFalse(snapshot.exists)
        self.assertEqual(2, run.call_count)

    def test_github_release_inspection_rejects_duplicate_list_matches(
        self,
    ) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps(
                    {
                        "message": "Not Found",
                        "status": "404",
                    }
                ),
                stderr="gh: Not Found (HTTP 404)",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    [
                        {"tag_name": "v0.1.14"},
                        {"tag_name": "v0.1.14"},
                    ]
                ),
                stderr="",
            ),
        ]

        with mock.patch.object(
            release_auto,
            "_run",
            side_effect=responses,
        ):
            with self.assertRaises(release_auto.ReleaseConflict) as caught:
                release_auto.GitHubCliAdapter("owner/repo").inspect_release(
                    "v0.1.14"
                )

        self.assertIn("duplicate releases", str(caught.exception))

    def test_github_release_inspection_does_not_list_on_non_404_failure(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "message": "Resource not accessible by integration",
                    "status": "403",
                }
            ),
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )

        with mock.patch.object(
            release_auto,
            "_run",
            return_value=completed,
        ) as run:
            with self.assertRaises(release_auto.ReleaseError):
                release_auto.GitHubCliAdapter("owner/repo").inspect_release(
                    "v0.1.14"
                )

        self.assertEqual(1, run.call_count)

    def test_github_release_inspection_rejects_invalid_listing_payloads(
        self,
    ) -> None:
        invalid_payloads = (
            "{",
            json.dumps({"tag_name": "v0.1.14"}),
            json.dumps([{"id": 77}]),
        )

        for listing_payload in invalid_payloads:
            with self.subTest(listing_payload=listing_payload):
                responses = [
                    subprocess.CompletedProcess(
                        args=[],
                        returncode=1,
                        stdout=json.dumps(
                            {
                                "message": "Not Found",
                                "status": "404",
                            }
                        ),
                        stderr="gh: Not Found (HTTP 404)",
                    ),
                    subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=listing_payload,
                        stderr="",
                    ),
                ]

                with mock.patch.object(
                    release_auto,
                    "_run",
                    side_effect=responses,
                ):
                    with self.assertRaises(release_auto.ReleaseError):
                        release_auto.GitHubCliAdapter(
                            "owner/repo"
                        ).inspect_release("v0.1.14")

    def test_github_release_inspection_rejects_unbounded_listing(
        self,
    ) -> None:
        missing = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "message": "Not Found",
                    "status": "404",
                }
            ),
            stderr="gh: Not Found (HTTP 404)",
        )
        full_page = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {"tag_name": f"v0.0.{index}"}
                    for index in range(100)
                ]
            ),
            stderr="",
        )

        with mock.patch.object(
            release_auto,
            "_run",
            side_effect=[missing, *([full_page] * 1000)],
        ) as run:
            with self.assertRaises(release_auto.ReleaseError) as caught:
                release_auto.GitHubCliAdapter("owner/repo").inspect_release(
                    "v0.1.14"
                )

        self.assertIn("pagination is unbounded", str(caught.exception))
        self.assertEqual(1001, run.call_count)

    def test_github_release_inspection_lists_starter_assets(self) -> None:
        release_payload = {
            "id": 77,
            "draft": False,
            "assets": [],
        }
        first_page = [
            {
                "id": index,
                "name": f"extra-{index}.bin",
                "state": "uploaded",
                "size": index,
            }
            for index in range(1, 100)
        ]
        first_page.append(
            {
                "id": 901,
                "name": "install.sh",
                "state": "starter",
                "size": 0,
            }
        )
        second_page = [
            {
                "id": 902,
                "name": "run.sh",
                "state": "uploaded",
                "size": 123,
            }
        ]
        responses = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(release_payload),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(first_page),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(second_page),
                stderr="",
            ),
        ]

        with mock.patch.object(
            release_auto,
            "_run",
            side_effect=responses,
        ) as run:
            snapshot = release_auto.GitHubCliAdapter(
                "owner/repo"
            ).inspect_release("v0.1.14")

        self.assertTrue(snapshot.exists)
        self.assertFalse(snapshot.draft)
        self.assertEqual(100, len(snapshot.assets))
        self.assertIn("run.sh", snapshot.assets)
        self.assertNotIn("install.sh", snapshot.assets)
        self.assertEqual(1, len(snapshot.starter_assets))
        starter = snapshot.starter_assets[0]
        self.assertEqual(
            (901, "install.sh", "starter", 0),
            (starter.asset_id, starter.name, starter.state, starter.size),
        )
        self.assertEqual(
            [
                [
                    "gh",
                    "api",
                    "repos/owner/repo/releases/tags/v0.1.14",
                ],
                [
                    "gh",
                    "api",
                    "repos/owner/repo/releases/77/assets?per_page=100&page=1",
                ],
                [
                    "gh",
                    "api",
                    "repos/owner/repo/releases/77/assets?per_page=100&page=2",
                ],
            ],
            [call.args[0] for call in run.call_args_list],
        )

    def test_github_release_inspection_rejects_duplicate_asset_names(self) -> None:
        release_payload = {
            "id": 77,
            "draft": False,
            "assets": [],
        }
        asset_page = [
            {
                "id": 901,
                "name": "install.sh",
                "state": "uploaded",
                "size": 123,
            },
            {
                "id": 902,
                "name": "install.sh",
                "state": "starter",
                "size": 0,
            },
        ]
        responses = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(release_payload),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(asset_page),
                stderr="",
            ),
        ]

        with mock.patch.object(
            release_auto,
            "_run",
            side_effect=responses,
        ):
            with self.assertRaises(release_auto.ReleaseConflict) as caught:
                release_auto.GitHubCliAdapter("owner/repo").inspect_release(
                    "v0.1.14"
                )

        self.assertIn("duplicate asset names", str(caught.exception))

    def test_github_release_delete_uses_exact_asset_id(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        asset = SimpleNamespace(
            asset_id=901,
            name="install.sh",
            state="starter",
            size=0,
        )

        with mock.patch.object(
            release_auto,
            "_run",
            return_value=completed,
        ) as run:
            release_auto.GitHubCliAdapter("owner/repo").delete_asset(
                "v0.1.14",
                asset,
            )

        self.assertEqual(
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                "repos/owner/repo/releases/assets/901",
            ],
            run.call_args.args[0],
        )

    def test_github_release_create_classifies_tag_name_exists_as_retryable(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "HTTP 422: Validation Failed\n"
                "Release.tag_name already exists"
            ),
        )

        with mock.patch.object(
            release_auto.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaises(
                release_auto.ReleaseCreateRetryable
            ) as caught:
                release_auto.GitHubCliAdapter("owner/repo").create_release(
                    "v0.1.14"
                )

        self.assertEqual("tag_name_exists", caught.exception.reason)

    def test_github_release_create_classifies_structured_tag_conflict(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "message": "Validation Failed",
                    "errors": [
                        {
                            "resource": "Release",
                            "field": "tag_name",
                            "code": "already_exists",
                        }
                    ],
                    "status": "422",
                }
            ),
            stderr="",
        )

        with mock.patch.object(
            release_auto.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaises(
                release_auto.ReleaseCreateRetryable
            ) as caught:
                release_auto.GitHubCliAdapter("owner/repo").create_release(
                    "v0.1.14"
                )

        self.assertEqual("tag_name_exists", caught.exception.reason)

    def test_github_release_create_classifies_server_and_transport_failures(
        self,
    ) -> None:
        cases = (
            ("HTTP 503: Service Unavailable", "server_error"),
            ("request timed out: Server Error", "server_error"),
            ("Post request failed: unexpected EOF", "transport_error"),
            ("connection force closed by remote host", "transport_error"),
            ("connection was forcibly closed by remote host", "transport_error"),
            ("context deadline exceeded", "transport_error"),
        )

        for stderr, reason in cases:
            with self.subTest(stderr=stderr):
                completed = subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="",
                    stderr=stderr,
                )
                with mock.patch.object(
                    release_auto.subprocess,
                    "run",
                    return_value=completed,
                ):
                    with self.assertRaises(
                        release_auto.ReleaseCreateRetryable
                    ) as caught:
                        release_auto.GitHubCliAdapter(
                            "owner/repo"
                        ).create_release("v0.1.14")
                self.assertEqual(reason, caught.exception.reason)

    def test_github_release_create_keeps_terminal_failures_untyped(self) -> None:
        cases = (
            "HTTP 401: Requires authentication",
            "HTTP 403: API rate limit exceeded",
            "HTTP 403: Forbidden\nRelease.tag_name already exists",
            "HTTP 429: Too Many Requests",
            json.dumps(
                {
                    "message": "API rate limit exceeded",
                    "errors": [
                        {
                            "resource": "Release",
                            "field": "tag_name",
                            "code": "already_exists",
                        }
                    ],
                    "status": "429",
                }
            ),
            "HTTP 422: Validation Failed\nRelease.name is invalid",
            "GraphQL: Resource not accessible by integration",
        )

        for stderr in cases:
            with self.subTest(stderr=stderr):
                completed = subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="",
                    stderr=stderr,
                )
                with mock.patch.object(
                    release_auto.subprocess,
                    "run",
                    return_value=completed,
                ):
                    with self.assertRaises(
                        release_auto.ReleaseError
                    ) as caught:
                        release_auto.GitHubCliAdapter(
                            "owner/repo"
                        ).create_release("v0.1.14")
                self.assertNotIsInstance(
                    caught.exception,
                    release_auto.ReleaseCreateRetryable,
                )


class CodexReleaseWorkflowTests(unittest.TestCase):
    AUTO_RELEASE = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"
    MANUAL_RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
    AUTO_RELEASE_REMOTE_GIT_STEPS = (
        "Reconcile existing release assets",
        "Confirm remote base before release refs",
        "Create and atomically push release refs",
        "Reconcile and verify release assets",
    )

    def _step(self, workflow: str, name: str) -> tuple[int, str]:
        marker = f"      - name: {name}\n"
        start = workflow.index(marker)
        next_step = workflow.find("\n      - name:", start + len(marker))
        end = len(workflow) if next_step == -1 else next_step
        return start, workflow[start:end]

    def test_release_workflows_pin_supported_python_before_use(self) -> None:
        for path in (self.AUTO_RELEASE, self.MANUAL_RELEASE):
            with self.subTest(workflow=path.name):
                workflow = path.read_text()
                setup_index, setup = self._step(workflow, "Set up Python")
                first_python_index = workflow.index("python3 ")

                self.assertIn("uses: actions/setup-python@v7", setup)
                self.assertIn('python-version: "3.12"', setup)
                self.assertLess(setup_index, first_python_index)

    def test_release_profile_validation_retries_once_with_verbose_diagnostics(
        self,
    ) -> None:
        cases = (
            (
                self.AUTO_RELEASE,
                ("Verify reconciliation source", "Verify release source"),
            ),
            (self.MANUAL_RELEASE, ("Verify scripts and specs",)),
        )
        command = "python3 scripts/test_codex_profile_switch.py"
        warning = (
            "::warning::Profile/Wrapper verification failed on the first "
            "attempt; retrying once with verbose diagnostics."
        )

        for path, step_names in cases:
            workflow = path.read_text()
            for step_name in step_names:
                with self.subTest(workflow=path.name, step=step_name):
                    _index, step = self._step(workflow, step_name)
                    first_attempt = step.index(f"if ! {command}; then")
                    warning_index = step.index(warning)
                    retry_index = step.index(f"{command} -v")

                    self.assertEqual(2, step.count(command))
                    self.assertLess(first_attempt, warning_index)
                    self.assertLess(warning_index, retry_index)
                    self.assertNotIn("|| true", step)

    def test_release_profile_retry_failure_emits_public_error_annotation(
        self,
    ) -> None:
        cases = (
            (
                self.AUTO_RELEASE,
                ("Verify reconciliation source", "Verify release source"),
            ),
            (self.MANUAL_RELEASE, ("Verify scripts and specs",)),
        )
        command = "python3 scripts/test_codex_profile_switch.py"
        required_fragments = (
            'profile_log="$RUNNER_TEMP/codex-switch-profile-wrapper.log"',
            f'if {command} -v >"$profile_log" 2>&1; then',
            'profile_status=$?',
            'cat "$profile_log"',
            'profile_error="$(tail -n 120 "$profile_log")"',
            'profile_error="${profile_error//%/%25}"',
            """profile_error="${profile_error//$'\\r'/%0D}\"""",
            """profile_error="${profile_error//$'\\n'/%0A}\"""",
            (
                "::error title=Profile/Wrapper verification failed after "
                "retry::$profile_error"
            ),
            'exit "$profile_status"',
        )

        for path, step_names in cases:
            workflow = path.read_text()
            for step_name in step_names:
                with self.subTest(workflow=path.name, step=step_name):
                    _index, step = self._step(workflow, step_name)
                    for fragment in required_fragments:
                        self.assertIn(fragment, step)
                    self.assertLess(
                        step.index('profile_status=$?'),
                        step.index('exit "$profile_status"'),
                    )
                    self.assertNotIn("continue-on-error", step)
                    self.assertNotIn("|| true", step)

    def test_auto_release_packages_and_validates_before_ref_push(self) -> None:
        workflow = self.AUTO_RELEASE.read_text()
        package_index, _package = self._step(
            workflow,
            "Package release bundle",
        )
        validate_index, _validate = self._step(
            workflow,
            "Validate deterministic release assets",
        )
        remote_index, _remote = self._step(
            workflow,
            "Confirm remote base before release refs",
        )
        push_index, push = self._step(
            workflow,
            "Create and atomically push release refs",
        )
        reconcile_index, _reconcile = self._step(
            workflow,
            "Reconcile and verify release assets",
        )

        self.assertLess(package_index, validate_index)
        self.assertLess(validate_index, remote_index)
        self.assertLess(remote_index, push_index)
        self.assertLess(push_index, reconcile_index)
        self.assertNotIn("git tag ", workflow[:validate_index])
        self.assertNotIn("git push ", workflow[:validate_index])
        self.assertIn(
            'git -c http.https://github.com/.extraheader="$GIT_AUTH_HEADER"',
            push,
        )
        self.assertIn("push --atomic origin", push)
        self.assertIn("--force-with-lease=", push)

    def test_auto_release_explicitly_abandons_only_v0_1_14(self) -> None:
        workflow = self.AUTO_RELEASE.read_text()
        _plan_index, plan = self._step(
            workflow,
            "Plan automatic release",
        )

        self.assertEqual(1, plan.count("--abandon-tag v0.1.14"))
        self.assertNotIn("release delete", workflow)

    def test_auto_release_critical_path_cannot_ignore_failures(self) -> None:
        workflow = self.AUTO_RELEASE.read_text()
        validate_index, _validate = self._step(
            workflow,
            "Validate deterministic release assets",
        )
        _push_index, push = self._step(
            workflow,
            "Create and atomically push release refs",
        )
        _reconcile_index, reconcile = self._step(
            workflow,
            "Reconcile and verify release assets",
        )
        critical_path = workflow[validate_index:]

        self.assertNotIn("continue-on-error:", critical_path)
        self.assertNotIn("|| true", critical_path)
        self.assertNotIn("--clobber", critical_path)
        self.assertEqual(1, push.count("git tag "))
        self.assertNotIn("gh release ", push)
        self.assertIn('python3 "$RELEASE_AUTO" reconcile', reconcile)

    def test_auto_release_uses_only_transient_step_scoped_git_auth(self) -> None:
        workflow = self.AUTO_RELEASE.read_text()
        checkout_index = workflow.index("        uses: actions/checkout@v4\n")
        checkout_end = workflow.find("\n      - name:", checkout_index)
        checkout = workflow[checkout_index:checkout_end]

        self.assertIn("persist-credentials: false", checkout)
        self.assertNotIn("persist-credentials: true", workflow)
        self.assertNotIn("git config --global", workflow)
        self.assertNotIn("credential.helper", workflow)
        self.assertNotIn("git config http.", workflow)

        remote_steps = []
        for name in self.AUTO_RELEASE_REMOTE_GIT_STEPS:
            with self.subTest(step=name):
                _index, step = self._step(workflow, name)
                remote_steps.append(step)
                self.assertIn("GITHUB_TOKEN: ${{ github.token }}", step)
                self.assertIn(
                    'GIT_AUTH_HEADER="AUTHORIZATION: basic '
                    "$(printf 'x-access-token:%s' \"$GITHUB_TOKEN\" | base64)\"",
                    step,
                )

        python_git_steps = (
            "Reconcile existing release assets",
            "Confirm remote base before release refs",
            "Reconcile and verify release assets",
        )
        for name in python_git_steps:
            with self.subTest(python_git_step=name):
                _index, step = self._step(workflow, name)
                self.assertIn("export GIT_CONFIG_COUNT=1", step)
                self.assertIn(
                    "export GIT_CONFIG_KEY_0=http.https://github.com/.extraheader",
                    step,
                )
                self.assertIn(
                    'export GIT_CONFIG_VALUE_0="$GIT_AUTH_HEADER"',
                    step,
                )

        for name in (
            "Confirm remote base before release refs",
            "Create and atomically push release refs",
        ):
            with self.subTest(direct_git_step=name):
                _index, step = self._step(workflow, name)
                self.assertIn(
                    'git -c http.https://github.com/.extraheader="$GIT_AUTH_HEADER"',
                    step,
                )

        _confirm_index, confirm = self._step(
            workflow,
            "Confirm remote base before release refs",
        )
        self.assertLess(
            confirm.index(
                'git -c http.https://github.com/.extraheader="$GIT_AUTH_HEADER"'
            ),
            confirm.index("export GIT_CONFIG_COUNT=1"),
        )
        self.assertLess(
            confirm.index("export GIT_CONFIG_VALUE_0="),
            confirm.index('python3 "$RELEASE_AUTO" prepare'),
        )

        workflow_without_remote_auth = workflow
        for step in remote_steps:
            workflow_without_remote_auth = workflow_without_remote_auth.replace(
                step,
                "",
                1,
            )
        for marker in (
            "GIT_AUTH_HEADER",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GITHUB_TOKEN: ${{ github.token }}",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, workflow_without_remote_auth)

    def test_auto_release_reconciles_then_restores_source_before_prepare(
        self,
    ) -> None:
        workflow = self.AUTO_RELEASE.read_text()
        checkout_index, checkout = self._step(
            workflow,
            "Check out release reconciliation commit",
        )
        reconcile_package_index, reconcile_package = self._step(
            workflow,
            "Package reconciliation bundle",
        )
        reconcile_validate_index, reconcile_validate = self._step(
            workflow,
            "Validate reconciliation assets",
        )
        reconcile_index, reconcile = self._step(
            workflow,
            "Reconcile existing release assets",
        )
        restore_index, restore = self._step(
            workflow,
            "Restore original source commit",
        )
        bump_index, bump = self._step(
            workflow,
            "Bump release version",
        )
        pending_package_index, pending_package = self._step(
            workflow,
            "Package release bundle",
        )
        push_index, push = self._step(
            workflow,
            "Create and atomically push release refs",
        )

        self.assertLess(checkout_index, reconcile_package_index)
        self.assertLess(reconcile_package_index, reconcile_validate_index)
        self.assertLess(reconcile_validate_index, reconcile_index)
        self.assertLess(reconcile_index, restore_index)
        self.assertLess(restore_index, bump_index)
        self.assertLess(bump_index, pending_package_index)
        self.assertLess(pending_package_index, push_index)
        self.assertIn(
            "steps.plan.outputs.reconcile_required == 'true'",
            checkout,
        )
        self.assertIn("CODEX_SWITCH_DIST_DIR: ${{ runner.temp }}", reconcile_package)
        self.assertIn("--allow-legacy", reconcile_validate)
        self.assertIn("--dist-dir \"$RECONCILE_DIST\"", reconcile_validate)
        self.assertIn('python3 "$RELEASE_AUTO" reconcile', reconcile)
        self.assertIn(
            "steps.plan.outputs.prepare_required == 'true'",
            restore,
        )
        self.assertIn('git checkout --detach "$SOURCE_COMMIT"', restore)
        self.assertIn(
            "steps.plan.outputs.prepare_required == 'true'",
            bump,
        )
        self.assertIn(
            "steps.plan.outputs.prepare_required == 'true'",
            pending_package,
        )
        self.assertIn("push --atomic origin", push)

    def test_manual_release_validates_before_reconciliation(self) -> None:
        workflow = self.MANUAL_RELEASE.read_text()
        package_index, _package = self._step(
            workflow,
            "Package release bundle",
        )
        validate_index, validate = self._step(
            workflow,
            "Validate deterministic release assets",
        )
        reconcile_index, reconcile = self._step(
            workflow,
            "Reconcile and verify release assets",
        )

        self.assertLess(package_index, validate_index)
        self.assertLess(validate_index, reconcile_index)
        self.assertNotIn("git tag ", workflow)
        self.assertNotIn("git push ", workflow)
        self.assertNotIn("continue-on-error:", workflow[validate_index:])
        self.assertNotIn("|| true", workflow[validate_index:])
        self.assertNotIn("--clobber", workflow)
        self.assertIn("--allow-legacy", validate)
        self.assertIn('python3 "$RELEASE_AUTO" reconcile', reconcile)

    def test_release_workflows_share_one_serial_concurrency_group(self) -> None:
        for path in (self.AUTO_RELEASE, self.MANUAL_RELEASE):
            with self.subTest(path=path.name):
                workflow = path.read_text()
                self.assertIn("group: codex-switch-release", workflow)
                self.assertIn("cancel-in-progress: false", workflow)

    def test_manual_release_resolves_remote_tag_before_target_code(self) -> None:
        workflow = self.MANUAL_RELEASE.read_text()
        required_steps = (
            "Check out trusted release tooling",
            "Stage trusted release tooling",
            "Resolve exact remote release tag",
            "Check out resolved release commit",
        )
        for name in required_steps:
            self.assertIn(f"      - name: {name}\n", workflow)

        trusted_index, trusted = self._step(
            workflow,
            "Check out trusted release tooling",
        )
        stage_index, _stage = self._step(
            workflow,
            "Stage trusted release tooling",
        )
        resolve_index, resolve = self._step(
            workflow,
            "Resolve exact remote release tag",
        )
        target_index, target = self._step(
            workflow,
            "Check out resolved release commit",
        )
        verify_index, _verify = self._step(
            workflow,
            "Verify scripts and specs",
        )

        self.assertLess(trusted_index, stage_index)
        self.assertLess(stage_index, resolve_index)
        self.assertLess(resolve_index, target_index)
        self.assertLess(target_index, verify_index)
        self.assertIn("ref: main", trusted)
        self.assertIn("persist-credentials: false", trusted)
        self.assertIn('python3 "$RELEASE_AUTO" resolve-tag', resolve)
        self.assertIn("ref: ${{ steps.resolve.outputs.commit }}", target)
        self.assertIn("persist-credentials: false", target)
        self.assertEqual(2, workflow.count("persist-credentials: false"))


class CodexObsoletePathCleanupTests(unittest.TestCase):
    def test_retired_fail_open_paths_are_absent_from_production(self) -> None:
        command = (REPO_ROOT / "scripts" / "codex-switch").read_text()
        plugins = (REPO_ROOT / "scripts" / "codex_switch_plugins.py").read_text()
        verify = (REPO_ROOT / "scripts" / "codex_switch_verify.py").read_text()
        workflows = "\n".join(
            path.read_text()
            for path in (
                REPO_ROOT / ".github" / "workflows" / "auto-release.yml",
                REPO_ROOT / ".github" / "workflows" / "release.yml",
            )
        )

        for marker in (
            "current.self-update",
            "current.previous",
            'rm -rf "$target"',
            'rm -rf "$lib_dir/current"',
            'elif [[ "$current_version" != "$latest_version" ]]',
        ):
            with self.subTest(area="promotion/update", marker=marker):
                self.assertNotIn(marker, command)

        for marker in (
            "def available_plugin_selectors",
            "stderr=subprocess.STDOUT",
            "return set()",
        ):
            with self.subTest(area="plugin catalog", marker=marker):
                self.assertNotIn(marker, plugins)

        for marker in (
            "def response_seen",
            "def read_stream_lines",
            "stderr=subprocess.STDOUT",
        ):
            with self.subTest(area="verification", marker=marker):
                self.assertNotIn(marker, verify)

        for marker in (
            '--clobber',
            'git push origin "HEAD:main" "refs/tags/$NEXT_TAG"',
        ):
            with self.subTest(area="release workflow", marker=marker):
                self.assertNotIn(marker, workflows)


class CodexImmutablePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.sources = self.root / "sources"
        self.outputs = self.root / "outputs"
        self.layout_root = self.root / "install"
        self.bundle_module = load_bundle_module()
        self.module = load_promotion_module()
        self.health_script = self.root / "health.py"
        self.health_script.write_text(
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            "import time\n"
            "\n"
            "mode = sys.argv[1]\n"
            "count_path = pathlib.Path(sys.argv[2])\n"
            "with count_path.open('a') as handle:\n"
            "    handle.write('health\\n')\n"
            "payload = {\n"
            "    'schema': 'codex-switch.promotion-handshake',\n"
            "    'schema_version': 1,\n"
            "    'run_id': os.environ['CODEX_SWITCH_PROMOTION_RUN_ID'],\n"
            "    'version': os.environ['CODEX_SWITCH_PROMOTION_VERSION'],\n"
            "    'digest': os.environ['CODEX_SWITCH_PROMOTION_DIGEST'],\n"
            "    'root': os.environ['CODEX_SWITCH_PROMOTION_ROOT'],\n"
            "}\n"
            "if mode == 'timeout':\n"
            "    time.sleep(2)\n"
            "elif mode == 'nonzero':\n"
            "    sys.exit(23)\n"
            "elif mode == 'malformed':\n"
            "    print('{not-json')\n"
            "    sys.exit(0)\n"
            "elif mode == 'error_json':\n"
            "    print(json.dumps({'error': 'not healthy'}))\n"
            "    sys.exit(0)\n"
            "elif mode == 'boolean_schema':\n"
            "    payload['schema_version'] = True\n"
            "elif mode == 'mismatch':\n"
            "    payload['digest'] = '0' * 64\n"
            "print(json.dumps(payload, sort_keys=True))\n"
        )
        self.original_script = self.root / "original.py"
        self.original_script.write_text(
            "import pathlib\n"
            "import sys\n"
            "count_path = pathlib.Path(sys.argv[1])\n"
            "with count_path.open('a') as handle:\n"
            "    handle.write('original\\n')\n"
            "sys.exit(int(sys.argv[2]))\n"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_source_repo(
        self,
        repo: Path,
        version: str,
        *,
        runner_text: str = "#!/usr/bin/env bash\nexit 0\n",
        command_text: str = "#!/usr/bin/env bash\nprintf 'smoke\\n'\n",
        helper_text: str = "VALUE = 1\n",
    ) -> None:
        (repo / "agents").mkdir(parents=True)
        (repo / "docs").mkdir()
        (repo / "evals").mkdir()
        (repo / "scripts").mkdir()
        (repo / "README.md").write_text(f"release {version}\n")
        (repo / "SKILL.md").write_text("skill\n")
        (repo / "VERSION").write_text(f"{version}\n")
        (repo / "run.sh").write_text(runner_text)
        (repo / "agents" / "openai.yaml").write_text("name: codex-switch\n")
        (repo / "docs" / "release.md").write_text(f"docs {version}\n")
        (repo / "evals" / "evals.json").write_text('{"evals": []}\n')
        (repo / "scripts" / "codex-switch").write_text(command_text)
        (repo / "scripts" / "package-release.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n"
        )
        write_required_python_modules(repo / "scripts")
        (repo / "scripts" / "helper.py").write_text(helper_text)
        for path in (
            repo / "run.sh",
            repo / "scripts" / "codex-switch",
            repo / "scripts" / "package-release.sh",
        ):
            path.chmod(0o755)

    def _build_candidate(
        self,
        version: str,
        label: str,
        *,
        runner_text: str = "#!/usr/bin/env bash\nexit 0\n",
        command_text: str = "#!/usr/bin/env bash\nprintf 'smoke\\n'\n",
        helper_text: str = "VALUE = 1\n",
    ) -> Path:
        source = self.sources / label
        output = self.outputs / label
        self._write_source_repo(
            source,
            version,
            runner_text=runner_text,
            command_text=command_text,
            helper_text=helper_text,
        )
        self.bundle_module.build_release_bundle(source, output)
        return output / "codex-switch"

    def _candidate(self, root: Path, version: str):
        return self.module.validate_candidate(
            root,
            expected_version=version,
            smoke_timeout=5.0,
            import_timeout=1.0,
        )

    def _health_command(self, mode: str, count_path: Path) -> list[str]:
        return [sys.executable, str(self.health_script), mode, str(count_path)]

    def _original_command(self, count_path: Path, returncode: int) -> list[str]:
        return [
            sys.executable,
            str(self.original_script),
            str(count_path),
            str(returncode),
        ]

    def _promote(
        self,
        candidate_root: Path,
        version: str,
        layout_root: Path,
        *,
        health_mode: str = "success",
        health_count: Path | None = None,
        health_timeout: float = 1.0,
        original_command: list[str] | None = None,
        fault_injector=None,
        run_id: str | None = None,
    ):
        if health_count is None:
            health_count = self.root / f"health-{version}.count"
        candidate = self._candidate(candidate_root, version)
        return self.module.promote_candidate(
            candidate,
            self.module.PromotionLayout(layout_root),
            self._health_command(health_mode, health_count),
            health_timeout=health_timeout,
            original_command=original_command,
            original_timeout=1.0,
            fault_injector=fault_injector,
            run_id=run_id,
        )

    def _assert_promotion_error(self, reason: str, operation) -> object:
        with self.assertRaises(self.module.PromotionError) as caught:
            operation()
        self.assertEqual(reason, caught.exception.reason)
        return caught.exception

    def _line_count(self, path: Path) -> int:
        if not path.exists():
            return 0
        return len(path.read_text().splitlines())

    def test_first_promotion_creates_digest_release_and_atomic_current(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "first")
        health_count = self.root / "first-health.count"
        receipt = self._promote(
            candidate_root,
            "1.0.0",
            self.layout_root,
            health_count=health_count,
            run_id="run-first",
        )
        release = self.layout_root / "releases" / receipt.digest
        state = json.loads(
            (self.layout_root / "promotion-state.json").read_text()
        )

        self.assertEqual(release.resolve(), receipt.active_root)
        self.assertIsNone(receipt.rollback_root)
        self.assertEqual("promoted", receipt.outcome)
        self.assertEqual("run-first", receipt.run_id)
        self.assertEqual("1.0.0", receipt.version)
        self.assertTrue(release.is_dir())
        self.assertTrue((self.layout_root / "current").is_symlink())
        self.assertEqual(
            f"releases/{receipt.digest}",
            os.readlink(self.layout_root / "current"),
        )
        self.assertFalse(os.path.lexists(str(self.layout_root / "rollback")))
        self.assertEqual(
            {"current", "promotion-state.json", "promotion.lock", "releases"},
            {path.name for path in self.layout_root.iterdir()},
        )
        self.assertEqual("promoted", state["outcome"])
        self.assertEqual(str(release.resolve()), state["active_root"])
        self.assertIsNone(state["rollback_root"])
        self.assertEqual(1, self._line_count(health_count))

    def test_second_promotion_keeps_both_releases_and_sets_rollback(self) -> None:
        first_root = self._build_candidate("1.0.0", "prior")
        second_root = self._build_candidate("2.0.0", "next")
        first = self._promote(first_root, "1.0.0", self.layout_root)
        second = self._promote(second_root, "2.0.0", self.layout_root)
        first_release = self.layout_root / "releases" / first.digest
        second_release = self.layout_root / "releases" / second.digest

        self.assertTrue(first_release.is_dir())
        self.assertTrue(second_release.is_dir())
        self.assertEqual(
            f"releases/{second.digest}",
            os.readlink(self.layout_root / "current"),
        )
        self.assertEqual(
            f"releases/{first.digest}",
            os.readlink(self.layout_root / "rollback"),
        )
        self.assertEqual(first_release.resolve(), second.rollback_root)

    def test_existing_digest_is_reused_without_mutation(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "reused")
        candidate = self._candidate(candidate_root, "1.0.0")
        release = self.layout_root / "releases" / candidate.digest
        release.parent.mkdir(parents=True)
        shutil.copytree(candidate_root, release)
        before = filesystem_snapshot(release)

        receipt = self.module.promote_candidate(
            candidate,
            self.module.PromotionLayout(self.layout_root),
            self._health_command("success", self.root / "reuse-health.count"),
            health_timeout=1.0,
            run_id="run-reuse",
        )

        self.assertEqual(before, filesystem_snapshot(release))
        self.assertTrue(receipt.reused_release)
        self.assertEqual(release.resolve(), receipt.active_root)

    def test_mismatched_existing_digest_destination_is_rejected(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "expected")
        foreign_root = self._build_candidate("9.0.0", "foreign")
        candidate = self._candidate(candidate_root, "1.0.0")
        destination = self.layout_root / "releases" / candidate.digest
        destination.parent.mkdir(parents=True)
        (self.layout_root / "promotion.lock").mkdir()
        shutil.copytree(foreign_root, destination)
        before = filesystem_snapshot(self.layout_root)

        self._assert_promotion_error(
            "release_digest_mismatch",
            lambda: self.module.promote_candidate(
                candidate,
                self.module.PromotionLayout(self.layout_root),
                self._health_command(
                    "success",
                    self.root / "foreign-health.count",
                ),
            ),
        )

        self.assertEqual(before, filesystem_snapshot(self.layout_root))

    def test_replaced_release_stage_is_preserved_and_rejected(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "replaced-stage")
        candidate = self._candidate(candidate_root, "1.0.0")
        replacement_payload = b"foreign staged replacement\n"
        replacement_path: list[Path] = []

        def replace_stage(phase: str) -> None:
            if phase != "release_before_publish":
                return
            stage = next(
                path
                for path in (self.layout_root / "releases").iterdir()
                if path.name.startswith(".promotion-release-")
            )
            shutil.rmtree(stage)
            stage.mkdir()
            (stage / "foreign.bin").write_bytes(replacement_payload)
            replacement_path.append(stage)

        self._assert_promotion_error(
            "release_stage_changed",
            lambda: self.module.promote_candidate(
                candidate,
                self.module.PromotionLayout(self.layout_root),
                self._health_command(
                    "success",
                    self.root / "replaced-stage-health.count",
                ),
                fault_injector=replace_stage,
            ),
        )

        self.assertEqual(1, len(replacement_path))
        self.assertEqual(
            replacement_payload,
            (replacement_path[0] / "foreign.bin").read_bytes(),
        )
        self.assertFalse(
            os.path.lexists(
                str(self.layout_root / "releases" / candidate.digest)
            )
        )
        self.assertFalse(os.path.lexists(str(self.layout_root / "current")))

    def test_candidate_validation_rejects_version_manifest_shell_and_python(
        self,
    ) -> None:
        version_root = self._build_candidate("1.0.0", "version")
        self._assert_promotion_error(
            "version_mismatch",
            lambda: self.module.validate_candidate(
                version_root,
                expected_version="2.0.0",
            ),
        )

        tampered_root = self._build_candidate("1.0.0", "tampered")
        (tampered_root / "scripts" / "helper.py").write_text("VALUE = 2\n")
        self._assert_promotion_error(
            "candidate_invalid",
            lambda: self.module.validate_candidate(tampered_root),
        )

        boolean_schema_root = self._build_candidate(
            "1.0.0",
            "boolean-schema",
        )
        manifest_path = boolean_schema_root / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] = True
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self._assert_promotion_error(
            "candidate_invalid",
            lambda: self.module.validate_candidate(boolean_schema_root),
        )

        reordered_historical_root = self._build_candidate(
            "1.0.0",
            "reordered-historical-required-paths",
        )
        manifest_path = reordered_historical_root / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        historical_paths = [
            path
            for path in manifest["required_paths"]
            if path
            not in {
                "scripts/codex_switch_selection.py",
                "scripts/codex_switch_shared_configuration.py",
            }
        ]
        historical_paths[-2:] = reversed(historical_paths[-2:])
        manifest["required_paths"] = historical_paths
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        self._assert_promotion_error(
            "candidate_invalid",
            lambda: self.module.validate_candidate(
                reordered_historical_root,
                allow_historical_required_paths=True,
            ),
        )

        shell_root = self._build_candidate(
            "1.0.0",
            "shell",
            runner_text="#!/usr/bin/env bash\nif then\n",
        )
        self._assert_promotion_error(
            "shell_syntax_invalid",
            lambda: self.module.validate_candidate(shell_root),
        )

        python_root = self._build_candidate("1.0.0", "python-syntax")
        (python_root / "scripts" / "helper.py").write_text(
            "def broken(:\n    pass\n"
        )
        self.bundle_module._create_manifest(python_root, "1.0.0")
        self._assert_promotion_error(
            "python_syntax_invalid",
            lambda: self.module.validate_candidate(python_root),
        )

        import_root = self._build_candidate("1.0.0", "python-import")
        (import_root / "scripts" / "helper.py").write_text(
            "import codex_switch_missing_dependency_for_test\n"
        )
        self.bundle_module._create_manifest(import_root, "1.0.0")
        self._assert_promotion_error(
            "python_import_invalid",
            lambda: self.module.validate_candidate(import_root),
        )

    def test_candidate_smoke_mutation_is_rejected(self) -> None:
        candidate_root = self._build_candidate(
            "1.0.0",
            "smoke-mutation",
            command_text=(
                "#!/usr/bin/env bash\n"
                "printf 'mutation\\n' > smoke-mutation.txt\n"
            ),
        )

        self._assert_promotion_error(
            "candidate_smoke_mutated",
            lambda: self.module.validate_candidate(
                candidate_root,
                smoke_timeout=1.0,
            ),
        )

    def test_candidate_helper_uses_production_smoke_budget(self) -> None:
        candidate_root = self._build_candidate(
            "1.0.0",
            "production-smoke-budget",
            command_text=(
                "#!/usr/bin/env bash\n"
                "sleep 1.2\n"
                "printf 'smoke\\n'\n"
            ),
        )

        candidate = self._candidate(candidate_root, "1.0.0")

        self.assertEqual("1.0.0", candidate.version)

    def test_candidate_smoke_explicit_short_timeout_is_rejected(self) -> None:
        candidate_root = self._build_candidate(
            "1.0.0",
            "explicit-smoke-timeout",
            command_text=(
                "#!/usr/bin/env bash\n"
                "sleep 0.2\n"
                "printf 'smoke\\n'\n"
            ),
        )

        self._assert_promotion_error(
            "candidate_smoke_timeout",
            lambda: self.module.validate_candidate(
                candidate_root,
                smoke_timeout=0.05,
            ),
        )

    def test_packaged_status_preserves_immutable_release_without_bytecode_env(
        self,
    ) -> None:
        output = self.root / "real-packaged-status"
        receipt = self.bundle_module.build_release_bundle(REPO_ROOT, output)
        package = receipt.package_dir
        python_runtime = shutil.which("python3.12") or sys.executable
        env = os.environ.copy()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        env.update(
            {
                "HOME": str(self.root / "packaged-status-home"),
                "CODEX_HOME": str(self.root / "packaged-status-codex-home"),
                "CODEX_SWITCH_HOME": str(
                    self.root / "packaged-status-switch-home"
                ),
                "CODEX_SWITCH_LIB_DIR": str(
                    self.root / "packaged-status-library"
                ),
                "CODEX_SWITCH_INSTALL_DIR": str(
                    self.root / "packaged-status-bin"
                ),
                "CODEX_SWITCH_PYTHON": python_runtime,
            }
        )
        Path(env["HOME"]).mkdir()

        result = subprocess.run(
            [
                str(package / "scripts" / "codex-switch"),
                "--skip-self-update",
                "status",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.module.validate_candidate(
            package,
            expected_version=(REPO_ROOT / "VERSION").read_text().strip(),
        )

    def test_nonblocking_lock_contention_is_byte_preserving(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "contended")
        candidate = self._candidate(candidate_root, "1.0.0")
        (self.layout_root / "releases").mkdir(parents=True)
        lock_path = self.layout_root / "promotion.lock"
        lock_path.mkdir()
        candidate_before = filesystem_snapshot(candidate_root)
        layout_before = filesystem_snapshot(self.layout_root)
        original_count = self.root / "contended-original.count"
        descriptor = os.open(str(lock_path), os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._assert_promotion_error(
                "lock_busy",
                lambda: self.module.promote_candidate(
                    candidate,
                    self.module.PromotionLayout(self.layout_root),
                    self._health_command(
                        "success",
                        self.root / "contended-health.count",
                    ),
                    original_command=self._original_command(original_count, 0),
                ),
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual(candidate_before, filesystem_snapshot(candidate_root))
        self.assertEqual(layout_before, filesystem_snapshot(self.layout_root))
        self.assertEqual(0, self._line_count(original_count))

    def test_foreign_state_is_rejected_without_overwrite(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "foreign-state")
        candidate = self._candidate(candidate_root, "1.0.0")
        self.layout_root.mkdir()
        state_path = self.layout_root / "promotion-state.json"
        foreign_state = json.dumps(
            {
                "schema": "codex-switch.promotion-state",
                "schema_version": 1,
                "phase": "promoted",
                "outcome": "promoted",
                "run_id": "foreign-state",
            },
            sort_keys=True,
        ).encode() + b"\n"
        state_path.write_bytes(foreign_state)
        health_count = self.root / "foreign-state-health.count"

        self._assert_promotion_error(
            "state_invalid",
            lambda: self.module.promote_candidate(
                candidate,
                self.module.PromotionLayout(self.layout_root),
                self._health_command("success", health_count),
            ),
        )

        self.assertEqual(foreign_state, state_path.read_bytes())
        self.assertFalse(os.path.lexists(str(self.layout_root / "current")))
        self.assertEqual(0, self._line_count(health_count))

    def test_state_replacement_before_publish_is_preserved(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "state-race")
        candidate = self._candidate(candidate_root, "1.0.0")
        state_path = self.layout_root / "promotion-state.json"
        foreign_state = b"foreign state replacement\n"

        def replace_state(phase: str) -> None:
            if phase == "state_before_replace:candidate_prepared":
                state_path.write_bytes(foreign_state)

        self._assert_promotion_error(
            "state_changed",
            lambda: self.module.promote_candidate(
                candidate,
                self.module.PromotionLayout(self.layout_root),
                self._health_command(
                    "success",
                    self.root / "state-race-health.count",
                ),
                fault_injector=replace_state,
            ),
        )

        self.assertEqual(foreign_state, state_path.read_bytes())
        self.assertFalse(os.path.lexists(str(self.layout_root / "current")))

    def test_current_ref_replacement_before_publish_is_preserved(self) -> None:
        candidate_root = self._build_candidate("1.0.0", "ref-race")
        candidate = self._candidate(candidate_root, "1.0.0")
        current = self.layout_root / "current"
        foreign_ref = b"foreign current replacement\n"

        def replace_current(phase: str) -> None:
            if phase == "ref_before_replace:current:candidate_active":
                current.write_bytes(foreign_ref)

        self._assert_promotion_error(
            "ref_changed",
            lambda: self.module.promote_candidate(
                candidate,
                self.module.PromotionLayout(self.layout_root),
                self._health_command(
                    "success",
                    self.root / "ref-race-health.count",
                ),
                fault_injector=replace_current,
            ),
        )

        self.assertEqual(foreign_ref, current.read_bytes())

    def test_active_state_replacement_rolls_back_and_skips_original(self) -> None:
        prior_root = self._build_candidate("1.0.0", "state-prior")
        candidate_root = self._build_candidate("2.0.0", "state-candidate")
        prior = self._promote(prior_root, "1.0.0", self.layout_root)
        candidate = self._candidate(candidate_root, "2.0.0")
        state_path = self.layout_root / "promotion-state.json"
        foreign_state = b"foreign state after activation\n"
        original_count = self.root / "state-rollback-original.count"

        def replace_active_state(phase: str) -> None:
            if phase == "candidate_ref_installed":
                state_path.unlink()
                state_path.write_bytes(foreign_state)

        self._assert_promotion_error(
            "state_changed",
            lambda: self.module.promote_candidate(
                candidate,
                self.module.PromotionLayout(self.layout_root),
                self._health_command(
                    "success",
                    self.root / "state-rollback-health.count",
                ),
                original_command=self._original_command(original_count, 0),
                fault_injector=replace_active_state,
            ),
        )

        self.assertEqual(
            f"releases/{prior.digest}",
            os.readlink(self.layout_root / "current"),
        )
        self.assertEqual(foreign_state, state_path.read_bytes())
        self.assertEqual(0, self._line_count(original_count))

    def test_legacy_migration_failure_restores_runnable_directory(self) -> None:
        legacy_root = self._build_candidate("1.0.0", "legacy-failure")
        candidate_root = self._build_candidate("2.0.0", "after-legacy-failure")
        self.layout_root.mkdir()
        shutil.copytree(legacy_root, self.layout_root / "current")

        def fail_after_move(phase: str) -> None:
            if phase == "legacy_current_moved":
                raise RuntimeError("injected legacy migration failure")

        self._assert_promotion_error(
            "legacy_migration_failed",
            lambda: self._promote(
                candidate_root,
                "2.0.0",
                self.layout_root,
                fault_injector=fail_after_move,
            ),
        )

        current = self.layout_root / "current"
        self.assertTrue(current.is_dir())
        self.assertFalse(current.is_symlink())
        restored = self.module.validate_candidate(
            current,
            expected_version="1.0.0",
        )
        self.assertEqual("1.0.0", restored.version)

    def test_unmanifested_legacy_migration_failure_restores_exact_directory(
        self,
    ) -> None:
        current = self.layout_root / "current"
        self._write_source_repo(current, "1.0.0")
        candidate_root = self._build_candidate(
            "2.0.0",
            "after-unmanifested-legacy-failure",
        )
        before = filesystem_snapshot(current)

        def fail_after_move(phase: str) -> None:
            if phase == "legacy_current_moved":
                raise RuntimeError("injected unmanifested legacy failure")

        self._assert_promotion_error(
            "legacy_migration_failed",
            lambda: self._promote(
                candidate_root,
                "2.0.0",
                self.layout_root,
                fault_injector=fail_after_move,
            ),
        )

        self.assertTrue(current.is_dir())
        self.assertFalse(current.is_symlink())
        self.assertEqual(before, filesystem_snapshot(current))

    def test_historical_legacy_canonicalization_rejects_scripts_symlink_without_external_writes(
        self,
    ) -> None:
        current = self.layout_root / "current"
        self._write_source_repo(current, "1.0.0")
        external_scripts = self.root / "external-legacy-scripts"
        shutil.move(current / "scripts", external_scripts)
        (current / "scripts").symlink_to(
            external_scripts,
            target_is_directory=True,
        )
        for module_name in (
            "codex_switch_release_bundle.py",
            "codex_switch_promotion.py",
            "codex_switch_update_policy.py",
            "codex_switch_official_release.py",
        ):
            (external_scripts / module_name).unlink()
        before = filesystem_snapshot(external_scripts)
        candidate_root = self._build_candidate(
            "2.0.0",
            "after-legacy-scripts-symlink",
        )

        self._assert_promotion_error(
            "legacy_migration_failed",
            lambda: self._promote(
                candidate_root,
                "2.0.0",
                self.layout_root,
            ),
        )

        self.assertEqual(before, filesystem_snapshot(external_scripts))
        self.assertTrue((current / "scripts").is_symlink())

    def test_legacy_pre_move_failure_preserves_runnable_directory(self) -> None:
        legacy_root = self._build_candidate("1.0.0", "legacy-pre-move")
        candidate_root = self._build_candidate("2.0.0", "after-pre-move")
        self.layout_root.mkdir()
        shutil.copytree(legacy_root, self.layout_root / "current")

        def fail_before_move(phase: str) -> None:
            if phase == "legacy_before_move":
                raise RuntimeError("injected failure before legacy move")

        self._assert_promotion_error(
            "legacy_migration_failed",
            lambda: self._promote(
                candidate_root,
                "2.0.0",
                self.layout_root,
                fault_injector=fail_before_move,
            ),
        )

        current = self.layout_root / "current"
        self.assertTrue(current.is_dir())
        self.assertFalse(current.is_symlink())
        restored = self.module.validate_candidate(
            current,
            expected_version="1.0.0",
        )
        self.assertEqual("1.0.0", restored.version)

    def test_interrupted_legacy_migration_recovers_before_retry(self) -> None:
        legacy_root = self._build_candidate("1.0.0", "legacy-interrupted")
        candidate_root = self._build_candidate("2.0.0", "after-interruption")
        self.layout_root.mkdir()
        shutil.copytree(legacy_root, self.layout_root / "current")

        class HardStop(BaseException):
            pass

        def interrupt_after_move(phase: str) -> None:
            if phase == "legacy_current_moved":
                raise HardStop("simulated process interruption")

        with self.assertRaises(HardStop):
            self._promote(
                candidate_root,
                "2.0.0",
                self.layout_root,
                fault_injector=interrupt_after_move,
            )

        interrupted_state = json.loads(
            (self.layout_root / "promotion-state.json").read_text()
        )
        backup = self.layout_root / interrupted_state["legacy_backup"]
        self.assertEqual("legacy_current_moved", interrupted_state["phase"])
        self.assertFalse(os.path.lexists(str(self.layout_root / "current")))
        self.assertTrue(backup.is_dir())

        receipt = self._promote(
            candidate_root,
            "2.0.0",
            self.layout_root,
            run_id="run-after-recovery",
        )

        self.assertEqual("promoted", receipt.outcome)
        self.assertIsNotNone(receipt.rollback_root)
        self.assertEqual("1.0.0", (receipt.rollback_root / "VERSION").read_text().strip())
        self.assertFalse(os.path.lexists(str(backup)))
        self.assertTrue((self.layout_root / "current").is_symlink())
        self.assertTrue((self.layout_root / "rollback").is_symlink())

    def test_interrupted_legacy_ref_install_recovers_before_retry(self) -> None:
        legacy_root = self._build_candidate("1.0.0", "legacy-ref-interrupted")
        candidate_root = self._build_candidate("2.0.0", "after-ref-interruption")
        self.layout_root.mkdir()
        shutil.copytree(legacy_root, self.layout_root / "current")

        class HardStop(BaseException):
            pass

        def interrupt_after_ref(phase: str) -> None:
            if phase == "legacy_ref_installed":
                raise HardStop("simulated interruption after legacy ref install")

        with self.assertRaises(HardStop):
            self._promote(
                candidate_root,
                "2.0.0",
                self.layout_root,
                fault_injector=interrupt_after_ref,
            )

        interrupted_state = json.loads(
            (self.layout_root / "promotion-state.json").read_text()
        )
        backup = self.layout_root / interrupted_state["legacy_backup"]
        self.assertEqual("legacy_current_moved", interrupted_state["phase"])
        self.assertTrue((self.layout_root / "current").is_symlink())
        self.assertTrue(backup.is_dir())

        receipt = self._promote(
            candidate_root,
            "2.0.0",
            self.layout_root,
            run_id="run-after-ref-recovery",
        )

        self.assertEqual("promoted", receipt.outcome)
        self.assertIsNotNone(receipt.rollback_root)
        self.assertEqual(
            "1.0.0",
            (receipt.rollback_root / "VERSION").read_text().strip(),
        )
        self.assertFalse(os.path.lexists(str(backup)))

    def test_interrupted_candidate_activation_restores_prior_before_retry(
        self,
    ) -> None:
        cases = {
            "candidate_ref_installed": "candidate_prepared",
            "candidate_active_recorded": "candidate_active",
        }
        for fault_phase, expected_state_phase in cases.items():
            with self.subTest(fault_phase=fault_phase):
                layout_root = self.root / f"install-{fault_phase}"
                prior_root = self._build_candidate(
                    "1.0.0",
                    f"prior-{fault_phase}",
                )
                candidate_root = self._build_candidate(
                    "2.0.0",
                    f"candidate-{fault_phase}",
                )
                prior = self._promote(prior_root, "1.0.0", layout_root)
                candidate = self._candidate(candidate_root, "2.0.0")

                class HardStop(BaseException):
                    pass

                def interrupt(phase: str) -> None:
                    if phase == fault_phase:
                        raise HardStop(f"simulated interruption at {phase}")

                with self.assertRaises(HardStop):
                    self.module.promote_candidate(
                        candidate,
                        self.module.PromotionLayout(layout_root),
                        self._health_command(
                            "success",
                            self.root / f"{fault_phase}-interrupted-health.count",
                        ),
                        fault_injector=interrupt,
                    )

                interrupted_state = json.loads(
                    (layout_root / "promotion-state.json").read_text()
                )
                self.assertEqual(
                    expected_state_phase,
                    interrupted_state["phase"],
                )
                self.assertEqual(
                    f"releases/{candidate.digest}",
                    os.readlink(layout_root / "current"),
                )

                receipt = self._promote(
                    candidate_root,
                    "2.0.0",
                    layout_root,
                    run_id=f"retry-{fault_phase}",
                )

                self.assertEqual("promoted", receipt.outcome)
                self.assertEqual(
                    f"releases/{candidate.digest}",
                    os.readlink(layout_root / "current"),
                )
                self.assertEqual(
                    f"releases/{prior.digest}",
                    os.readlink(layout_root / "rollback"),
                )
                self.assertEqual(
                    "1.0.0",
                    (receipt.rollback_root / "VERSION").read_text().strip(),
                )

    def test_health_failures_restore_prior_and_skip_original_command(self) -> None:
        cases = {
            "nonzero": "health_nonzero",
            "timeout": "health_timeout",
            "malformed": "handshake_invalid",
            "error_json": "handshake_invalid",
            "boolean_schema": "handshake_invalid",
            "mismatch": "handshake_mismatch",
        }
        for mode, reason in cases.items():
            with self.subTest(mode=mode):
                layout_root = self.root / f"install-{mode}"
                prior_root = self._build_candidate("1.0.0", f"prior-{mode}")
                failed_root = self._build_candidate("2.0.0", f"failed-{mode}")
                prior = self._promote(prior_root, "1.0.0", layout_root)
                failed_candidate = self._candidate(failed_root, "2.0.0")
                health_count = self.root / f"{mode}-health.count"
                original_count = self.root / f"{mode}-original.count"

                self._assert_promotion_error(
                    reason,
                    lambda: self.module.promote_candidate(
                        failed_candidate,
                        self.module.PromotionLayout(layout_root),
                        self._health_command(mode, health_count),
                        health_timeout=0.1 if mode == "timeout" else 1.0,
                        original_command=self._original_command(
                            original_count,
                            0,
                        ),
                        original_timeout=1.0,
                        run_id=f"run-{mode}",
                    ),
                )

                self.assertEqual(
                    f"releases/{prior.digest}",
                    os.readlink(layout_root / "current"),
                )
                self.assertTrue(
                    (layout_root / "releases" / failed_candidate.digest).is_dir()
                )
                self.assertEqual(1, self._line_count(health_count))
                self.assertEqual(0, self._line_count(original_count))
                state = json.loads(
                    (layout_root / "promotion-state.json").read_text()
                )
                self.assertEqual("rolled_back", state["outcome"])
                self.assertEqual(reason, state["failure_reason"])
                self.assertEqual(
                    str((layout_root / "releases" / prior.digest).resolve()),
                    state["active_root"],
                )

    def test_successful_handshake_and_original_command_run_exactly_once(
        self,
    ) -> None:
        candidate_root = self._build_candidate("3.0.0", "successful-command")
        health_count = self.root / "successful-health.count"
        original_count = self.root / "successful-original.count"
        run_id = "run-structured-success"
        receipt = self._promote(
            candidate_root,
            "3.0.0",
            self.layout_root,
            health_count=health_count,
            original_command=self._original_command(original_count, 7),
            run_id=run_id,
        )
        state = json.loads(
            (self.layout_root / "promotion-state.json").read_text()
        )

        self.assertEqual("promoted", receipt.outcome)
        self.assertEqual(run_id, receipt.run_id)
        self.assertEqual(1, receipt.health_command_count)
        self.assertEqual(1, receipt.original_command_count)
        self.assertEqual(7, receipt.original_command_returncode)
        self.assertEqual(1, self._line_count(health_count))
        self.assertEqual(1, self._line_count(original_count))
        self.assertEqual(run_id, state["run_id"])
        self.assertEqual(receipt.version, state["version"])
        self.assertEqual(receipt.digest, state["digest"])
        self.assertEqual(str(receipt.active_root), state["active_root"])

    def test_adapter_executes_command_from_promoted_root_after_current_changes(
        self,
    ) -> None:
        first_count = self.root / "adapter-first.count"
        second_count = self.root / "adapter-second.count"
        first_root = self._build_candidate(
            "4.0.0",
            "adapter-first",
            command_text=(
                "#!/usr/bin/env bash\n"
                'if [[ "${1:-}" == "--version" ]]; then\n'
                "  printf '4.0.0\\n'\n"
                "  exit 0\n"
                "fi\n"
                f"printf 'first\\n' >> {str(first_count)!r}\n"
                "exit 7\n"
            ),
        )
        second_root = self._build_candidate(
            "5.0.0",
            "adapter-second",
            command_text=(
                "#!/usr/bin/env bash\n"
                'if [[ "${1:-}" == "--version" ]]; then\n'
                "  printf '5.0.0\\n'\n"
                "  exit 0\n"
                "fi\n"
                f"printf 'second\\n' >> {str(second_count)!r}\n"
                "exit 9\n"
            ),
        )
        second_candidate = self.module.validate_candidate(
            second_root,
            expected_version="5.0.0",
            smoke_timeout=5.0,
            import_timeout=5.0,
        )
        original_promote = self.module.promote_candidate
        first_receipts = []

        def promote_then_change_current(candidate, layout, health_command, **kwargs):
            receipt = original_promote(
                candidate,
                layout,
                health_command,
                **kwargs,
            )
            first_receipts.append(receipt)
            original_promote(
                second_candidate,
                layout,
                health_command,
                health_timeout=1.0,
                run_id="concurrent-promotion",
            )
            return receipt

        self.module.promote_candidate = promote_then_change_current
        try:
            status = self.module.main(
                [
                    "--candidate-root",
                    str(first_root),
                    "--layout-root",
                    str(self.layout_root),
                    "--health-timeout",
                    "1",
                    "--exec-command",
                    "--",
                    "status",
                ]
            )
        finally:
            self.module.promote_candidate = original_promote

        self.assertEqual(7, status)
        self.assertEqual(["first"], first_count.read_text().splitlines())
        self.assertFalse(second_count.exists())
        self.assertEqual(1, len(first_receipts))
        self.assertEqual(1, first_receipts[0].original_command_count)
        self.assertEqual(7, first_receipts[0].original_command_returncode)
        self.assertEqual(
            f"releases/{second_candidate.digest}",
            os.readlink(self.layout_root / "current"),
        )


class CodexInternalUpdatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_update_policy_module()

    def run_env_check(
        self,
        current_output: str,
        latest_tag: str,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            internal_bin = root / "codex"
            internal_bin.write_text(
                "#!/bin/sh\n"
                "cat <<'EOF'\n"
                f"{current_output}\n"
                "EOF\n"
            )
            internal_bin.chmod(0o755)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                f"location: https://example.invalid/{latest_tag}\n"
                "EOF\n"
            )
            curl.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["CODEX_SWITCH_PYTHON"] = sys.executable
            return subprocess.run(
                [
                    str(REPO_ROOT / "scripts" / "codex_env_setup"),
                    "--internal-bin",
                    str(internal_bin),
                    "--latest-url",
                    "https://example.invalid/latest",
                    "check-internal",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

    def decide(
        self,
        current: str | None,
        latest: str | None,
        *,
        blocked: tuple[str, ...] = (),
        fallback: str | None = None,
    ):
        return self.module.decide_internal_update(
            current_version=current,
            latest_version=latest,
            blocked_versions=blocked,
            fallback_version=fallback,
        )

    def assert_decision(
        self,
        decision,
        *,
        outcome: str,
        target: str | None,
    ) -> None:
        self.assertEqual(outcome, decision.outcome)
        self.assertEqual(target, decision.target_version)

    def test_equal_healthy_version_is_up_to_date(self) -> None:
        self.assert_decision(
            self.decide("1.2.3", "1.2.3"),
            outcome="up_to_date",
            target=None,
        )

    def test_healthy_newer_current_is_never_downgraded(self) -> None:
        self.assert_decision(
            self.decide("1.3.0", "1.2.9"),
            outcome="newer_current",
            target=None,
        )

    def test_env_check_keeps_healthy_newer_current(self) -> None:
        result = self.run_env_check(
            "codex-cli 1.3.0",
            "internal-rust-v1.2.9",
        )
        output = result.stdout + result.stderr

        self.assertEqual(0, result.returncode, output)
        self.assertIn(
            "Update: not needed (healthy current 1.3.0 is newer than "
            "reported latest 1.2.9)",
            output,
        )
        self.assertNotIn("available or unable to compare", output)

    def test_env_check_fails_closed_for_unparseable_current(self) -> None:
        result = self.run_env_check(
            "codex-cli unknown",
            "internal-rust-v1.2.9",
        )
        output = result.stdout + result.stderr

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Update: unable to compare versions", output)
        self.assertNotIn("Update: available", output)

    def test_healthy_older_current_upgrades_to_latest(self) -> None:
        self.assert_decision(
            self.decide("1.2.3", "1.3.0"),
            outcome="upgrade",
            target="1.3.0",
        )

    def test_blocked_current_may_select_lower_fallback(self) -> None:
        self.assert_decision(
            self.decide(
                "2.1.0",
                "2.1.0",
                blocked=("2.1.0",),
                fallback="2.0.4",
            ),
            outcome="blocked_fallback",
            target="2.0.4",
        )

    def test_blocked_current_fallback_does_not_require_latest(self) -> None:
        self.assert_decision(
            self.decide(
                "2.1.0",
                None,
                blocked=("2.1.0",),
                fallback="2.0.4",
            ),
            outcome="blocked_fallback",
            target="2.0.4",
        )

    def test_blocked_latest_keeps_healthy_newer_current(self) -> None:
        self.assert_decision(
            self.decide(
                "2.2.0",
                "2.1.0",
                blocked=("2.1.0",),
                fallback="2.0.4",
            ),
            outcome="newer_current",
            target=None,
        )

    def test_blocked_latest_cannot_downgrade_healthy_older_current(self) -> None:
        self.assert_decision(
            self.decide(
                "2.0.0",
                "2.1.0",
                blocked=("2.1.0",),
                fallback="1.9.9",
            ),
            outcome="failed",
            target=None,
        )

    def test_missing_or_unparseable_versions_fail_closed(self) -> None:
        cases = (
            (None, "1.2.3"),
            ("", "1.2.3"),
            ("not-a-version", "1.2.3"),
            ("1.2.3", None),
            ("1.2.3", ""),
            ("1.2.3", "latest"),
        )
        for current, latest in cases:
            with self.subTest(current=current, latest=latest):
                self.assert_decision(
                    self.decide(current, latest),
                    outcome="failed",
                    target=None,
                )

    def test_malformed_blocked_versions_fail_closed(self) -> None:
        self.assert_decision(
            self.decide(
                "1.0.0",
                "1.1.0",
                blocked=("1.1.0x",),
            ),
            outcome="failed",
            target=None,
        )

    def test_prerelease_versions_follow_semantic_order(self) -> None:
        cases = (
            ("1.2.3-alpha.2", "1.2.3-alpha.10", "upgrade", "1.2.3-alpha.10"),
            ("1.2.3-rc.1", "1.2.3", "upgrade", "1.2.3"),
            ("1.2.3", "1.2.4-alpha.1", "upgrade", "1.2.4-alpha.1"),
            ("1.2.3", "1.2.3-rc.1", "newer_current", None),
        )
        for current, latest, outcome, target in cases:
            with self.subTest(current=current, latest=latest):
                self.assert_decision(
                    self.decide(current, latest),
                    outcome=outcome,
                    target=target,
                )

    def test_extracts_full_multi_digit_prerelease_and_build_version(self) -> None:
        self.assertEqual(
            "10.20.30-rc-alpha.1+build-7",
            self.module.extract_semantic_version(
                "codex-cli 10.20.30-rc-alpha.1+build-7"
            ),
        )
        self.assertEqual(
            "10.20.30-rc-alpha.2+build-8",
            self.module.extract_semantic_version(
                "internal-rust-v10.20.30-rc-alpha.2+build-8"
            ),
        )


class CodexStagedInternalUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.install_root = self.root / "internal-bin"
        self.install_root.mkdir()
        self.bound_bin = self.install_root / "codex"
        self._write_script(
            self.bound_bin,
            "#!/usr/bin/env bash\n"
            'printf \'bound-probe:%s\\n\' "$*" >> "$CODEX_TEST_EVENTS"\n'
            'if [[ "${1:-}" == "--version" ]]; then\n'
            "  printf 'codex-cli 1.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        self.bound_copy = self.root / "bound-codex.expected"
        shutil.copy2(self.bound_bin, self.bound_copy)
        self.events = self.root / "events.log"
        self.signed_marker = self.root / "candidate.signed"
        self.installer_script = self.root / "team-install.sh"
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self._write_script(
            self.fake_bin / "curl",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf 'curl\\n' >> \"$CODEX_TEST_EVENTS\"\n"
            "cat \"$CODEX_TEST_INSTALLER\"\n",
        )
        self._write_script(
            self.fake_bin / "codesign",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf 'codesign:%s\\n' \"${@: -1}\" "
            ">> \"$CODEX_TEST_EVENTS\"\n"
            ": > \"$CODEX_TEST_SIGNED_MARKER\"\n",
        )
        self._write_script(
            self.fake_bin / "uname",
            "#!/usr/bin/env bash\n"
            "printf 'Darwin\\n'\n",
        )
        self.base_env = os.environ.copy()
        self.base_env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}{os.pathsep}{os.environ['PATH']}",
                "CODEX_INSTALL_AK": "ak-staged-test",
                "CODEX_SWITCH_PYTHON": (
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
                "CODEX_TEST_BOUND_BIN": str(self.bound_bin),
                "CODEX_TEST_BOUND_COPY": str(self.bound_copy),
                "CODEX_TEST_EVENTS": str(self.events),
                "CODEX_TEST_INSTALLER": str(self.installer_script),
                "CODEX_TEST_REQUIRE_SIGNED": "0",
                "CODEX_TEST_SIGNED_MARKER": str(self.signed_marker),
                "CODEX_SWITCH_SHELL_PROFILE": str(
                    self.root / "isolated-shell-profile"
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_script(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def _bound_snapshot(self) -> tuple[bytes, int]:
        return self.bound_bin.read_bytes(), stat.S_IMODE(self.bound_bin.stat().st_mode)

    def _candidate_dir(self, label: str) -> Path:
        return self.install_root / f".codex-internal-update-{label}"

    def _write_installer(
        self,
        *,
        version: str = "2.0.0",
        exit_status: int = 0,
        candidate_checks_bound: bool = True,
        mutate_bound: bool = False,
        mutate_ambient_state: bool = False,
        block_until_signal: bool = False,
    ) -> None:
        candidate_script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            + (
                "cmp \"$CODEX_TEST_BOUND_BIN\" \"$CODEX_TEST_BOUND_COPY\"\n"
                if candidate_checks_bound
                else ""
            )
            +
            "printf 'candidate-probe:%s\\n' \"$*\" "
            ">> \"$CODEX_TEST_EVENTS\"\n"
            'if [[ "${1:-}" == "--version" ]]; then\n'
            '  if [[ "${CODEX_TEST_REQUIRE_SIGNED:-0}" == "1" '
            '&& ! -f "$CODEX_TEST_SIGNED_MARKER" ]]; then\n'
            "    exit 29\n"
            "  fi\n"
            f"  printf 'codex-cli {version}\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 23\n"
        )
        lines = [
            "#!/usr/bin/env bash\n",
            "set -euo pipefail\n",
            (
                "printf 'installer|%s|%s|%s|%s|%s\\n' "
                '"$CODEX_INSTALL_DIR" "$CODEX_INSTALL_MODEL" '
                '"$CODEX_INSTALL_AZURE_BASE_URL" "$CODEX_INSTALL_AK" '
                '"${1:-}" >> "$CODEX_TEST_EVENTS"\n'
            ),
            'cmp "$CODEX_TEST_BOUND_BIN" "$CODEX_TEST_BOUND_COPY"\n',
        ]
        if mutate_ambient_state:
            lines.extend(
                [
                    (
                        "printf 'ambient|%s|%s|%s\\n' "
                        '"$HOME" "$CODEX_HOME" "$PATH" '
                        '>> "$CODEX_TEST_EVENTS"\n'
                    ),
                    (
                        '"$CODEX_SWITCH_PYTHON" -I -B - '
                        '"$HOME" "$CODEX_HOME" '
                        '>> "$CODEX_TEST_EVENTS" <<\'PY\'\n'
                    ),
                    "import os\n",
                    "import stat\n",
                    "import sys\n",
                    "paths = [os.path.dirname(sys.argv[1]), *sys.argv[1:]]\n",
                    "print(\n",
                    "    'ambient-mode|' + '|'.join(\n",
                    "        str(stat.S_IMODE(os.stat(path).st_mode))\n",
                    "        for path in paths\n",
                    "    )\n",
                    ")\n",
                    "PY\n",
                    'mkdir -p "$CODEX_HOME"\n',
                    (
                        "printf 'installer-default-config\\n' "
                        '> "$CODEX_HOME/config.toml"\n'
                    ),
                    'case ":$PATH:" in\n',
                    '  *":$CODEX_INSTALL_DIR:"*) ;;\n',
                    "  *)\n",
                    (
                        "    printf '\\n# Added by Codex installer\\n"
                        "export PATH=\"%s:$PATH\"\\n' "
                        '"$CODEX_INSTALL_DIR" >> "$HOME/.zshrc"\n'
                    ),
                    "    ;;\n",
                    "esac\n",
                ]
            )
        if block_until_signal:
            lines.extend(
                [
                    'printf \'installer-blocked\\n\' >> "$CODEX_TEST_EVENTS"\n',
                    "while :; do sleep 1; done\n",
                ]
            )
        if exit_status:
            lines.append(f"exit {exit_status}\n")
        else:
            if mutate_bound:
                lines.extend(
                    [
                        'cat > "$CODEX_TEST_BOUND_BIN" <<\'BOUND_MUTATION\'\n',
                        "#!/usr/bin/env bash\n",
                        "printf 'mutated-bound\\n'\n",
                        "BOUND_MUTATION\n",
                        'chmod 755 "$CODEX_TEST_BOUND_BIN"\n',
                    ]
                )
            lines.extend(
                [
                    'mkdir -p "$CODEX_INSTALL_DIR"\n',
                    'cat > "$CODEX_INSTALL_DIR/codex" <<\'CODEX_CANDIDATE\'\n',
                    candidate_script,
                    "CODEX_CANDIDATE\n",
                    'chmod 755 "$CODEX_INSTALL_DIR/codex"\n',
                ]
            )
        self._write_script(self.installer_script, "".join(lines))

    def _run_env_setup(
        self,
        candidate_dir: Path,
        *,
        version: str = "2.0.0",
        env_overrides: Dict[str, str] | None = None,
        extra_args: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        env = self.base_env.copy()
        if env_overrides is not None:
            env.update(env_overrides)
        command = [
            str(ENV_SETUP),
            "update-internal",
            "--internal-bin",
            str(self.bound_bin),
            "--install-dir",
            str(candidate_dir),
            "--version",
            version,
            "--model",
            "gpt-internal-staged",
            "--azure-base-url",
            "https://internal.example.invalid/api",
            "--skip-source-check",
            "--skip-proxy",
            *extra_args,
        ]
        return subprocess.run(
            [
                "bash",
                "-c",
                'umask 022; exec "$@"',
                "codex-staged-update-test",
                *command,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def _write_internal_manifest(self) -> Path:
        store = self.root / "store"
        profile_dir = store / "profiles" / "internal"
        profile_dir.mkdir(parents=True)
        internal_home = store / "homes" / "internal"
        internal_home.mkdir(parents=True)
        (profile_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "internal",
                    "codex_bin": str(self.bound_bin),
                    "codex_home": str(internal_home),
                }
            )
            + "\n"
        )
        return store

    def _write_staged_wrapper_helper(
        self,
        candidate_source: Path,
    ) -> tuple[Path, Path]:
        helper = self.root / "staged-wrapper-helper"
        helper_log = self.root / "staged-wrapper-helper.json"
        self._write_script(
            helper,
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "install_dir=''\n"
            "while (($#)); do\n"
            "  case \"$1\" in\n"
            "    --install-dir|--candidate-dir)\n"
            "      install_dir=\"$2\"\n"
            "      shift 2\n"
            "      ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            '[[ -n "$install_dir" ]]\n'
            'if [[ "$install_dir" == "$(dirname "$CODEX_TEST_BOUND_BIN")" ]]; then\n'
            "  exit 92\n"
            "fi\n"
            'printf \'{"candidate_dir":"%s"}\\n\' "$install_dir" '
            '> "$CODEX_TEST_HELPER_LOG"\n'
            'mkdir -m 700 -p "$install_dir"\n'
            'cp "$CODEX_TEST_CANDIDATE_SOURCE" "$install_dir/codex"\n'
            'chmod 755 "$install_dir/codex"\n',
        )
        self.base_env.update(
            {
                "CODEX_SWITCH_ENV_SETUP": str(helper),
                "CODEX_TEST_CANDIDATE_SOURCE": str(candidate_source),
                "CODEX_TEST_HELPER_LOG": str(helper_log),
            }
        )
        return helper, helper_log

    def _write_locked_promotion_driver(self) -> tuple[Path, Path]:
        driver = self.root / "locked-promotion-driver.py"
        trace = self.root / "locked-promotion-trace.jsonl"
        driver.write_text(
            "#!/usr/bin/env python3\n"
            "import fcntl\n"
            "import hashlib\n"
            "import json\n"
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "argv = sys.argv[1:]\n"
            "trace = Path(os.environ['CODEX_TEST_PROMOTION_TRACE'])\n"
            "\n"
            "def append(record):\n"
            "    with trace.open('a') as handle:\n"
            "        handle.write(json.dumps(record, sort_keys=True) + '\\n')\n"
            "\n"
            "def option(name):\n"
            "    index = argv.index(name)\n"
            "    return argv[index + 1]\n"
            "\n"
            "if 'promote-internal-update' not in argv:\n"
            "    append({'event': 'unexpected-command', 'argv': argv})\n"
            "    raise SystemExit(97)\n"
            "\n"
            "store = Path(option('--store-dir'))\n"
            "bound = Path(option('--bound-bin'))\n"
            "candidate = Path(option('--candidate-bin'))\n"
            "backup = Path(option('--backup-bin'))\n"
            "target_version = option('--target-version')\n"
            "observed_version = subprocess.check_output(\n"
            "    [str(candidate), '--version'],\n"
            "    text=True,\n"
            ").strip()\n"
            "if target_version not in observed_version:\n"
            "    raise SystemExit(96)\n"
            "\n"
            "artifact_root = candidate.parent / 'runtime-artifacts'\n"
            "artifact_root.mkdir(mode=0o700)\n"
            "artifacts = {\n"
            "    'capability_receipt': artifact_root / 'capability-receipt.json',\n"
            "    'parity_receipt': artifact_root / 'parity-receipt.json',\n"
            "    'parity_overlay': artifact_root / 'model-catalog.json',\n"
            "    'profile_config': artifact_root / 'profile-config.toml',\n"
            "    'shared_config': artifact_root / 'shared-config.toml',\n"
            "    'active_runtime_config': artifact_root / 'runtime-config.toml',\n"
            "    'launcher': artifact_root / 'codex-internal-app',\n"
            "    'manifest': artifact_root / 'manifest.json',\n"
            "}\n"
            "for role, path in artifacts.items():\n"
            "    path.write_text(role + ':' + str(candidate) + '\\n')\n"
            "\n"
            "fingerprint_paths = {\n"
            "    key: Path(value)\n"
            "    for key, value in json.loads(\n"
            "        os.environ['CODEX_TEST_FINGERPRINT_PATHS']\n"
            "    ).items()\n"
            "}\n"
            "fingerprint_paths['candidate_binary'] = candidate\n"
            "fingerprint_paths['capability_receipt'] = artifacts[\n"
            "    'capability_receipt'\n"
            "]\n"
            "\n"
            "def digest(path):\n"
            "    return hashlib.sha256(path.read_bytes()).hexdigest()\n"
            "\n"
            "prepared = {\n"
            "    label: digest(path)\n"
            "    for label, path in sorted(fingerprint_paths.items())\n"
            "}\n"
            "append({\n"
            "    'event': 'prepare',\n"
            "    'bound_bin': str(bound),\n"
            "    'candidate_bin': str(candidate),\n"
            "    'backup_bin': str(backup),\n"
            "    'artifact_roles': sorted(artifacts),\n"
            "})\n"
            "\n"
            "descriptor = os.open(store, os.O_RDONLY)\n"
            "try:\n"
            "    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    append({'event': 'lock', 'store': str(store)})\n"
            "    observed = {\n"
            "        label: digest(path)\n"
            "        for label, path in sorted(fingerprint_paths.items())\n"
            "    }\n"
            "    append({\n"
            "        'event': 'revalidate',\n"
            "        'fingerprints': sorted(observed),\n"
            "        'stable': observed == prepared,\n"
            "    })\n"
            "    if observed != prepared:\n"
            "        raise SystemExit(74)\n"
            "finally:\n"
            "    fcntl.flock(descriptor, fcntl.LOCK_UN)\n"
            "    os.close(descriptor)\n"
            "\n"
            "raise SystemExit(int(os.environ.get('CODEX_TEST_PROMOTION_EXIT', '73')))\n"
        )
        driver.chmod(0o755)
        return driver, trace

    def test_env_setup_stages_private_sibling_and_preserves_bound_inputs(
        self,
    ) -> None:
        self._write_installer()
        candidate_dir = self._candidate_dir("valid")
        before = self._bound_snapshot()

        result = self._run_env_setup(candidate_dir)

        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertEqual(before, self._bound_snapshot())
        self.assertTrue((candidate_dir / "codex").is_file())
        self.assertEqual(0o700, stat.S_IMODE(candidate_dir.stat().st_mode))
        self.assertIn(
            f"Internal Codex candidate ready: {candidate_dir / 'codex'}",
            output,
        )
        self.assertNotIn("Internal Codex after update", output)
        self.assertIn(
            (
                f"installer|{candidate_dir}|gpt-internal-staged|"
                "https://internal.example.invalid/api|ak-staged-test|2.0.0"
            ),
            self.events.read_text(),
        )
        self.assertFalse(
            any(".preinstall." in path.name for path in self.install_root.iterdir())
        )

    def test_env_setup_isolates_installer_config_and_shell_side_effects(
        self,
    ) -> None:
        live_codex_home = self.root / "live-codex-home"
        live_codex_home.mkdir()
        live_config = live_codex_home / "config.toml"
        live_config.write_text("live-config-sentinel = true\n")
        live_config.chmod(0o600)
        live_shell = self.home / ".zshrc"
        live_shell.write_text("live-shell-sentinel\n")
        live_shell.chmod(0o640)
        config_before = (
            live_config.read_bytes(),
            stat.S_IMODE(live_config.stat().st_mode),
        )
        shell_before = (
            live_shell.read_bytes(),
            stat.S_IMODE(live_shell.stat().st_mode),
        )
        self._write_installer(mutate_ambient_state=True)
        candidate_dir = self._candidate_dir("ambient-isolation")

        result = self._run_env_setup(
            candidate_dir,
            env_overrides={"CODEX_HOME": str(live_codex_home)},
        )

        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertEqual(
            config_before,
            (
                live_config.read_bytes(),
                stat.S_IMODE(live_config.stat().st_mode),
            ),
        )
        self.assertEqual(
            shell_before,
            (
                live_shell.read_bytes(),
                stat.S_IMODE(live_shell.stat().st_mode),
            ),
        )
        ambient_event = next(
            line
            for line in self.events.read_text().splitlines()
            if line.startswith("ambient|")
        )
        _, installer_home, installer_codex_home, installer_path = (
            ambient_event.split("|", 3)
        )
        self.assertNotEqual(str(self.home), installer_home)
        self.assertNotEqual(str(live_codex_home), installer_codex_home)
        self.assertEqual(
            Path(installer_home).parent,
            Path(installer_codex_home).parent,
        )
        self.assertEqual(
            str(candidate_dir),
            installer_path.split(os.pathsep)[0],
        )
        ambient_mode = next(
            line
            for line in self.events.read_text().splitlines()
            if line.startswith("ambient-mode|")
        )
        self.assertEqual(
            ["448", "448", "448"],
            ambient_mode.split("|")[1:],
        )
        self.assertFalse(Path(installer_home).parent.exists())
        self.assertTrue((candidate_dir / "codex").is_file())

    def test_env_setup_isolates_and_cleans_failed_installer_state(
        self,
    ) -> None:
        live_codex_home = self.root / "live-codex-home-failure"
        live_codex_home.mkdir()
        live_config = live_codex_home / "config.toml"
        live_config.write_text("live-config-failure-sentinel = true\n")
        live_config.chmod(0o600)
        live_shell = self.home / ".zshrc"
        live_shell.write_text("live-shell-failure-sentinel\n")
        live_shell.chmod(0o640)
        config_before = (
            live_config.read_bytes(),
            stat.S_IMODE(live_config.stat().st_mode),
        )
        shell_before = (
            live_shell.read_bytes(),
            stat.S_IMODE(live_shell.stat().st_mode),
        )
        self._write_installer(
            exit_status=17,
            mutate_ambient_state=True,
        )
        candidate_dir = self._candidate_dir("ambient-failure")

        result = self._run_env_setup(
            candidate_dir,
            env_overrides={"CODEX_HOME": str(live_codex_home)},
        )

        output = result.stdout + result.stderr
        self.assertEqual(17, result.returncode, output)
        self.assertEqual(
            config_before,
            (
                live_config.read_bytes(),
                stat.S_IMODE(live_config.stat().st_mode),
            ),
        )
        self.assertEqual(
            shell_before,
            (
                live_shell.read_bytes(),
                stat.S_IMODE(live_shell.stat().st_mode),
            ),
        )
        ambient_event = next(
            line
            for line in self.events.read_text().splitlines()
            if line.startswith("ambient|")
        )
        _, installer_home, installer_codex_home, installer_path = (
            ambient_event.split("|", 3)
        )
        self.assertNotEqual(str(self.home), installer_home)
        self.assertNotEqual(str(live_codex_home), installer_codex_home)
        self.assertEqual(
            str(candidate_dir),
            installer_path.split(os.pathsep)[0],
        )
        self.assertFalse(Path(installer_home).parent.exists())
        self.assertFalse((candidate_dir / "codex").exists())

    def test_env_setup_initialization_failure_removes_installer_root(
        self,
    ) -> None:
        self._write_installer()
        self._write_script(
            self.fake_bin / "chmod",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'target="${@: -1}"\n'
            'case "$target" in\n'
            "  /tmp/codex-switch-internal-installer.*)\n"
            '    printf \'scratch-chmod:%s\\n\' "$target" '
            '>> "$CODEX_TEST_EVENTS"\n'
            '    exec /bin/chmod 755 "$target"\n'
            "    ;;\n"
            "esac\n"
            'exec /bin/chmod "$@"\n',
        )
        candidate_dir = self._candidate_dir("scratch-init-failure")

        result = self._run_env_setup(candidate_dir)

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        scratch_event = next(
            line
            for line in self.events.read_text().splitlines()
            if line.startswith("scratch-chmod:")
        )
        installer_root = Path(scratch_event.split(":", 1)[1])
        self.assertTrue(
            installer_root.name.startswith(
                "codex-switch-internal-installer."
            )
        )
        self.addCleanup(shutil.rmtree, installer_root, ignore_errors=True)
        self.assertFalse(installer_root.exists())
        self.assertFalse((candidate_dir / "codex").exists())

    def test_env_setup_removes_private_installer_state_on_signals(self) -> None:
        for signal_value, label in (
            (signal.SIGHUP, "hup"),
            (signal.SIGINT, "int"),
            (signal.SIGTERM, "term"),
        ):
            with self.subTest(signal=label):
                if self.events.exists():
                    self.events.unlink()
                live_codex_home = self.root / f"live-codex-home-{label}"
                live_codex_home.mkdir()
                live_config = live_codex_home / "config.toml"
                live_config.write_text(
                    f"live-config-{label}-sentinel = true\n"
                )
                live_config.chmod(0o600)
                live_shell = self.home / ".zshrc"
                live_shell.write_text(f"live-shell-{label}-sentinel\n")
                live_shell.chmod(0o640)
                config_before = (
                    live_config.read_bytes(),
                    stat.S_IMODE(live_config.stat().st_mode),
                )
                shell_before = (
                    live_shell.read_bytes(),
                    stat.S_IMODE(live_shell.stat().st_mode),
                )
                self._write_installer(
                    mutate_ambient_state=True,
                    block_until_signal=True,
                )
                candidate_dir = self._candidate_dir(f"{label}-cleanup")
                env = self.base_env.copy()
                env["CODEX_HOME"] = str(live_codex_home)
                command = [
                    str(ENV_SETUP),
                    "update-internal",
                    "--internal-bin",
                    str(self.bound_bin),
                    "--install-dir",
                    str(candidate_dir),
                    "--version",
                    "2.0.0",
                    "--model",
                    "gpt-internal-staged",
                    "--azure-base-url",
                    "https://internal.example.invalid/api",
                    "--skip-source-check",
                    "--skip-proxy",
                ]
                process = subprocess.Popen(
                    [
                        "bash",
                        "-c",
                        'umask 022; exec "$@"',
                        f"codex-staged-update-{label}-test",
                        *command,
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
                installer_root: Path | None = None
                try:
                    for _ in range(200):
                        if self.events.exists():
                            events = self.events.read_text()
                            ambient_events = [
                                line
                                for line in events.splitlines()
                                if line.startswith("ambient|")
                            ]
                            if ambient_events and "installer-blocked" in events:
                                _, installer_home, _, _ = ambient_events[
                                    -1
                                ].split("|", 3)
                                installer_root = Path(installer_home).parent
                                break
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(installer_root)
                    assert installer_root is not None
                    self.assertTrue(
                        installer_root.name.startswith(
                            "codex-switch-internal-installer."
                        )
                    )
                    self.addCleanup(
                        shutil.rmtree,
                        installer_root,
                        ignore_errors=True,
                    )
                    os.killpg(process.pid, signal_value)
                    stdout, stderr = process.communicate(timeout=10)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=10)

                self.assertNotEqual(0, process.returncode, stdout + stderr)
                self.assertEqual(
                    config_before,
                    (
                        live_config.read_bytes(),
                        stat.S_IMODE(live_config.stat().st_mode),
                    ),
                )
                self.assertEqual(
                    shell_before,
                    (
                        live_shell.read_bytes(),
                        stat.S_IMODE(live_shell.stat().st_mode),
                    ),
                )
                self.assertFalse(
                    installer_root.exists(),
                    stdout + stderr,
                )

    def test_env_setup_parent_signal_terminates_and_reaps_installer(
        self,
    ) -> None:
        live_codex_home = self.root / "live-codex-home-parent-term"
        live_codex_home.mkdir()
        live_config = live_codex_home / "config.toml"
        live_config.write_text("live-config-parent-term-sentinel = true\n")
        live_config.chmod(0o600)
        live_shell = self.home / ".zshrc"
        live_shell.write_text("live-shell-parent-term-sentinel\n")
        live_shell.chmod(0o640)
        config_before = (
            live_config.read_bytes(),
            stat.S_IMODE(live_config.stat().st_mode),
        )
        shell_before = (
            live_shell.read_bytes(),
            stat.S_IMODE(live_shell.stat().st_mode),
        )
        self._write_installer(
            mutate_ambient_state=True,
            block_until_signal=True,
        )
        candidate_dir = self._candidate_dir("parent-term-cleanup")
        env = self.base_env.copy()
        env["CODEX_HOME"] = str(live_codex_home)
        command = [
            str(ENV_SETUP),
            "update-internal",
            "--internal-bin",
            str(self.bound_bin),
            "--install-dir",
            str(candidate_dir),
            "--version",
            "2.0.0",
            "--model",
            "gpt-internal-staged",
            "--azure-base-url",
            "https://internal.example.invalid/api",
            "--skip-source-check",
            "--skip-proxy",
        ]
        process = subprocess.Popen(
            [
                "bash",
                "-c",
                'umask 022; exec "$@"',
                "codex-staged-update-parent-term-test",
                *command,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        installer_root: Path | None = None
        timed_out = False
        try:
            for _ in range(200):
                if self.events.exists():
                    events = self.events.read_text()
                    ambient_events = [
                        line
                        for line in events.splitlines()
                        if line.startswith("ambient|")
                    ]
                    if ambient_events and "installer-blocked" in events:
                        _, installer_home, _, _ = ambient_events[-1].split(
                            "|",
                            3,
                        )
                        installer_root = Path(installer_home).parent
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(installer_root)
            assert installer_root is not None
            self.addCleanup(
                shutil.rmtree,
                installer_root,
                ignore_errors=True,
            )
            os.kill(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)

        self.assertFalse(
            timed_out,
            "top-level TERM did not terminate the blocked installer",
        )
        self.assertEqual(143, process.returncode, stdout + stderr)
        self.assertEqual(
            config_before,
            (
                live_config.read_bytes(),
                stat.S_IMODE(live_config.stat().st_mode),
            ),
        )
        self.assertEqual(
            shell_before,
            (
                live_shell.read_bytes(),
                stat.S_IMODE(live_shell.stat().st_mode),
            ),
        )
        self.assertFalse(installer_root.exists(), stdout + stderr)

    def test_env_setup_detects_bound_mutation_by_installer(self) -> None:
        self._write_installer(
            candidate_checks_bound=False,
            mutate_bound=True,
        )
        candidate_dir = self._candidate_dir("bound-mutation")

        result = self._run_env_setup(candidate_dir)

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn(
            "Bound internal binary changed during candidate install",
            output,
        )

    def test_env_setup_rejects_non_sibling_candidate_before_installer(
        self,
    ) -> None:
        self._write_installer()
        candidate_dir = self.root / "elsewhere" / ".codex-internal-update-invalid"
        before = self._bound_snapshot()

        result = self._run_env_setup(candidate_dir)

        output = result.stdout + result.stderr
        self.assertEqual(2, result.returncode, output)
        self.assertIn("private sibling", output.lower())
        self.assertEqual(before, self._bound_snapshot())
        self.assertFalse(self.events.exists())
        self.assertFalse(candidate_dir.exists())

    def test_env_setup_rejects_unintended_candidate_version(self) -> None:
        self._write_installer(version="2.0.1")
        candidate_dir = self._candidate_dir("wrong-version")
        before = self._bound_snapshot()

        result = self._run_env_setup(candidate_dir, version="2.0.0")

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("expected 2.0.0", output)
        self.assertIn("observed 2.0.1", output)
        self.assertEqual(before, self._bound_snapshot())
        self.assertNotIn("candidate ready", output.lower())

    def test_env_setup_codesigns_before_candidate_validation(self) -> None:
        self._write_installer()
        candidate_dir = self._candidate_dir("codesign-order")

        result = self._run_env_setup(
            candidate_dir,
            env_overrides={"CODEX_TEST_REQUIRE_SIGNED": "1"},
        )

        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        events = self.events.read_text().splitlines()
        codesign_index = events.index(f"codesign:{candidate_dir / 'codex'}")
        probe_index = events.index("candidate-probe:--version")
        self.assertLess(codesign_index, probe_index)

    def test_env_setup_propagates_installer_failure_status(self) -> None:
        self._write_installer(exit_status=17)
        candidate_dir = self._candidate_dir("installer-failure")
        before = self._bound_snapshot()

        result = self._run_env_setup(candidate_dir)

        output = result.stdout + result.stderr
        self.assertEqual(17, result.returncode, output)
        self.assertIn("17", output)
        self.assertEqual(before, self._bound_snapshot())
        self.assertFalse((candidate_dir / "codex").exists())

    def test_update_internal_dry_run_reports_complete_zero_mutation_plan(
        self,
    ) -> None:
        store = self._write_internal_manifest()
        before_store = filesystem_snapshot(store)
        before_install = filesystem_snapshot(self.install_root)
        env = self.base_env.copy()
        env["CODEX_SWITCH_SKIP_SELF_UPDATE"] = "1"

        result = subprocess.run(
            [
                str(WRAPPER),
                "--skip-self-update",
                "--store-dir",
                str(store),
                "update-internal",
                "--version",
                "2.0.0",
                "--dry-run",
                "--skip-source-check",
                "--skip-proxy",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        for expected in (
            "Target version: 2.0.0",
            "Candidate path class: private sibling",
            "Parity checks:",
            "Artifact set:",
            "Promotion order:",
            "Capability receipt: staged candidate",
            "Parity receipt: staged candidate",
            "Model catalog overlay: staged candidate",
            "Projected profile config",
            "Projected shared config when changed",
            "Projected active runtime config when materialized",
            "Managed launcher and internal manifest",
            (
                "Locked revalidation: bound binary, candidate binary, "
                "internal manifest, official reference, source catalog, "
                "capability receipt, and config inputs"
            ),
            "Bound app-server smoke: post-promotion only",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)
        self.assertNotIn("would move existing", output)
        self.assertEqual(before_store, filesystem_snapshot(store))
        self.assertEqual(before_install, filesystem_snapshot(self.install_root))
        self.assertFalse(self.events.exists())

    def test_candidate_probe_failure_preserves_bound_before_parity_promotion(
        self,
    ) -> None:
        store = self._write_internal_manifest()
        candidate_source = self.root / "failed-candidate"
        self._write_script(
            candidate_source,
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == "--version" ]]; then\n'
            "  printf 'codex-cli 2.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 23\n",
        )
        helper = self.root / "staged-env-helper"
        helper_log = self.root / "helper-install-dir.txt"
        self._write_script(
            helper,
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "install_dir=''\n"
            "while (($#)); do\n"
            "  case \"$1\" in\n"
            "    --install-dir|--candidate-dir)\n"
            "      install_dir=\"$2\"\n"
            "      shift 2\n"
            "      ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            '[[ -n "$install_dir" ]]\n'
            'printf "%s\\n" "$install_dir" > "$CODEX_TEST_HELPER_LOG"\n'
            'mkdir -p "$install_dir"\n'
            'chmod 700 "$install_dir"\n'
            'cp "$CODEX_TEST_FAILED_CANDIDATE" "$install_dir/codex"\n'
            'chmod 755 "$install_dir/codex"\n',
        )
        before = self._bound_snapshot()
        env = self.base_env.copy()
        env.update(
            {
                "CODEX_SWITCH_ENV_SETUP": str(helper),
                "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
                "CODEX_TEST_FAILED_CANDIDATE": str(candidate_source),
                "CODEX_TEST_HELPER_LOG": str(helper_log),
            }
        )

        result = subprocess.run(
            [
                str(WRAPPER),
                "--skip-self-update",
                "--store-dir",
                str(store),
                "update-internal",
                "--version",
                "2.0.0",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertEqual(before, self._bound_snapshot())
        candidate_dir = Path(helper_log.read_text().strip())
        self.assertEqual(self.install_root, candidate_dir.parent)
        self.assertNotEqual(self.install_root, candidate_dir)
        self.assertEqual(0o700, stat.S_IMODE(candidate_dir.stat().st_mode))
        self.assertNotIn("verified installed version", output)
        self.assertNotIn("update-internal: completed", output)

    def test_candidate_capability_failure_never_probes_bound_path(self) -> None:
        store = self._write_internal_manifest()
        self._write_installer(version="1.0.0")
        before = self._bound_snapshot()
        env = self.base_env.copy()
        env["CODEX_SWITCH_SKIP_SELF_UPDATE"] = "1"

        result = subprocess.run(
            [
                str(WRAPPER),
                "--skip-self-update",
                "--store-dir",
                str(store),
                "update-internal",
                "--version",
                "1.0.0",
                "--skip-source-check",
                "--skip-proxy",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        events = self.events.read_text().splitlines() if self.events.exists() else []
        self.assertTrue(
            any(
                event.startswith(
                    "candidate-probe:app-server generate-json-schema"
                )
                for event in events
            ),
            events,
        )
        self.assertFalse(
            any(event.startswith("bound-probe:") for event in events),
            events,
        )
        self.assertEqual(before, self._bound_snapshot())
        self.assertNotIn("verified installed version", output)

    def test_wrapper_delegates_complete_candidate_bundle_with_locked_revalidation(
        self,
    ) -> None:
        store = self._write_internal_manifest()
        profile_dir = store / "profiles" / "internal"
        profile_config = profile_dir / "config.toml"
        profile_config.write_text(
            'model = "gpt-internal-staged"\n'
            'model_provider = "azure"\n'
        )
        shared_config = self.root / "shared-config.toml"
        shared_config.write_text("[agents]\nmax_threads = 4\n")
        active_runtime_config = store / "homes" / "internal" / "config.toml"
        active_runtime_config.write_text('profile = "internal"\n')
        source_catalog = self.root / "source-catalog.json"
        source_catalog.write_text(
            '{"models":[{"slug":"gpt-internal-staged"}]}\n'
        )
        official_reference = self.root / "official-reference-codex"
        official_reference.write_bytes(b"official-reference\n")
        candidate_source = self.root / "candidate-source"
        self._write_script(
            candidate_source,
            "#!/usr/bin/env bash\n"
            'printf \'candidate-probe:%s\\n\' "$*" >> "$CODEX_TEST_EVENTS"\n'
            'if [[ "${1:-}" == "--version" ]]; then\n'
            "  printf 'codex-cli 1.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 23\n",
        )
        _helper, helper_log = self._write_staged_wrapper_helper(
            candidate_source
        )
        driver, trace = self._write_locked_promotion_driver()
        before = self._bound_snapshot()
        env = self.base_env.copy()
        env.update(
            {
                "CODEX_SWITCH_SCRIPT": str(driver),
                "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
                "CODEX_TEST_PROMOTION_TRACE": str(trace),
                "CODEX_TEST_PROMOTION_EXIT": "73",
                "CODEX_TEST_FINGERPRINT_PATHS": json.dumps(
                    {
                        "active_runtime_config": str(active_runtime_config),
                        "bound_binary": str(self.bound_bin),
                        "internal_manifest": str(
                            profile_dir / "manifest.json"
                        ),
                        "official_reference": str(official_reference),
                        "profile_config": str(profile_config),
                        "shared_config": str(shared_config),
                        "source_catalog": str(source_catalog),
                    },
                    sort_keys=True,
                ),
            }
        )

        result = subprocess.run(
            [
                str(WRAPPER),
                "--skip-self-update",
                "--store-dir",
                str(store),
                "update-internal",
                "--version",
                "1.0.0",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        output = result.stdout + result.stderr
        self.assertEqual(73, result.returncode, output)
        helper_record = json.loads(helper_log.read_text())
        candidate_dir = Path(helper_record["candidate_dir"])
        candidate_bin = candidate_dir / "codex"
        self.assertEqual(self.install_root, candidate_dir.parent)
        self.assertNotEqual(self.install_root, candidate_dir)
        self.assertEqual(0o700, stat.S_IMODE(candidate_dir.stat().st_mode))
        records = [
            json.loads(line)
            for line in trace.read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(
            ["prepare", "lock", "revalidate"],
            [record["event"] for record in records],
        )
        self.assertEqual(str(self.bound_bin), records[0]["bound_bin"])
        self.assertEqual(str(candidate_bin), records[0]["candidate_bin"])
        self.assertEqual(
            self.install_root,
            Path(records[0]["backup_bin"]).parent,
        )
        self.assertEqual(
            {
                "active_runtime_config",
                "capability_receipt",
                "launcher",
                "manifest",
                "parity_overlay",
                "parity_receipt",
                "profile_config",
                "shared_config",
            },
            set(records[0]["artifact_roles"]),
        )
        self.assertEqual(
            {
                "active_runtime_config",
                "bound_binary",
                "candidate_binary",
                "capability_receipt",
                "internal_manifest",
                "official_reference",
                "profile_config",
                "shared_config",
                "source_catalog",
            },
            set(records[2]["fingerprints"]),
        )
        self.assertTrue(records[2]["stable"])
        events = self.events.read_text().splitlines()
        self.assertFalse(
            any(event.startswith("bound-probe:") for event in events),
            events,
        )
        self.assertEqual(before, self._bound_snapshot())
        self.assertNotIn("verified installed version", output)

    def test_cli_only_promotion_commits_digest_bound_manifest_without_desktop_artifacts(
        self,
    ) -> None:
        store = self._write_internal_manifest()
        profile_dir = store / "profiles" / "internal"
        desktop_artifacts = {
            store / "bin" / "codex-internal-app": b"desktop-launcher\n",
            profile_dir / "parity" / "receipt.json": b"parity-receipt\n",
            profile_dir / "parity" / "model-catalog.json": b"overlay\n",
            profile_dir / "config.toml": b'model = "old"\n',
        }
        for path, payload in desktop_artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o755 if path.name == "codex-internal-app" else 0o600)
        candidate_dir = self._candidate_dir("cli-only-success")
        candidate_dir.mkdir(mode=0o700)
        candidate = candidate_dir / "codex"
        self._write_script(
            candidate,
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == "--version" ]]; then\n'
            "  printf 'codex-cli 2.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        candidate_payload = candidate.read_bytes()
        backup = self.install_root / ".codex-internal-backup-cli-only"

        result = subprocess.run(
            [
                self.base_env["CODEX_SWITCH_PYTHON"],
                str(PROFILE_SWITCH_MODULE_PATH),
                "--store-dir",
                str(store),
                "--official-codex-home",
                str(self.root / "official-home"),
                "--internal-codex-home",
                str(store / "homes" / "internal"),
                "--launch-agent-path",
                str(self.root / "agent.plist"),
                "promote-internal-update",
                "--bound-bin",
                str(self.bound_bin),
                "--candidate-bin",
                str(candidate),
                "--backup-bin",
                str(backup),
                "--target-version",
                "2.0.0",
                "--cli-only",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.base_env,
        )

        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        self.assertEqual(candidate_payload, self.bound_bin.read_bytes())
        self.assertFalse(candidate.exists())
        self.assertFalse(backup.exists())
        manifest = json.loads(
            (profile_dir / "manifest.json").read_text()
        )
        self.assertEqual(
            {
                "schema_version": 1,
                "scope": "cli-only",
                "backend_sha256": hashlib.sha256(
                    candidate_payload
                ).hexdigest(),
                "backend_version": "2.0.0",
            },
            manifest["internal_cli_generation"],
        )
        self.assertEqual(
            "unverified",
            manifest["internal_app_readiness"],
        )
        for path, payload in desktop_artifacts.items():
            self.assertEqual(payload, path.read_bytes(), str(path))
        self.assertIn("CLI-only promotion: passed", output)
        self.assertNotIn("App-server smoke: passed", output)
        self.assertNotIn("Restart required", output)

    def test_cli_only_promotion_rolls_back_binary_and_manifest_when_postcondition_fails(
        self,
    ) -> None:
        store = self._write_internal_manifest()
        manifest_path = store / "profiles" / "internal" / "manifest.json"
        old_manifest = manifest_path.read_bytes()
        old_bound = self.bound_bin.read_bytes()
        candidate_dir = self._candidate_dir("cli-only-rollback")
        candidate_dir.mkdir(mode=0o700)
        candidate = candidate_dir / "codex"
        probe_count = self.root / "cli-only-probe-count"
        self._write_script(
            candidate,
            "#!/usr/bin/env bash\n"
            f"count_file={str(probe_count)!r}\n"
            "count=0\n"
            '[[ -f "$count_file" ]] && count="$(cat "$count_file")"\n'
            "count=$((count + 1))\n"
            'printf "%s\\n" "$count" > "$count_file"\n'
            'if [[ "${1:-}" == "--version" && "$count" == "1" ]]; then\n'
            "  printf 'codex-cli 2.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 31\n",
        )
        candidate_payload = candidate.read_bytes()
        backup = self.install_root / ".codex-internal-backup-cli-rollback"

        result = subprocess.run(
            [
                self.base_env["CODEX_SWITCH_PYTHON"],
                str(PROFILE_SWITCH_MODULE_PATH),
                "--store-dir",
                str(store),
                "--official-codex-home",
                str(self.root / "official-home"),
                "--internal-codex-home",
                str(store / "homes" / "internal"),
                "--launch-agent-path",
                str(self.root / "agent.plist"),
                "promote-internal-update",
                "--bound-bin",
                str(self.bound_bin),
                "--candidate-bin",
                str(candidate),
                "--backup-bin",
                str(backup),
                "--target-version",
                "2.0.0",
                "--cli-only",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.base_env,
        )

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertEqual(old_bound, self.bound_bin.read_bytes())
        self.assertEqual(old_manifest, manifest_path.read_bytes())
        self.assertEqual(candidate_payload, candidate.read_bytes())
        self.assertEqual("2", probe_count.read_text().strip())
        self.assertFalse(backup.exists())
        self.assertFalse(
            (store / ".runtime-binding-rebind.json").exists()
        )
        self.assertNotIn("CLI-only promotion: passed", output)

    def test_cli_only_promotion_rolls_back_when_managed_generation_is_invalid(
        self,
    ) -> None:
        store = self._write_internal_manifest()
        manifest_path = store / "profiles" / "internal" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["codex_home"] = "relative-internal-home"
        manifest_path.write_text(json.dumps(manifest) + "\n")
        old_manifest = manifest_path.read_bytes()
        old_bound = self.bound_bin.read_bytes()
        candidate_dir = self._candidate_dir("cli-only-invalid-generation")
        candidate_dir.mkdir(mode=0o700)
        candidate = candidate_dir / "codex"
        self._write_script(
            candidate,
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == "--version" ]]; then\n'
            "  printf 'codex-cli 2.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        candidate_payload = candidate.read_bytes()
        backup = self.install_root / ".codex-internal-backup-cli-invalid"

        result = subprocess.run(
            [
                self.base_env["CODEX_SWITCH_PYTHON"],
                str(PROFILE_SWITCH_MODULE_PATH),
                "--store-dir",
                str(store),
                "--official-codex-home",
                str(self.root / "official-home"),
                "--internal-codex-home",
                str(store / "homes" / "internal"),
                "--launch-agent-path",
                str(self.root / "agent.plist"),
                "promote-internal-update",
                "--bound-bin",
                str(self.bound_bin),
                "--candidate-bin",
                str(candidate),
                "--backup-bin",
                str(backup),
                "--target-version",
                "2.0.0",
                "--cli-only",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.base_env,
        )

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("CLI CODEX_HOME path is not absolute", output)
        self.assertEqual(old_bound, self.bound_bin.read_bytes())
        self.assertEqual(old_manifest, manifest_path.read_bytes())
        self.assertEqual(candidate_payload, candidate.read_bytes())
        self.assertFalse(backup.exists())
        self.assertFalse(
            (store / ".runtime-binding-rebind.json").exists()
        )
        self.assertNotIn("CLI-only promotion: passed", output)

    def test_cli_only_promotion_rolls_back_when_managed_shell_probe_fails(
        self,
    ) -> None:
        store = self._write_internal_manifest()
        manifest_path = store / "profiles" / "internal" / "manifest.json"
        old_manifest = manifest_path.read_bytes()
        old_bound = self.bound_bin.read_bytes()
        managed_home = (store / "homes" / "internal").resolve()
        candidate_dir = self._candidate_dir("cli-only-managed-shell-failure")
        candidate_dir.mkdir(mode=0o700)
        candidate = candidate_dir / "codex"
        self._write_script(
            candidate,
            "#!/usr/bin/env bash\n"
            f"managed_home={str(managed_home)!r}\n"
            'if [[ "${CODEX_HOME:-}" == "$managed_home" ]]; then\n'
            "  exit 41\n"
            "fi\n"
            'if [[ "${1:-}" == "--version" ]]; then\n'
            "  printf 'codex-cli 2.0.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        candidate_payload = candidate.read_bytes()
        backup = self.install_root / ".codex-internal-backup-cli-shell"

        result = subprocess.run(
            [
                self.base_env["CODEX_SWITCH_PYTHON"],
                str(PROFILE_SWITCH_MODULE_PATH),
                "--store-dir",
                str(store),
                "--official-codex-home",
                str(self.root / "official-home"),
                "--internal-codex-home",
                str(managed_home),
                "--launch-agent-path",
                str(self.root / "agent.plist"),
                "promote-internal-update",
                "--bound-bin",
                str(self.bound_bin),
                "--candidate-bin",
                str(candidate),
                "--backup-bin",
                str(backup),
                "--target-version",
                "2.0.0",
                "--cli-only",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.base_env,
        )

        output = result.stdout + result.stderr
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("Managed internal CLI shell version probe failed", output)
        self.assertEqual(old_bound, self.bound_bin.read_bytes())
        self.assertEqual(old_manifest, manifest_path.read_bytes())
        self.assertEqual(candidate_payload, candidate.read_bytes())
        self.assertFalse(backup.exists())
        self.assertFalse(
            (store / ".runtime-binding-rebind.json").exists()
        )
        self.assertNotIn("CLI-only promotion: passed", output)


class CodexInstallerRunnerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bundle_module = load_bundle_module()
        self.promotion_module = load_promotion_module()
        self.health_script = self.root / "health.py"
        self.health_script.write_text(
            "import json\n"
            "import os\n"
            "\n"
            "print(json.dumps({\n"
            "    'schema': 'codex-switch.promotion-handshake',\n"
            "    'schema_version': 1,\n"
            "    'run_id': os.environ['CODEX_SWITCH_PROMOTION_RUN_ID'],\n"
            "    'version': os.environ['CODEX_SWITCH_PROMOTION_VERSION'],\n"
            "    'digest': os.environ['CODEX_SWITCH_PROMOTION_DIGEST'],\n"
            "    'root': os.environ['CODEX_SWITCH_PROMOTION_ROOT'],\n"
            "}, sort_keys=True))\n"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_source(
        self,
        root: Path,
        version: str,
        *,
        helper_text: str = "VALUE = 1\n",
        runner_text: str = "#!/usr/bin/env bash\nexit 0\n",
        command_text: str | None = None,
    ) -> None:
        (root / "agents").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "evals").mkdir()
        (root / "scripts").mkdir()
        (root / "README.md").write_text(f"release {version}\n")
        (root / "SKILL.md").write_text("skill\n")
        (root / "VERSION").write_text(f"{version}\n")
        (root / "run.sh").write_text(runner_text)
        (root / "agents" / "openai.yaml").write_text("name: codex-switch\n")
        (root / "docs" / "release.md").write_text(f"docs {version}\n")
        (root / "evals" / "evals.json").write_text('{"evals": []}\n')
        if command_text is None:
            command_text = (
                "#!/usr/bin/env bash\n"
                f"printf 'codex-switch {version}\\n'\n"
            )
        (root / "scripts" / "codex-switch").write_text(command_text)
        (root / "scripts" / "package-release.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n"
        )
        write_required_python_modules(root / "scripts")
        (root / "scripts" / "helper.py").write_text(helper_text)
        for path in (
            root / "run.sh",
            root / "scripts" / "codex-switch",
            root / "scripts" / "package-release.sh",
        ):
            path.chmod(0o755)

    def _build_candidate(
        self,
        label: str,
        version: str,
        *,
        helper_text: str = "VALUE = 1\n",
        runner_text: str = "#!/usr/bin/env bash\nexit 0\n",
        command_text: str | None = None,
    ) -> tuple[Path, Path]:
        source = self.root / "sources" / label
        output = self.root / "bundles" / label
        self._write_source(
            source,
            version,
            helper_text=helper_text,
            runner_text=runner_text,
            command_text=command_text,
        )
        self.bundle_module.build_release_bundle(source, output)
        return output / "codex-switch", output / "codex-switch.tar.gz"

    def _promote(self, candidate_root: Path, layout_root: Path, label: str) -> None:
        candidate = self.promotion_module.validate_candidate(
            candidate_root,
            smoke_timeout=1.0,
            import_timeout=1.0,
        )
        self.promotion_module.promote_candidate(
            candidate,
            self.promotion_module.PromotionLayout(layout_root),
            [sys.executable, str(self.health_script)],
            health_timeout=1.0,
            run_id=f"adapter-{label}",
        )

    def _prepare_prior_layout(self, label: str) -> Path:
        layout_root = self.root / "layouts" / label
        first_root, _ = self._build_candidate(
            f"{label}-prior-1",
            "0.9.0",
        )
        second_root, _ = self._build_candidate(
            f"{label}-prior-2",
            "1.0.0",
        )
        self._promote(first_root, layout_root, f"{label}-prior-1")
        self._promote(second_root, layout_root, f"{label}-prior-2")
        return layout_root

    def _reference_snapshot(self, layout_root: Path) -> Dict[str, bytes]:
        result: Dict[str, bytes] = {}
        for name in ("current", "rollback"):
            path = layout_root / name
            info = path.lstat()
            self.assertTrue(path.is_symlink(), f"{path} must be a symlink")
            result[name] = (
                f"{stat.S_IMODE(info.st_mode):04o}:".encode()
                + os.fsencode(os.readlink(path))
            )
        return result

    def _run_entrypoint(
        self,
        entrypoint: Path,
        archive: Path,
        layout_root: Path,
        install_dir: Path,
        args: tuple[str, ...],
        *,
        env_overrides: Dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_SWITCH_INSTALL_DIR": str(install_dir),
                "CODEX_SWITCH_LIB_DIR": str(layout_root),
                "CODEX_SWITCH_PYTHON": sys.executable,
                "CODEX_SWITCH_SOURCE_TARBALL_URL": "",
                "CODEX_SWITCH_TARBALL_URL": archive.as_uri(),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if env_overrides is not None:
            env.update(env_overrides)
        return subprocess.run(
            [
                "bash",
                "-c",
                'if "$@"; then exit 0; else exit "$?"; fi',
                "codex-switch-adapter-test",
                str(entrypoint),
                *args,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def _run_piped_entrypoint(
        self,
        entrypoint: Path,
        archive: Path,
        layout_root: Path,
        install_dir: Path,
        args: tuple[str, ...],
        *,
        source_archive: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "CODEX_SWITCH_INSTALL_DIR": str(install_dir),
                "CODEX_SWITCH_LIB_DIR": str(layout_root),
                "CODEX_SWITCH_PYTHON": sys.executable,
                "CODEX_SWITCH_SOURCE_TARBALL_URL": (
                    source_archive.as_uri() if source_archive is not None else ""
                ),
                "CODEX_SWITCH_TARBALL_URL": archive.as_uri(),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        cwd = self.root / "piped-cwd" / entrypoint.stem / str(len(args))
        cwd.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            ["bash", "-s", "--", *args],
            check=False,
            cwd=cwd,
            input=entrypoint.read_text(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def _write_source_archive(
        self,
        label: str,
        version: str,
        *,
        malicious_module: str | None = None,
        sentinel: Path | None = None,
    ) -> Path:
        source = self.root / "source-archives" / label
        self._write_source(source, version)
        if malicious_module is not None:
            self.assertIsNotNone(sentinel)
            module_path = source / "scripts" / malicious_module
            module_path.write_text(
                "import os\n"
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('executed\\n')\n"
            )
        archive = self.root / "source-archives" / f"{label}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(source, arcname=f"codex-switch-{label}")
        return archive

    def _self_update_wrapper_text(
        self,
        label: str,
        *,
        health_mode: str = "normal",
    ) -> str:
        lines = (REPO_ROOT / "scripts" / "codex-switch").read_text().splitlines(
            keepends=True
        )
        self.assertTrue(lines and lines[0].startswith("#!"))
        prelude = [f"export CODEX_SWITCH_TEST_RELEASE_LABEL={label!r}\n"]
        if health_mode == "mismatch":
            prelude.extend(
                [
                    'if [[ -n "${CODEX_SWITCH_PROMOTION_RUN_ID:-}" '
                    '&& "${1:-}" == "--version" ]]; then\n',
                    "  printf '999.0.0\\n'\n",
                    "  exit 0\n",
                    "fi\n",
                ]
            )
        elif health_mode == "timeout":
            prelude.extend(
                [
                    'if [[ -n "${CODEX_SWITCH_PROMOTION_RUN_ID:-}" '
                    '&& "${1:-}" == "--version" ]]; then\n',
                    "  sleep 1\n",
                    "fi\n",
                ]
            )
        else:
            self.assertEqual("normal", health_mode)
        return "".join([lines[0], *prelude, *lines[1:]])

    def _write_self_update_command_logger(self, label: str) -> Path:
        logger = self.root / f"{label}-command-logger.py"
        logger.write_text(
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            "\n"
            "log = pathlib.Path(os.environ['CODEX_SWITCH_TEST_COMMAND_LOG'])\n"
            "with log.open('a') as handle:\n"
            "    handle.write(json.dumps({\n"
            "        'label': os.environ.get("
            "'CODEX_SWITCH_TEST_RELEASE_LABEL', '<missing>'),\n"
            "        'args': sys.argv[1:],\n"
            "    }, sort_keys=True) + '\\n')\n"
            "raise SystemExit(int(os.environ.get("
            "'CODEX_SWITCH_TEST_COMMAND_EXIT', '0')))\n"
        )
        return logger

    def _write_self_update_python_shim(self, label: str) -> Path:
        shim = self.root / f"{label}-python-shim"
        shim.write_text(
            f"#!{sys.executable}\n"
            "import os\n"
            "import pathlib\n"
            "import subprocess\n"
            "import sys\n"
            "\n"
            "real_python = os.environ['CODEX_SWITCH_TEST_REAL_PYTHON']\n"
            "args = list(sys.argv[1:])\n"
            "script_index = 1 if args[:1] == ['-B'] else 0\n"
            "is_promotion = bool(\n"
            "    len(args) > script_index\n"
            "    and pathlib.Path(args[script_index]).name == "
            "'codex_switch_promotion.py'\n"
            "    and '--candidate-root' in args\n"
            "    and '--layout-root' in args\n"
            ")\n"
            "mode = os.environ.get('CODEX_SWITCH_TEST_PYTHON_SHIM_MODE', '')\n"
            "if mode == 'timeout' and is_promotion:\n"
            "    insert_at = script_index + 1\n"
            "    args[insert_at:insert_at] = ['--health-timeout', '0.1']\n"
            "result = subprocess.run(\n"
            "    [real_python, *args],\n"
            "    check=False,\n"
            "    text=True,\n"
            "    stdout=subprocess.PIPE,\n"
            "    stderr=subprocess.PIPE,\n"
            "    env=os.environ.copy(),\n"
            ")\n"
            "if mode == 'concurrent' and is_promotion "
            "and result.returncode == 0:\n"
            "    layout = args[args.index('--layout-root') + 1]\n"
            "    concurrent = os.environ["
            "'CODEX_SWITCH_TEST_CONCURRENT_CANDIDATE']\n"
            "    concurrent_result = subprocess.run(\n"
            "        [\n"
            "            real_python,\n"
            "            *args[:script_index + 1],\n"
            "            '--candidate-root',\n"
            "            concurrent,\n"
            "            '--layout-root',\n"
            "            layout,\n"
            "            '--health-timeout',\n"
            "            '1',\n"
            "        ],\n"
            "        check=False,\n"
            "        text=True,\n"
            "        stdout=subprocess.PIPE,\n"
            "        stderr=subprocess.PIPE,\n"
            "        env=os.environ.copy(),\n"
            "    )\n"
            "    if concurrent_result.returncode != 0:\n"
            "        sys.stderr.write(concurrent_result.stderr)\n"
            "        raise SystemExit(91)\n"
            "sys.stdout.write(result.stdout)\n"
            "sys.stderr.write(result.stderr)\n"
            "raise SystemExit(result.returncode)\n"
        )
        shim.chmod(0o755)
        return shim

    def _self_update_python_runtime(self) -> str:
        if sys.version_info >= (3, 11):
            return sys.executable
        runtime = shutil.which("python3.12") or shutil.which("python3.11")
        self.assertIsNotNone(
            runtime,
            "self-update adapter tests require a Python 3.11+ CLI runtime",
        )
        return str(runtime)

    def _prepare_self_update_layout(self, label: str) -> Path:
        layout_root = self.root / "self-update-layouts" / label
        rollback_root, _ = self._build_candidate(
            f"{label}-rollback",
            "0.9.0",
        )
        prior_root, _ = self._build_candidate(
            f"{label}-prior",
            "1.0.0",
            command_text=self._self_update_wrapper_text("prior"),
        )
        self._promote(rollback_root, layout_root, f"{label}-rollback")
        self._promote(prior_root, layout_root, f"{label}-prior")
        return layout_root

    def _run_self_update(
        self,
        label: str,
        layout_root: Path,
        candidate_root: Path | None,
        *,
        expected_version: str | None,
        command_exit: int = 0,
        python_shim_mode: str = "",
        concurrent_candidate: Path | None = None,
        env_overrides: Dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        logger = self._write_self_update_command_logger(label)
        command_log = self.root / f"{label}-command.log"
        home = self.root / f"{label}-home"
        home.mkdir()
        env = os.environ.copy()
        env.pop("CODEX_SWITCH_SKIP_SELF_UPDATE", None)
        env.pop("CODEX_SWITCH_SELF_UPDATE_REEXECED", None)
        env.pop("CODEX_SWITCH_SOURCE_DIR", None)
        env.pop("CODEX_SWITCH_SELF_UPDATE_VERSION", None)
        env.pop("CODEX_SWITCH_SELF_UPDATE_TARBALL_URL", None)
        env.pop("CODEX_SWITCH_TARBALL_URL", None)
        env.pop("CODEX_SWITCH_SELF_UPDATE_SOURCE_TARBALL_URL", None)
        env.pop("CODEX_SWITCH_SOURCE_TARBALL_URL", None)
        python_runtime = self._self_update_python_runtime()
        env.update(
            {
                "HOME": str(home),
                "CODEX_SWITCH_HOME": str(self.root / f"{label}-store"),
                "CODEX_SWITCH_LIB_DIR": str(layout_root),
                "CODEX_SWITCH_SCRIPT": str(logger),
                "CODEX_SWITCH_TEST_COMMAND_LOG": str(command_log),
                "CODEX_SWITCH_TEST_COMMAND_EXIT": str(command_exit),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if candidate_root is not None:
            env["CODEX_SWITCH_SOURCE_DIR"] = str(candidate_root)
        if expected_version is not None:
            env["CODEX_SWITCH_SELF_UPDATE_VERSION"] = expected_version
        if python_shim_mode:
            shim = self._write_self_update_python_shim(label)
            env.update(
                {
                    "CODEX_SWITCH_PYTHON": str(shim),
                    "CODEX_SWITCH_TEST_REAL_PYTHON": python_runtime,
                    "CODEX_SWITCH_TEST_PYTHON_SHIM_MODE": python_shim_mode,
                }
            )
        else:
            env["CODEX_SWITCH_PYTHON"] = python_runtime
        if concurrent_candidate is not None:
            env["CODEX_SWITCH_TEST_CONCURRENT_CANDIDATE"] = str(
                concurrent_candidate
            )
        if env_overrides is not None:
            env.update(env_overrides)
        result = subprocess.run(
            [
                str(layout_root / "current" / "scripts" / "codex-switch"),
                "status",
                "--adapter-probe",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return result, command_log

    def _read_self_update_command_log(self, path: Path) -> list[dict[str, object]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def _assert_failure_preserves_refs(
        self,
        label: str,
        archive: Path,
        *,
        make_releases_read_only: bool = False,
    ) -> None:
        for entrypoint_label, entrypoint, args in (
            ("installer", INSTALLER, ()),
            ("runner", REMOTE_RUNNER, ("status",)),
        ):
            with self.subTest(
                failure=label,
                entrypoint=entrypoint_label,
            ):
                layout_root = self._prepare_prior_layout(
                    f"{label}-{entrypoint_label}"
                )
                install_dir = self.root / "bin" / label / entrypoint_label
                before = self._reference_snapshot(layout_root)
                releases_dir = layout_root / "releases"
                if make_releases_read_only:
                    releases_dir.chmod(0o500)
                try:
                    result = self._run_entrypoint(
                        entrypoint,
                        archive,
                        layout_root,
                        install_dir,
                        args,
                    )
                finally:
                    if make_releases_read_only:
                        releases_dir.chmod(0o700)

                self.assertNotEqual(
                    0,
                    result.returncode,
                    result.stdout + result.stderr,
                )
                self.assertEqual(before, self._reference_snapshot(layout_root))
                self.assertEqual(
                    "1.0.0",
                    (layout_root / "current" / "VERSION").read_text().strip(),
                )

    def test_copy_failure_is_explicit_and_preserves_current_and_rollback(
        self,
    ) -> None:
        _, archive = self._build_candidate("copy-failure", "2.0.0")

        self._assert_failure_preserves_refs(
            "copy-failure",
            archive,
            make_releases_read_only=True,
        )

    def test_import_failure_is_explicit_and_preserves_current_and_rollback(
        self,
    ) -> None:
        candidate_root, archive = self._build_candidate(
            "import-failure",
            "2.0.0",
        )
        (candidate_root / "scripts" / "helper.py").write_text(
            "import codex_switch_missing_adapter_dependency\n"
        )
        self.bundle_module._create_manifest(candidate_root, "2.0.0")
        self.bundle_module._create_archive(candidate_root, archive)

        self._assert_failure_preserves_refs("import-failure", archive)

    def test_syntax_failure_is_explicit_and_preserves_current_and_rollback(
        self,
    ) -> None:
        _, archive = self._build_candidate(
            "syntax-failure",
            "2.0.0",
            runner_text="#!/usr/bin/env bash\nif then\n",
        )

        self._assert_failure_preserves_refs("syntax-failure", archive)

    def test_smoke_failure_is_explicit_and_preserves_current_and_rollback(
        self,
    ) -> None:
        _, archive = self._build_candidate(
            "smoke-failure",
            "2.0.0",
            command_text="#!/usr/bin/env bash\nexit 23\n",
        )

        self._assert_failure_preserves_refs("smoke-failure", archive)

    def test_health_version_mismatch_rolls_back_and_preserves_refs(
        self,
    ) -> None:
        _, archive = self._build_candidate(
            "health-version-mismatch",
            "2.0.0",
            command_text="#!/usr/bin/env bash\nprintf '0.0.0\\n'\n",
        )

        self._assert_failure_preserves_refs(
            "health-version-mismatch",
            archive,
        )

    def test_archive_mktemp_failure_stops_before_curl_and_preserves_refs(
        self,
    ) -> None:
        _, archive = self._build_candidate("mktemp-failure", "2.0.0")

        for entrypoint_label, entrypoint, args in (
            ("installer", INSTALLER, ()),
            ("runner", REMOTE_RUNNER, ("status",)),
        ):
            with self.subTest(entrypoint=entrypoint_label):
                layout_root = self._prepare_prior_layout(
                    f"mktemp-failure-{entrypoint_label}"
                )
                before = self._reference_snapshot(layout_root)
                fake_bin = self.root / "fake-bin" / entrypoint_label
                fake_bin.mkdir(parents=True)
                state_path = self.root / f"{entrypoint_label}-mktemp-state"
                curl_path = self.root / f"{entrypoint_label}-curl-called"
                fake_mktemp = fake_bin / "mktemp"
                fake_mktemp.write_text(
                    "#!/bin/sh\n"
                    'if [ ! -f "$CODEX_SWITCH_MKTEMP_STATE" ]; then\n'
                    '  : > "$CODEX_SWITCH_MKTEMP_STATE"\n'
                    '  exec /usr/bin/mktemp "$@"\n'
                    "fi\n"
                    "exit 17\n"
                )
                fake_mktemp.chmod(0o755)
                fake_curl = fake_bin / "curl"
                fake_curl.write_text(
                    "#!/bin/sh\n"
                    ': > "$CODEX_SWITCH_CURL_CALLED"\n'
                    "exit 18\n"
                )
                fake_curl.chmod(0o755)
                path = f"{fake_bin}:{os.environ['PATH']}"

                result = self._run_entrypoint(
                    entrypoint,
                    archive,
                    layout_root,
                    self.root / "bin" / entrypoint_label,
                    args,
                    env_overrides={
                        "CODEX_SWITCH_CURL_CALLED": str(curl_path),
                        "CODEX_SWITCH_MKTEMP_STATE": str(state_path),
                        "PATH": path,
                    },
                )

                self.assertNotEqual(0, result.returncode)
                self.assertFalse(curl_path.exists())
                self.assertEqual(before, self._reference_snapshot(layout_root))

    def test_piped_entrypoints_bootstrap_only_hash_bound_modules(self) -> None:
        _, archive = self._build_candidate("piped-bootstrap", "2.0.0")

        for entrypoint_label, entrypoint, args in (
            ("installer", INSTALLER, ()),
            ("runner", REMOTE_RUNNER, ("status",)),
        ):
            with self.subTest(entrypoint=entrypoint_label):
                layout_root = self.root / "piped-layouts" / entrypoint_label
                result = self._run_piped_entrypoint(
                    entrypoint,
                    archive,
                    layout_root,
                    self.root / "piped-bin" / entrypoint_label,
                    args,
                )

                self.assertEqual(
                    0,
                    result.returncode,
                    result.stdout + result.stderr,
                )
                self.assertTrue((layout_root / "current").is_symlink())
                self.assertEqual(
                    "2.0.0",
                    (layout_root / "current" / "VERSION").read_text().strip(),
                )
                self.assertFalse(
                    any(
                        path.name.startswith(".install-candidate.")
                        or path.name.startswith(".run-candidate.")
                        for path in layout_root.iterdir()
                    )
                )

    def test_piped_entrypoints_reject_malicious_bootstrap_modules(self) -> None:
        missing_archive = self.root / "missing-release.tar.gz"

        for malicious_module in (
            "codex_switch_release_bundle.py",
            "codex_switch_promotion.py",
        ):
            for entrypoint_label, entrypoint, args in (
                ("installer", INSTALLER, ()),
                ("runner", REMOTE_RUNNER, ("status",)),
            ):
                with self.subTest(
                    module=malicious_module,
                    entrypoint=entrypoint_label,
                ):
                    sentinel = (
                        self.root
                        / "malicious-sentinels"
                        / f"{malicious_module}-{entrypoint_label}"
                    )
                    sentinel.parent.mkdir(parents=True, exist_ok=True)
                    source_archive = self._write_source_archive(
                        f"{Path(malicious_module).stem}-{entrypoint_label}",
                        "2.0.0",
                        malicious_module=malicious_module,
                        sentinel=sentinel,
                    )
                    layout_root = (
                        self.root
                        / "malicious-layouts"
                        / Path(malicious_module).stem
                        / entrypoint_label
                    )
                    result = self._run_piped_entrypoint(
                        entrypoint,
                        missing_archive,
                        layout_root,
                        self.root / "malicious-bin" / entrypoint_label,
                        args,
                        source_archive=source_archive,
                    )

                    self.assertNotEqual(
                        0,
                        result.returncode,
                        result.stdout + result.stderr,
                    )
                    self.assertNotIn(
                        "BASH_SOURCE[0]: unbound variable",
                        result.stderr,
                    )
                    self.assertIn(
                        "promotion modules are unavailable",
                        result.stderr,
                    )
                    self.assertFalse(sentinel.exists())
                    self.assertFalse(
                        os.path.lexists(str(layout_root / "current"))
                    )

    def test_installer_and_runner_migrate_historical_unmanifested_legacy_current(
        self,
    ) -> None:
        _, archive = self._build_candidate("legacy-upgrade", "2.0.0")

        for entrypoint_label, entrypoint, args in (
            ("installer", INSTALLER, ()),
            ("runner", REMOTE_RUNNER, ("status",)),
        ):
            with self.subTest(entrypoint=entrypoint_label):
                layout_root = self.root / "legacy-layouts" / entrypoint_label
                self._write_source(
                    layout_root / "current",
                    "1.0.0",
                )
                for module_name in (
                    "codex_switch_release_bundle.py",
                    "codex_switch_promotion.py",
                    "codex_switch_update_policy.py",
                    "codex_switch_official_release.py",
                ):
                    (layout_root / "current" / "scripts" / module_name).unlink()
                install_dir = self.root / "legacy-bin" / entrypoint_label

                result = self._run_entrypoint(
                    entrypoint,
                    archive,
                    layout_root,
                    install_dir,
                    args,
                )

                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue((layout_root / "current").is_symlink())
                self.assertTrue((layout_root / "rollback").is_symlink())
                self.assertEqual(
                    "2.0.0",
                    (layout_root / "current" / "VERSION").read_text().strip(),
                )
                self.assertEqual(
                    "1.0.0",
                    (layout_root / "rollback" / "VERSION").read_text().strip(),
                )
                self.assertFalse(
                    any(
                        path.name.startswith(".legacy-current-")
                        or path.name.startswith(".legacy-canonical-")
                        for path in layout_root.iterdir()
                    )
                )
                if entrypoint_label == "installer":
                    self.assertTrue((install_dir / "codex-switch").is_symlink())

    def test_installer_upgrades_supported_historical_manifest_v1_current(
        self,
    ) -> None:
        _, archive = self._build_candidate(
            "historical-manifest-v1-upgrade",
            "2.0.0",
        )
        layout_root = self._prepare_prior_layout(
            "historical-manifest-v1-upgrade"
        )
        current_release = (
            layout_root / os.readlink(layout_root / "current")
        ).resolve()
        manifest_path = current_release / "bundle-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        historical_required_paths = [
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
            "bundle-manifest.json",
        ]
        manifest["required_paths"] = historical_required_paths
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        historical_snapshot = filesystem_snapshot(current_release)
        install_dir = self.root / "historical-manifest-v1-bin"

        result = self._run_entrypoint(
            INSTALLER,
            archive,
            layout_root,
            install_dir,
            (),
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "2.0.0",
            (layout_root / "current" / "VERSION").read_text().strip(),
        )
        self.assertEqual(
            "1.0.0",
            (layout_root / "rollback" / "VERSION").read_text().strip(),
        )
        self.assertEqual(
            historical_snapshot,
            filesystem_snapshot((layout_root / "rollback").resolve()),
        )
        self.assertEqual(
            historical_required_paths,
            json.loads(
                (layout_root / "rollback" / "bundle-manifest.json").read_text()
            )["required_paths"],
        )
        self.assertTrue((install_dir / "codex-switch").is_symlink())

    def test_installer_upgrades_immediately_prior_twenty_path_manifests(
        self,
    ) -> None:
        _, archive = self._build_candidate(
            "immediately-prior-manifest-upgrade",
            "2.0.0",
        )
        layout_root = self._prepare_prior_layout(
            "immediately-prior-manifest-upgrade"
        )
        prior_required_paths = [
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
            "bundle-manifest.json",
        ]
        for reference in ("current", "rollback"):
            release = (layout_root / os.readlink(layout_root / reference)).resolve()
            manifest_path = release / "bundle-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["required_paths"] = prior_required_paths
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
        previous_current = (layout_root / "current").resolve()
        previous_current_snapshot = filesystem_snapshot(previous_current)
        install_dir = self.root / "immediately-prior-manifest-bin"

        result = self._run_entrypoint(
            INSTALLER,
            archive,
            layout_root,
            install_dir,
            (),
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            "2.0.0",
            (layout_root / "current" / "VERSION").read_text().strip(),
        )
        self.assertEqual(
            "1.0.0",
            (layout_root / "rollback" / "VERSION").read_text().strip(),
        )
        self.assertEqual(
            previous_current_snapshot,
            filesystem_snapshot((layout_root / "rollback").resolve()),
        )
        self.assertEqual(
            prior_required_paths,
            json.loads(
                (layout_root / "rollback" / "bundle-manifest.json").read_text()
            )["required_paths"],
        )
        self.assertTrue((install_dir / "codex-switch").is_symlink())

    def test_runner_preserves_signal_exit_status_after_promotion(self) -> None:
        _, archive = self._build_candidate(
            "signal-exit",
            "2.0.0",
            command_text=(
                "#!/usr/bin/env bash\n"
                'if [[ "${1:-}" == "--version" ]]; then\n'
                "  printf '2.0.0\\n'\n"
                "  exit 0\n"
                "fi\n"
                'kill -TERM "$$"\n'
            ),
        )
        layout_root = self._prepare_prior_layout("signal-exit")

        result = self._run_entrypoint(
            REMOTE_RUNNER,
            archive,
            layout_root,
            self.root / "signal-bin",
            ("status",),
        )

        self.assertEqual(143, result.returncode)
        self.assertEqual(
            "2.0.0",
            (layout_root / "current" / "VERSION").read_text().strip(),
        )
        self.assertEqual(
            "1.0.0",
            (layout_root / "rollback" / "VERSION").read_text().strip(),
        )

    def test_self_update_rejects_invalid_structure_before_ref_change(self) -> None:
        layout_root = self._prepare_self_update_layout("invalid-structure")
        candidate_root, _ = self._build_candidate(
            "invalid-structure-candidate",
            "2.0.0",
            command_text=self._self_update_wrapper_text("candidate"),
        )
        (candidate_root / "scripts" / "codex_switch_promotion.py").unlink()
        before = self._reference_snapshot(layout_root)

        result, command_log = self._run_self_update(
            "invalid-structure",
            layout_root,
            candidate_root,
            expected_version="2.0.0",
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self._reference_snapshot(layout_root))
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "prior"}],
            self._read_self_update_command_log(command_log),
        )
        self.assertIn("sync failed; continuing", result.stderr)

    def test_self_update_same_version_skips_malformed_legacy_candidate(
        self,
    ) -> None:
        label = "same-version-legacy"
        layout_root = self._prepare_self_update_layout(label)
        candidate_root = self.root / "sources" / f"{label}-candidate"
        self._write_source(
            candidate_root,
            "1.0.0",
            command_text=self._self_update_wrapper_text("candidate"),
        )
        (candidate_root / "scripts" / "codex_switch_release_bundle.py").unlink()
        before = self._reference_snapshot(layout_root)

        result, command_log = self._run_self_update(
            label,
            layout_root,
            candidate_root,
            expected_version="1.0.0",
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self._reference_snapshot(layout_root))
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "prior"}],
            self._read_self_update_command_log(command_log),
        )
        self.assertIn(
            "codex-switch self-update: already up to date 1.0.0",
            result.stderr,
        )
        self.assertNotIn("source_invalid", result.stderr)
        self.assertNotIn("sync failed; continuing", result.stderr)
        self.assertFalse(
            any(path.name.startswith(".self-update.") for path in layout_root.iterdir())
        )

    def test_self_update_older_version_skips_malformed_legacy_candidate(
        self,
    ) -> None:
        label = "older-version-legacy"
        layout_root = self._prepare_self_update_layout(label)
        candidate_root = self.root / "sources" / f"{label}-candidate"
        self._write_source(
            candidate_root,
            "0.8.0",
            command_text=self._self_update_wrapper_text("candidate"),
        )
        (candidate_root / "scripts" / "codex_switch_release_bundle.py").unlink()
        before = self._reference_snapshot(layout_root)

        result, command_log = self._run_self_update(
            label,
            layout_root,
            candidate_root,
            expected_version="0.8.0",
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self._reference_snapshot(layout_root))
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "prior"}],
            self._read_self_update_command_log(command_log),
        )
        self.assertIn(
            "codex-switch self-update: already up to date 1.0.0",
            result.stderr,
        )
        self.assertNotIn("source_invalid", result.stderr)
        self.assertNotIn("sync failed; continuing", result.stderr)
        self.assertFalse(
            any(path.name.startswith(".self-update.") for path in layout_root.iterdir())
        )

    def test_self_update_default_latest_same_version_stops_before_download(
        self,
    ) -> None:
        label = "default-latest-same-version"
        layout_root = self._prepare_self_update_layout(label)
        fake_bin = self.root / f"{label}-bin"
        fake_bin.mkdir()
        download_called = self.root / f"{label}-download-called"
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env bash\n"
            'url="${!#}"\n'
            'if [[ "$url" == '
            '"https://github.com/cYz26/codex-switch/releases/latest" ]]; then\n'
            "  printf '%s\\n' 'HTTP/2 302'\n"
            "  printf '%s\\n' "
            "'location: https://github.com/cYz26/codex-switch/releases/tag/v1.0.0'\n"
            "  exit 0\n"
            "fi\n"
            f"touch {str(download_called)!r}\n"
            "exit 97\n"
        )
        fake_curl.chmod(0o755)
        before = self._reference_snapshot(layout_root)

        result, command_log = self._run_self_update(
            label,
            layout_root,
            None,
            expected_version=None,
            env_overrides={
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            },
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self._reference_snapshot(layout_root))
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "prior"}],
            self._read_self_update_command_log(command_log),
        )
        self.assertFalse(download_called.exists())
        self.assertIn(
            "codex-switch self-update: already up to date 1.0.0",
            result.stderr,
        )
        self.assertNotIn("source_invalid", result.stderr)
        self.assertNotIn("sync failed; continuing", result.stderr)
        self.assertFalse(
            any(path.name.startswith(".self-update.") for path in layout_root.iterdir())
        )

    def test_self_update_rejects_expected_version_before_ref_change(self) -> None:
        layout_root = self._prepare_self_update_layout("version-mismatch")
        candidate_root, _ = self._build_candidate(
            "version-mismatch-candidate",
            "2.0.0",
            command_text=self._self_update_wrapper_text("candidate"),
        )
        before = self._reference_snapshot(layout_root)

        result, command_log = self._run_self_update(
            "version-mismatch",
            layout_root,
            candidate_root,
            expected_version="2.1.0",
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self._reference_snapshot(layout_root))
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "prior"}],
            self._read_self_update_command_log(command_log),
        )
        self.assertIn("sync failed; continuing", result.stderr)

    def test_self_update_handshake_mismatch_rolls_back_before_command(self) -> None:
        layout_root = self._prepare_self_update_layout("handshake-mismatch")
        candidate_root, _ = self._build_candidate(
            "handshake-mismatch-candidate",
            "2.0.0",
            command_text=self._self_update_wrapper_text(
                "candidate",
                health_mode="mismatch",
            ),
        )
        candidate = self.promotion_module.validate_candidate(
            candidate_root,
            expected_version="2.0.0",
        )
        before = self._reference_snapshot(layout_root)

        result, command_log = self._run_self_update(
            "handshake-mismatch",
            layout_root,
            candidate_root,
            expected_version="2.0.0",
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self._reference_snapshot(layout_root))
        self.assertTrue(
            (layout_root / "releases" / candidate.digest).is_dir()
        )
        state = json.loads((layout_root / "promotion-state.json").read_text())
        self.assertEqual("rolled_back", state["outcome"])
        self.assertEqual("handshake_mismatch", state["failure_reason"])
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "prior"}],
            self._read_self_update_command_log(command_log),
        )

    def test_self_update_handshake_timeout_rolls_back_before_command(self) -> None:
        layout_root = self._prepare_self_update_layout("handshake-timeout")
        candidate_root, _ = self._build_candidate(
            "handshake-timeout-candidate",
            "2.0.0",
            command_text=self._self_update_wrapper_text(
                "candidate",
                health_mode="timeout",
            ),
        )
        candidate = self.promotion_module.validate_candidate(
            candidate_root,
            expected_version="2.0.0",
        )
        before = self._reference_snapshot(layout_root)

        result, command_log = self._run_self_update(
            "handshake-timeout",
            layout_root,
            candidate_root,
            expected_version="2.0.0",
            python_shim_mode="timeout",
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self._reference_snapshot(layout_root))
        self.assertTrue(
            (layout_root / "releases" / candidate.digest).is_dir()
        )
        state = json.loads((layout_root / "promotion-state.json").read_text())
        self.assertEqual("rolled_back", state["outcome"])
        self.assertEqual("health_timeout", state["failure_reason"])
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "prior"}],
            self._read_self_update_command_log(command_log),
        )

    def test_self_update_replays_receipt_root_after_concurrent_promotion(
        self,
    ) -> None:
        layout_root = self._prepare_self_update_layout("concurrent-promotion")
        candidate_root, _ = self._build_candidate(
            "concurrent-primary",
            "2.0.0",
            command_text=self._self_update_wrapper_text("candidate"),
        )
        concurrent_root, _ = self._build_candidate(
            "concurrent-secondary",
            "3.0.0",
            command_text=self._self_update_wrapper_text("concurrent"),
        )
        candidate = self.promotion_module.validate_candidate(
            candidate_root,
            expected_version="2.0.0",
        )
        concurrent = self.promotion_module.validate_candidate(
            concurrent_root,
            expected_version="3.0.0",
        )

        result, command_log = self._run_self_update(
            "concurrent-promotion",
            layout_root,
            candidate_root,
            expected_version="2.0.0",
            python_shim_mode="concurrent",
            concurrent_candidate=concurrent_root,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue((layout_root / "current").is_symlink())
        self.assertTrue((layout_root / "rollback").is_symlink())
        self.assertEqual(
            f"releases/{concurrent.digest}",
            os.readlink(layout_root / "current"),
        )
        self.assertEqual(
            f"releases/{candidate.digest}",
            os.readlink(layout_root / "rollback"),
        )
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "candidate"}],
            self._read_self_update_command_log(command_log),
        )

    def test_self_update_successful_handshake_replays_nonzero_command_once(
        self,
    ) -> None:
        layout_root = self._prepare_self_update_layout("exactly-once")
        candidate_root, _ = self._build_candidate(
            "exactly-once-candidate",
            "2.0.0",
            command_text=self._self_update_wrapper_text("candidate"),
        )
        candidate = self.promotion_module.validate_candidate(
            candidate_root,
            expected_version="2.0.0",
        )
        prior_digest = os.readlink(layout_root / "current").split("/", 1)[1]

        result, command_log = self._run_self_update(
            "exactly-once",
            layout_root,
            candidate_root,
            expected_version="2.0.0",
            command_exit=17,
        )

        self.assertEqual(17, result.returncode, result.stdout + result.stderr)
        self.assertTrue((layout_root / "current").is_symlink())
        self.assertTrue((layout_root / "rollback").is_symlink())
        self.assertEqual(
            f"releases/{candidate.digest}",
            os.readlink(layout_root / "current"),
        )
        self.assertEqual(
            f"releases/{prior_digest}",
            os.readlink(layout_root / "rollback"),
        )
        self.assertEqual(
            [{"args": ["status", "--adapter-probe"], "label": "candidate"}],
            self._read_self_update_command_log(command_log),
        )
        self.assertNotIn("sync failed; continuing", result.stderr)


class CodexStandaloneRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Register the fixture lifetime before any installer process is started.
        self.fixture = CodexStagedInternalUpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.f = self.fixture

    def write_installer(self, mutation: str = "") -> None:
        runtime = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
root = pathlib.Path(__file__).resolve().parent.parent
assert subprocess.check_output([str(root / "bin/codex-code-mode-host")], text=True).strip() == "host-ok"
assert subprocess.check_output([str(root / "codex-path/rg")], text=True).strip() == "rg-ok"
assert (root / "codex-resources/data.txt").read_text() == "resource-ok"
if sys.argv[1:] == ["--version"]:
    print("codex-cli 2.0.0")
else:
    print(json.dumps({"args": sys.argv[1:], "home": os.environ["HOME"], "codex_home": os.environ.get("CODEX_HOME"), "root": str(root)}))
'''
        script = '''#!/usr/bin/env bash
set -euo pipefail
"$CODEX_SWITCH_PYTHON" -I -B - <<'PY'
import json, os, pathlib
home = pathlib.Path(os.environ["CODEX_HOME"])
package = home / "packages/standalone/releases/2.0.0-aarch64-apple-darwin"
package.mkdir(parents=True)
files = FILES
for name, content in files.items():
    path = package / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755 if name.startswith(("bin/", "codex-path/")) else 0o644)
(package / "codex").symlink_to("bin/codex")
(package.parent.parent / "current").symlink_to(package)
candidate = pathlib.Path(os.environ["CODEX_INSTALL_DIR"]) / "codex"
candidate.symlink_to(package.parent.parent / "current/bin/codex")
(home / "config.toml").write_text("private-installer-config")
(home / "auth.json").write_text("private-installer-credential")
with open(os.environ["CODEX_TEST_EVENTS"], "a") as out:
    out.write("scratch|" + str(home.parent) + "\\n")
    out.write("noninteractive|" + os.environ.get("CODEX_NON_INTERACTIVE", "") + "\\n")
MUTATION
PY
'''
        files = {
            "bin/codex": runtime,
            "bin/codex-code-mode-host": "#!/bin/sh\nprintf 'host-ok\\n'\n",
            "codex-path/rg": "#!/bin/sh\nprintf 'rg-ok\\n'\n",
            "codex-resources/data.txt": "resource-ok",
            "codex-package.json": '{"version":"2.0.0"}',
        }
        self.f._write_script(
            self.f.installer_script,
            script.replace("FILES", repr(files)).replace("MUTATION", mutation),
        )

    def stage(self, label: str = "standalone") -> tuple[Path, subprocess.CompletedProcess[str]]:
        candidate = self.f._candidate_dir(label)
        return candidate / "codex", self.f._run_env_setup(candidate)

    def run_candidate(self, candidate: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(candidate), *args], env=self.f.base_env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_complete_package_survives_private_installer_cleanup(self) -> None:
        self.write_installer()
        before = self.f._bound_snapshot()
        candidate, result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(candidate.is_symlink())
        events = self.f.events.read_text().splitlines()
        scratch = Path(next(line.split("|", 1)[1] for line in events if line.startswith("scratch|")))
        self.assertFalse(scratch.exists())
        run = self.run_candidate(candidate, "--version")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "codex-cli 2.0.0")
        self.assertEqual(self.f._bound_snapshot(), before)

    def test_rejects_directory_in_place_of_package_metadata(self) -> None:
        self.write_installer('(package / "codex-package.json").unlink()\n(package / "codex-package.json").mkdir()')
        before = self.f._bound_snapshot()
        _, result = self.stage()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.f._bound_snapshot(), before)

    def test_noninteractive_install_does_not_retain_private_state(self) -> None:
        self.write_installer('assert os.environ.get("CODEX_NON_INTERACTIVE") == "1"')
        candidate, result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("noninteractive|1", self.f.events.read_text())
        for path in self.f.install_root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"private-installer-", path.read_bytes(), str(path))
        signed = [line for line in self.f.events.read_text().splitlines() if line.startswith("codesign:")]
        self.assertEqual(len(signed), 3, signed)
        self.assertFalse(any(line.endswith(str(candidate)) for line in signed), signed)

    def test_arguments_and_caller_environment_survive_relocation(self) -> None:
        moved = self.f.install_root.with_name("bin with 'quotes and spaces")
        self.f.install_root.rename(moved)
        self.f.install_root = moved
        self.f.bound_bin = moved / "codex"
        self.f.base_env["CODEX_TEST_BOUND_BIN"] = str(self.f.bound_bin)
        self.f.base_env["CODEX_HOME"] = str(self.f.root / "caller-home")
        self.write_installer()
        candidate, result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = ("exec", "a b", "'quoted'", "$(literal)", "line\nbreak")
        run = self.run_candidate(candidate, *args)
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["args"], list(args))
        self.assertEqual(payload["home"], str(self.f.home))
        self.assertEqual(payload["codex_home"], self.f.base_env["CODEX_HOME"])

    def test_rejects_unsafe_or_incomplete_package_before_activation(self) -> None:
        mutations = [
            '(package / "bin/codex-code-mode-host").unlink()',
            '(package / "codex-path/rg").unlink()',
            '(package / "codex-resources/auth.json").symlink_to(home / "auth.json")',
            '(package / "config.toml").write_text("private-config")',
            'candidate.unlink(); candidate.symlink_to(os.environ["CODEX_TEST_BOUND_BIN"])',
            '(package / "bin/codex-code-mode-host").chmod(0o644)',
        ]
        before = self.f._bound_snapshot()
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                self.write_installer(mutation)
                _, result = self.stage("unsafe-" + str(index))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Standalone runtime preparation failed", result.stderr)
                self.assertEqual(self.f._bound_snapshot(), before)

    def test_installer_failure_status_and_scratch_cleanup(self) -> None:
        self.write_installer("raise SystemExit(17)")
        before = self.f._bound_snapshot()
        _, result = self.stage()
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertFalse((self.f.install_root / ".codex-internal-runtimes").exists())
        scratch = Path(next(line.split("|", 1)[1] for line in self.f.events.read_text().splitlines() if line.startswith("scratch|")))
        self.assertFalse(scratch.exists())
        self.assertEqual(self.f._bound_snapshot(), before)

    def test_signing_failure_preserves_bound_command(self) -> None:
        self.write_installer()
        self.f._write_script(self.f.fake_bin / "codesign", "#!/bin/sh\nexit 29\n")
        before = self.f._bound_snapshot()
        _, result = self.stage()
        self.assertEqual(result.returncode, 29, result.stdout + result.stderr)
        self.assertEqual(self.f._bound_snapshot(), before)

    def test_runtime_tampering_fails_before_executing_payload(self) -> None:
        self.write_installer()
        candidate, result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        generation = Path(json.loads(self.run_candidate(candidate, "locate").stdout)["root"])
        for name in ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg", "codex-resources/data.txt"):
            path = generation / name
            original = path.read_bytes()
            with self.subTest(file=name):
                path.write_bytes(original + b"tampered")
                run = self.run_candidate(candidate, "--version")
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("runtime manifest mismatch", run.stderr)
                path.write_bytes(original)
        extra = generation / "extra"
        extra.write_text("extra-file")
        self.assertNotEqual(self.run_candidate(candidate, "--version").returncode, 0)
        extra.unlink()
        helper = generation / "codex-path/rg"
        helper.chmod(0o644)
        self.assertNotEqual(self.run_candidate(candidate, "--version").returncode, 0)
        helper.chmod(0o755)
        original = helper.read_bytes()
        helper.unlink()
        helper.symlink_to(self.f.bound_bin)
        self.assertNotEqual(self.run_candidate(candidate, "--version").returncode, 0)
        helper.unlink()
        helper.write_bytes(original)
        helper.chmod(0o755)
        self.assertEqual(self.run_candidate(candidate, "--version").returncode, 0)

    def test_reuses_identical_generation_and_preserves_foreign_content(self) -> None:
        self.write_installer()
        first, result = self.stage("first")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second, result = self.stage("second")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        generation = Path(json.loads(self.run_candidate(first, "locate").stdout)["root"])
        extra = generation / "foreign"
        extra.write_text("retain-me")
        _, result = self.stage("third")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(extra.read_text(), "retain-me")

    def promotion(self, candidate: Path, store: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.f.base_env["CODEX_SWITCH_PYTHON"], str(PROFILE_SWITCH_MODULE_PATH),
             "--store-dir", str(store), "--official-codex-home", str(self.f.root / "official-home"),
             "--internal-codex-home", str(store / "homes/internal"),
             "--launch-agent-path", str(self.f.root / "agent.plist"), "promote-internal-update",
             "--bound-bin", str(self.f.bound_bin), "--candidate-bin", str(candidate),
             "--backup-bin", str(self.f.install_root / ".codex-internal-backup-test"),
             "--target-version", "2.0.0", "--cli-only"],
            env=self.f.base_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )

    def test_cli_only_promotion_keeps_stable_command_after_candidate_cleanup(self) -> None:
        store = self.f._write_internal_manifest()
        self.write_installer()
        candidate, result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.promotion(candidate, store)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        candidate.parent.rmdir()
        run = self.run_candidate(self.f.bound_bin, "--version")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "codex-cli 2.0.0")

    def test_cli_only_postcondition_failure_restores_prior_command_and_manifest(self) -> None:
        store = self.f._write_internal_manifest()
        manifest_path = store / "profiles/internal/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["codex_home"] = "invalid-relative-home"
        manifest_path.write_text(json.dumps(manifest))
        before_manifest = manifest_path.read_bytes()
        before_bound = self.f._bound_snapshot()
        self.write_installer()
        candidate, result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.promotion(candidate, store)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CLI CODEX_HOME path is not absolute", result.stdout + result.stderr)
        self.assertEqual(self.f._bound_snapshot(), before_bound)
        self.assertEqual(manifest_path.read_bytes(), before_manifest)
        self.assertEqual(self.run_candidate(candidate, "--version").returncode, 0)

    def test_tampered_package_is_rejected_before_transactional_promotion(self) -> None:
        store = self.f._write_internal_manifest()
        before_bound = self.f._bound_snapshot()
        before_manifest = (store / "profiles/internal/manifest.json").read_bytes()
        self.write_installer()
        candidate, result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        generation = Path(json.loads(self.run_candidate(candidate, "locate").stdout)["root"])
        (generation / "codex-path/rg").write_text("tampered")
        result = self.promotion(candidate, store)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.f._bound_snapshot(), before_bound)
        self.assertEqual((store / "profiles/internal/manifest.json").read_bytes(), before_manifest)

    def test_raw_and_legacy_standalone_layouts(self) -> None:
        self.write_installer('(package / "codex-package.json").unlink()')
        candidate, result = self.stage("raw")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.run_candidate(candidate, "--version").returncode, 0)
        self.write_installer('''import shutil
(package / "codex").unlink()
(package / "codex").write_text("#!/bin/sh\\nprintf 'codex-cli 2.0.0\\\\n'\\n")
(package / "codex").chmod(0o755)
(package / "codex-path/rg").rename(package / "codex-resources/rg")
shutil.rmtree(package / "bin")
shutil.rmtree(package / "codex-path")
(package / "codex-package.json").unlink()
candidate.unlink()
candidate.symlink_to(package / "codex")''')
        candidate, result = self.stage("legacy-package")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.run_candidate(candidate, "--version").returncode, 0)



    def test_full_bundle_transaction_promotes_and_rolls_back_standalone_runtime(self) -> None:
        import_path = mock.patch.object(sys, "path", [str(REPO_ROOT / "scripts"), *sys.path])
        import_path.start()
        self.addCleanup(import_path.stop)
        import test_codex_transaction as transaction_support
        from codex_switch_constants import SwitchError
        from codex_switch_transaction import (
            RuntimeBindingExecutableSwap,
            commit_runtime_binding_bundle,
            locked_store_mutation,
        )

        for fail_postcondition in (False, True):
            with self.subTest(fail_postcondition=fail_postcondition):
                root = self.f.root / ("full-failure" if fail_postcondition else "full-success")
                root.mkdir()
                harness = transaction_support.TransactionTests()
                store, artifacts, paths, old_payloads = harness.arrange_runtime_binding_bundle(
                    root, include_shared_config=True, include_active_runtime_config=True,
                )
                self.write_installer()
                candidate, result = self.stage(root.name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                old_binary = self.f.bound_bin.read_bytes()
                candidate_bytes = candidate.read_bytes()
                swap = RuntimeBindingExecutableSwap(
                    bound_path=self.f.bound_bin, candidate_path=candidate,
                    backup_path=self.f.install_root / ".codex-internal-backup-full",
                    old_mode=0o755, old_sha256=hashlib.sha256(old_binary).hexdigest(),
                    new_mode=0o755, new_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
                )
                probes = []

                def validate() -> None:
                    run = self.run_candidate(self.f.bound_bin, "--version")
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(run.stdout.strip(), "codex-cli 2.0.0")
                    probes.append(True)
                    if fail_postcondition:
                        raise SwitchError("test full-bundle postcondition failed")

                def commit() -> None:
                    with locked_store_mutation(store, operation="standalone package test") as locked:
                        commit_runtime_binding_bundle(
                            locked, artifacts=artifacts, executable_swap=swap,
                            prepared_validator=validate, retire_executable_backup=True,
                        )

                if fail_postcondition:
                    with self.assertRaisesRegex(SwitchError, "test full-bundle postcondition"):
                        commit()
                    self.assertEqual(self.f.bound_bin.read_bytes(), old_binary)
                    for path, payload in old_payloads.items():
                        self.assertEqual(path.read_bytes(), payload)
                else:
                    commit()
                    self.assertEqual(self.f.bound_bin.read_bytes(), candidate_bytes)
                    candidate.parent.rmdir()
                    for artifact in artifacts:
                        self.assertEqual(paths[artifact.role].read_bytes(), artifact.payload)
                self.assertEqual(probes, [True])
                self.assertFalse(swap.backup_path.exists())
                self.assertFalse((store.root / ".runtime-binding-rebind.json").exists())
                self.assertEqual(self.run_candidate(self.f.bound_bin, "--version").returncode, 0)

    def test_rejects_nested_private_state_files(self) -> None:
        for name in ("auth.json", "config.toml", ".zprofile"):
            with self.subTest(name=name):
                self.write_installer(f'(package / "codex-resources" / {name!r}).write_text("private-installer-state")')
                _, result = self.stage("private-" + name)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_materialization_termination_preserves_signal_exit_status(self) -> None:
        self.write_installer()
        self.f._write_script(
            self.f.fake_bin / "codesign",
            "#!/usr/bin/env python3\nimport os, time\n"
            "with open(os.environ['CODEX_TEST_EVENTS'], 'a') as output:\n"
            "    output.write('materializing\\n')\n"
            "time.sleep(2)\n",
        )
        candidate = self.f._candidate_dir("signal-materialization")
        process = subprocess.Popen(
            [str(ENV_SETUP), "update-internal", "--internal-bin", str(self.f.bound_bin),
             "--install-dir", str(candidate), "--version", "2.0.0",
             "--model", "test-model", "--azure-base-url", "https://example.invalid/api",
             "--skip-source-check", "--skip-proxy"],
            env=self.f.base_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.f.events.exists() and "materializing" in self.f.events.read_text():
                    break
                if process.poll() is not None:
                    self.fail("helper exited before the materialization signal test")
                time.sleep(0.05)
            else:
                self.fail("materialization did not start")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            self.assertNotIn("candidate ready", stdout)
            self.assertEqual(self.f.bound_bin.read_bytes(), self.f.bound_copy.read_bytes())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

    def test_native_launcher_preserves_stable_argv_zero(self) -> None:
        # Exercise real native argv[0], without launching a live Codex process.
        scratch = self.f.root / "native-scratch"
        scratch.mkdir(mode=0o700)
        package = scratch / "codex-home/packages/standalone/releases/2.0.0-aarch64-apple-darwin"
        (package / "bin").mkdir(parents=True)
        if sys.platform == "darwin":
            # Apple platform shells cannot be relocated as ordinary app
            # binaries on every macOS host; compile a tiny fixture instead.
            compiler = shutil.which("cc")
            if compiler is None:
                self.skipTest("native macOS argv[0] fixture requires a C compiler")
            subprocess.run(
                [compiler, "-x", "c", "-", "-o", str(package / "bin/codex")],
                input='#include <stdio.h>\nint main(int argc, char **argv) { fputs(argv[0], stdout); return 0; }\n',
                text=True, env=self.f.base_env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        else:
            shutil.copyfile("/bin/sh", package / "bin/codex")
            (package / "bin/codex").chmod(0o755)
        self.f._write_script(package / "bin/codex-code-mode-host", "#!/bin/sh\nexit 0\n")
        self.f._write_script(package / "codex-path/rg", "#!/bin/sh\nexit 0\n")
        (package / "codex").symlink_to("bin/codex")
        candidate = self.f._candidate_dir("native 'quoted path") / "codex"
        candidate.parent.mkdir(mode=0o700)
        candidate.symlink_to(package / "bin/codex")
        result = subprocess.run(
            [self.f.base_env["CODEX_SWITCH_PYTHON"], "-I", "-B",
             str(REPO_ROOT / "scripts/codex_switch_internal_runtime.py"),
             "--candidate", str(candidate), "--scratch", str(scratch), "--version", "2.0.0"],
            env=self.f.base_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        env = {**self.f.base_env, "PATH": str(candidate.parent) + os.pathsep + self.f.base_env["PATH"]}
        for command in (str(candidate), "codex", os.path.relpath(candidate, self.f.root)):
            with self.subTest(command=command):
                result = subprocess.run(
                    [command, "-c", 'printf "%s" "$0"'], cwd=self.f.root, env=env,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(result.stdout).resolve(), candidate.resolve())


if __name__ == "__main__":
    unittest.main()
