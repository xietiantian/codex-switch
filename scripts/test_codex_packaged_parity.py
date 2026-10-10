import copy
import json
from pathlib import Path
import unittest
from dataclasses import replace

import codex_switch_parity as parity

FIXTURE = Path(__file__).resolve().parent.parent / 'evals/fixtures/packaged-desktop-extensions.json'


class PackagedParityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())

    def comparison(self, methods=None):
        return parity.compare_protocol_inventories(*[
            parity.ProtocolInventory(side=side, methods=tuple(
                parity.ProtocolMethodRecord(**m) for m in self.fixture[side]['methods']
                if methods is None or m['method'] in methods))
            for side in ('official', 'internal')])

    def test_current_elicitation_is_backward_compatible(self):
        comparison = self.comparison({'mcpServer/elicitation/request'})
        self.assertTrue(comparison.entries[0].compatible, comparison.entries[0].reason_codes)

    def test_elicitation_new_required_field_is_still_rejected(self):
        # Select the actual method rather than depending on fixture order.
        method = next(m for m in self.fixture['official']['methods']
                      if m['method'] == 'mcpServer/elicitation/request')
        params = method['schema']['properties']['params']
        variant = next(v for v in params['oneOf']
                       if v['properties']['mode']['enum'] == ['openai/userVerification'])
        variant['required'].append('_meta')
        self.assertFalse(self.comparison({'mcpServer/elicitation/request'}).entries[0].compatible)

    def evaluate(self, *, observed_protocol=(), requested_features=()):
        comparison = self.comparison()
        features = parity.compare_feature_inventories(*[
            parity.FeatureInventory(side=side, features=tuple(
                parity.FeatureRecord(**f) for f in self.fixture[side]['features']))
            for side in ('official', 'internal')])
        trace = replace(parity.current_parity_acceptance_trace(),
                        observed_protocol_methods=observed_protocol)
        return parity.evaluate_parity_policy(feature_comparison=features,
            protocol_comparison=comparison, official_model_metadata={}, internal_model_metadata={},
            custom_model_catalog=True, method_coverage=parity.build_method_coverage(comparison),
            acceptance_trace=trace, requested_internal_features=frozenset(requested_features))

    def test_reviewed_current_defaults_and_unused_gateway_are_optional(self):
        result = self.evaluate()
        self.assertTrue(result.healthy, result.findings)

    def test_gateway_use_or_schema_drift_is_not_silently_accepted(self):
        self.assertFalse(self.evaluate(observed_protocol=(('client_request', 'account/gatewayOAuth/read'),)).healthy)
        m = next(m for m in self.fixture['official']['methods'] if m['method'] == 'account/gatewayOAuth/read')
        m['schema']['required'].append('params')
        self.assertFalse(self.evaluate().healthy)

    def test_explicit_missing_feature_or_changed_default_is_rejected(self):
        self.assertFalse(self.evaluate(requested_features=('agent_message_board',)).healthy)
        self.assertFalse(self.evaluate(requested_features=('guardianv2.thread_context',)).healthy)
        self.fixture['official']['features'][0]['effective_state'] = True
        self.assertFalse(self.evaluate().healthy)


if __name__ == '__main__':
    unittest.main()
