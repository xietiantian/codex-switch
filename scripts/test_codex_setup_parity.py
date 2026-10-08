"""Setup acceptance for optional runtime differences at public parity seams."""
import unittest
from dataclasses import replace

import codex_switch_parity as parity


class SetupParityTests(unittest.TestCase):
    def feature(self, name, enabled, *, stage="stable", default=True):
        return parity.FeatureRecord(name=name, stage=stage,
            isolated_default=default, effective_state=enabled)

    def evaluate(self, official=(), internal=(), *, methods=((), ()), observed=(), **options):
        features = parity.compare_feature_inventories(
            parity.FeatureInventory(side="official", features=tuple(official)),
            parity.FeatureInventory(side="internal", features=tuple(internal)))
        protocols = parity.compare_protocol_inventories(
            parity.ProtocolInventory(side="official", methods=tuple(methods[0])),
            parity.ProtocolInventory(side="internal", methods=tuple(methods[1])))
        return parity.evaluate_parity_policy(
            feature_comparison=features, protocol_comparison=protocols,
            official_model_metadata={}, internal_model_metadata={},
            custom_model_catalog=True, observed_features=frozenset(observed),
            method_coverage=parity.build_method_coverage(protocols), **options)

    def test_supported_choices_preserve_boolean_preferences(self):
        for name in ("apps", "guardian_approval", "prevent_idle_sleep", "nonfatal_clock_read_errors"):
            for enabled in (False, True):
                with self.subTest(name=name, official=enabled):
                    official = self.feature(name, enabled)
                    internal = replace(official, effective_state=not enabled)
                    result = self.evaluate([official], [internal])
                    self.assertTrue(result.healthy, result.findings)
                    findings = [item for item in result.findings if item.category == "feature"]
                    self.assertEqual(len(findings), 1)
                    finding = findings[0]
                    self.assertEqual(finding.code, "parity.feature.configuration_difference")
                    self.assertEqual(finding.severity, "warning")
                    self.assertIn(name, finding.message)
                    self.assertIn(f"official={str(enabled).lower()}", finding.message)
                    self.assertIn(f"internal={str(not enabled).lower()}", finding.message)

    def test_supported_choice_does_not_allow_capability_or_core_drift(self):
        for name in ("apps", "guardian_approval", "prevent_idle_sleep"):
            official = self.feature(name, True)
            for internal in ((), (replace(official, stage="removed"),),
                             (replace(official, isolated_default=False),)):
                with self.subTest(name=name, internal=internal):
                    self.assertFalse(self.evaluate([official], internal).healthy)
            self.assertFalse(self.evaluate([official], [replace(official, effective_state=False)],
                                           observed=(name,)).healthy)

    def test_removed_and_unknown_features_still_block(self):
        official = self.feature("enable_fanout", False, stage="removed", default=False)
        result = self.evaluate([official], [replace(official, effective_state=True)])
        self.assertFalse(result.healthy)
        self.assertEqual(result.findings[0].code, "parity.feature.core_drift")
        self.assertIn("official=false (stage=removed", result.findings[0].message)
        self.assertIn("internal=true (stage=removed", result.findings[0].message)
        self.assertFalse(self.evaluate([self.feature("future_unknown", True)]).healthy)

    def test_missing_optional_clock_does_not_depend_on_official_setting(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                official = self.feature("nonfatal_clock_read_errors", enabled,
                                        stage="under development", default=False)
                result = self.evaluate([official])
                self.assertTrue(result.healthy, result.findings)
                self.assertIn("parity.feature.optional_missing", {f.code for f in result.findings})
                self.assertFalse(self.evaluate([official], observed=(official.name,)).healthy)

    def test_candidate_cannot_request_unsupported_clock_feature(self):
        name = "nonfatal_clock_read_errors"
        official = self.feature(name, False, stage="under development", default=False)
        for reference in ((), (official,)):
            result = self.evaluate(reference, requested_internal_features=frozenset({name}))
            self.assertFalse(result.healthy)
            finding = next(f for f in result.findings if f.code == "parity.config.unsupported_feature")
            self.assertIn(name, finding.message)

    def test_reviewed_system_proxy_retry_missing_is_optional(self):
        name = "system_proxy_fallback"
        for enabled in (False, True):
            official = self.feature(name, enabled)
            result = self.evaluate([official])
            self.assertTrue(result.healthy, result.findings)
            self.assertIn("parity.feature.optional_proxy_retry_missing", {f.code for f in result.findings})
            self.assertFalse(self.evaluate([official], observed=(name,)).healthy)
            result = self.evaluate([official], requested_internal_features=frozenset({name}))
            self.assertFalse(result.healthy)
            self.assertIn("parity.config.unsupported_feature", {f.code for f in result.findings})
        for official in (self.feature(name, True, stage="experimental"),
                         self.feature(name, True, default=False)):
            self.assertFalse(self.evaluate([official]).healthy)
        self.assertFalse(self.evaluate([], [self.feature(name, True)]).healthy)
        self.assertFalse(self.evaluate(requested_internal_features=frozenset({name})).healthy)

    def test_missing_rollout_compress_is_optional_but_not_unknown_or_core_methods(self):
        method = parity.ProtocolMethodRecord(direction="client_request",
            method="rollout/compress", schema={"type": "object", "properties": {}})
        result = self.evaluate(methods=((method,), ()))
        self.assertTrue(result.healthy, result.findings)
        self.assertIn("parity.protocol.optional_maintenance_missing", {f.code for f in result.findings})
        self.assertTrue(any("rollout/compress" in f.message for f in result.findings))
        self.assertFalse(self.evaluate(methods=((method,), ()),
            observed_protocol_methods=frozenset({("client_request", method.method)})).healthy)
        for name in ("rollout/future_unknown", "thread/start"):
            unknown = parity.ProtocolMethodRecord(direction="client_request", method=name, schema=method.schema)
            self.assertFalse(self.evaluate(methods=((unknown,), ())).healthy)

    def test_public_diagnostics_are_bounded_and_redacted(self):
        finding = parity.ParityFinding(category="feature", code="parity.config.unsupported_feature",
            severity="error", message='token: Bearer private-credential\n' + 'x' * 3000,
            observed='api_key="secret-value"')
        details = parity.parity_diagnostics([finding] * 100)
        self.assertLessEqual(len(details), 64)
        self.assertLessEqual(len(details[0]["message"]), 2048)
        self.assertNotIn("private-credential", str(details))
        self.assertNotIn("secret-value", str(details))


if __name__ == "__main__":
    unittest.main()
