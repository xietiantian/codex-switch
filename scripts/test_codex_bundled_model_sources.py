"""Default model evidence without pre-existing workstation caches."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import codex_switch_parity as parity
import test_codex_model_catalog_routing as routing
import test_codex_transaction as transactions
import codex_switch_transaction as transaction


class BundledModelSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = routing.ModelCatalogRoutingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_missing_both_caches_uses_each_binary_catalog(self):
        f = self.fixture
        f.internal_cache.unlink()
        f.official_cache.unlink()
        export = mock.Mock(return_value=f.model_payload)
        bundle = f.prepare(catalog_loader=export)
        self.assertTrue(bundle.healthy)
        self.assertEqual(export.call_count, 2)
        self.assertEqual(bundle.manifest_metadata["parity_model_catalog_kind"], "runtime-bundled")
        self.assertFalse(f.internal_cache.exists())
        self.assertFalse(f.official_cache.exists())
        self.assertEqual(f.profile.read_text(), f.profile_payload)
        self.assertNotIn(
            "parity.model.custom_catalog_not_applicable",
            {finding.code for finding in bundle.findings},
        )
        self.assertEqual(parity.ParityReceipt.from_payload(json.loads(bundle.receipt_payload)), bundle.receipt)
        self.assertEqual({reference.side for reference in bundle.receipt.model_sources}, {"internal", "official"})

    def prepare_without_caches(self):
        f = self.fixture
        for cache in (f.internal_cache, f.official_cache):
            cache.unlink(missing_ok=True)
        return f.prepare(catalog_loader=lambda *_args: f.model_payload)

    def publish_fixture(self, bundle):
        candidate = self.fixture.rebound(bundle)
        for _side, path, payload in bundle.model_source_artifacts:
            path.write_bytes(payload)
            path.chmod(0o600)
        return candidate

    def test_only_missing_side_is_exported(self):
        f = self.fixture
        for cache, binding in ((f.internal_cache, f.candidate.internal_binding), (f.official_cache, f.candidate.official_binding)):
            with self.subTest(cache=str(cache)):
                cache.unlink()
                export = mock.Mock(return_value=f.model_payload)
                bundle = f.prepare(catalog_loader=export)
                self.assertTrue(bundle.healthy)
                self.assertEqual([call.args[0] for call in export.call_args_list], [binding.backend_cli])
                cache.write_bytes(f.model_payload)

    def test_existing_cache_and_custom_catalog_do_not_export(self):
        f = self.fixture
        export = mock.Mock(side_effect=AssertionError("unexpected export"))
        self.assertTrue(f.prepare(catalog_loader=export).healthy)
        f.explicit_catalog()
        f.internal_cache.unlink()
        f.official_cache.unlink()
        self.assertTrue(f.prepare(catalog_loader=export).healthy)

    def test_present_invalid_caches_never_fall_back(self):
        f = self.fixture
        for cache in (f.internal_cache, f.official_cache):
            for state in ("malformed", "symlink"):
                with self.subTest(cache=str(cache), state=state):
                    cache.unlink()
                    if state == "malformed":
                        cache.write_bytes(b"invalid-json")
                    else:
                        target = f.root / "external-cache"
                        target.write_bytes(f.model_payload)
                        cache.symlink_to(target)
                    export = mock.Mock()
                    with self.assertRaises(parity.ParityValidationError):
                        f.prepare(catalog_loader=export)
                    export.assert_not_called()
                    cache.unlink()
                    cache.write_bytes(f.model_payload)

    def test_invalid_exports_and_missing_active_model_fail(self):
        f = self.fixture
        f.internal_cache.unlink()
        for payload in (b"", b"broken", b"\xff", b'{"models":[]}', b'{"models":[{"slug":"different"}]}', b'x' * (parity.MAX_PARITY_CATALOG_BYTES + 1)):
            with self.subTest(size=len(payload)), self.assertRaises(parity.ParityValidationError):
                f.prepare(catalog_loader=lambda *_args: payload)
        self.assertEqual(f.profile.read_text(), f.profile_payload)

    def test_bundled_metadata_differences_still_block(self):
        f = self.fixture
        f.internal_cache.unlink()
        f.official_cache.unlink()
        def export(cli, _work, _timeout):
            return f.model_payload if cli == f.candidate.internal_binding.backend_cli else f.model_payload.replace(b'"v2"', b'"v1"')
        with self.assertRaisesRegex(parity.ParityValidationError, "parity.model.unclassified_drift"):
            f.prepare(catalog_loader=export)

    def test_new_cache_and_changed_staged_sources_block_promotion(self):
        f = self.fixture
        bundle = self.prepare_without_caches()
        for source in bundle.model_sources:
            with self.subTest(side=source.reference.side):
                source.staged_path.write_bytes(source.payload + b"\n")
                for check in (parity.revalidate_parity_bundle_inputs, parity.revalidate_parity_bundle_immutable_inputs):
                    with self.assertRaises(parity.ParityValidationError):
                        check(bundle)
                source.staged_path.write_bytes(source.payload)
                source.cache_path.write_bytes(f.model_payload)
                with self.assertRaises(parity.ParityValidationError):
                    parity.revalidate_parity_bundle_immutable_inputs(bundle)
                source.cache_path.unlink()

    def test_binary_change_during_export_is_rejected(self):
        f = self.fixture
        f.internal_cache.unlink()
        def export(cli, _work, _timeout):
            cli.write_bytes(cli.read_bytes() + b"\n# changed\n")
            return f.model_payload
        with self.assertRaises(parity.ParityValidationError):
            f.prepare(catalog_loader=export)

    def test_repeat_collects_fresh_binary_sources_and_can_return_to_cache(self):
        f = self.fixture
        bundle = self.prepare_without_caches()
        candidate = self.publish_fixture(bundle)
        fresh = b'{"models":[{"slug":"model-a","multi_agent_version":"v2","display_name":"fresh"}]}'
        export = mock.Mock(return_value=fresh)
        repeated = f.prepare(candidate, catalog_loader=export)
        self.assertEqual(export.call_count, 2)
        self.assertEqual(repeated.receipt.internal_fingerprint.source_catalog_sha256, hashlib.sha256(fresh).hexdigest())
        self.assertEqual(repeated.manifest_metadata["parity_model_catalog_kind"], "runtime-bundled")
        f.internal_cache.write_bytes(f.model_payload)
        f.official_cache.write_bytes(f.model_payload)
        cached = f.prepare(candidate, catalog_loader=mock.Mock(side_effect=AssertionError("unexpected export")))
        self.assertEqual(cached.manifest_metadata["parity_model_catalog_kind"], "runtime-cache")

    def test_changed_published_source_and_origin_kind_are_rejected(self):
        f = self.fixture
        bundle = self.prepare_without_caches()
        candidate = self.publish_fixture(bundle)
        for kind in ("custom", "runtime-cache"):
            manifest = dict(candidate.internal_manifest, parity_model_catalog_kind=kind)
            with self.subTest(kind=kind), self.assertRaises(parity.ParityValidationError):
                f.prepare(replace(candidate, internal_manifest=manifest))
        source = bundle.model_sources[0]
        source.reference.path.write_bytes(source.payload + b"\n")
        with self.assertRaises(parity.ParityValidationError):
            f.prepare(candidate)

    def test_current_default_receipt_requires_both_source_references(self):
        bundle = self.prepare_without_caches()
        for value in (None, []):
            document = json.loads(bundle.receipt_payload)
            if value is None:
                document.pop("model_sources")
            else:
                document["model_sources"] = value
            with self.subTest(value=value), self.assertRaises(parity.ParityValidationError):
                parity.ParityReceipt.from_payload(document)

    def test_repeat_rejects_missing_current_receipt_sources(self):
        f = self.fixture
        candidate = f.rebound(f.prepare())
        manifest = dict(candidate.internal_manifest)
        receipt_path = Path(manifest["parity_receipt_path"])
        document = json.loads(receipt_path.read_bytes())
        document.pop("model_sources")
        payload = json.dumps(document).encode()
        receipt_path.write_bytes(payload)
        manifest["parity_receipt_sha256"] = hashlib.sha256(payload).hexdigest()
        with self.assertRaises(parity.ParityValidationError):
            f.prepare(replace(candidate, internal_manifest=manifest))

    def test_previous_cache_policy_provenance_is_reprepared(self):
        f = self.fixture
        candidate = f.rebound(f.prepare())
        manifest = dict(candidate.internal_manifest)
        receipt_path = Path(manifest["parity_receipt_path"])
        document = json.loads(receipt_path.read_bytes())
        document.pop("model_sources")
        document["policy_version"] = "4"
        payload = json.dumps(document).encode()
        receipt_path.write_bytes(payload)
        manifest.update(parity_policy_version="4", parity_receipt_sha256=hashlib.sha256(payload).hexdigest())
        bundle = f.prepare(replace(candidate, internal_manifest=manifest))
        self.assertTrue(bundle.healthy)
        self.assertEqual(str(bundle.receipt.policy_version), parity.PARITY_POLICY_VERSION)
        self.assertEqual(len(bundle.receipt.model_sources), 2)

    def test_source_reference_binary_drift_is_rejected(self):
        f = self.fixture
        candidate = f.rebound(f.prepare())
        receipt_path = Path(candidate.internal_manifest["parity_receipt_path"])
        original = json.loads(receipt_path.read_bytes())
        for index in range(2):
            document = json.loads(json.dumps(original))
            document["model_sources"][index]["binary_sha256"] = "0" * 64
            with self.subTest(side=index):
                with self.assertRaises(parity.ParityValidationError):
                    parity.ParityReceipt.from_payload(document)
                payload = json.dumps(document).encode()
                receipt_path.write_bytes(payload)
                manifest = dict(candidate.internal_manifest, parity_receipt_sha256=hashlib.sha256(payload).hexdigest())
                with self.assertRaises(parity.ParityValidationError):
                    f.prepare(replace(candidate, internal_manifest=manifest))


class BundledExporterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = routing.ModelCatalogRoutingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.internal_cache.unlink()
        self.cli = self.fixture.candidate.internal_binding.backend_cli

    def script(self, body):
        self.cli.write_text(f"#!{sys.executable}\n" + body)
        self.cli.chmod(0o755)
        candidate = self.fixture.candidate
        receipt = replace(candidate.capability_receipt.receipt, backend_sha256=hashlib.sha256(self.cli.read_bytes()).hexdigest())
        payload = (json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n").encode()
        artifact = replace(candidate.capability_receipt, receipt=receipt, payload=payload, payload_sha256=hashlib.sha256(payload).hexdigest())
        self.fixture.candidate = replace(candidate, capability_receipt=artifact)

    def prepare(self, timeout=3):
        return self.fixture.prepare(timeouts=parity.ParityTimeouts(command_seconds=timeout, probe_seconds=1))

    def test_export_uses_private_home_and_no_inherited_credentials(self):
        self.script("import json, os, sys\nprint(json.dumps({'models': [{'slug': 'model-a', 'multi_agent_version': 'v2'}], 'environment': dict(os.environ), 'args': sys.argv[1:], 'cwd': os.getcwd()}))\n")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret", "TEST_MODEL_API_KEY": "test-secret"}):
            bundle = self.prepare()
        document = json.loads(bundle.model_sources[0].payload)
        self.assertNotIn("OPENAI_API_KEY", document["environment"])
        self.assertNotIn("TEST_MODEL_API_KEY", document["environment"])
        self.assertEqual(document["args"], ["-c", 'cli_auth_credentials_store="file"', "debug", "models", "--bundled"])
        self.assertEqual(Path(document["cwd"]).resolve(), Path(document["environment"]["CODEX_HOME"]).resolve())
        self.assertTrue(Path(document["cwd"]).resolve().is_relative_to(self.fixture.work.resolve()))
        self.assertFalse(Path(document["cwd"]).exists())

    def test_export_rejects_failure_timeout_and_output_overflow(self):
        cases = [
            ("import sys\nsys.exit(2)\n", 3, "parity.model.export_failed"),
            ("import time\ntime.sleep(10)\n", 0.1, "parity.model.export_timeout"),
            (f"import os\nos.write(1, b'x' * {parity.MAX_PARITY_CATALOG_BYTES + 1})\n", 3, "parity.model.export_oversized"),
        ]
        for body, timeout, code in cases:
            with self.subTest(code=code):
                self.script(body)
                with self.assertRaises(parity.ParityValidationError) as raised:
                    self.prepare(timeout)
                self.assertEqual(raised.exception.code, code)


class BundledSourceTransactionTests(unittest.TestCase):
    def test_bundled_snapshots_publish_and_rollback_with_the_bundle(self):
        fixture = transactions.TransactionTests()
        class Interruption(BaseException):
            pass
        for phase in (None, "after_parity_internal_model_source", "after_parity_official_model_source", "after_manifest"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                store, artifacts, _paths, old = fixture.arrange_runtime_binding_bundle(
                    Path(temporary), include_shared_config=True, include_active_runtime_config=True,
                )
                sources = tuple(transaction.RuntimeBindingTextArtifact(
                    role=f"parity_{side}_model_source",
                    path=store.profile_dir("internal") / "parity" / f"{side}-model-source.json",
                    payload=b'{"models":[{"slug":"new"}]}', mode=0o600,
                ) for side in ("internal", "official"))
                sources[0].path.write_bytes(b'{"models":[{"slug":"previous"}]}')
                sources[0].path.chmod(0o600)
                old[sources[0].path] = sources[0].path.read_bytes()
                def interrupt(observed):
                    if observed == phase:
                        raise Interruption()
                def commit():
                    with transaction.locked_store_mutation(store, operation="bundled source publication") as locked:
                        transaction.commit_runtime_binding_bundle(locked, artifacts=(*artifacts, *sources), fault_hook=interrupt)
                if phase is None:
                    commit()
                    for source in sources:
                        self.assertEqual(source.path.read_bytes(), source.payload)
                else:
                    with self.assertRaises(Interruption):
                        commit()
                    with transaction.locked_store_mutation(store, operation="bundled source recovery"):
                        pass
                    self.assertFalse(sources[1].path.exists())
                    for path, payload in old.items():
                        self.assertEqual(path.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
