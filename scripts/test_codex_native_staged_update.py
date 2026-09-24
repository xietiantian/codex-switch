"""Opt-in staged updates with real CLIs and a loopback-only model provider.

Set CODEX_SWITCH_TEST_BACKEND to a verified runtime entrypoint. Optionally set
CODEX_SWITCH_TEST_OFFICIAL_BACKEND; otherwise use read-only canonical Desktop
discovery. The test copies complete standalone generations and Desktop CLI
bytes into owned temporary roots. Only installer transport and Desktop root
selection are fixture adapters: schemas, features, catalogs, protocol probes,
runtime publication and receipts use production code. No caller configuration,
credentials, installed commands or application state is modified.

The compatible-default case explicitly uses the same real runtime on both
sides; all other Desktop comparisons use the supplied/discovered Desktop CLI.
The process-death case kills only its owned child after the durable committed
marker, then exercises observational status and the native recovery seam.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_switch_update as update
from codex_switch_internal_runtime import launcher_text, runtime_manifest
from codex_switch_runtime_binding import (
    CURRENT_CHATGPT_BUNDLE_ID,
    DesktopRoots,
    discover_desktop_hosts,
)
from codex_switch_store import make_store
from test_codex_native_parity import FIXTURES, start_loopback_server


BACKEND = os.environ.get("CODEX_SWITCH_TEST_BACKEND")
OFFICIAL_BACKEND = os.environ.get("CODEX_SWITCH_TEST_OFFICIAL_BACKEND")


@unittest.skipUnless(BACKEND, "set CODEX_SWITCH_TEST_BACKEND for real staged-update proof")
class NativeStagedUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets_temporary = tempfile.TemporaryDirectory(prefix="codex-native-update-assets-")
        cls.addClassCleanup(cls.assets_temporary.cleanup)
        cls.assets = Path(cls.assets_temporary.name).resolve()
        cls.original_backend = Path(BACKEND).resolve(strict=True)
        if OFFICIAL_BACKEND:
            cls.original_official = Path(OFFICIAL_BACKEND).resolve(strict=True)
        else:
            host = discover_desktop_hosts().current
            if host is None or not host.healthy:
                cls.assets_temporary.cleanup()
                raise unittest.SkipTest("A verified Desktop CLI is required")
            cls.original_official = host.bundled_cli
        cls.original_runtime = update.runtime_identity(cls.original_backend)
        cls.original_official_state = update.file_state(cls.original_official)
        cls.candidate = cls.assets / "codex"
        if "package_path" in cls.original_runtime:
            packages = cls.assets / "packages"
            packages.mkdir(mode=0o700)
            generation = packages / "generation"
            shutil.copytree(Path(cls.original_runtime["package_path"]), generation, symlinks=True)
            generation.chmod(0o700)
            entry = "bin/codex" if (generation / "bin/codex").is_file() else "codex"
            cls.candidate.write_text(launcher_text(generation, entry, runtime_manifest(generation)))
            cls.candidate.chmod(0o755)
        else:
            shutil.copy2(cls.original_backend, cls.candidate)
        cls.official = cls.assets / "desktop-cli"
        shutil.copy2(cls.original_official, cls.official)

    @classmethod
    def tearDownClass(cls):
        try:
            if update.runtime_identity(cls.original_backend) != cls.original_runtime:
                raise AssertionError("The read-only original runtime changed during testing")
            if update.file_state(cls.original_official) != cls.original_official_state:
                raise AssertionError("The read-only Desktop CLI changed during testing")
        finally:
            cls.assets_temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-native-staged-update-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "user"
        self.home.mkdir()
        self.official_home = self.root / "official-home"
        self.official_home.mkdir()
        (self.official_home / "config.toml").write_text("")
        self.internal_home = self.root / "internal-home"
        self.final_home = self.root / "final-home"
        self.final_home.mkdir()
        self.target = self.root / "commands/codex"
        self.requests = []
        self.server, self.thread = start_loopback_server(self.requests)
        self.addCleanup(self.close_server)
        environment = {
            "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1", "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
            "CODEX_SWITCH_PYTHON": sys.executable,
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, environment, clear=True))
        previous_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous_cwd)
        bundle = self.root / "Applications/ChatGPT.app"
        contents = bundle / "Contents"
        (contents / "Resources").mkdir(parents=True)
        (contents / "MacOS").mkdir()
        self.plist = contents / "Info.plist"
        self.plist.write_bytes(plistlib.dumps({
            "CFBundleIdentifier": CURRENT_CHATGPT_BUNDLE_ID,
            "CFBundleShortVersionString": "1.2026.1",
            "CFBundleExecutable": "ChatGPT",
        }))
        shutil.copy2(self.official, contents / "Resources/codex")
        main = contents / "MacOS/ChatGPT"
        main.write_text("#!/bin/sh\nexit 0\n")
        main.chmod(0o755)
        self.desktop_roots = DesktopRoots(
            chatgpt=bundle, legacy_codex=self.root / "Applications/Codex.app",
            chatgpt_classic=self.root / "Applications/ChatGPT Classic.app",
        )
        self.inventory = discover_desktop_hosts(self.desktop_roots)
        self.assertIsNotNone(self.inventory.current)
        self.assertTrue(self.inventory.current.healthy)
        self.stack.enter_context(patch.object(update, "discover_desktop_hosts",
            side_effect=lambda: discover_desktop_hosts(self.desktop_roots)))
        self.stack.enter_context(patch("codex_switch_runtime_binding.discover_desktop_hosts",
            side_effect=lambda roots=None: discover_desktop_hosts(roots or self.desktop_roots)))
        self.version = update.probe_version(self.candidate)
        self.helper = self.root / "fixture-installer"
        self.helper.write_text(
            "#!/bin/sh\nset -eu\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in --install-dir) destination=$2; shift 2;; *) shift;; esac\n"
            "done\nmkdir -m 700 -p \"$destination\"\n"
            f"cp {shlex.quote(str(self.candidate))} \"$destination/codex\"\n"
            "chmod 755 \"$destination/codex\"\n"
        )
        self.helper.chmod(0o755)
        self.global_args = [
            "--store-dir", str(self.root / "store"),
            "--official-codex-home", str(self.official_home),
            "--internal-codex-home", str(self.internal_home),
            "--launch-agent-path", str(self.home / "Library/LaunchAgents/test.plist"),
            "--helper", str(self.helper),
        ]
        self.marker = self.root / "caller-hook-ran"
        for caller_home in (self.root / ".codex", self.home / ".codex", self.final_home):
            caller_home.mkdir(exist_ok=True)
            (caller_home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{
                "hooks": [{"type": "command", "command": "echo executed > " + shlex.quote(str(self.marker))}]
            }]}}))
        self.catalog = self.root / "custom-models.json"
        self.catalog.write_bytes((FIXTURES / "parity-probe-model.json").read_bytes())
        self.write_final_config()

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def write_final_config(self, *, custom=True, model="gpt-5.5"):
        catalog_line = f"model_catalog_json={json.dumps(str(self.catalog))}\n" if custom else ""
        self.config = (
            f'model={json.dumps(model)}\nmodel_provider="probe"\n'
            'cli_auth_credentials_store="file"\n' + catalog_line +
            f"notify={json.dumps(['/bin/sh', '-c', 'echo executed > ' + shlex.quote(str(self.marker))])}\n"
            '[model_providers.probe]\nname="Loopback fixture"\n'
            f'base_url="http://127.0.0.1:{self.server.server_port}/v1"\n'
            'wire_api="responses"\nrequires_openai_auth=false\nrequest_max_retries=0\n'
        )
        (self.final_home / "config.toml").write_text(self.config)

    def args(self, *tokens):
        args = update.parser().parse_args(self.global_args + list(tokens))
        args.rebind_desktop_inventory = discover_desktop_hosts(self.desktop_roots)
        return args

    def command(self, operation, *tokens):
        args = self.args(operation, *tokens)
        store = make_store(args)
        output = io.StringIO()
        with redirect_stdout(output):
            result = {"stage": update.stage, "apply": update.apply_update,
                      "status": update.status, "cancel": update.cancel}[operation](args, store)
        self.assertNotIn(self.config, output.getvalue())
        exposed = json.dumps(result, sort_keys=True) + output.getvalue()
        for private_value in ("input_fingerprint", "Loopback fixture", "notify="):
            self.assertNotIn(private_value, exposed)
        return result, store

    def stage(self):
        return self.command("stage", "--version", self.version,
                            "--internal-bin", str(self.target), "--json")

    def test_real_first_full_apply_retains_candidate_and_replays_without_probes(self):
        staged, store = self.stage()
        self.assertFalse(self.target.exists())
        self.assertFalse(store.profile_dir("internal").exists())
        candidate = Path(staged["runtime_path"])
        retained_identity = update.runtime_identity(candidate)
        applied, store = self.command("apply", staged["update_id"],
            "--from-codex-home", str(self.final_home), "--json")
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["scope"], "full")
        self.assertTrue(store.manifest_path("internal").is_file())
        self.assertEqual(update.runtime_identity(candidate), retained_identity)
        self.assertEqual(update.probe_version(self.target), self.version)
        self.assertEqual((self.final_home / "config.toml").read_text(), self.config)
        self.assertIn("notify", (self.internal_home / "config.toml").read_text())
        self.assertFalse((self.internal_home / "hooks.json").exists())
        self.assertFalse(self.marker.exists())
        self.assertGreaterEqual(len(self.requests), 4)
        receipt = json.loads((store.profile_dir("internal") / "parity/receipt.json").read_text())
        self.assertTrue(receipt["healthy"])
        probe_count = len(self.requests)
        replay_subprocesses = []
        observing_replay = [True]
        def observe_subprocess(event, _arguments):
            if observing_replay[0] and event == "subprocess.Popen":
                replay_subprocesses.append(event)
        sys.addaudithook(observe_subprocess)
        try:
            replay, _ = self.command("apply", staged["update_id"],
                "--from-codex-home", str(self.final_home), "--json")
        finally:
            observing_replay[0] = False
        self.assertEqual(replay["transaction_id"], applied["transaction_id"])
        self.assertEqual(len(self.requests), probe_count)
        self.assertEqual(replay_subprocesses, [])
        status, _ = self.command("status", staged["update_id"], "--json")
        self.assertEqual(status["state"], "applied")
        from codex_switch_verify import collect_parity_report
        from codex_switch_transaction import TransactionRequest, execute_transaction
        report = collect_parity_report(store, None)
        self.assertTrue(report.healthy, [(finding.code, finding.message) for finding in report.findings])
        runtime_before = (self.internal_home / "config.toml").read_bytes()
        request = TransactionRequest(operation="switch", profile="internal", options={
            "config_mode": "shared", "skip_shim": True,
            "skip_app_cli": True, "skip_launchctl": True,
        })
        switched = execute_transaction(store, request)
        self.assertEqual(switched.outcome, "committed")
        self.assertEqual((self.internal_home / "config.toml").read_bytes(), runtime_before)
        self.assertTrue(collect_parity_report(store, None).healthy)
        from codex_switch_capture import capture_profile
        with redirect_stdout(io.StringIO()):
            capture_profile(store, "internal", self.internal_home, str(self.target), "", True, True)
        manifest = store.load_manifest("internal")
        self.assertEqual(manifest["parity_catalog_provenance"]["source_kind"], "custom")
        self.assertEqual(manifest["parity_catalog_provenance"]["source_path"], str(self.catalog))
        self.assertNotIn("parity_receipt_path", manifest)
        recaptured_runtime = (self.internal_home / "config.toml").read_text()
        self.config = 'model_reasoning_effort="low"\n' + "".join(
            line for line in recaptured_runtime.splitlines(keepends=True)
            if not line.lstrip().startswith("notify")
        )
        (self.final_home / "config.toml").write_text(self.config)
        with self.assertRaisesRegex(update.UpdateError, "different inputs"):
            self.command("apply", staged["update_id"], "--from-codex-home", str(self.final_home), "--json")
        current, _ = self.command("stage", "--current", "--internal-bin", str(self.target), "--json")
        self.assertEqual(current["source"], "current")
        before_current = update.runtime_identity(self.target)
        reapplied, _ = self.command("apply", current["update_id"],
            "--from-codex-home", str(self.final_home), "--json")
        self.assertEqual(reapplied["state"], "applied")
        self.assertEqual(update.runtime_identity(self.target), before_current)
        self.assertTrue(collect_parity_report(store, None).healthy)
        self.assertEqual(store.load_manifest("internal")["parity_catalog_provenance"]["source_path"], str(self.catalog))
        runtime = (self.internal_home / "config.toml").read_text()
        self.assertIn('model_reasoning_effort="low"', runtime.replace(" = ", "="))
        self.assertNotIn("notify", runtime)
        self.assertFalse(self.marker.exists())

    def test_real_default_catalog_export_preserves_compatibility_guard(self):
        from codex_switch_verify import collect_parity_report
        from codex_switch_parity import ParityValidationError
        self.write_final_config(custom=False)
        staged, store = self.stage()
        try:
            applied, _ = self.command("apply", staged["update_id"],
                "--from-codex-home", str(self.final_home), "--json")
        except ParityValidationError as error:
            # Independently distributed real CLIs may contain different model
            # instructions. Missing caches must reach the real comparison and
            # preserve its fail-closed policy, never relabel defaults as custom.
            self.assertEqual(error.code, "parity.preparation.policy_unhealthy")
            self.assertIn("parity.model.unclassified_drift", str(error))
            status, _ = self.command("status", staged["update_id"], "--json")
            self.assertEqual(status["state"], "failed")
            self.assertTrue(status["safe_to_restore"])
            self.assertFalse(self.target.exists())
            self.assertFalse(store.profile_dir("internal").exists())
            self.assertFalse((self.internal_home / "models_cache.json").exists())
            self.assertFalse((self.official_home / "models_cache.json").exists())
            self.assertFalse(self.marker.exists())
            self.assertEqual(self.requests, [])
            return
        self.assertEqual(applied["state"], "applied")
        self.assertTrue(collect_parity_report(store, None).healthy)
        self.assertEqual(store.load_manifest("internal")["parity_model_catalog_kind"], "runtime-bundled")
        self.assertFalse((self.internal_home / "models_cache.json").exists())
        self.assertFalse((self.official_home / "models_cache.json").exists())
        self.assertTrue((store.profile_dir("internal") / "parity/internal-model-source.json").is_file())
        self.assertTrue((store.profile_dir("internal") / "parity/official-model-source.json").is_file())
        self.assertFalse(self.marker.exists())

    def test_managed_overlay_model_change_replays_without_probes_and_rejects_origin_drift(self):
        from codex_switch_verify import collect_parity_report
        import tomllib

        model = json.loads(self.catalog.read_text())["models"][0]
        model.pop("multi_agent_version", None)
        models = [dict(model, slug="model-a"), dict(model, slug="model-b")]
        self.catalog.write_text(json.dumps({"models": models}))
        source_before = self.catalog.read_bytes()
        self.write_final_config(model="model-a")
        initial, store = self.stage()
        first, _ = self.command("apply", initial["update_id"],
            "--from-codex-home", str(self.final_home), "--json")
        self.assertEqual(first["state"], "applied")

        overlay = store.profile_dir("internal") / "parity/model-catalog.json"
        first_overlay = overlay.read_bytes()
        by_slug = {entry["slug"]: entry for entry in json.loads(first_overlay)["models"]}
        self.assertEqual(by_slug["model-a"]["multi_agent_version"], "v2")
        self.assertNotIn("multi_agent_version", by_slug["model-b"])
        self.config = (self.internal_home / "config.toml").read_text().replace('"model-a"', '"model-b"')
        private_config = tomllib.loads(self.config)
        self.assertEqual(private_config["model"], "model-b")
        self.assertEqual(private_config["model_catalog_json"], str(overlay))
        (self.final_home / "config.toml").write_text(self.config)
        private_before = (self.final_home / "config.toml").read_bytes()
        self.assertFalse((self.final_home / "auth.json").exists())
        current, _ = self.command("stage", "--current", "--internal-bin", str(self.target), "--json")
        applied, _ = self.command("apply", current["update_id"],
            "--from-codex-home", str(self.final_home), "--json")
        self.assertEqual(applied["state"], "applied")
        self.assertNotEqual(overlay.read_bytes(), first_overlay)
        by_slug = {entry["slug"]: entry for entry in json.loads(overlay.read_text())["models"]}
        self.assertNotIn("multi_agent_version", by_slug["model-a"])
        self.assertEqual(by_slug["model-b"]["multi_agent_version"], "v2")
        self.assertEqual(self.catalog.read_bytes(), source_before)
        self.assertEqual((self.final_home / "config.toml").read_bytes(), private_before)
        self.assertFalse((self.final_home / "auth.json").exists())
        self.assertTrue(collect_parity_report(store, None).healthy)

        request_count = len(self.requests)
        self.assertGreaterEqual(request_count, 8)
        replay_subprocesses = []
        observing_replay = [True]
        def observe_subprocess(event, _arguments):
            if observing_replay[0] and event == "subprocess.Popen":
                replay_subprocesses.append(event)
        sys.addaudithook(observe_subprocess)
        try:
            replay, _ = self.command("apply", current["update_id"],
                "--from-codex-home", str(self.final_home), "--json")
            self.assertEqual(replay["state"], "applied")
            self.assertEqual(replay["transaction_id"], applied["transaction_id"])
            self.assertEqual(len(self.requests), request_count)
            self.assertEqual(replay_subprocesses, [])

            self.catalog.write_bytes(source_before + b"\n")
            with self.assertRaises(update.UpdateError) as changed:
                self.command("apply", current["update_id"],
                    "--from-codex-home", str(self.final_home), "--json")
            self.assertEqual(changed.exception.reason, "input_mismatch")
            self.assertEqual(len(self.requests), request_count)
            self.assertEqual(replay_subprocesses, [])
        finally:
            observing_replay[0] = False
        self.assertFalse(self.marker.exists())

    def test_compatible_native_default_exports_publish_without_runtime_caches(self):
        """Use the same real runtime on both sides solely for default success."""
        from codex_switch_verify import collect_parity_report
        shutil.copy2(self.candidate, self.inventory.current.bundled_cli)
        inspection_home = self.root / "bundled-model-inspection"
        inspection_home.mkdir(mode=0o700)
        environment = dict(os.environ, HOME=str(inspection_home), CODEX_HOME=str(inspection_home))
        exported = subprocess.run([str(self.candidate), "-c", 'cli_auth_credentials_store="file"',
            "debug", "models", "--bundled"], cwd=inspection_home, env=environment,
            capture_output=True, text=True, timeout=15)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        bundled = json.loads(exported.stdout)["models"]
        supported = [model["slug"] for model in bundled if model.get("multi_agent_version") == "v2"]
        if not supported:
            self.skipTest("This runtime has no bundled v2 model compatible with the unchanged default policy")
        self.write_final_config(custom=False, model=supported[0])
        staged, store = self.stage()
        applied, _ = self.command("apply", staged["update_id"],
            "--from-codex-home", str(self.final_home), "--json")
        self.assertEqual(applied["state"], "applied")
        self.assertTrue(collect_parity_report(store, None).healthy)
        self.assertEqual(store.load_manifest("internal")["parity_model_catalog_kind"], "runtime-bundled")
        self.assertFalse((self.internal_home / "models_cache.json").exists())
        self.assertFalse((self.official_home / "models_cache.json").exists())
        self.assertTrue((store.profile_dir("internal") / "parity/internal-model-source.json").is_file())
        self.assertTrue((store.profile_dir("internal") / "parity/official-model-source.json").is_file())
        self.assertFalse(self.marker.exists())

    def test_native_process_death_after_commit_is_observed_without_reprobe(self):
        from codex_switch_transaction import locked_store_mutation, read_runtime_binding_terminal_receipt
        staged, store = self.stage()
        script = """
