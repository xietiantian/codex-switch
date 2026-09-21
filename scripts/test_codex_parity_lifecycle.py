"""Public home and parity contracts across capture, rebind and activation."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_switch_home_select as homes
from codex_switch_constants import SwitchError
from codex_switch_store import Store


class ParityHomeSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="codex-parity-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.live = self.root / "user/.codex"
        self.store = Store(
            self.root / "store", self.live, self.root / "agent.plist", "test",
            official_codex_home_source="legacy_arg",
            internal_codex_home=self.live, internal_codex_home_source="explicit",
        )
        self.store.ensure()
        for profile in ("internal", "openai-official"):
            directory = self.store.profile_dir(profile)
            directory.mkdir()
            (directory / "manifest.json").write_text(json.dumps({"name": profile}))

    def test_recorded_official_home_wins_over_legacy_live_home(self) -> None:
        official = self.root / "separate-official"
        self.store.manifest_path("openai-official").write_text(json.dumps({
            "name": "openai-official", "codex_home": str(official),
            "home_selection_confirmed": True,
        }))
        selected = homes.resolve_runtime_homes(self.store)
        self.assertEqual(selected.internal.path, self.live)
        self.assertEqual(selected.official.path, official)

    def test_first_adopted_home_resolves_independent_official_home_without_writes(self) -> None:
        before = self.store.manifest_path("openai-official").read_bytes()
        selected = homes.resolve_runtime_homes(self.store)
        self.assertEqual(selected.internal.path, self.live)
        self.assertEqual(selected.official.path, self.store.managed_home("openai-official"))
        self.assertEqual(self.store.manifest_path("openai-official").read_bytes(), before)
        self.assertFalse(selected.official.path.exists())

    def test_explicit_colliding_homes_remain_invalid(self) -> None:
        self.store.official_codex_home_source = "official_arg"
        with self.assertRaisesRegex(SwitchError, "same Codex home"):
            homes.resolve_runtime_homes(self.store)


@unittest.skipUnless(os.environ.get("CODEX_SWITCH_TEST_BACKEND"), "requires an explicit native test backend")
class NativeParityLifecycleTests(unittest.TestCase):
    def test_first_and_recaptured_activation_preserve_healthy_receipts(self) -> None:
        from codex_profile_switch import build_parser
        from codex_switch_store import make_store
        from codex_switch_verify import collect_parity_report
        from test_codex_native_parity import start_loopback_server

        backend = Path(os.environ["CODEX_SWITCH_TEST_BACKEND"]).resolve()
        model = os.environ.get("CODEX_SWITCH_TEST_MODEL", "gpt-6-astra")
        for active, explicit_official in ((False, False), (True, False), (False, True)):
            with self.subTest(active=active, explicit_official=explicit_official), tempfile.TemporaryDirectory(prefix="codex-parity-lifecycle-") as tmp:
                root = Path(tmp).resolve()
                home = root / "user"
                live = home / ".codex"
                live.mkdir(parents=True)
                (home / "Library/LaunchAgents").mkdir(parents=True)
                requests = []
                server, thread = start_loopback_server(requests)
                try:
                    config = (
                        f"model={json.dumps(model)}\nmodel_provider=\"probe\"\n"
                        'cli_auth_credentials_store="file"\n'
                        '[model_providers.probe]\nname="Loopback fixture"\n'
                        f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
                        'wire_api="responses"\nrequires_openai_auth=false\nrequest_max_retries=0\n'
                    )
                    (live / "config.toml").write_text(config)
                    environment = {
                        "HOME": str(home), "CODEX_HOME": str(live), "PATH": os.environ["PATH"],
                        "CODEX_SWITCH_SHELL_PROFILE": str(home / ".zshrc"),
                        "PYTHONDONTWRITEBYTECODE": "1", "CODEX_SWITCH_SKIP_SELF_UPDATE": "1",
                        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                        "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
                    }
                    with patch.dict(os.environ, environment, clear=True):
                        parser = build_parser()
                        global_args = [
                            "--store-dir", str(root / "store"), "--live-codex-home", str(live),
                            "--internal-codex-home", str(live), "--launch-agent-path", str(home / "Library/LaunchAgents/test.plist"),
                        ]

                        def command(*tokens):
                            args = parser.parse_args(global_args + list(tokens))
                            args.func(args)
                            return make_store(args)

                        command("init", "--codex-bin", str(backend), "--capture-current", "internal", "--overwrite-capture")
                        if active:
                            command("switch", "internal", "--skip-launchctl")
                            (live / "config.toml").write_text(config + "[features]\nhooks=true\n")
                            store = command("init", "--codex-bin", str(backend), "--capture-current", "internal", "--overwrite-capture")
                            self.assertIn("parity.receipt.missing", {f.code for f in collect_parity_report(store, None).findings})
                        ordinary_args = global_args.copy()
                        if explicit_official:
                            global_args.extend(["--official-codex-home", str(root / "custom-official")])
                        store = make_store(parser.parse_args(global_args + ["set-bin", "internal", str(backend)]))
                        protected_paths = [
                            store.manifest_path("internal"), store.manifest_path("openai-official"),
                            store.profile_dir("internal") / "config.toml", live / "config.toml",
                            homes.resolve_runtime_homes(store).official.path / "config.toml",
                            store.bin_dir / "codex-internal-app",
                            store.profile_dir("internal") / "parity/receipt.json",
                        ]
                        before = {path: path.read_bytes() if path.exists() else None for path in protected_paths}

                        def fail_commit(phase):
                            if phase == ("after_manifest" if active else "after_official_manifest"):
                                raise OSError("injected lifecycle publication failure")

                        failed_args = parser.parse_args(global_args + ["set-bin", "internal", str(backend)])
                        failed_args.rebind_commit_fault_hook = fail_commit
                        with self.assertRaisesRegex(OSError, "injected lifecycle"):
                            failed_args.func(failed_args)
                        self.assertEqual(before, {path: path.read_bytes() if path.exists() else None for path in protected_paths})
                        self.assertFalse((store.root / ".runtime-binding-rebind.json").exists())
                        command("set-bin", "internal", str(backend))
                        global_args[:] = ordinary_args
                        for _ in range(2):
                            store = command("switch", "internal", "--skip-launchctl")
                            report = collect_parity_report(store, None)
                            self.assertTrue(report.healthy, [(f.code, f.message) for f in report.findings])
                        self.assertNotEqual(homes.resolve_runtime_homes(store).official.path, live)
                        if explicit_official:
                            self.assertEqual(homes.resolve_runtime_homes(store).official.path, root / "custom-official")
                        self.assertFalse((live / "models_cache.json").exists())
                        official_manifest = store.manifest_path("openai-official")
                        official_before = official_manifest.read_bytes()
                        official_manifest.write_text("invalid JSON")
                        try:
                            report = collect_parity_report(store, None)
                            self.assertIn("parity.config.source_stale", {f.code for f in report.findings})
                        finally:
                            official_manifest.write_bytes(official_before)
                        profile_config = store.profile_dir("internal") / "config.toml"
                        profile_config.write_text(profile_config.read_text() + "\n# changed after preparation\n")
                        report = collect_parity_report(store, None)
                        self.assertFalse(report.healthy)
                        self.assertIn("parity.config.source_stale", {f.code for f in report.findings})
                        before_switch = {path: path.read_bytes() for path in (store.active_path, live / "config.toml", store.bin_dir / "codex-internal-app")}
                        with self.assertRaisesRegex(SwitchError, "[Pp]arity|[Pp]repared"):
                            command("switch", "internal", "--skip-launchctl")
                        self.assertEqual(before_switch, {path: path.read_bytes() for path in before_switch})
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
