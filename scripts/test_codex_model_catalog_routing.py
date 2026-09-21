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

    def prepare(self, candidate=None, *, probe_runner=None, catalog_loader=None, timeouts=None):
        selected = candidate or self.candidate
        selected = replace(selected, source_config=parity.ConfigInputs.capture(
            profile_config=self.profile, source_paths=(self.profile, self.shared),
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

    def test_absent_catalog_reaches_default_model_comparison(self):
        bundle = self.prepare()
        self.assertTrue(bundle.healthy)
        self.assertNotIn("parity.model.custom_catalog_not_applicable", {f.code for f in bundle.findings})
        self.assertEqual(bundle.overlay.source_catalog, self.internal_cache)
        self.assertEqual(self.profile.read_text(), self.profile_payload)
        self.assertEqual(self.internal_cache.read_bytes(), self.model_payload)
        parity.revalidate_parity_bundle_inputs(bundle)


    def rebound(self, bundle):
        overlay = bundle.receipt.overlay_path
        overlay.parent.mkdir(mode=0o700, exist_ok=True)
        overlay.write_bytes(bundle.overlay.overlay_payload)
        bundle.receipt.overlay_path.with_name("receipt.json").write_bytes(bundle.receipt_payload)
        self.profile.write_bytes(bundle.config_projection.payload_for(self.profile))
        manifest = dict(self.candidate.internal_manifest)
        manifest.update(bundle.manifest_metadata)
        return replace(self.candidate, internal_manifest=manifest)

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
