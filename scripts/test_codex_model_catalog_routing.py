"""Full-preparation regressions for explicit and default model catalogs."""
from __future__ import annotations

import hashlib
import json
import plistlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import codex_switch_parity as parity
import test_codex_parity as fixtures
from codex_switch_protocol_adapter import BackendCapabilities, CapabilityReceipt, CapabilityReceiptArtifact, protocol_adapter_rule_set_digest
from codex_switch_runtime_binding import CURRENT_CHATGPT_BUNDLE_ID, ChatGPTDesktopHost, RuntimeBinding
from test_codex_parity import (
    RecordingFeatureRunner, feature_result,
    protocol_documents, write_executable,
)


class ModelCatalogRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="codex-catalog-routing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "store/profiles/internal/config.toml"
        self.official_home = self.root / "official-home"
        self.internal_home = self.root / "internal-home"
        for p in (self.profile.parent, self.official_home, self.internal_home):
            p.mkdir(parents=True)
        self.profile_payload = (
            'model = "model-a"\nmodel_provider = "provider"\n'
            '[model_providers.provider]\n'
            'base_url = "https://provider.example/api"\nwire_api = "responses"\n'
            'env_key = "TEST_MODEL_API_KEY"\n'
        )
        self.profile.write_text(self.profile_payload)
        self.shared = self.official_home / "config.toml"
        self.shared.write_text('[notice]\nkeep = true\n')
        self.internal_cache = self.internal_home / "models_cache.json"
        self.official_cache = self.official_home / "models_cache.json"
        self.model_payload = b'{"models":[{"slug":"model-a","multi_agent_version":"v2"}]}'
        self.internal_cache.write_bytes(self.model_payload)
        self.official_cache.write_bytes(self.model_payload)
        bundle_root = self.root / "Applications/ChatGPT.app"
        contents = bundle_root / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": CURRENT_CHATGPT_BUNDLE_ID,
            "CFBundleShortVersionString": "1.2026.196",
        }))
        official_cli = write_executable(contents / "Resources/codex")
        main = write_executable(contents / "MacOS/ChatGPT")
        internal_cli = write_executable(self.root / "candidate/codex")
        canonical_cli = write_executable(self.root / "bound/codex")
        official_binding = RuntimeBinding(
            profile="openai-official", shell_cli=official_cli,
            desktop_cli=official_cli, backend_cli=official_cli,
            codex_home=self.official_home, requires_proxy=False,
            desktop_host=ChatGPTDesktopHost(
                kind="chatgpt", bundle_root=bundle_root,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_executable=main, bundled_cli=official_cli,
                healthy=True, migration_only=False,
            ),
        )
        internal_binding = RuntimeBinding(
            profile="internal", shell_cli=internal_cli,
            desktop_cli=self.root / "store/bin/codex-internal-app",
            backend_cli=internal_cli, codex_home=self.internal_home,
            desktop_host=None, requires_proxy=True,
        )
        self.schema = json.dumps({"schema_documents": protocol_documents()}, sort_keys=True).encode()
        capability = CapabilityReceipt(
            backend_sha256=hashlib.sha256(internal_cli.read_bytes()).hexdigest(),
            schema_sha256=hashlib.sha256(self.schema).hexdigest(),
            capabilities=BackendCapabilities(True, True, True),
        )
        payload = (json.dumps(capability.to_dict(), sort_keys=True, indent=2) + "\n").encode()
        self.candidate = parity.ParityCandidate(
            official_binding=official_binding, internal_binding=internal_binding,
            canonical_internal_binding=replace(internal_binding, shell_cli=canonical_cli, backend_cli=canonical_cli),
            internal_manifest={"name": "internal", "codex_bin": str(canonical_cli), "app_cli_path": str(internal_binding.desktop_cli)},
            capability_receipt=CapabilityReceiptArtifact(
                receipt=capability, payload=payload,
                payload_sha256=hashlib.sha256(payload).hexdigest(), reused=False,
            ),
            source_config=parity.ConfigInputs.capture(profile_config=self.profile, source_paths=(self.profile, self.shared)),
            adapter_rule_set_sha256=protocol_adapter_rule_set_digest(),
        )
        self.work = self.root / "work"
        self.work.mkdir(mode=0o700)

    def prepare(self, candidate=None, *, probe_runner=None, catalog_loader=None, timeouts=None, optional_sources=(), profile_source=None):
        selected = candidate or self.candidate
        input_options = {"optional_sources": optional_sources} if optional_sources else {}
        selected = replace(selected, source_config=parity.ConfigInputs.capture(
            profile_config=self.profile, profile_source=profile_source, source_paths=(profile_source or self.profile, self.shared), **input_options,
        ))
        def success(request):
            return parity.ParityProbeCommandResult(
                returncode=0,
                stdout=fixtures.ParityPreparationTests.CORE_SUCCESS if request.name == "core_protocol" else fixtures.ParityPreparationTests.TYPED_SUCCESS,
                stderr="",
            )
        return parity.prepare_parity_bundle(
            selected, work_root=self.work,
            timeouts=timeouts or parity.ParityTimeouts(command_seconds=1.0, probe_seconds=1.0),
            _schema_loader=lambda _path, _timeout: self.schema,
            _version_loader=lambda _path, _timeout: "codex-cli 0.155.0",
            _feature_runner=RecordingFeatureRunner([feature_result("multi_agent_v2  stable  true\n") for _ in range(4)]),
            _probe_runner=probe_runner or success,
            _catalog_loader=catalog_loader,
        )

    def test_explicit_private_seed_controls_final_runtime_and_probes_disable_hooks(self):
        source = self.root / "private/config.toml"
        source.parent.mkdir()
        source.write_text(self.profile_payload + '[hooks]\nenabled = true\n[[hooks.start]]\ncommand = "touch forbidden"\n')
        self.profile.write_text('model = "obsolete"\n')
        (self.internal_home / "config.toml").write_text('model = "obsolete"\nnotify = ["bad"]\n')
        observed = []
        def probe(request):
            observed.append(request)
            config = (request.codex_home / "config.toml").read_text()
            self.assertNotIn('hooks', config)
            self.assertNotIn('forbidden', config)
            self.assertIn('model = "model-a"', config)
            return parity.ParityProbeCommandResult(returncode=0, stdout=fixtures.ParityPreparationTests.CORE_SUCCESS if request.name == "core_protocol" else fixtures.ParityPreparationTests.TYPED_SUCCESS, stderr="")
        bundle = self.prepare(profile_source=source, probe_runner=probe)
        self.assertTrue(observed)
        self.assertIn(b'[hooks]', bundle.config_projection.payload_for(self.profile))
        self.assertNotIn(b'obsolete', bundle.staged_runtime_config_payload)
        self.assertNotIn(b'hooks', bundle.staged_runtime_config_payload)
        self.assertEqual(self.profile.read_text(), 'model = "obsolete"\n')

    def test_private_auth_is_available_only_in_isolated_native_probe_home(self):
        auth = b'{"OPENAI_API_KEY":"isolated-fixture-value"}'
        candidate = replace(self.candidate, auth_payload=auth)
        observed = []
        def probe(request):
            observed.append(request.codex_home)
            self.assertEqual((request.codex_home / "auth.json").read_bytes(), auth)
            self.assertEqual((request.codex_home / "auth.json").stat().st_mode & 0o777, 0o600)
            return parity.ParityProbeCommandResult(returncode=0, stdout=fixtures.ParityPreparationTests.CORE_SUCCESS if request.name == "core_protocol" else fixtures.ParityPreparationTests.TYPED_SUCCESS, stderr="")
        bundle = self.prepare(candidate, probe_runner=probe)
        self.assertTrue(bundle.healthy)
        self.assertTrue(observed)
        self.assertFalse((self.internal_home / "auth.json").exists())
        self.assertNotIn(b'isolated-fixture-value', bundle.receipt_payload)

    def test_private_seed_uses_native_rendering_without_old_profile_layers(self):
        from codex_switch_home_sync import plugin_support_snapshot_name
        source = self.root / "private/config.toml"
        source.parent.mkdir()
        source.write_text(self.profile_payload)
        (self.internal_home / "config.toml").write_text('model = "obsolete"\nnotify = ["removed-hook"]\n')
        (self.internal_home / "internal.config.toml").write_text('model = "obsolete"\n[hooks]\nenabled = true\n')
        (self.profile.parent / plugin_support_snapshot_name("internal")).write_text('[hooks.state."removed:stop:0"]\ntrusted_hash = "sha256:old"\n')
        bundle = self.prepare(replace(self.candidate, active_runtime_config_path=self.internal_home / "config.toml"), profile_source=source)
        runtime = bundle.active_runtime_config_payload
        self.assertIn(b'# codex-switch: managed runtime config for profile internal', runtime)
        self.assertIn(b'keep = true', runtime)
        self.assertNotIn(b'obsolete', runtime)
        self.assertNotIn(b'removed-hook', runtime)
        self.assertNotIn(b'[hooks', runtime)

    def test_private_preparation_remains_verified_across_ordinary_activation(self):
        from unittest.mock import patch
        from codex_switch_home_sync import plugin_support_snapshot_name
        from codex_switch_protocol_adapter import capability_receipt_path_for_launcher
        from codex_switch_store import Store
        from codex_switch_transaction import TransactionRequest, execute_transaction
        from codex_switch_verify import validate_prepared_parity_config
        source = self.root / "private/config.toml"
        source.parent.mkdir()
        source.write_text(self.profile_payload)
        (self.profile.parent / plugin_support_snapshot_name("internal")).write_text('[hooks.state."removed:stop:0"]\ntrusted_hash = "sha256:old"\n')
        runtime = self.internal_home / "config.toml"
        bundle = self.prepare(replace(self.candidate, active_runtime_config_path=runtime), profile_source=source)
        rebound = self.rebound(bundle)
        bundle.receipt.overlay_path.with_name("receipt.json").chmod(0o600)
        bundle.receipt.overlay_path.chmod(0o600)
        runtime.write_bytes(bundle.active_runtime_config_payload)
        self.shared.write_bytes(bundle.config_projection.payload_for(self.shared))
        store = Store(self.root / "store", self.official_home, self.root / "agent.plist", internal_codex_home=self.internal_home)
        store.ensure()
        launcher = rebound.canonical_internal_binding.desktop_cli
        launcher.parent.mkdir(exist_ok=True)
        write_executable(launcher)
        capability_path = capability_receipt_path_for_launcher(launcher)
        capability_path.write_bytes(rebound.capability_receipt.payload)
        manifest = dict(rebound.internal_manifest)
        manifest.update({
            "codex_home": str(self.internal_home), "home_selection_confirmed": True,
            "app_capability_receipt_path": str(capability_path),
            "app_capability_receipt_sha256": rebound.capability_receipt.payload_sha256,
            "app_schema_sha256": rebound.capability_receipt.receipt.schema_sha256,
        })
        store.manifest_path("internal").write_text(json.dumps(manifest))
        store.profile_dir("openai-official").mkdir()
        store.manifest_path("openai-official").write_text(json.dumps({
            "name": "openai-official", "codex_home": str(self.official_home),
            "home_selection_confirmed": True,
        }))
        request = TransactionRequest(operation="switch", profile="internal", options={
            "config_mode": "shared", "skip_shim": True, "skip_app_cli": True, "skip_launchctl": True,
        })
        for _ in range(2):
            with patch("codex_switch_protocol_adapter.prepare_capability_receipt_artifact", return_value=rebound.capability_receipt):
                result = execute_transaction(store, request)
            self.assertEqual(result.outcome, "committed")
            self.assertEqual(runtime.read_bytes(), bundle.active_runtime_config_payload)
            self.assertNotIn(b"[hooks", runtime.read_bytes())
            projection = parity.prepare_parity_config_projection(
                config_inputs=parity.ConfigInputs.capture(profile_config=self.profile, source_paths=(self.profile, self.shared)),
                overlay_path=bundle.receipt.overlay_path,
            )
            validate_prepared_parity_config(store, projection, runtime.read_bytes())

    def test_absent_catalog_reaches_default_model_comparison(self):
        bundle = self.prepare()
        self.assertTrue(bundle.healthy)
        self.assertNotIn("parity.model.custom_catalog_not_applicable", {f.code for f in bundle.findings})
        self.assertEqual(bundle.overlay.source_catalog, self.internal_cache)
        self.assertEqual(self.profile.read_text(), self.profile_payload)
        self.assertEqual(self.internal_cache.read_bytes(), self.model_payload)
        parity.revalidate_parity_bundle_inputs(bundle)

    def test_absent_official_config_is_explicit_shared_input_until_publication(self):
        self.shared.unlink()
        bundle = self.prepare(optional_sources=(self.shared,))
        self.assertTrue(bundle.healthy)
        self.assertFalse(self.shared.exists())
        self.assertEqual(bundle.config_projection.payload_for(self.shared), b"")
        self.assertIn(self.shared, bundle.config_projection.changed_paths)
        parity.revalidate_parity_bundle_inputs(bundle)
        self.shared.write_bytes(b"")
        with self.assertRaisesRegex(parity.ParityValidationError, "changed|stale"):
            parity.revalidate_parity_bundle_inputs(bundle)

    def test_optional_shared_config_never_hides_invalid_present_sources(self):
        for kind in ("malformed", "symlink", "directory"):
            with self.subTest(kind=kind):
                self.shared.unlink()
                if kind == "malformed":
                    self.shared.write_bytes(b"[broken")
                elif kind == "symlink":
                    self.shared.symlink_to(self.root / "absent-target")
                else:
                    self.shared.mkdir()
                with self.assertRaises(parity.ParityValidationError):
                    self.prepare(optional_sources=(self.shared,))
                if kind == "directory":
                    self.shared.rmdir()
                    self.shared.write_bytes(b"")

    def test_missing_internal_profile_cannot_be_an_optional_source(self):
        self.profile.unlink()
        with self.assertRaises(parity.ParityValidationError):
            self.prepare(optional_sources=(self.profile, self.shared))


    def rebound(self, bundle):
        overlay = bundle.receipt.overlay_path
        overlay.parent.mkdir(mode=0o700, exist_ok=True)
        overlay.write_bytes(bundle.overlay.overlay_payload)
        bundle.receipt.overlay_path.with_name("receipt.json").write_bytes(bundle.receipt_payload)
        self.profile.write_bytes(bundle.config_projection.payload_for(self.profile))
        manifest = dict(self.candidate.internal_manifest)
        manifest.update(bundle.manifest_metadata)
        return replace(self.candidate, internal_manifest=manifest)

    def test_capture_retains_custom_origin_after_health_receipt_is_removed(self):
        from codex_switch_capture import capture_profile
        from codex_switch_store import Store
        source = self.explicit_catalog()
        candidate = self.rebound(self.prepare())
        store = Store(self.root / "store", self.official_home, self.root / "agent.plist", internal_codex_home=self.internal_home)
        store.manifest_path("internal").write_text(json.dumps(dict(candidate.internal_manifest), default=dict))
        (self.internal_home / "config.toml").write_bytes(self.profile.read_bytes())
        capture_profile(store, "internal", self.internal_home,
            str(candidate.canonical_internal_binding.backend_cli), "", True, True)
        manifest = store.load_manifest("internal")
        from codex_switch_home_select import resolve_runtime_homes
        restored_store = Store(store.root, self.official_home, self.root / "agent.plist")
        self.assertEqual(resolve_runtime_homes(restored_store).internal.path, self.internal_home)
        self.assertNotIn("parity_receipt_sha256", manifest)
        self.profile.parent.joinpath("parity/receipt.json").unlink()
        rebound = replace(candidate, internal_manifest=manifest)
        bundle = self.prepare(rebound)
        self.assertTrue(bundle.healthy)
        self.assertEqual(bundle.overlay.source_catalog, source)
        self.assertEqual(bundle.manifest_metadata["parity_model_catalog_kind"], "custom")

    def test_default_origin_survives_recapture_and_still_compares_official_models(self):
        from codex_switch_capture import capture_profile
        from codex_switch_store import Store
        first = self.prepare()
        candidate = self.rebound(first)
        manifest = dict(candidate.internal_manifest)
        manifest["parity_catalog_provenance"] = parity.catalog_provenance_for_bundle(first)
        store = Store(self.root / "store", self.official_home, self.root / "agent.plist", internal_codex_home=self.internal_home)
        store.manifest_path("internal").write_text(json.dumps(manifest, default=dict))
        (self.internal_home / "config.toml").write_bytes(self.profile.read_bytes())
        capture_profile(store, "internal", self.internal_home, str(candidate.canonical_internal_binding.backend_cli), "", True, True)
        self.profile.parent.joinpath("parity/receipt.json").unlink()
        rebound = replace(candidate, internal_manifest=store.load_manifest("internal"))
        self.assertEqual(self.prepare(rebound).manifest_metadata["parity_model_catalog_kind"], "runtime-cache")
        self.official_cache.write_bytes(b'{"models":[{"slug":"model-a","multi_agent_version":"v1"}]}')
        with self.assertRaisesRegex(parity.ParityValidationError, "parity.model.unclassified_drift"):
            self.prepare(rebound)

    def test_recapture_rejects_changed_managed_overlay_without_publishing(self):
        from codex_switch_capture import capture_profile
        from codex_switch_store import Store
        self.explicit_catalog()
        first = self.prepare()
        candidate = self.rebound(first)
        store = Store(self.root / "store", self.official_home, self.root / "agent.plist", internal_codex_home=self.internal_home)
        manifest = dict(candidate.internal_manifest)
        manifest["parity_catalog_provenance"] = parity.catalog_provenance_for_bundle(first)
        original = json.dumps(manifest, default=dict)
        store.manifest_path("internal").write_text(original)
        (self.internal_home / "config.toml").write_bytes(self.profile.read_bytes())
        first.receipt.overlay_path.write_bytes(b'{"models":[]}')
        with self.assertRaises(parity.ParityValidationError):
            capture_profile(store, "internal", self.internal_home, str(candidate.canonical_internal_binding.backend_cli), "", True, True)
        self.assertEqual(store.manifest_path("internal").read_text(), original)

    def test_default_overlay_keeps_official_comparison_on_repeat(self):
        bundle = self.prepare()
        rebound = self.rebound(bundle)
        repeated = self.prepare(rebound)
        self.assertNotIn("parity.model.custom_catalog_not_applicable", {f.code for f in repeated.findings})
        self.official_cache.write_bytes(b'{"models":[{"slug":"model-a","multi_agent_version":"v1"}]}')
        with self.assertRaisesRegex(parity.ParityValidationError, "parity.model.unclassified_drift"):
            self.prepare(rebound)


    def explicit_catalog(self, path=None):
        source = path or self.root / "custom-models.json"
        source.write_bytes(self.model_payload)
        self.profile.write_text(f"model_catalog_json = {json.dumps(str(source))}\n" + self.profile_payload)
        return source

    def test_default_metadata_difference_remains_blocking(self):
        self.official_cache.write_bytes(b'{"models":[{"slug":"model-a","multi_agent_version":"v1"}]}')
        with self.assertRaisesRegex(parity.ParityValidationError, "parity.model.unclassified_drift"):
            self.prepare()
        self.assertEqual(self.profile.read_text(), self.profile_payload)

    def test_explicit_catalog_does_not_use_either_default_cache(self):
        source = self.explicit_catalog()
        self.internal_cache.unlink()
        self.official_cache.unlink()
        bundle = self.prepare()
        self.assertTrue(bundle.healthy)
        self.assertEqual(bundle.overlay.source_catalog, source)
        self.assertIn("parity.model.custom_catalog_not_applicable", {f.code for f in bundle.findings})
        self.official_cache.write_bytes(b'broken-cache')
        parity.revalidate_parity_bundle_inputs(bundle)

    def test_explicit_cache_path_is_still_an_explicit_catalog(self):
        self.explicit_catalog(self.internal_cache)
        self.official_cache.unlink()
        self.assertTrue(self.prepare().healthy)

    def test_invalid_explicit_catalog_never_uses_default_caches(self):
        for value in ['""', '"   "', 'false', '42', '[]']:
            with self.subTest(value=value):
                self.profile.write_text(f"model_catalog_json = {value}\n" + self.profile_payload)
                with self.assertRaises(parity.ParityValidationError) as raised:
                    self.prepare()
                self.assertEqual(raised.exception.code, "parity.preparation.config_invalid")

    def test_explicit_catalog_missing_or_malformed_does_not_fall_back(self):
        source = self.explicit_catalog()
        source.unlink()
        with self.assertRaises(parity.ParityValidationError):
            self.prepare()
        for payload in [b'broken', b'{"models":[]}', b'{"models":[{"slug":"other"}]}']:
            source.write_bytes(payload)
            with self.subTest(payload=payload), self.assertRaises(parity.ParityValidationError):
                self.prepare()

    def test_present_default_caches_are_safely_read(self):
        for cache in (self.internal_cache, self.official_cache):
            for state in ("symlink", "malformed", "missing-model"):
                with self.subTest(cache=cache.name + str(cache.parent), state=state):
                    cache.unlink()
                    if state == "symlink":
                        other = self.root / "cache-target.json"
                        other.write_bytes(self.model_payload)
                        cache.symlink_to(other)
                    elif state == "malformed":
                        cache.write_bytes(b'broken')
                    elif state == "missing-model":
                        cache.write_bytes(b'{"models":[{"slug":"other"}]}')
                    with self.assertRaises(parity.ParityValidationError):
                        self.prepare()
                    if cache.exists() or cache.is_symlink():
                        cache.unlink()
                    cache.write_bytes(self.model_payload)

    def test_cache_changes_before_promotion_are_rejected(self):
        for cache in (self.internal_cache, self.official_cache):
            for mutation in ("content", "mode", "inode"):
                with self.subTest(cache=str(cache), mutation=mutation):
                    bundle = self.prepare()
                    original_mode = cache.stat().st_mode & 0o777
                    if mutation == "content":
                        cache.write_bytes(self.model_payload + b"\n")
                    elif mutation == "mode":
                        cache.chmod(original_mode ^ 0o040)
                    else:
                        saved = cache.with_suffix(".saved")
                        cache.rename(saved)
                        cache.write_bytes(self.model_payload)
                        self.assertNotEqual(cache.stat().st_ino, saved.stat().st_ino)
                    for revalidate in (parity.revalidate_parity_bundle_inputs, parity.revalidate_parity_bundle_immutable_inputs):
                        with self.assertRaises(parity.ParityValidationError):
                            revalidate(bundle)
                    cache.write_bytes(self.model_payload)
                    cache.chmod(original_mode)

    def test_cache_changes_during_probes_are_rejected(self):
        for cache in (self.internal_cache, self.official_cache):
            def mutate(request):
                cache.write_bytes(self.model_payload + b"\n")
                return parity.ParityProbeCommandResult(
                    returncode=0,
                    stdout=fixtures.ParityPreparationTests.CORE_SUCCESS if request.name == "core_protocol" else fixtures.ParityPreparationTests.TYPED_SUCCESS,
                    stderr="",
                )
            with self.subTest(cache=str(cache)), self.assertRaises(parity.ParityValidationError):
                self.prepare(probe_runner=mutate)
            cache.write_bytes(self.model_payload)

    def test_current_overlay_rejects_missing_or_invalid_source_kind(self):
        candidate = self.rebound(self.prepare())
        for value in (None, "unknown", False, [], {}):
            manifest = dict(candidate.internal_manifest)
            if value is None:
                manifest.pop("parity_model_catalog_kind")
            else:
                manifest["parity_model_catalog_kind"] = value
            with self.subTest(value=value), self.assertRaises(parity.ParityValidationError):
                self.prepare(replace(candidate, internal_manifest=manifest))

    def test_malformed_manifest_policy_has_a_stable_validation_error(self):
        candidate = self.rebound(self.prepare())
        for value in (None, False, 3, {}, [{}]):
            manifest = dict(candidate.internal_manifest)
            manifest.pop("parity_model_catalog_kind")
            manifest["parity_policy_version"] = value
            with self.subTest(value=value), self.assertRaises(parity.ParityValidationError):
                self.prepare(replace(candidate, internal_manifest=manifest))

    def test_default_overlay_kind_cannot_be_flipped_to_skip_comparison(self):
        candidate = self.rebound(self.prepare())
        manifest = dict(candidate.internal_manifest)
        manifest["parity_model_catalog_kind"] = "custom"
        with self.assertRaises(parity.ParityValidationError):
            self.prepare(replace(candidate, internal_manifest=manifest))

    def test_default_overlay_rejects_a_different_home_cache(self):
        candidate = self.rebound(self.prepare())
        alternate = self.root / "different-home-cache.json"
        alternate.write_bytes(self.model_payload)
        manifest = dict(candidate.internal_manifest)
        manifest["parity_source_catalog_path"] = str(alternate)
        with self.assertRaisesRegex(parity.ParityValidationError, "Runtime Binding"):
            self.prepare(replace(candidate, internal_manifest=manifest))

    def test_manifest_cannot_downgrade_default_overlay_to_legacy_custom(self):
        candidate = self.rebound(self.prepare())
        manifest = dict(candidate.internal_manifest)
        manifest.pop("parity_model_catalog_kind")
        manifest["parity_policy_version"] = "3"
        self.official_cache.write_bytes(b'{"models":[{"slug":"model-a","multi_agent_version":"v1"}]}')
        with self.assertRaises(parity.ParityValidationError):
            self.prepare(replace(candidate, internal_manifest=manifest))

    def test_legacy_custom_overlay_without_kind_can_be_prepared_again(self):
        self.explicit_catalog()
        self.official_cache.unlink()
        candidate = self.rebound(self.prepare())
        manifest = dict(candidate.internal_manifest)
        manifest.pop("parity_model_catalog_kind")
        manifest["parity_policy_version"] = "3"
        receipt_path = Path(manifest["parity_receipt_path"])
        historical_receipt = json.loads(receipt_path.read_bytes())
        historical_receipt["policy_version"] = "3"
        payload = json.dumps(historical_receipt, sort_keys=True).encode()
        receipt_path.write_bytes(payload)
        manifest["parity_receipt_sha256"] = hashlib.sha256(payload).hexdigest()
        bundle = self.prepare(replace(candidate, internal_manifest=manifest))
        self.assertTrue(bundle.healthy)
        self.assertEqual(bundle.manifest_metadata["parity_model_catalog_kind"], "custom")


if __name__ == "__main__":
    unittest.main()
