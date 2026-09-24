"""Private staged preparation and native transaction publication contracts."""
from __future__ import annotations

import tempfile
import json
import hashlib

import codex_switch_transaction as transaction
from codex_switch_store import Store
import unittest
from pathlib import Path

from codex_switch_parity import ConfigInputs, prepare_parity_config_projection


class PrivateConfigProjectionTests(unittest.TestCase):
    def test_private_seed_projects_to_canonical_profile_without_live_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'store/profiles/internal/config.toml'
            source = root / 'private/config.toml'
            shared = root / 'official/config.toml'
            source.parent.mkdir()
            shared.parent.mkdir()
            source.write_text('model = "requested"\n[hooks]\nenabled = true\n')
            shared.write_text('model = "old"\n')
            inputs = ConfigInputs.capture(
                profile_config=profile, profile_source=source,
                source_paths=(source, shared),
            )
            projection = prepare_parity_config_projection(
                config_inputs=inputs,
                overlay_path=profile.parent / 'parity/model-catalog.json',
            )
            self.assertTrue(projection.healthy)
            self.assertIn(b'model = "requested"', projection.payload_for(profile))
            self.assertIn(b'[hooks]', projection.payload_for(profile))
            self.assertFalse(profile.exists())
            self.assertEqual({p for p, _ in projection.payloads}, {profile, shared})
            self.assertEqual(source.read_text(), 'model = "requested"\n[hooks]\nenabled = true\n')

    def test_explicit_seed_preserves_shared_thread_ambiguity_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'store/profiles/internal/config.toml'
            source = root / 'private/config.toml'
            shared = root / 'official/config.toml'
            source.parent.mkdir()
            shared.parent.mkdir()
            source.write_text('model = "requested"\n[agents]\nmax_threads = 8\n')
            shared.write_text('[agents]\nmax_threads = 4\n')
            inputs = ConfigInputs.capture(profile_config=profile, profile_source=source, source_paths=(source, shared))
            projection = prepare_parity_config_projection(config_inputs=inputs, overlay_path=profile.parent / 'parity/model-catalog.json')
            self.assertFalse(projection.healthy)
            self.assertEqual(projection.findings[0].code, "parity.config.max_threads_ambiguous")
            self.assertEqual(shared.read_bytes(), b'[agents]\nmax_threads = 4\n')