import os, signal, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import codex_switch_update as update
from codex_switch_runtime_binding import DesktopRoots, discover_desktop_hosts
from codex_switch_store import make_store
fixture = Path(sys.argv[2])
roots = DesktopRoots(chatgpt=fixture / 'Applications/ChatGPT.app',
    legacy_codex=fixture / 'Applications/Codex.app',
    chatgpt_classic=fixture / 'Applications/ChatGPT Classic.app')
args = update.parser().parse_args(sys.argv[3:])
args.rebind_desktop_inventory = discover_desktop_hosts(roots)
def kill_after_commit(phase):
    if phase == 'after_committed_marker':
        os.kill(os.getpid(), signal.SIGKILL)
args.rebind_commit_fault_hook = kill_after_commit
with patch.object(update, 'discover_desktop_hosts', side_effect=lambda: discover_desktop_hosts(roots)):
    update.apply_update(args, make_store(args))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(Path(__file__).resolve().parent),
             str(self.root), *self.global_args, "apply", staged["update_id"],
             "--from-codex-home", str(self.final_home), "--json"],
            cwd=self.root, env=dict(os.environ), text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        marker = store.root / ".runtime-binding-rebind.json"
        committed_marker = marker.read_bytes()
        self.assertEqual(json.loads(committed_marker)["state"], "committed")
        count = len(self.requests)
        self.assertGreaterEqual(count, 4)
        status, _ = self.command("status", staged["update_id"], "--json")
        self.assertEqual(status["state"], "applied")
        self.assertEqual(marker.read_bytes(), committed_marker)
        replay, _ = self.command("apply", staged["update_id"],
            "--from-codex-home", str(self.final_home), "--json")
        self.assertEqual(replay["transaction_id"], status["transaction_id"])
        self.assertEqual(len(self.requests), count)
        with locked_store_mutation(store, operation="recover isolated committed update"):
            pass
        receipt = read_runtime_binding_terminal_receipt(store, staged["update_id"])
        self.assertEqual(receipt["state"], "committed")
        self.assertEqual(receipt["transaction_id"], status["transaction_id"])
        self.assertFalse(marker.exists())
        self.assertFalse(self.marker.exists())

    def test_verified_bootstrap_current_apply_creates_first_profile(self):
        from codex_switch_first_install import publish
        from codex_switch_verify import collect_parity_report
        args = self.args("stage", "--current", "--internal-bin", str(self.target))
        store = make_store(args)
        bootstrap_dir = self.target.parent / ".codex-internal-update-bootstrap-fixture"
        bootstrap_dir.mkdir(mode=0o700, parents=True)
        bootstrap_candidate = bootstrap_dir / "codex"
        shutil.copy2(self.candidate, bootstrap_candidate)
        publish(self.target, store.root, bootstrap_candidate, self.version)
        self.assertFalse(store.profile_dir("internal").exists())
        target_before = update.runtime_identity(self.target)
        self.helper.unlink()
        staged, _ = self.command("stage", "--current", "--internal-bin", str(self.target), "--json")
        self.assertEqual(staged["source"], "current")
        self.assertFalse(staged["profile_present"])
        applied, _ = self.command("apply", staged["update_id"],
            "--from-codex-home", str(self.final_home), "--json")
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(update.runtime_identity(self.target), target_before)
        self.assertTrue(collect_parity_report(store, None).healthy)
        self.assertFalse(self.marker.exists())

    def test_candidate_drift_is_stale_before_native_probes(self):
        staged, store = self.stage()
        candidate = Path(staged["runtime_path"])
        with candidate.open("ab") as stream:
            stream.write(b"\n# concurrent candidate edit\n")
        edited = candidate.read_bytes()
        with self.assertRaises(update.UpdateError):
            self.command("apply", staged["update_id"], "--from-codex-home", str(self.final_home), "--json")
        status, _ = self.command("status", staged["update_id"], "--json")
        self.assertEqual(status["state"], "stale")
        self.assertEqual(candidate.read_bytes(), edited)
        self.assertFalse(self.target.exists())
        self.assertFalse(store.profile_dir("internal").exists())
        self.assertEqual(self.requests, [])

    def test_desktop_drift_is_stale_before_native_probes(self):
        staged, store = self.stage()
        data = plistlib.loads(self.plist.read_bytes())
        data["CFBundleShortVersionString"] = "1.2026.2"
        self.plist.write_bytes(plistlib.dumps(data))
        with self.assertRaises(update.UpdateError):
            self.command("apply", staged["update_id"], "--from-codex-home", str(self.final_home), "--json")
        status, _ = self.command("status", staged["update_id"], "--json")
        self.assertEqual(status["state"], "stale")
        self.assertFalse(self.target.exists())
        self.assertFalse(store.profile_dir("internal").exists())
        self.assertEqual(self.requests, [])

    def test_source_drift_during_real_preparation_preserves_user_edit(self):
        staged, store = self.stage()
        edited = self.config + "\n# concurrent source edit\n"
        applied_seen = threading.Event()
        stop = threading.Event()
        record_path = store.root / "updates" / staged["update_id"] / "record.json"
        def mutate_after_capture():
            while not stop.wait(0.01):
                try:
                    record = json.loads(record_path.read_text())
                except (OSError, ValueError):
                    continue
                if record["state"] == "applying":
                    (self.final_home / "config.toml").write_text(edited)
                    applied_seen.set()
                    return
        editor = threading.Thread(target=mutate_after_capture, daemon=True)
        editor.start()
        try:
            with self.assertRaises(update.UpdateError):
                self.command("apply", staged["update_id"], "--from-codex-home", str(self.final_home), "--json")
        finally:
            stop.set()
            editor.join(timeout=2)
        self.assertTrue(applied_seen.is_set())
        status, _ = self.command("status", staged["update_id"], "--json")
        self.assertEqual(status["state"], "stale")
        self.assertEqual((self.final_home / "config.toml").read_text(), edited)
        self.assertFalse(self.target.exists())
        self.assertFalse(store.profile_dir("internal").exists())
        self.assertGreaterEqual(len(self.requests), 4)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
