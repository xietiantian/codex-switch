#!/usr/bin/env python3
"""Behavioral tests of the durable public update commands."""

import json
from contextlib import contextmanager
import os
import subprocess
import signal
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_codex_first_install as bootstrap_tests

WRAPPER = bootstrap_tests.WRAPPER


class StagedUpdateTests(unittest.TestCase):
    setUp = bootstrap_tests.FirstInstallTests.setUp
    write = bootstrap_tests.FirstInstallTests.write
    write_installer = bootstrap_tests.FirstInstallTests.write_installer
    run_install = bootstrap_tests.FirstInstallTests.run_install
    def command(self, *args):
        return subprocess.run(
            [str(WRAPPER), "--skip-self-update", "--store-dir", str(self.store),
             "update-internal", *args], env=self.env, text=True,
            capture_output=True, timeout=30,
        )

    def stage(self, *args):
        return self.command("stage", "--version", "2.0.0", "--install-dir",
                            str(self.target.parent), "--skip-source-check",
                            "--skip-proxy", "--json", *args)

    def test_stage_without_profile_retains_candidate_and_live_absence(self):
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["state"], "staged")
        self.assertEqual(record["actual_version"], "2.0.0")
        self.assertEqual(len(record["runtime_digest"]), 64)
        candidate = self.root.__class__(record["runtime_path"])
        self.assertTrue(candidate.is_file())
        self.assertFalse(os.path.lexists(self.target))
        self.assertFalse((self.store / "profiles/internal").exists())
        self.assertFalse((self.home / ".codex/config.toml").exists())
        status = self.command("status", record["update_id"], "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "staged")

    def test_current_bootstrap_runtime_without_profile_does_not_reinstall(self):
        bootstrap = self.run_install()
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
        before = self.target.read_bytes()
        self.installer.unlink()
        result = self.command("stage", "--current", "--internal-bin", str(self.target), "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["source"], "current")
        self.assertFalse(record["profile_present"])
        self.assertEqual(self.target.read_bytes(), before)
        cancelled = self.command("cancel", record["update_id"], "--json")
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(json.loads(cancelled.stdout)["state"], "cancelled")
        self.assertEqual(self.target.read_bytes(), before)

    def test_installer_failure_has_queryable_id_and_never_publishes(self):
        self.write_installer(before="exit 17")
        result = self.stage()
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["state"], "failed")
        self.assertTrue(record["safe_to_restore"])
        self.assertFalse(self.target.exists())
        result = self.command("status", record["update_id"], "--json")
        self.assertEqual(json.loads(result.stdout)["state"], "failed")

    def test_source_version_and_current_are_exclusive_before_writes(self):
        result = self.stage("--current")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.store.exists())
        self.assertFalse(self.target.parent.exists())

    def test_current_unowned_binary_is_refused_without_installer(self):
        self.write(self.target, '#!/bin/sh\necho "codex-cli 2.0.0"\n')
        self.installer.unlink()
        result = self.command("stage", "--current", "--internal-bin", str(self.target), "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verified bootstrap", result.stdout)
        self.assertFalse(self.store.exists())

    def test_drift_before_apply_is_stale_and_keeps_new_user_state(self):
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.write(self.target, "#!/bin/sh\necho user-owned\n")
        private = self.root / "private"
        private.mkdir()
        (private / "config.toml").write_text('model="example"\n')
        result = self.command("apply", record["update_id"], "--from-codex-home", str(private), "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["state"], "stale")
        self.assertIn("user-owned", self.target.read_text())
        self.assertFalse((self.store / "profiles/internal").exists())

    def test_record_tamper_does_not_allow_cancel_to_touch_paths(self):
        record = json.loads(self.stage().stdout)
        path = self.store / "updates" / record["update_id"] / "record.json"
        saved = json.loads(path.read_text())
        saved["runtime_path"] = str(self.installer)
        path.write_text(json.dumps(saved))
        before = self.installer.read_bytes()
        result = self.command("cancel", record["update_id"], "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.installer.read_bytes(), before)

    def test_only_cancelled_empty_stage_permits_cli_only_bootstrap(self):
        staged = self.stage()
        self.assertEqual(staged.returncode, 0, staged.stderr)
        record = json.loads(staged.stdout)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())
        cancelled = self.command("cancel", record["update_id"], "--json")
        self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.target.is_file())
        self.assertFalse((self.store / "profiles/internal").exists())

    def test_terminating_public_stage_stops_installer_before_return(self):
        self.assert_public_update_cancellation(staged=True)

    def test_terminating_one_shot_stops_installer_before_return(self):
        self.write(self.target, '#!/bin/sh\necho "codex-cli 1.0.0"\n')
        profile = self.store / "profiles/internal"
        profile.mkdir(parents=True)
        (profile / "manifest.json").write_text(json.dumps({"name": "internal",
            "codex_bin": str(self.target), "managed_files": ["config.toml"]}))
        (profile / "config.toml").write_text('model="example"\n')
        self.assert_public_update_cancellation(staged=False)

    def assert_public_update_cancellation(self, *, staged):
        started = self.root / "installer-started"
        self.env["TEST_STARTED"] = str(started)
        self.write_installer(before='echo $$ > "$TEST_STARTED"\nsleep 20')
        command = [str(WRAPPER), "--skip-self-update", "--store-dir", str(self.store), "update-internal"]
        if staged:
            command.append("stage")
        command += ["--version", "2.0.0", "--install-dir", str(self.target.parent), "--skip-source-check", "--skip-proxy"]
        before = self.target.read_bytes() if self.target.exists() else None
        child = subprocess.Popen(command, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists(), "installer did not start")
            installer_pid = int(started.read_text())
            installer_group = os.getpgid(installer_pid)
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=15)
            with self.assertRaises(ProcessLookupError):
                os.kill(installer_pid, 0)
            self.assertEqual(self.target.read_bytes() if self.target.exists() else None, before)
        finally:
            if "installer_group" in locals():
                try:
                    os.killpg(installer_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()

    def test_one_shot_cancellation_during_both_child_start_windows(self):
        function = "run_update_engine_and_wait() {" + WRAPPER.read_text().split(
            "run_update_engine_and_wait() {", 1)[1].split("\nrun_update_internal_env_setup()", 1)[0]
        for trigger in ('"$@"', 'update_child=$!'):
            with self.subTest(trigger=trigger):
                script = self.root / "cancel-window.sh"
                script.write_text('set -uo pipefail\n' + function + '''
injected=0
trigger="$1"
set -T
trap 'if [[ "$BASH_COMMAND" == "$trigger" && "$injected" == 0 ]]; then injected=1; kill -TERM "$$"; fi' DEBUG
run_update_engine_and_wait /bin/sleep 20
rc=$?
trap - DEBUG
[[ "$injected" == 1 ]] || exit 97
exit "$rc"
''')
                result = subprocess.run(["bash", str(script), trigger], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 143, result.stdout + result.stderr)


class CatalogInputFingerprintTests(unittest.TestCase):
    def test_managed_custom_origin_changes_input_fingerprint(self):
        import codex_switch_update as update
        import test_codex_model_catalog_routing as routing
        from codex_switch_store import Store
        fixture = routing.ModelCatalogRoutingTests()
        with patch.object(tempfile, "tempdir", str(Path(tempfile.gettempdir()).resolve())):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.explicit_catalog()
        candidate = fixture.rebound(fixture.prepare())
        store = Store(fixture.root / "store", fixture.official_home,
                      fixture.root / "agent.plist", internal_codex_home=fixture.internal_home)
        store.manifest_path("internal").write_text(json.dumps(dict(candidate.internal_manifest), default=dict))
        before = update.source_inputs(store, None)[0]
        source.write_bytes(source.read_bytes() + b"\n")
        after = update.source_inputs(store, None)[0]
        self.assertNotEqual(update.digest(before), update.digest(after))


class StagedEngineIntegrationTests(unittest.TestCase):
    """Native transaction/handshake integration; parity policy has its own suite."""

    @contextmanager
    def prepared_update(self, *, staged=False, first=False):
        import codex_switch_update as update
        import codex_switch_bindings as bindings
        import test_codex_runtime_binding as fixture_module
        from contextlib import redirect_stdout
        import io
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = fixture_module.RuntimeBindingTests()
            store, target, _launcher, _binding = fixture.write_internal_store_fixture(root)
            fixture.write_rebind_backend(target)
            bundle, *_ = fixture.parity_rebind_bundle_fixture(root, store)
            source = root / "private"
            source.mkdir()
            (source / "config.toml").write_bytes((store.profile_dir("internal") / "config.toml").read_bytes())
            args = fixture.rebind_args(store, target)
            args.current = True
            args.version = None
            args.internal_bin = str(target)
            args.install_dir = None
            args.from_codex_home = source
            reference = {"present": True, "bundle_path": str(fixture.make_roots(root).chatgpt)}
            if staged:
                retained = fixture.write_rebind_backend(root / "installer-source/codex")
                helper = root / "installer-helper"
                helper.write_text('#!/usr/bin/env python3\nimport sys,pathlib,shutil\n'
                    'p=pathlib.Path(sys.argv[sys.argv.index("--install-dir")+1]);p.mkdir(mode=0o700)\n'
                    f'shutil.copy2({str(retained)!r},p/"codex")\n')
                helper.chmod(0o755)
                args.helper = str(helper)
                args.current = False
                args.version = "0.142.4"
            if first:
                target.unlink()
                store.manifest_path("internal").unlink()
                (store.profile_dir("internal") / "config.toml").unlink()
                store.profile_dir("internal").rmdir()
                store.active_path.unlink()
                (store.bin_dir / "codex-internal-app").unlink()
            def prepared(candidate, **_kwargs):
                result = fixture.bind_staged_capability_receipt(bundle, candidate)
                runtime = Path(record["runtime_path"])
                result.receipt.internal_fingerprint = SimpleNamespace(backend_cli=target, binary_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest())
                return result
            with patch.object(update, "desktop_reference", return_value=reference), \
                    patch.object(bindings, "prepare_parity_bundle", side_effect=prepared) as prepare, \
                    patch.object(bindings, "revalidate_parity_bundle_inputs"), \
                    patch.object(bindings, "revalidate_parity_bundle_immutable_inputs"), redirect_stdout(io.StringIO()):
                record = update.stage(args, store)
                args.update_id = record["update_id"]
                yield update, store, args, record, prepare

    def test_current_apply_commit_replay_and_different_input_refusal(self):
        with self.prepared_update() as (update, store, args, record, prepare):
                result = update.apply_update(args, store)
                self.assertEqual(result["state"], "applied")
                calls = prepare.call_count
                again = update.apply_update(args, store)
                self.assertEqual(again["transaction_id"], result["transaction_id"])
                self.assertEqual(prepare.call_count, calls)
                self.assertEqual(update.status(args, store)["state"], "applied")
                self.assertEqual(update.cancel(args, store)["state"], "applied")
                self.assertTrue(Path(record["runtime_path"]).is_file())
                with (args.from_codex_home / "config.toml").open("a") as stream:
                    stream.write('\nchanged = true\n')
                with self.assertRaisesRegex(update.UpdateError, "different inputs"):
                    update.apply_update(args, store)

    def test_staged_first_full_apply_keeps_retained_runtime_and_publishes_profile(self):
        with self.prepared_update(staged=True, first=True) as (update, store, args, record, _prepare):
            self.assertFalse(store.profile_dir("internal").exists())
            result = update.apply_update(args, store)
            self.assertEqual(result["state"], "applied")
            self.assertTrue(store.manifest_path("internal").is_file())
            self.assertEqual(Path(record["runtime_path"]).read_bytes(), Path(args.internal_bin).read_bytes())

    def test_source_drift_during_preparation_is_refused_without_publication(self):
        with self.prepared_update() as (update, store, args, record, prepare):
            before = (store.profile_dir("internal") / "config.toml").read_bytes()
            original = prepare.side_effect
            def changed(*values, **keywords):
                bundle = original(*values, **keywords)
                (args.from_codex_home / "config.toml").write_text('model="changed"\n')
                return bundle
            prepare.side_effect = changed
            with self.assertRaisesRegex(update.UpdateError, "source configuration"):
                update.apply_update(args, store)
            self.assertEqual((store.profile_dir("internal") / "config.toml").read_bytes(), before)
            self.assertEqual(update.status(args, store)["state"], "stale")

    def test_cancel_checks_transaction_owner_after_acquiring_store_lock(self):
        import codex_switch_transaction as transaction
        with self.prepared_update() as (update, store, args, record, _prepare):
            marker = store.root / ".runtime-binding-rebind.json"
            marker.write_text(json.dumps({"update": {"update_id": record["update_id"]}}))
            original_enter = transaction._StoreLock.__enter__
            foreign = {"update": {"update_id": "f" * 24}}
            def replaced_before_lock(lock):
                marker.write_text(json.dumps(foreign))
                return original_enter(lock)
            with patch.object(transaction._StoreLock, "__enter__", replaced_before_lock), \
                    patch.object(transaction, "_recover_runtime_binding_rebind") as recover:
                with self.assertRaisesRegex(update.UpdateError, "Another transaction"):
                    update.cancel(args, store)
                recover.assert_not_called()
            self.assertEqual(json.loads(marker.read_text()), foreign)

    def test_process_death_after_commit_is_read_only_confirmed_and_not_replayed(self):
        import multiprocessing
        with self.prepared_update() as (update, store, args, record, _prepare):
            args.rebind_commit_fault_hook = lambda phase: os._exit(77) if phase == "after_committed_marker" else None
            child = multiprocessing.get_context("fork").Process(target=update.apply_update, args=(args, store))
            child.start()
            child.join(30)
            if child.is_alive():
                child.kill()
                child.join()
                self.fail("apply child did not terminate")
            self.assertEqual(child.exitcode, 77)
            marker = store.root / ".runtime-binding-rebind.json"
            before = marker.read_bytes()
            self.assertEqual(update.status(args, store)["state"], "applied")
            self.assertEqual(marker.read_bytes(), before)
            self.assertEqual(update.apply_update(args, store)["state"], "applied")
            self.assertEqual(marker.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