class AbsentPublicationTests(unittest.TestCase):
    def test_cli_only_publication_returns_bound_receipt_and_revalidates_once(self):
        from codex_switch_bindings import _promote_internal_cli_update
        from test_codex_runtime_binding import RuntimeBindingTests
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = RuntimeBindingTests()
            store, target, _launcher, _binding = fixture.write_internal_store_fixture(root)
            fixture.write_rebind_backend(target, version="0.142.3")
            before = target.read_bytes()
            stage = target.parent / ".codex-internal-update-cli-test"
            stage.mkdir(mode=0o700)
            candidate = fixture.write_rebind_backend(stage / "codex")
            swap = transaction.RuntimeBindingExecutableSwap(target, candidate, target.with_name("codex.backup"), 0o755,
                hashlib.sha256(before).hexdigest(), 0o755, hashlib.sha256(candidate.read_bytes()).hexdigest())
            calls = []
            def revalidate():
                calls.append(target.read_bytes())
                self.assertEqual(target.read_bytes(), before)
            receipt = _promote_internal_cli_update(store, manifest=store.load_manifest("internal"),
                executable_swap=swap, target_version="0.142.4", update_id="cli-bound",
                input_fingerprint="f" * 64, frozen_input_validator=revalidate)
            self.assertEqual(calls, [before])
            self.assertEqual(receipt["state"], "committed")
            self.assertEqual(receipt["bundle_scope"], "cli-only")
            self.assertEqual(receipt, transaction.read_runtime_binding_terminal_receipt(store, "cli-bound", input_fingerprint="f" * 64))

    def test_first_profile_publication_retains_a_bound_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "store", root / "official", root / "launch.plist")
            store.ensure()
            profile = store.profile_dir("internal")
            launcher = store.bin_dir / "codex-internal-app"
            artifacts = tuple(transaction.RuntimeBindingTextArtifact(role, path, payload, mode) for role, path, payload, mode in (
                ("manifest", store.manifest_path("internal"), b'{"name":"internal"}', 0o600),
                ("launcher", launcher, b'#!/bin/sh\nexit 0\n', 0o755),
                ("capability_receipt", launcher.with_name(launcher.name + '.capabilities.json'), b'{}', 0o600),
                ("parity_receipt", profile / "parity/receipt.json", b'{}', 0o600),
                ("parity_overlay", profile / "parity/model-catalog.json", b'{}', 0o600),
                ("profile_config", profile / "config.toml", b'model = "requested"\n', 0o600),
            ))
            from codex_switch_protocol_adapter import capability_receipt_path_for_launcher
            artifacts = tuple(
                transaction.RuntimeBindingTextArtifact(a.role, capability_receipt_path_for_launcher(launcher), a.payload, a.mode)
                if a.role == 'capability_receipt' else a for a in artifacts
            )
            target = root / 'target/codex'
            stage = target.parent / '.codex-internal-update-test'
            stage.mkdir(parents=True, mode=0o700)
            candidate = stage / 'codex'
            candidate.write_bytes(b'#!/bin/sh\nexit 0\n')
            candidate.chmod(0o755)
            swap = transaction.RuntimeBindingExecutableSwap(target, candidate, target.with_name('codex.backup'), None, None, 0o755, hashlib.sha256(candidate.read_bytes()).hexdigest())
            with transaction.locked_store_mutation(store, operation="test first full apply") as lock:
                receipt = transaction.commit_runtime_binding_bundle(
                    lock, artifacts=artifacts, executable_swap=swap, allow_absent_profile=True,
                    update_id="update-test", input_fingerprint="a" * 64,
                )
            self.assertEqual(target.read_bytes(), b'#!/bin/sh\nexit 0\n')
            self.assertEqual(receipt['state'], 'committed')
            self.assertEqual(receipt['update_id'], 'update-test')
            self.assertFalse((store.root / '.runtime-binding-rebind.json').exists())
            retained = transaction.read_runtime_binding_terminal_receipt(store, 'update-test', input_fingerprint='a' * 64)
            self.assertEqual(retained, receipt)
            self.assertEqual((profile / "config.toml").read_bytes(), b'model = "requested"\n')
            with self.assertRaises(Exception):
                transaction.read_runtime_binding_terminal_receipt(store, 'update-test', input_fingerprint='b' * 64)


class TransactionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'store', self.root / 'official', self.root / 'agent.plist')
        self.store.ensure()
        self.profile = self.store.profile_dir('internal')
        from codex_switch_protocol_adapter import capability_receipt_path_for_launcher
        launcher = self.store.bin_dir / 'codex-internal-app'
        self.artifacts = tuple(transaction.RuntimeBindingTextArtifact(role, path, payload, mode) for role, path, payload, mode in (
            ('manifest', self.store.manifest_path('internal'), b'{"name":"internal"}', 0o600),
            ('launcher', launcher, b'#!/bin/sh\nexit 0\n', 0o755),
            ('capability_receipt', capability_receipt_path_for_launcher(launcher), b'{}', 0o600),
            ('parity_receipt', self.profile / 'parity/receipt.json', b'{}', 0o600),
            ('parity_overlay', self.profile / 'parity/model-catalog.json', b'{}', 0o600),
            ('profile_config', self.profile / 'config.toml', b'model = "new"\n', 0o600),
            ('profile_auth', self.profile / 'auth.json', None, 0o600),
            ('active_runtime_auth', self.store.managed_home('internal') / 'auth.json', None, 0o600),
        ))

    def commit(self, **options):
        with transaction.locked_store_mutation(self.store, operation='isolated publication') as lock:
            return transaction.commit_runtime_binding_bundle(lock, artifacts=self.artifacts,
                allow_absent_profile=True, update_id='recovery-test', input_fingerprint='d' * 64, **options)

    def test_auth_absence_deletes_old_auth_in_both_canonical_locations(self):
        for path in (self.profile / 'auth.json', self.store.managed_home('internal') / 'auth.json'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'{"access_token":"isolated-test"}')
        self.commit()
        for path in (self.profile / 'auth.json', self.store.managed_home('internal') / 'auth.json'):
            self.assertFalse(path.exists())

    def test_failed_postconditions_restore_auth_and_old_absence(self):
        auth = self.store.managed_home('internal') / 'auth.json'
        auth.parent.mkdir(parents=True)
        auth.write_bytes(b'{"access_token":"isolated-test"}')
        def fail():
            raise ValueError('installed postcondition failed')
        with self.assertRaisesRegex(ValueError, 'postcondition'):
            self.commit(prepared_validator=fail)
        self.assertEqual(auth.read_bytes(), b'{"access_token":"isolated-test"}')
        self.assertFalse(self.store.manifest_path('internal').exists())
        self.assertFalse((self.profile / 'config.toml').exists())
        self.assertFalse(self.profile.exists())
        self.assertEqual(transaction.read_runtime_binding_terminal_receipt(self.store, 'recovery-test')['state'], 'rolled_back')

    def test_failed_first_runtime_publication_restores_absent_target_and_retains_candidate(self):
        target = self.root / 'target/codex'
        stage = target.parent / '.codex-internal-update-rollback'
        stage.mkdir(parents=True, mode=0o700)
        candidate = stage / 'codex'
        candidate.write_bytes(b'#!/bin/sh\nexit 0\n')
        candidate.chmod(0o755)
        swap = transaction.RuntimeBindingExecutableSwap(target, candidate, target.with_name('codex.backup'), None, None, 0o755, hashlib.sha256(candidate.read_bytes()).hexdigest())
        def fail():
            raise ValueError('installed-path validation')
        with self.assertRaisesRegex(ValueError, 'installed-path'):
            self.commit(executable_swap=swap, prepared_validator=fail)
        self.assertFalse(target.exists())
        self.assertTrue(candidate.exists())
        self.assertFalse(self.profile.exists())

    def test_uncommitted_interruption_recovers_only_unchanged_owned_artifacts(self):
        def interrupt(phase):
            if phase == 'after_profile_config':
                raise SystemExit(92)
        with self.assertRaises(SystemExit):
            self.commit(fault_hook=interrupt)
        self.assertIsNone(transaction.read_runtime_binding_terminal_receipt(self.store, 'recovery-test'))
        with transaction.locked_store_mutation(self.store, operation='recover isolated interruption') as lock:
            lock.revalidate()
        self.assertFalse(self.profile.exists())
        self.assertEqual(transaction.read_runtime_binding_terminal_receipt(self.store, 'recovery-test')['state'], 'rolled_back')

    def test_recovery_preserves_foreign_profile_edits_and_pending_journal(self):
        def interrupt(phase):
            if phase == 'after_profile_config':
                raise SystemExit(92)
        with self.assertRaises(SystemExit):
            self.commit(fault_hook=interrupt)
        config = self.profile / 'config.toml'
        config.write_bytes(b'model = "foreign"\n')
        with self.assertRaisesRegex(Exception, 'foreign'):
            with transaction.locked_store_mutation(self.store, operation='recover drifted interruption'):
                pass
        self.assertEqual(config.read_bytes(), b'model = "foreign"\n')
        self.assertTrue((self.store.root / '.runtime-binding-rebind.json').exists())

    def test_terminal_receipt_rejects_incomplete_artifact_evidence(self):
        receipt = self.commit()
        path = Path(receipt['receipt_path'])
        damaged = dict(receipt)
        damaged['artifacts'] = []
        path.write_text(json.dumps(damaged))
        with self.assertRaisesRegex(Exception, 'terminal receipt'):
            transaction.read_runtime_binding_terminal_receipt(self.store, 'recovery-test')

    def test_read_only_status_resolves_committed_marker_before_cleanup(self):
        def interrupt(phase):
            if phase == 'after_committed_marker':
                raise SystemExit(91)
        with self.assertRaises(SystemExit):
            self.commit(fault_hook=interrupt)
        marker_path = self.store.root / '.runtime-binding-rebind.json'
        before = marker_path.read_bytes()
        receipt = transaction.read_runtime_binding_terminal_receipt(self.store, 'recovery-test', input_fingerprint='d' * 64)
        self.assertEqual(receipt['state'], 'committed')
        self.assertEqual(marker_path.read_bytes(), before)
        self.assertNotIn('payload', json.dumps(receipt))



