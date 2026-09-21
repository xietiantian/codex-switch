from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

import codex_switch_parity as parity


FIXTURE = Path(__file__).resolve().parent.parent / "evals" / "fixtures" / "current-runtime-extensions.json"
IMAGE_METHODS = frozenset({
    "thread/queue/add", "thread/queue/update", "thread/resume",
    "turn/start", "turn/steer",
})


class CurrentRuntimeParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text())

    def protocols(self, *, image_only: bool = False):
        inventories = []
        for side in ("official", "internal"):
            inventories.append(parity.ProtocolInventory(
                side=side,
                methods=tuple(parity.ProtocolMethodRecord(**record)
                    for record in self.fixture[side]["methods"]
                    if not image_only or record["method"] in IMAGE_METHODS),
            ))
        return parity.compare_protocol_inventories(*inventories)

    def features(self):
        return parity.compare_feature_inventories(*[
            parity.FeatureInventory(side=side, features=tuple(
                parity.FeatureRecord(**record)
                for record in self.fixture[side]["features"]
            )) for side in ("official", "internal")
        ])

    def evaluate(self, protocol, *, features=None, coverage=None, trace=None):
        return parity.evaluate_parity_policy(
            feature_comparison=features or parity.FeatureInventoryComparison(entries=()),
            protocol_comparison=protocol,
            official_model_metadata={}, internal_model_metadata={},
            custom_model_catalog=True,
            method_coverage=parity.build_method_coverage(protocol) if coverage is None else coverage,
            acceptance_trace=trace or parity.current_parity_acceptance_trace(),
        )

    def test_exact_image_extensions_preserve_the_common_core_contract(self) -> None:
        comparison = self.protocols(image_only=True)
        coverage = parity.build_method_coverage(comparison)
        self.assertEqual(len(coverage), 5)
        self.assertEqual({record.disposition for record in coverage}, {"optional_extension"})
        self.assertTrue(all(record.optional_extension_ids == ("image_file_reference",)
                            for record in coverage))
        self.assertTrue(all(not record.adapter_rule_digests for record in coverage))
        result = self.evaluate(comparison, coverage=coverage)
        self.assertTrue(result.healthy, result.findings)
        self.assertEqual({item.identifier for item in result.synchronization_queue}, {
            f"client_request:{method}#image_file_reference" for method in IMAGE_METHODS
        })

    def test_all_current_differences_are_classified_without_changing_inputs(self) -> None:
        before = copy.deepcopy(self.fixture)
        result = self.evaluate(self.protocols(), features=self.features())
        self.assertTrue(result.healthy, result.findings)
        self.assertEqual(len(result.synchronization_queue), 13)
        self.assertEqual(self.fixture, before)

    def test_image_extensions_escalate_when_used_or_when_proof_changes(self) -> None:
        comparison = self.protocols(image_only=True)
        original = self.fixture
        for method in sorted(IMAGE_METHODS):
            with self.subTest(method=method):
                trace = replace(parity.current_parity_acceptance_trace(),
                    observed_protocol_extensions=(("client_request", method, "image_file_reference"),))
                result = self.evaluate(comparison, trace=trace)
                self.assertFalse(result.healthy)
                self.assertIn("parity.protocol.observed_extension_incompatible",
                              {finding.code for finding in result.findings})
                self.fixture = copy.deepcopy(original)
                record = next(record for record in self.fixture["internal"]["methods"]
                              if record["method"] == method)
                record["schema"]["required"] = [*record["schema"].get("required", []), "new_required_field"]
                changed = self.protocols(image_only=True)
                self.assertFalse(self.evaluate(changed).healthy)
        self.fixture = original
        self.assertFalse(self.evaluate(comparison, coverage=()).healthy)

    def test_reviewed_features_require_exact_states_and_no_core_dependence(self) -> None:
        original = copy.deepcopy(self.fixture)
        for record in original["official"]["features"]:
            name = record["name"]
            with self.subTest(feature=name, change="observed"):
                trace = replace(parity.current_parity_acceptance_trace(),
                                observed_features=("multi_agent_v2", name))
                self.assertFalse(self.evaluate(self.protocols(),
                    features=self.features(), trace=trace).healthy)
            for field in ("stage", "isolated_default", "effective_state"):
                with self.subTest(feature=name, change=field):
                    self.fixture = copy.deepcopy(original)
                    changed = next(item for item in self.fixture["official"]["features"]
                                   if item["name"] == name)
                    changed[field] = ("stable" if changed[field] != "stable" else "experimental") \
                        if field == "stage" else not changed[field]
                    self.assertFalse(self.evaluate(self.protocols(), features=self.features()).healthy)
            self.fixture = copy.deepcopy(original)

    def test_backend_extension_requires_exact_method_schema_and_no_dependency(self) -> None:
        original = copy.deepcopy(self.fixture)
        for observation in (
            {"observed_protocol_methods": (("client_request", "thread/rollback"),)},
            {"observed_protocol_extensions": (("client_request", "thread/rollback", "backend_only_request"),)},
            {"observed_protocol_extensions": (("client_request", "thread/rollback", "rollback_options"),)},
        ):
            with self.subTest(observation=observation):
                trace = replace(parity.current_parity_acceptance_trace(), **observation)
                self.assertFalse(self.evaluate(self.protocols(), trace=trace).healthy)
        for field, value in (("method", "thread/unknown"), ("direction", "server_request"),
                             ("schema", {"type": "object", "required": ["newField"]})):
            with self.subTest(field=field):
                self.fixture = copy.deepcopy(original)
                changed = next(item for item in self.fixture["internal"]["methods"]
                               if item["method"] == "thread/rollback")
                changed[field] = value
                self.assertFalse(self.evaluate(self.protocols()).healthy)


if __name__ == "__main__":
    unittest.main()