class PrivateBindingTests(unittest.TestCase):
    def test_private_first_publication_passes_installed_native_handshake(self):
        self._assert_private_full_publication(absent_target=True)

    def test_current_runtime_without_profile_passes_installed_native_handshake(self):
        self._assert_private_full_publication(absent_target=False)

    def _assert_private_full_publication(self, *, absent_target):
        import codex_switch_bindings as bindings
        import test_codex_runtime_binding as fixture_module
        from unittest.mock import patch
        from types import SimpleNamespace
        from contextlib import redirect_stdout
        import io
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = fixture_module.RuntimeBindingTests()
            store, target, _launcher, _binding = fixture.write_internal_store_fixture(root)
            bundle, *_rest = fixture.parity_rebind_bundle_fixture(root, store)
            source = root / 'private'
            source.mkdir()
            (source / 'config.toml').write_bytes((store.profile_dir('internal') / 'config.toml').read_bytes())
            store.manifest_path('internal').unlink()
            (store.profile_dir('internal') / 'config.toml').unlink()
            store.profile_dir('internal').rmdir()
            store.active_path.unlink()
            if absent_target:
                target.unlink()
                retained = fixture.write_rebind_backend(root / 'retained/codex')
                stage = target.parent / '.codex-internal-update-private'
                stage.mkdir(mode=0o700)
                publication = stage / 'codex'
                shutil.copyfile(retained, publication)
                publication.chmod(0o755)
                swap = transaction.RuntimeBindingExecutableSwap(target, publication, target.with_name('codex.backup'), None, None, 0o755, hashlib.sha256(publication.read_bytes()).hexdigest())
            else:
                retained = fixture.write_rebind_backend(target)
                swap = None
            args = fixture.rebind_args(store, target)
            args.rebind_execution_codex_bin = str(retained)
            args.rebind_executable_swap = swap
            args.rebind_private_inputs = bindings.PrivateRuntimePreparation(
                candidate_manifest={'name': 'internal'}, source_home=source,
                update_id='first-handshake', input_fingerprint='e' * 64,
                frozen_input_validator=lambda: None, allow_absent_profile=True,
            )
            args.rebind_prepared_validator = lambda result: bindings._verify_internal_update_promotion(
                store, result=result, executable_swap=swap, target_version='0.142.4')
            def prepared(candidate, **_kwargs):
                self.assertFalse(store.profile_dir('internal').exists())
                self.assertEqual(candidate.source_config.profile_source, source / 'config.toml')
                result = fixture.bind_staged_capability_receipt(bundle, candidate)
                result.receipt.internal_fingerprint = SimpleNamespace(backend_cli=target, binary_sha256=hashlib.sha256(retained.read_bytes()).hexdigest())
                return result
            with patch.object(bindings, 'prepare_parity_bundle', side_effect=prepared), patch.object(bindings, 'revalidate_parity_bundle_inputs'), patch.object(bindings, 'revalidate_parity_bundle_immutable_inputs'), redirect_stdout(io.StringIO()):
                result = bindings.cmd_set_bin(args)
            self.assertEqual(result.terminal_receipt['state'], 'committed')
            self.assertTrue(retained.exists())
            self.assertEqual(target.read_bytes(), retained.read_bytes())
            self.assertFalse((store.profile_dir('internal') / 'auth.json').exists())
            self.assertNotIn('internal_config_source', store.load_manifest('internal'))

    def test_private_context_does_not_publish_profile_when_desktop_is_absent(self):
        import codex_switch_bindings as bindings
        from codex_switch_runtime_binding import DesktopInventory
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / 'store', root / 'official', root / 'launch.plist')
            store.ensure()
            source = root / 'private'
            source.mkdir()
            (source / 'config.toml').write_text('model = "test"\n')
            backend = root / 'codex'
            backend.write_text('#!/bin/sh\nexit 0\n')
            backend.chmod(0o755)
            context = bindings.PrivateRuntimePreparation(
                candidate_manifest={'name': 'internal'}, source_home=source,
                update_id='private-test', input_fingerprint='c' * 64,
                frozen_input_validator=lambda: None, allow_absent_profile=True,
            )
            args = SimpleNamespace(name='internal', store_dir=store.root,
                official_codex_home=store.official_codex_home,
                launch_agent_path=store.launch_agent_path, launch_agent_label='',
                codex_bin=str(backend), preserve_app_cli=False,
                rebind_private_inputs=context,
                rebind_desktop_inventory=DesktopInventory(current=None))
            with self.assertRaises(Exception) as error:
                bindings.cmd_set_bin(args)
            self.assertNotIn('Profile not found', str(error.exception))
            self.assertFalse(store.profile_dir('internal').exists())


if __name__ == '__main__':
    unittest.main()
