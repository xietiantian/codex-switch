#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock

import codex_switch_parity as parity_module
import codex_switch_protocol_adapter as protocol_adapter_module
from codex_switch_parity import (
    ALLOWED_IDENTITY_DIFFERENCES,
    PARITY_POLICY_VERSION,
    FeatureCommandRequest,
    FeatureCommandResult,
    FeatureInventory,
    FeatureInventoryComparison,
    FeatureRecord,
    InternalFingerprint,
    OfficialReference,
    ParityCandidate,
    ParityFinding,
    ParityPolicyEvaluation,
    ParityPolicyVersion,
    ParityQueueItem,
    ParityReport,
    ParityValidationError,
    ProtocolInventory,
    ProtocolInventoryComparison,
    ProtocolInventoryComparisonEntry,
    ProtocolMethodRecord,
    collect_feature_inventory,
    collect_protocol_inventory,
    compare_feature_inventories,
    compare_protocol_inventories,
)
from codex_switch_protocol_adapter import (
    BackendCapabilities,
    CapabilityReceipt,
    CapabilityReceiptArtifact,
    protocol_adapter_rule_set_digest,
)
from codex_switch_runtime_binding import (
    ChatGPTDesktopHost,
    CURRENT_CHATGPT_BUNDLE_ID,
    DesktopRoots,
    RuntimeBinding,
    RuntimeBindingContext,
    discover_desktop_hosts,
    resolve_runtime_binding,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class RecordingFeatureRunner:
    def __init__(self, results: list[FeatureCommandResult]) -> None:
        self.results = list(results)
        self.requests: list[FeatureCommandRequest] = []

    def __call__(self, request: FeatureCommandRequest) -> FeatureCommandResult:
        self.requests.append(request)
        if not self.results:
            raise AssertionError("unexpected feature-list request")
        return self.results.pop(0)


def feature_result(
    stdout: str,
    *,
    returncode: int | None = 0,
    stderr: str = "",
    timed_out: bool = False,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> FeatureCommandResult:
    return FeatureCommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


PROTOCOL_ROOTS = {
    "client_request": "ClientRequest.json",
    "client_notification": "ClientNotification.json",
    "server_request": "ServerRequest.json",
    "server_notification": "ServerNotification.json",
}


def protocol_variant(
    direction: str,
    method: str,
    params: dict[str, object],
    *,
    description: str = "documentation only",
) -> dict[str, object]:
    properties: dict[str, object] = {
        "method": {
            "description": description,
            "type": "string",
            "enum": [method],
            "title": f"{method} method",
        },
        "params": params,
    }
    required = ["method", "params"]
    if direction.endswith("_request"):
        properties = {
            "id": {"description": description, "type": "string"},
            **properties,
        }
        required = ["id", *required]
    return {
        "description": description,
        "type": "object",
        "required": required,
        "properties": properties,
        "title": f"{method} envelope",
    }


def protocol_documents(
    shapes: dict[str, tuple[str, dict[str, object]]] | None = None,
) -> dict[str, dict[str, object]]:
    selected = shapes or {
        "client_request": (
            "client/request",
            {"type": "object", "properties": {}},
        ),
        "client_notification": (
            "client/notification",
            {"type": "object", "properties": {}},
        ),
        "server_request": (
            "server/request",
            {"type": "object", "properties": {}},
        ),
        "server_notification": (
            "server/notification",
            {"type": "object", "properties": {}},
        ),
    }
    documents: dict[str, dict[str, object]] = {}
    for direction, root_name in PROTOCOL_ROOTS.items():
        method, params = selected[direction]
        documents[root_name] = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "description": f"{direction} root documentation",
            "oneOf": [protocol_variant(direction, method, params)],
            "title": root_name.removesuffix(".json"),
        }
    return documents


def protocol_object(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
    additional_properties: bool | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    if additional_properties is not None:
        schema["additionalProperties"] = additional_properties
    return schema


class ParityReferenceTests(unittest.TestCase):
    def write_chatgpt_bundle(self, root: Path) -> tuple[DesktopRoots, Path]:
        applications = root / "Applications"
        roots = DesktopRoots(
            chatgpt=applications / "ChatGPT.app",
            legacy_codex=applications / "Codex.app",
            chatgpt_classic=applications / "ChatGPT Classic.app",
        )
        contents = roots.chatgpt / "Contents"
        contents.mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": CURRENT_CHATGPT_BUNDLE_ID,
                    "CFBundleShortVersionString": "1.2026.196",
                },
                handle,
            )
        write_executable(contents / "MacOS" / "ChatGPT")
        bundled_cli = write_executable(contents / "Resources" / "codex")
        return roots, bundled_cli

    def make_official_binding(
        self,
        root: Path,
        *,
        path_codex: Path | None = None,
    ) -> RuntimeBinding:
        roots, _bundled_cli = self.write_chatgpt_bundle(root)
        manifest = {}
        if path_codex is not None:
            manifest = {
                "codex_bin": str(path_codex),
                "app_cli_path": str(path_codex),
            }
        context = RuntimeBindingContext(
            profile="openai-official",
            manifest=manifest,
            store_root=root / "store",
            bin_dir=root / "store" / "bin",
            profile_home=root / "official-home",
            path_codex=path_codex,
        )
        return resolve_runtime_binding(context, discover_desktop_hosts(roots))

    def make_reference(
        self,
        root: Path,
        *,
        path_codex: Path | None = None,
    ) -> OfficialReference:
        return OfficialReference.from_runtime_binding(
            self.make_official_binding(root, path_codex=path_codex),
            bundle_version="1.2026.196",
            cli_version="0.146.0-alpha.3.1",
            binary_sha256=SHA_A,
            schema_sha256=SHA_B,
            feature_inventory_sha256=SHA_C,
        )

    def make_internal_fingerprint(self, root: Path) -> InternalFingerprint:
        return InternalFingerprint(
            backend_cli=root / "internal" / "codex",
            cli_version="0.144.6",
            binary_sha256=SHA_D,
            active_model="gpt-5.6-sol",
            provider_id="azure",
            wire_api="responses",
            endpoint_sha256=SHA_E,
            auth_source_kind="env",
            capability_receipt_sha256=SHA_F,
            source_catalog=root / "internal" / "models.json",
            source_catalog_sha256=SHA_A,
            config_sha256s=(
                ("profile", SHA_B),
                ("runtime", SHA_C),
            ),
        )

    def test_verified_chatgpt_bundle_is_the_reference_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = self.make_reference(root)
            expected_cli = (
                root
                / "Applications"
                / "ChatGPT.app"
                / "Contents"
                / "Resources"
                / "codex"
            )

            self.assertEqual(reference.authority, "chatgpt-bundle")
            self.assertEqual(reference.bundle_id, CURRENT_CHATGPT_BUNDLE_ID)
            self.assertEqual(reference.bundled_cli, expected_cli)
            self.assertEqual(reference.cli_version, "0.146.0-alpha.3.1")
            self.assertEqual(reference.binary_sha256, SHA_A)
            self.assertTrue(reference.matches_fingerprint(reference.fingerprint_sha256))

    def test_path_observation_cannot_override_the_bundled_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path_codex = write_executable(root / "path-bin" / "codex")

            reference = self.make_reference(root, path_codex=path_codex)

            self.assertNotEqual(reference.bundled_cli, path_codex)
            self.assertEqual(
                reference.bundled_cli,
                root
                / "Applications"
                / "ChatGPT.app"
                / "Contents"
                / "Resources"
                / "codex",
            )

    def test_network_latest_cannot_be_constructed_as_reference_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = self.make_reference(root)

            with self.assertRaises(ParityValidationError) as raised:
                replace(reference, authority="network-latest")

            self.assertEqual(
                raised.exception.code,
                "parity.reference.authority_invalid",
            )

    def test_allowed_identity_difference_whitelist_is_exact_and_immutable(self) -> None:
        self.assertEqual(
            ALLOWED_IDENTITY_DIFFERENCES,
            frozenset({"binary", "model", "endpoint", "provider", "auth"}),
        )
        with self.assertRaises(AttributeError):
            ALLOWED_IDENTITY_DIFFERENCES.add("protocol")  # type: ignore[attr-defined]

    def test_reference_fingerprint_change_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = self.make_reference(root)
            other_bundle_root = root / "Other" / "ChatGPT.app"
            mutations = (
                {"bundle_version": "1.2026.197"},
                {
                    "bundle_root": other_bundle_root,
                    "bundled_cli": (
                        other_bundle_root / "Contents" / "Resources" / "codex"
                    ),
                },
                {"cli_version": "0.146.0-alpha.4"},
                {"binary_sha256": SHA_D},
                {"schema_sha256": SHA_E},
                {"feature_inventory_sha256": SHA_F},
            )

            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    changed = replace(reference, **mutation)
                    self.assertTrue(
                        changed.is_stale(reference.fingerprint_sha256)
                    )
                    self.assertFalse(
                        changed.matches_fingerprint(reference.fingerprint_sha256)
                    )

    def test_invalid_bundle_identity_and_digest_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = self.make_reference(root)

            with self.assertRaises(ParityValidationError) as bundle_error:
                replace(reference, bundle_id="com.example.codex")
            self.assertEqual(
                bundle_error.exception.code,
                "parity.reference.bundle_id_invalid",
            )

            with self.assertRaises(ParityValidationError) as digest_error:
                replace(reference, binary_sha256=SHA_A.upper())
            self.assertEqual(
                digest_error.exception.code,
                "parity.reference.digest_invalid",
            )

    def test_reference_and_foundation_records_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = self.make_reference(root)
            internal = self.make_internal_fingerprint(root)
            official_binding = self.make_official_binding(root / "official")
            internal_binding = RuntimeBinding(
                profile="internal",
                shell_cli=root / "internal" / "codex",
                desktop_cli=root / "store" / "bin" / "codex-internal-app",
                backend_cli=root / "internal" / "codex",
                codex_home=root / "internal-home",
                desktop_host=None,
                requires_proxy=True,
            )
            candidate = ParityCandidate(
                official_binding=official_binding,
                internal_binding=internal_binding,
                internal_manifest={"name": "internal"},
                capability_receipt=object(),
                source_config=object(),
                adapter_rule_set_sha256=protocol_adapter_rule_set_digest(),
            )
            policy_version = ParityPolicyVersion(PARITY_POLICY_VERSION)
            finding = ParityFinding(
                category="reference",
                code="parity.reference.stale",
                severity="error",
                message="Reference evidence is stale.",
            )
            queue_item = ParityQueueItem(
                category="feature",
                identifier="skill_search",
                finding_code="parity.feature.optional_missing",
            )
            report = ParityReport(
                healthy=False,
                policy_version=policy_version,
                official_reference=reference,
                internal_fingerprint=internal,
                findings=(finding,),
                synchronization_queue=(queue_item,),
            )

            for record, field_name, value in (
                (reference, "cli_version", "changed"),
                (internal, "active_model", "changed"),
                (candidate, "source_config", None),
                (policy_version, "value", "changed"),
                (finding, "message", "changed"),
                (queue_item, "identifier", "changed"),
                (report, "healthy", True),
            ):
                with self.subTest(record=type(record).__name__):
                    with self.assertRaises(FrozenInstanceError):
                        setattr(record, field_name, value)

            with self.assertRaises(TypeError):
                candidate.internal_manifest["name"] = "changed"
            self.assertTrue(
                internal.matches_fingerprint(internal.fingerprint_sha256)
            )
            changed_internal = replace(internal, active_model="gpt-5.7-sol")
            self.assertTrue(
                changed_internal.is_stale(internal.fingerprint_sha256)
            )

    def test_candidate_binds_validated_adapter_rule_set_digest(self) -> None:
        self.assertIn(
            "adapter_rule_set_sha256",
            inspect.signature(ParityCandidate).parameters,
            "ParityCandidate adapter rule-set digest input is missing",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            official_binding = self.make_official_binding(root / "official")
            internal_binding = RuntimeBinding(
                profile="internal",
                shell_cli=root / "internal" / "codex",
                desktop_cli=root / "store" / "bin" / "codex-internal-app",
                backend_cli=root / "internal" / "codex",
                codex_home=root / "internal-home",
                desktop_host=None,
                requires_proxy=True,
            )
            digest = protocol_adapter_rule_set_digest()

            candidate = ParityCandidate(
                official_binding=official_binding,
                internal_binding=internal_binding,
                internal_manifest={"name": "internal"},
                capability_receipt=object(),
                source_config=object(),
                adapter_rule_set_sha256=digest,
            )

            self.assertEqual(candidate.adapter_rule_set_sha256, digest)
            with self.assertRaises(ParityValidationError) as raised:
                replace(
                    candidate,
                    adapter_rule_set_sha256=digest.upper(),
                )
            self.assertEqual(
                raised.exception.code,
                "parity.candidate.adapter_rule_set_digest_invalid",
            )

    def test_parity_module_owns_error_report_classification(self) -> None:
        self.assertTrue(
            hasattr(parity_module, "parity_error_report"),
            "Parity must own evidence-error finding classification.",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend_cli = root / "internal" / "codex"
            profile_dir = root / "store" / "profiles" / "internal"

            report = parity_module.parity_error_report(
                code="parity.config.source_stale",
                message="Config evidence changed.",
                backend_cli=backend_cli,
                profile_dir=profile_dir,
            )

            self.assertFalse(report.healthy)
            self.assertEqual(PARITY_POLICY_VERSION, str(report.policy_version))
            self.assertEqual(backend_cli, report.internal_fingerprint.backend_cli)
            self.assertEqual(
                profile_dir / "parity" / "unresolved-model-catalog.json",
                report.internal_fingerprint.source_catalog,
            )
            self.assertEqual((), report.synchronization_queue)
            self.assertEqual(1, len(report.findings))
            finding = report.findings[0]
            self.assertEqual("config", finding.category)
            self.assertEqual("parity.config.source_stale", finding.code)
            self.assertEqual("error", finding.severity)
            self.assertEqual("Config evidence changed.", finding.message)


class ParityFeatureInventoryTests(unittest.TestCase):
    def collect(
        self,
        root: Path,
        runner: RecordingFeatureRunner,
        *,
        side: str = "internal",
        cli_name: str = "codex",
    ) -> FeatureInventory:
        return collect_feature_inventory(
            side=side,
            cli_path=root / "bin" / cli_name,
            isolated_home=root / "isolated-home",
            effective_home=root / "effective-home",
            runner=runner,
            timeout_seconds=2.5,
            max_output_bytes=4096,
        )

    def test_namespaced_features_retain_identity_and_effective_state(self) -> None:
        isolated = (
            "guardianv2                               under development  false\n"
            "guardianv2.thread_context                under development  false\n"
        )
        effective = (
            "guardianv2                               under development  false\n"
            "guardianv2.thread_context                under development  true\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            official = self.collect(
                root / "official",
                RecordingFeatureRunner([
                    feature_result(isolated), feature_result(isolated),
                ]),
                side="official",
            )
            internal = self.collect(
                root / "internal",
                RecordingFeatureRunner([
                    feature_result(isolated), feature_result(effective),
                ]),
            )
            comparison = compare_feature_inventories(official, internal)

        self.assertEqual(
            [entry.name for entry in comparison.entries],
            ["guardianv2", "guardianv2.thread_context"],
        )
        self.assertEqual(
            json.loads(internal.canonical_bytes)["features"],
            [
                {
                    "name": "guardianv2", "stage": "under development",
                    "isolated_default": False, "effective_state": False,
                },
                {
                    "name": "guardianv2.thread_context",
                    "stage": "under development",
                    "isolated_default": False, "effective_state": True,
                },
            ],
        )
        entry = comparison.entry("guardianv2.thread_context")
        self.assertFalse(entry.official.effective_state)
        self.assertTrue(entry.internal.effective_state)

    def test_isolated_defaults_are_distinct_from_effective_state(self) -> None:
        isolated = (
            "multi_agent_v2  under development  false\n"
            "skill_search    stable             false\n"
        )
        effective = (
            "multi_agent_v2  under development  true\n"
            "skill_search    stable             false\n"
        )
        runner = RecordingFeatureRunner(
            [feature_result(isolated), feature_result(effective)]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inventory = self.collect(root, runner)

            multi_agent = inventory.feature("multi_agent_v2")
            self.assertFalse(multi_agent.isolated_default)
            self.assertTrue(multi_agent.effective_state)
            self.assertEqual(multi_agent.stage, "under development")
            self.assertEqual(
                [request.phase for request in runner.requests],
                ["isolated", "effective"],
            )
            self.assertEqual(
                runner.requests[0].command,
                (str(root / "bin" / "codex"), "features", "list"),
            )
            self.assertEqual(
                runner.requests[0].codex_home,
                root / "isolated-home",
            )
            self.assertEqual(
                runner.requests[1].codex_home,
                root / "effective-home",
            )
            self.assertEqual(runner.requests[0].timeout_seconds, 2.5)
            self.assertEqual(runner.requests[0].max_output_bytes, 4096)

    def test_official_and_internal_only_features_are_retained(self) -> None:
        official_output = (
            "shared_feature    stable             true\n"
            "official_only     stable             true\n"
        )
        internal_output = (
            "shared_feature    stable             true\n"
            "internal_only     under development  false\n"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            official = self.collect(
                root / "official",
                RecordingFeatureRunner(
                    [
                        feature_result(official_output),
                        feature_result(official_output),
                    ]
                ),
                side="official",
                cli_name="official-codex",
            )
            internal = self.collect(
                root / "internal",
                RecordingFeatureRunner(
                    [
                        feature_result(internal_output),
                        feature_result(internal_output),
                    ]
                ),
                side="internal",
                cli_name="internal-codex",
            )

            comparison = compare_feature_inventories(official, internal)

            self.assertEqual(
                [entry.name for entry in comparison.entries],
                ["internal_only", "official_only", "shared_feature"],
            )
            self.assertIsNone(comparison.entry("official_only").internal)
            self.assertIsNotNone(comparison.entry("official_only").official)
            self.assertIsNone(comparison.entry("internal_only").official)
            self.assertIsNotNone(comparison.entry("internal_only").internal)

    def test_feature_order_and_canonical_bytes_are_deterministic(self) -> None:
        first_isolated = (
            "zeta_feature   experimental       false\n"
            "alpha_feature  stable             true\n"
        )
        first_effective = (
            "alpha_feature  stable             true\n"
            "zeta_feature   experimental       true\n"
        )
        second_isolated = (
            "alpha_feature  stable             true\n"
            "zeta_feature   experimental       false\n"
        )
        second_effective = (
            "zeta_feature   experimental       true\n"
            "alpha_feature  stable             true\n"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self.collect(
                root / "first",
                RecordingFeatureRunner(
                    [
                        feature_result(first_isolated),
                        feature_result(first_effective),
                    ]
                ),
            )
            second = self.collect(
                root / "second",
                RecordingFeatureRunner(
                    [
                        feature_result(second_isolated),
                        feature_result(second_effective),
                    ]
                ),
            )

            self.assertEqual(
                [feature.name for feature in first.features],
                ["alpha_feature", "zeta_feature"],
            )
            self.assertEqual(first.canonical_bytes, second.canonical_bytes)
            self.assertEqual(first.payload_sha256, second.payload_sha256)

    def test_malformed_feature_output_fails_closed(self) -> None:
        cases = (
            ("empty", "", ""),
            (
                "invalid bool",
                "multi_agent_v2  stable  maybe\n",
                "multi_agent_v2  stable  maybe\n",
            ),
            (
                "invalid stage",
                "multi_agent_v2  beta  false\n",
                "multi_agent_v2  beta  false\n",
            ),
            (
                "duplicate",
                (
                    "multi_agent_v2  stable  false\n"
                    "multi_agent_v2  stable  false\n"
                ),
                (
                    "multi_agent_v2  stable  false\n"
                    "multi_agent_v2  stable  false\n"
                ),
            ),
            (
                "stage changed",
                "multi_agent_v2  under development  false\n",
                "multi_agent_v2  stable  true\n",
            ),
            (
                "duplicate namespaced feature",
                "guardianv2.thread_context  under development  false\n" * 2,
                "guardianv2.thread_context  under development  false\n" * 2,
            ),
            (
                "namespaced stage changed",
                "guardianv2.thread_context  under development  false\n",
                "guardianv2.thread_context  stable  false\n",
            ),
            (
                "namespaced invalid bool",
                "guardianv2.thread_context  under development  maybe\n",
                "guardianv2.thread_context  under development  maybe\n",
            ),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for label, isolated, effective in cases:
                with self.subTest(label=label):
                    runner = RecordingFeatureRunner(
                        [
                            feature_result(isolated),
                            feature_result(effective),
                        ]
                    )
                    with self.assertRaises(ParityValidationError) as raised:
                        self.collect(root / label.replace(" ", "-"), runner)
                    self.assertEqual(
                        raised.exception.code,
                        "parity.feature.output_invalid",
                    )

    def test_namespaced_feature_segments_are_nonempty_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = "guardianv2.thread_context.v2  under development  false\n"
            inventory = self.collect(root, RecordingFeatureRunner([
                feature_result(output), feature_result(output),
            ]))
            self.assertEqual(
                inventory.features[0].name, "guardianv2.thread_context.v2",
            )
            for name in (
                ".guardianv2", "guardianv2.", "guardianv2..thread_context",
                "guardianv2._context", "guardianv2.Thread_context",
                "guardianv2/thread_context", "guardianv2.thread-context",
                "guardianv2 thread_context", "guardianv2.线程",
            ):
                with self.subTest(name=name):
                    output = f"{name}  under development  false\n"
                    with self.assertRaises(ParityValidationError) as raised:
                        self.collect(root, RecordingFeatureRunner([
                            feature_result(output), feature_result(output),
                        ]))
                    self.assertEqual(
                        raised.exception.code, "parity.feature.output_invalid",
                    )

    def test_runner_failure_timeout_and_truncation_fail_closed(self) -> None:
        failures = (
            feature_result("", returncode=2, stderr="failed"),
            feature_result("", returncode=None, timed_out=True),
            feature_result(
                "multi_agent_v2  stable  false\n",
                stdout_truncated=True,
            ),
            feature_result("", stderr_truncated=True),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for result in failures:
                with self.subTest(result=result):
                    runner = RecordingFeatureRunner([result])
                    with self.assertRaises(ParityValidationError) as raised:
                        self.collect(root, runner)
                    self.assertEqual(
                        raised.exception.code,
                        "parity.feature.command_failed",
                    )


class ParityProtocolInventoryTests(unittest.TestCase):
    def collect(
        self,
        documents: dict[str, dict[str, object]],
        *,
        side: str = "official",
    ) -> ProtocolInventory:
        return collect_protocol_inventory(
            side=side,
            schema_documents=documents,
        )

    def test_all_four_directions_and_transitive_local_refs_are_recorded(
        self,
    ) -> None:
        documents = protocol_documents()
        client_request = documents["ClientRequest.json"]
        client_request["definitions"] = {
            "Unused": {"type": "boolean"},
            "Payload": {
                "description": "payload documentation",
                "type": "object",
                "required": ["count"],
                "properties": {
                    "count": {
                        "description": "count documentation",
                        "type": "integer",
                    }
                },
            },
            "OpenParams": {
                "type": "object",
                "required": ["payload"],
                "properties": {
                    "payload": {"$ref": "#/definitions/Payload"}
                },
            },
        }
        variant = client_request["oneOf"][0]
        assert isinstance(variant, dict)
        properties = variant["properties"]
        assert isinstance(properties, dict)
        properties["params"] = {"$ref": "#/definitions/OpenParams"}

        inventory = self.collect(documents)

        self.assertEqual(
            [
                (record.direction, record.method)
                for record in inventory.methods
            ],
            [
                ("client_request", "client/request"),
                ("client_notification", "client/notification"),
                ("server_request", "server/request"),
                ("server_notification", "server/notification"),
            ],
        )
        request = inventory.method("client_request", "client/request")
        request_schema = dict(request.schema)
        params = request_schema["properties"]["params"]
        self.assertEqual(
            params,
            {
                "properties": {
                    "payload": {
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                        "type": "object",
                    }
                },
                "required": ["payload"],
                "type": "object",
            },
        )
        self.assertNotIn(b'"description"', request.canonical_bytes)
        self.assertNotIn(b'"title"', request.canonical_bytes)
        self.assertNotIn(b"Unused", request.canonical_bytes)

    def test_documentation_and_input_order_do_not_change_canonical_bytes(
        self,
    ) -> None:
        first = protocol_documents()
        first_client = first["ClientRequest.json"]
        first_client["definitions"] = {
            "Zeta": {"type": "string", "description": "first"},
            "Alpha": {
                "type": "object",
                "properties": {
                    "zeta": {"$ref": "#/definitions/Zeta"},
                    "alpha": {"type": "integer"},
                },
            },
        }
        first_variant = first_client["oneOf"][0]
        assert isinstance(first_variant, dict)
        first_properties = first_variant["properties"]
        assert isinstance(first_properties, dict)
        first_properties["params"] = {"$ref": "#/definitions/Alpha"}

        second = copy.deepcopy(first)
        second_client = second["ClientRequest.json"]
        second_client["description"] = "different root documentation"
        second_client["definitions"] = {
            "Alpha": {
                "properties": {
                    "alpha": {"type": "integer"},
                    "zeta": {"$ref": "#/definitions/Zeta"},
                },
                "type": "object",
                "title": "different title",
            },
            "Zeta": {"description": "second", "type": "string"},
        }
        second["ServerNotification.json"]["oneOf"].reverse()

        first_inventory = self.collect(first)
        second_inventory = self.collect(second)

        self.assertEqual(
            first_inventory.canonical_bytes,
            second_inventory.canonical_bytes,
        )
        self.assertEqual(
            first_inventory.payload_sha256,
            second_inventory.payload_sha256,
        )

    def test_official_and_internal_only_methods_are_retained(self) -> None:
        official_documents = protocol_documents()
        official_documents["ClientRequest.json"]["oneOf"].append(
            protocol_variant(
                "client_request",
                "official/only",
                protocol_object({}),
            )
        )
        internal_documents = protocol_documents()
        internal_documents["ClientRequest.json"]["oneOf"].append(
            protocol_variant(
                "client_request",
                "internal/only",
                protocol_object({}),
            )
        )

        comparison = compare_protocol_inventories(
            self.collect(official_documents),
            self.collect(internal_documents, side="internal"),
        )

        self.assertIsNone(
            comparison.entry("client_request", "official/only").internal
        )
        self.assertIsNone(
            comparison.entry("client_request", "internal/only").official
        )
        self.assertEqual(
            [
                (entry.direction, entry.method)
                for entry in comparison.entries
                if entry.method.endswith("/only")
            ],
            [
                ("client_request", "internal/only"),
                ("client_request", "official/only"),
            ],
        )

    def test_compatibility_uses_the_actual_producer_direction(self) -> None:
        narrow = protocol_object(
            {"mode": {"type": "string", "enum": ["stable"]}},
            required=("mode",),
        )
        wide = protocol_object(
            {"mode": {"type": "string", "enum": ["stable", "beta"]}},
            required=("mode",),
        )
        official = protocol_documents(
            {
                "client_request": ("client/request", narrow),
                "client_notification": ("client/notification", wide),
                "server_request": ("server/request", narrow),
                "server_notification": ("server/notification", wide),
            }
        )
        internal = protocol_documents(
            {
                "client_request": ("client/request", wide),
                "client_notification": ("client/notification", narrow),
                "server_request": ("server/request", wide),
                "server_notification": ("server/notification", narrow),
            }
        )

        comparison = compare_protocol_inventories(
            self.collect(official),
            self.collect(internal, side="internal"),
        )

        client_request = comparison.entry(
            "client_request",
            "client/request",
        )
        self.assertTrue(client_request.compatible)
        self.assertEqual(client_request.producer_side, "official")
        self.assertEqual(client_request.consumer_side, "internal")

        client_notification = comparison.entry(
            "client_notification",
            "client/notification",
        )
        self.assertFalse(client_notification.compatible)
        self.assertIn(
            "parity.protocol.enum_incompatible",
            client_notification.reason_codes,
        )

        server_request = comparison.entry(
            "server_request",
            "server/request",
        )
        self.assertFalse(server_request.compatible)
        self.assertEqual(server_request.producer_side, "internal")
        self.assertEqual(server_request.consumer_side, "official")

        server_notification = comparison.entry(
            "server_notification",
            "server/notification",
        )
        self.assertTrue(server_notification.compatible)

    def test_additive_optional_properties_are_compatible(self) -> None:
        core = protocol_object(
            {"value": {"type": "string"}},
            required=("value",),
        )
        with_optional = protocol_object(
            {
                "trace": {"type": "string"},
                "value": {"type": "string"},
            },
            required=("value",),
        )
        official = protocol_documents(
            {
                "client_request": ("client/request", core),
                "client_notification": ("client/notification", core),
                "server_request": ("server/request", core),
                "server_notification": ("server/notification", core),
            }
        )
        internal = protocol_documents(
            {
                "client_request": ("client/request", with_optional),
                "client_notification": (
                    "client/notification",
                    with_optional,
                ),
                "server_request": ("server/request", with_optional),
                "server_notification": (
                    "server/notification",
                    with_optional,
                ),
            }
        )

        comparison = compare_protocol_inventories(
            self.collect(official),
            self.collect(internal, side="internal"),
        )

        self.assertTrue(
            comparison.entry(
                "client_request",
                "client/request",
            ).compatible
        )
        self.assertTrue(
            comparison.entry(
                "server_notification",
                "server/notification",
            ).compatible
        )

    def test_additional_properties_default_uses_subset_semantics(self) -> None:
        producer_open = protocol_object(
            {"value": {"type": "string"}},
            required=("value",),
        )
        consumer_closed = protocol_object(
            {"value": {"type": "string"}},
            required=("value",),
            additional_properties=False,
        )
        official = protocol_documents()
        internal = protocol_documents()
        official["ClientRequest.json"]["oneOf"][0] = protocol_variant(
            "client_request",
            "client/request",
            producer_open,
        )
        internal["ClientRequest.json"]["oneOf"][0] = protocol_variant(
            "client_request",
            "client/request",
            consumer_closed,
        )
        official["ServerNotification.json"]["oneOf"][0] = protocol_variant(
            "server_notification",
            "server/notification",
            producer_open,
        )
        internal["ServerNotification.json"]["oneOf"][0] = protocol_variant(
            "server_notification",
            "server/notification",
            consumer_closed,
        )

        comparison = compare_protocol_inventories(
            self.collect(official),
            self.collect(internal, side="internal"),
        )

        client = comparison.entry("client_request", "client/request")
        self.assertFalse(client.compatible)
        self.assertIn(
            "parity.protocol.additional_property_incompatible",
            client.reason_codes,
        )

        server = comparison.entry(
            "server_notification",
            "server/notification",
        )
        self.assertTrue(server.compatible)

    def test_new_consumer_requirement_is_incompatible(self) -> None:
        producer = protocol_object(
            {"value": {"type": "string"}},
            required=("value",),
        )
        consumer = protocol_object(
            {
                "requiredByConsumer": {"type": "string"},
                "value": {"type": "string"},
            },
            required=("requiredByConsumer", "value"),
        )
        official = protocol_documents()
        internal = protocol_documents()
        official["ClientRequest.json"]["oneOf"][0] = protocol_variant(
            "client_request",
            "client/request",
            producer,
        )
        internal["ClientRequest.json"]["oneOf"][0] = protocol_variant(
            "client_request",
            "client/request",
            consumer,
        )

        entry = compare_protocol_inventories(
            self.collect(official),
            self.collect(internal, side="internal"),
        ).entry("client_request", "client/request")

        self.assertFalse(entry.compatible)
        self.assertIn(
            "parity.protocol.required_field_incompatible",
            entry.reason_codes,
        )

    def test_reference_cycles_fail_closed(self) -> None:
        documents = protocol_documents()
        client_request = documents["ClientRequest.json"]
        client_request["definitions"] = {
            "A": {"$ref": "#/definitions/B"},
            "B": {"$ref": "#/definitions/A"},
        }
        variant = client_request["oneOf"][0]
        assert isinstance(variant, dict)
        properties = variant["properties"]
        assert isinstance(properties, dict)
        properties["params"] = {"$ref": "#/definitions/A"}

        with self.assertRaises(ParityValidationError) as raised:
            self.collect(documents)

        self.assertEqual(
            raised.exception.code,
            "parity.protocol.reference_cycle",
        )

    def test_boolean_schemas_are_preserved(self) -> None:
        documents = protocol_documents()
        variant = documents["ClientRequest.json"]["oneOf"][0]
        assert isinstance(variant, dict)
        properties = variant["properties"]
        assert isinstance(properties, dict)
        properties["params"] = protocol_object(
            {
                "acceptsAnything": True,
                "acceptsNothing": False,
            }
        )

        inventory = self.collect(documents)
        request = inventory.method("client_request", "client/request")
        params = request.schema["properties"]["params"]

        self.assertEqual(
            params["properties"],
            {
                "acceptsAnything": True,
                "acceptsNothing": False,
            },
        )

    def test_boolean_property_schemas_use_subset_semantics(self) -> None:
        official = protocol_documents()
        internal = protocol_documents()

        official_client = official["ClientRequest.json"]["oneOf"][0]
        internal_client = internal["ClientRequest.json"]["oneOf"][0]
        assert isinstance(official_client, dict)
        assert isinstance(internal_client, dict)
        official_client["properties"]["params"] = protocol_object(
            {"value": True},
            required=("value",),
        )
        internal_client["properties"]["params"] = protocol_object(
            {"value": False},
            required=("value",),
        )

        official_server = official["ServerNotification.json"]["oneOf"][0]
        internal_server = internal["ServerNotification.json"]["oneOf"][0]
        assert isinstance(official_server, dict)
        assert isinstance(internal_server, dict)
        official_server["properties"]["params"] = protocol_object(
            {"value": True},
            required=("value",),
        )
        internal_server["properties"]["params"] = protocol_object(
            {"value": False},
            required=("value",),
        )

        comparison = compare_protocol_inventories(
            self.collect(official),
            self.collect(internal, side="internal"),
        )

        client = comparison.entry("client_request", "client/request")
        self.assertFalse(client.compatible)
        self.assertIn(
            "parity.protocol.schema_incompatible",
            client.reason_codes,
        )

        server = comparison.entry(
            "server_notification",
            "server/notification",
        )
        self.assertTrue(server.compatible)

    def test_boolean_array_items_use_subset_semantics(self) -> None:
        official = protocol_documents()
        internal = protocol_documents()

        official_client = official["ClientRequest.json"]["oneOf"][0]
        internal_client = internal["ClientRequest.json"]["oneOf"][0]
        assert isinstance(official_client, dict)
        assert isinstance(internal_client, dict)
        official_client["properties"]["params"] = {
            "type": "array",
            "items": True,
        }
        internal_client["properties"]["params"] = {
            "type": "array",
            "items": False,
        }

        official_server = official["ServerNotification.json"]["oneOf"][0]
        internal_server = internal["ServerNotification.json"]["oneOf"][0]
        assert isinstance(official_server, dict)
        assert isinstance(internal_server, dict)
        official_server["properties"]["params"] = {
            "type": "array",
            "items": True,
        }
        internal_server["properties"]["params"] = {
            "type": "array",
            "items": False,
        }

        comparison = compare_protocol_inventories(
            self.collect(official),
            self.collect(internal, side="internal"),
        )

        client = comparison.entry("client_request", "client/request")
        self.assertFalse(client.compatible)
        self.assertIn(
            "parity.protocol.items_incompatible",
            client.reason_codes,
        )

        server = comparison.entry(
            "server_notification",
            "server/notification",
        )
        self.assertTrue(server.compatible)

    def test_boolean_combinator_branches_use_subset_semantics(self) -> None:
        official = protocol_documents()
        internal = protocol_documents()

        official["ClientRequest.json"]["oneOf"][0] = protocol_variant(
            "client_request",
            "client/request",
            {"anyOf": [True, {"type": "string"}]},
        )
        internal["ClientRequest.json"]["oneOf"][0] = protocol_variant(
            "client_request",
            "client/request",
            {"type": "string"},
        )
        official["ClientNotification.json"]["oneOf"][0] = protocol_variant(
            "client_notification",
            "client/notification",
            {"type": "string"},
        )
        internal["ClientNotification.json"]["oneOf"][0] = protocol_variant(
            "client_notification",
            "client/notification",
            {"allOf": [False, {"type": "string"}]},
        )

        comparison = compare_protocol_inventories(
            self.collect(official),
            self.collect(internal, side="internal"),
        )

        for direction, method, reason_code in (
            (
                "client_request",
                "client/request",
                "parity.protocol.type_incompatible",
            ),
            (
                "client_notification",
                "client/notification",
                "parity.protocol.schema_incompatible",
            ),
        ):
            with self.subTest(direction=direction, method=method):
                entry = comparison.entry(direction, method)
                self.assertFalse(entry.compatible)
                self.assertIn(reason_code, entry.reason_codes)

    def test_nullable_union_spellings_are_semantically_equal(self) -> None:
        official = protocol_documents()
        internal = protocol_documents()
        nullable_any_of = {
            "anyOf": [
                {"type": "string"},
                {"type": "null"},
            ]
        }
        nullable_type_array = {"type": ["null", "string"]}
        methods = {
            "server_request": (
                "item/commandExecution/requestApproval",
                "item/permissions/requestApproval",
            ),
            "server_notification": (
                "item/autoApprovalReview/completed",
                "item/autoApprovalReview/started",
            ),
        }
        for direction, method_names in methods.items():
            root_name = PROTOCOL_ROOTS[direction]
            official[root_name]["oneOf"] = [
                protocol_variant(
                    direction,
                    method,
                    protocol_object(
                        {"reason": copy.deepcopy(nullable_any_of)},
                        required=("reason",),
                    ),
                )
                for method in method_names
            ]
            internal[root_name]["oneOf"] = [
                protocol_variant(
                    direction,
                    method,
                    protocol_object(
                        {"reason": copy.deepcopy(nullable_type_array)},
                        required=("reason",),
                    ),
                )
                for method in method_names
            ]

        official_inventory = self.collect(official)
        internal_inventory = self.collect(internal, side="internal")
        comparison = compare_protocol_inventories(
            official_inventory,
            internal_inventory,
        )

        for direction, method_names in methods.items():
            for method in method_names:
                with self.subTest(direction=direction, method=method):
                    official_record = official_inventory.method(
                        direction,
                        method,
                    )
                    internal_record = internal_inventory.method(
                        direction,
                        method,
                    )
                    self.assertEqual(
                        official_record.schema_sha256,
                        internal_record.schema_sha256,
                    )
                    entry = comparison.entry(direction, method)
                    self.assertTrue(entry.compatible)
                    self.assertEqual(entry.reason_codes, ())

    def test_unsupported_schema_constructs_fail_closed(self) -> None:
        documents = protocol_documents()
        variant = documents["ClientRequest.json"]["oneOf"][0]
        assert isinstance(variant, dict)
        properties = variant["properties"]
        assert isinstance(properties, dict)
        properties["params"] = {
            "type": "object",
            "not": {"required": ["forbidden"]},
        }

        with self.assertRaises(ParityValidationError) as raised:
            self.collect(documents)

        self.assertEqual(
            raised.exception.code,
            "parity.protocol.schema_unsupported",
        )


class ParityPolicyTests(unittest.TestCase):
    OPTIONAL_PROTOCOL_METHODS = (
        ("client_request", "app/installed"),
        ("client_request", "app/read"),
        ("client_request", "environment/status"),
        ("client_request", "thread/searchOccurrences"),
        ("server_notification", "thread/environment/connected"),
        ("server_notification", "thread/environment/disconnected"),
    )
    BASELINE_PROTOCOL_METHODS = (
        ("client_request", "initialize"),
        ("client_request", "config/read"),
        ("client_request", "model/list"),
        ("client_request", "collaborationMode/list"),
        ("client_request", "thread/start"),
        ("client_request", "turn/start"),
        ("server_request", "item/tool/call"),
        ("server_request", "execCommandApproval"),
        ("server_notification", "item/completed"),
        ("server_notification", "turn/completed"),
    )

    def feature(
        self,
        name: str,
        *,
        stage: str = "stable",
        isolated_default: bool = True,
        effective_state: bool = True,
    ) -> FeatureRecord:
        return FeatureRecord(
            name=name,
            stage=stage,
            isolated_default=isolated_default,
            effective_state=effective_state,
        )

    def feature_comparison(
        self,
        *,
        official: tuple[FeatureRecord, ...] = (),
        internal: tuple[FeatureRecord, ...] = (),
    ) -> FeatureInventoryComparison:
        return compare_feature_inventories(
            FeatureInventory(side="official", features=official),
            FeatureInventory(side="internal", features=internal),
        )

    def protocol_record(
        self,
        direction: str,
        method: str,
    ) -> ProtocolMethodRecord:
        return ProtocolMethodRecord(
            direction=direction,
            method=method,
            schema={"type": "object", "properties": {}},
        )

    def protocol_difference(
        self,
        direction: str,
        method: str,
        *,
        internal_present: bool = False,
        compatible: bool | None = None,
        reason_codes: tuple[str, ...] = (),
    ) -> ProtocolInventoryComparisonEntry:
        official = self.protocol_record(direction, method)
        internal = (
            self.protocol_record(direction, method)
            if internal_present
            else None
        )
        if compatible is None:
            compatible = (
                not direction.startswith("client_")
                if internal is None
                else True
            )
        if not reason_codes and not compatible:
            reason_codes = ("parity.protocol.method_missing",)
        return ProtocolInventoryComparisonEntry(
            direction=direction,
            method=method,
            official=official,
            internal=internal,
            producer_side=(
                "official" if direction.startswith("client_") else "internal"
            ),
            consumer_side=(
                "internal" if direction.startswith("client_") else "official"
            ),
            compatible=compatible,
            reason_codes=reason_codes,
        )

    def evaluate(
        self,
        *,
        features: FeatureInventoryComparison | None = None,
        protocol: ProtocolInventoryComparison | None = None,
        official_model_metadata: dict[str, object] | None = None,
        internal_model_metadata: dict[str, object] | None = None,
        custom_model_catalog: bool = False,
        observed_protocol_methods: frozenset[tuple[str, str]] = frozenset(),
        observed_features: frozenset[str] = frozenset(),
    ):
        evaluate_policy = getattr(
            parity_module,
            "evaluate_parity_policy",
            None,
        )
        self.assertTrue(
            callable(evaluate_policy),
            "Parity policy evaluation seam is missing",
        )
        return evaluate_policy(
            feature_comparison=features or self.feature_comparison(),
            protocol_comparison=protocol
            or ProtocolInventoryComparison(entries=()),
            official_model_metadata=official_model_metadata
            or {"multi_agent_version": "v2"},
            internal_model_metadata=internal_model_metadata
            or {"multi_agent_version": "v2"},
            custom_model_catalog=custom_model_catalog,
            observed_protocol_methods=observed_protocol_methods,
            observed_features=observed_features,
        )

    def test_custom_catalog_metadata_is_not_compared_to_official_models(self) -> None:
        result = self.evaluate(
            custom_model_catalog=True,
            official_model_metadata={
                "multi_agent_version": "official-value",
                "tool_mode": "official-tools",
                "future_field": "official-only",
            },
            internal_model_metadata={"tool_mode": "provider-tools"},
        )
        self.assertTrue(result.healthy)
        self.assertEqual(result.canonical_payload()["findings"], [{
            "category": "model_metadata",
            "code": "parity.model.custom_catalog_not_applicable",
            "severity": "info",
        }])
        self.assertEqual(result.synchronization_queue, ())

    def test_custom_catalog_does_not_bypass_runtime_protocol_failures(self) -> None:
        result = self.evaluate(
            custom_model_catalog=True,
            protocol=ProtocolInventoryComparison(entries=(
                self.protocol_difference("client_request", "initialize"),
            )),
        )
        self.assertFalse(result.healthy)
        self.assertIn("parity.protocol.core_missing", {
            finding.code for finding in result.findings
        })

    def test_custom_catalog_does_not_bypass_runtime_feature_failures(self) -> None:
        result = self.evaluate(
            custom_model_catalog=True,
            features=self.feature_comparison(
                official=(self.feature("unknown_runtime_feature"),),
                internal=(),
            ),
        )
        self.assertFalse(result.healthy)
        self.assertIn("parity.feature.unclassified_drift", {
            finding.code for finding in result.findings
        })

    def test_custom_catalog_context_requires_an_explicit_boolean(self) -> None:
        with self.assertRaises(ParityValidationError):
            self.evaluate(custom_model_catalog="false")

    def test_namespaced_feature_policy_keeps_unclassified_drift_blocking(self) -> None:
        name = "guardianv2.thread_context"
        feature = self.feature(
            name, stage="under development",
            isolated_default=False, effective_state=False,
        )
        trace = parity_module.ParityAcceptanceTrace(
            schema_version=1, trace_id="namespaced-feature-test",
            observed_protocol_methods=(), observed_features=(name,),
            item_ids_observed_dependencies=(), observed_protocol_extensions=(),
        )
        self.assertEqual(trace.canonical_payload()["observed_features"], [name])
        for same_state in (True, False):
            with self.subTest(same_state=same_state):
                comparison = self.feature_comparison(
                    official=(feature,),
                    internal=(replace(feature, effective_state=not same_state),),
                )
                result = parity_module.evaluate_parity_policy(
                    feature_comparison=comparison,
                    protocol_comparison=ProtocolInventoryComparison(entries=()),
                    official_model_metadata={}, internal_model_metadata={},
                    custom_model_catalog=True, acceptance_trace=trace,
                )
                self.assertEqual(result.healthy, same_state)
                feature_findings = [
                    finding for finding in result.findings
                    if finding.category == "feature"
                ]
                if same_state:
                    self.assertEqual(feature_findings, [])
                else:
                    self.assertEqual(len(feature_findings), 1)
                    self.assertEqual(
                        feature_findings[0].code,
                        "parity.feature.unclassified_drift",
                    )
                    self.assertEqual(feature_findings[0].severity, "error")
                    self.assertIn(name, feature_findings[0].message)
                self.assertEqual(result.canonical_payload()["healthy"], same_state)

    def test_baseline_protocol_closures_are_core_and_unhealthy(self) -> None:
        entries = [
            self.protocol_difference(direction, method)
            for direction, method in self.BASELINE_PROTOCOL_METHODS
        ]
        entries[-1] = self.protocol_difference(
            "server_notification",
            "turn/completed",
            internal_present=True,
            compatible=False,
            reason_codes=("parity.protocol.type_incompatible",),
        )

        result = self.evaluate(
            protocol=ProtocolInventoryComparison(entries=tuple(entries))
        )

        self.assertFalse(result.healthy)
        self.assertEqual(
            {finding.code for finding in result.findings},
            {
                "parity.protocol.core_incompatible",
                "parity.protocol.core_missing",
            },
        )
        self.assertTrue(
            all(finding.severity == "error" for finding in result.findings)
        )
        self.assertEqual(result.synchronization_queue, ())

    def test_multi_agent_v2_feature_and_model_metadata_are_core(self) -> None:
        result = self.evaluate(
            features=self.feature_comparison(
                official=(self.feature("multi_agent_v2"),),
                internal=(
                    self.feature(
                        "multi_agent_v2",
                        stage="under development",
                        isolated_default=False,
                        effective_state=False,
                    ),
                ),
            ),
            official_model_metadata={"multi_agent_version": "v2"},
            internal_model_metadata={"multi_agent_version": "v1"},
        )

        self.assertFalse(result.healthy)
        self.assertEqual(
            {finding.code for finding in result.findings},
            {
                "parity.feature.core_drift",
                "parity.model.multi_agent_version_core",
            },
        )
        self.assertEqual(result.synchronization_queue, ())

    def test_six_known_official_only_methods_are_optional_unless_observed(
        self,
    ) -> None:
        result = self.evaluate(
            protocol=ProtocolInventoryComparison(
                entries=tuple(
                    self.protocol_difference(direction, method)
                    for direction, method in reversed(
                        self.OPTIONAL_PROTOCOL_METHODS
                    )
                )
            )
        )

        self.assertTrue(result.healthy)
        self.assertEqual(
            result.policy_version,
            ParityPolicyVersion(PARITY_POLICY_VERSION),
        )
        self.assertEqual(len(result.findings), 6)
        self.assertTrue(
            all(
                finding.code == "parity.protocol.optional_missing"
                and finding.severity == "warning"
                for finding in result.findings
            )
        )
        self.assertEqual(
            {
                item.identifier
                for item in result.synchronization_queue
            },
            {
                f"{direction}:{method}"
                for direction, method in self.OPTIONAL_PROTOCOL_METHODS
            },
        )

    def test_skill_search_is_optional_unless_observed(self) -> None:
        features = self.feature_comparison(
            official=(self.feature("skill_search"),),
        )

        optional = self.evaluate(features=features)
        observed = self.evaluate(
            features=features,
            observed_features=frozenset({"skill_search"}),
        )

        self.assertTrue(optional.healthy)
        self.assertEqual(
            [finding.code for finding in optional.findings],
            ["parity.feature.optional_missing"],
        )
        self.assertEqual(
            [item.identifier for item in optional.synchronization_queue],
            ["skill_search"],
        )
        self.assertFalse(observed.healthy)
        self.assertEqual(
            [finding.code for finding in observed.findings],
            ["parity.feature.observed_core_missing"],
        )
        self.assertEqual(observed.synchronization_queue, ())

    def test_known_development_and_stage_default_drift_are_optional(
        self,
    ) -> None:
        development = (
            "code_mode_buffered_exec",
            "executor_capability_discovery",
            "external_agent_memory_import",
            "mcp_2026_07_28",
        )
        metadata_only = ("enable_fanout", "item_ids", "memories")
        official = tuple(
            self.feature(
                name,
                stage="under development",
                isolated_default=False,
                effective_state=False,
            )
            for name in development
        ) + tuple(
            self.feature(name)
            for name in metadata_only
        )
        internal = tuple(
            self.feature(
                name,
                stage="experimental",
                isolated_default=False,
                effective_state=True,
            )
            for name in reversed(metadata_only)
        )

        result = self.evaluate(
            features=self.feature_comparison(
                official=tuple(reversed(official)),
                internal=internal,
            )
        )

        self.assertTrue(result.healthy)
        self.assertEqual(
            {finding.code for finding in result.findings},
            {
                "parity.feature.optional_drift",
                "parity.feature.optional_missing",
            },
        )
        queue_keys = tuple(
            item.sort_key
            for item in result.synchronization_queue
        )
        self.assertEqual(queue_keys, tuple(sorted(queue_keys)))
        finding_keys = tuple(finding.sort_key for finding in result.findings)
        self.assertEqual(finding_keys, tuple(sorted(finding_keys)))

    def test_tool_mode_is_pending_provider_optional_evidence(self) -> None:
        result = self.evaluate(
            official_model_metadata={
                "multi_agent_version": "v2",
                "tool_mode": "code_mode",
            },
            internal_model_metadata={"multi_agent_version": "v2"},
        )

        self.assertTrue(result.healthy)
        self.assertEqual(
            [finding.code for finding in result.findings],
            ["parity.model.tool_mode_pending_provider"],
        )
        self.assertEqual(
            [
                (
                    item.category,
                    item.identifier,
                    item.finding_code,
                )
                for item in result.synchronization_queue
            ],
            [
                (
                    "model_metadata",
                    "tool_mode",
                    "parity.model.tool_mode_pending_provider",
                )
            ],
        )

    def test_observed_optional_protocol_method_escalates_to_core(self) -> None:
        method = ("client_request", "app/read")
        result = self.evaluate(
            protocol=ProtocolInventoryComparison(
                entries=(self.protocol_difference(*method),)
            ),
            observed_protocol_methods=frozenset({method}),
        )

        self.assertFalse(result.healthy)
        self.assertEqual(
            [finding.code for finding in result.findings],
            ["parity.protocol.observed_core_missing"],
        )
        self.assertEqual(result.synchronization_queue, ())

    def test_unknown_feature_drift_is_unclassified_and_unhealthy(self) -> None:
        result = self.evaluate(
            features=self.feature_comparison(
                official=(self.feature("future_unknown_feature"),),
            )
        )

        self.assertFalse(result.healthy)
        self.assertEqual(
            [finding.code for finding in result.findings],
            ["parity.feature.unclassified_drift"],
        )
        self.assertEqual(
            [finding.severity for finding in result.findings],
            ["error"],
        )
        self.assertEqual(result.synchronization_queue, ())

    def test_known_optional_labels_do_not_whitelist_internal_only_drift(
        self,
    ) -> None:
        internal_method = self.protocol_record(
            "client_request",
            "app/read",
        )
        result = self.evaluate(
            features=self.feature_comparison(
                internal=(self.feature("skill_search"),),
            ),
            protocol=ProtocolInventoryComparison(
                entries=(
                    ProtocolInventoryComparisonEntry(
                        direction="client_request",
                        method="app/read",
                        official=None,
                        internal=internal_method,
                        producer_side="official",
                        consumer_side="internal",
                        compatible=True,
                    ),
                )
            ),
        )

        self.assertFalse(result.healthy)
        self.assertEqual(
            {finding.code for finding in result.findings},
            {
                "parity.feature.unclassified_drift",
                "parity.protocol.unclassified_drift",
            },
        )
        self.assertEqual(result.synchronization_queue, ())


class _FixtureProtocolMethodRecord(ProtocolMethodRecord):
    __slots__ = ("_fixture_schema_sha256",)

    def __init__(
        self,
        *,
        direction: str,
        method: str,
        schema_sha256: str,
    ) -> None:
        super().__init__(
            direction=direction,
            method=method,
            schema={"type": "object", "properties": {}},
        )
        object.__setattr__(
            self,
            "_fixture_schema_sha256",
            schema_sha256,
        )

    @property
    def schema_sha256(self) -> str:
        return self._fixture_schema_sha256


class ParityMethodCoverageTests(unittest.TestCase):
    FIXTURE = (
        Path(__file__).resolve().parents[1]
        / "testdata"
        / "parity"
        / "current-method-coverage-redacted.json"
    )

    def fixture(self) -> dict[str, object]:
        payload = json.loads(self.FIXTURE.read_text())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["saved_error_counts"]["total"], 13)
        return payload

    def comparison(
        self,
        *,
        normalized: bool,
        methods: frozenset[tuple[str, str]] | None = None,
    ) -> ProtocolInventoryComparison:
        rows = self.fixture()["methods"]
        self.assertIsInstance(rows, list)
        entries: list[ProtocolInventoryComparisonEntry] = []
        for row in rows:
            self.assertIsInstance(row, dict)
            key = (row["direction"], row["method"])
            if methods is not None and key not in methods:
                continue
            official_digest_key = (
                "normalized_official_schema_sha256"
                if normalized
                else "official_schema_sha256"
            )
            internal_digest_key = (
                "normalized_internal_schema_sha256"
                if normalized
                else "internal_schema_sha256"
            )
            compatible = (
                normalized
                and row["planned_disposition"] == "native_equivalent"
            )
            entries.append(
                ProtocolInventoryComparisonEntry(
                    direction=row["direction"],
                    method=row["method"],
                    official=_FixtureProtocolMethodRecord(
                        direction=row["direction"],
                        method=row["method"],
                        schema_sha256=row[official_digest_key],
                    ),
                    internal=_FixtureProtocolMethodRecord(
                        direction=row["direction"],
                        method=row["method"],
                        schema_sha256=row[internal_digest_key],
                    ),
                    producer_side=(
                        "official"
                        if row["direction"].startswith("client_")
                        else "internal"
                    ),
                    consumer_side=(
                        "internal"
                        if row["direction"].startswith("client_")
                        else "official"
                    ),
                    compatible=compatible,
                    reason_codes=(
                        ()
                        if compatible
                        else tuple(row["reason_codes"])
                    ),
                )
            )
        return ProtocolInventoryComparison(entries=tuple(entries))

    def feature_comparison(
        self,
        *names: str,
    ) -> FeatureInventoryComparison:
        official = tuple(
            FeatureRecord(
                name=name,
                stage="stable",
                isolated_default=True,
                effective_state=True,
            )
            for name in names
        )
        internal = tuple(
            FeatureRecord(
                name=name,
                stage="experimental",
                isolated_default=False,
                effective_state=False,
            )
            for name in names
        )
        return compare_feature_inventories(
            FeatureInventory(side="official", features=official),
            FeatureInventory(side="internal", features=internal),
        )

    def coverage(
        self,
        comparison: ProtocolInventoryComparison,
        *,
        adapter_rules: tuple[object, ...] | None = None,
    ) -> tuple[object, ...]:
        build = getattr(parity_module, "build_method_coverage", None)
        self.assertTrue(
            callable(build),
            "Parity method-coverage builder is missing",
        )
        kwargs = {}
        if adapter_rules is not None:
            kwargs["adapter_rules"] = adapter_rules
        return build(comparison, **kwargs)

    def v2_evidence(
        self,
        *,
        typed_probe_result: str | None = None,
        fingerprints_revalidated: bool = False,
    ) -> object:
        evidence_type = getattr(
            parity_module,
            "MultiAgentV2Evidence",
            None,
        )
        self.assertTrue(
            callable(evidence_type),
            "Typed-v2 policy evidence is missing",
        )
        return evidence_type(
            overlay_projected=True,
            config_projected=True,
            typed_probe_result=typed_probe_result,
            fingerprints_revalidated=fingerprints_revalidated,
        )

    def evaluate(
        self,
        *,
        protocol: ProtocolInventoryComparison,
        features: FeatureInventoryComparison | None = None,
        method_coverage: tuple[object, ...] = (),
        evaluation_stage: str = "final",
        multi_agent_v2_evidence: object | None = None,
        item_ids_observed_dependencies: frozenset[str] = frozenset(),
        observed_protocol_extensions: frozenset[
            tuple[str, str, str]
        ] = frozenset(),
    ) -> ParityPolicyEvaluation:
        evaluate_policy = parity_module.evaluate_parity_policy
        required_parameters = {
            "evaluation_stage",
            "item_ids_observed_dependencies",
            "method_coverage",
            "multi_agent_v2_evidence",
            "observed_protocol_extensions",
        }
        observed_parameters = set(
            inspect.signature(evaluate_policy).parameters
        )
        self.assertTrue(
            required_parameters.issubset(observed_parameters),
            "Parity policy method-coverage/two-pass inputs are missing",
        )
        return evaluate_policy(
            feature_comparison=features
            or FeatureInventoryComparison(entries=()),
            protocol_comparison=protocol,
            official_model_metadata={"multi_agent_version": "v2"},
            internal_model_metadata={"multi_agent_version": "v2"},
            method_coverage=method_coverage,
            evaluation_stage=evaluation_stage,
            multi_agent_v2_evidence=multi_agent_v2_evidence,
            item_ids_observed_dependencies=(
                item_ids_observed_dependencies
            ),
            observed_protocol_extensions=observed_protocol_extensions,
        )

    def test_retained_thirteen_error_fixture_closes_only_as_one_final_policy(
        self,
    ) -> None:
        fixture = self.fixture()
        saved = self.evaluate(
            protocol=self.comparison(normalized=False),
            features=self.feature_comparison(
                "item_ids",
                "multi_agent_v2",
            ),
        )
        saved_errors = [
            finding
            for finding in saved.findings
            if finding.severity == "error"
        ]
        self.assertEqual(len(saved_errors), 13)
        self.assertEqual(
            sum(
                finding.code
                == "parity.protocol.core_incompatible"
                for finding in saved_errors
            ),
            8,
        )
        self.assertEqual(
            sum(
                finding.code
                == "parity.protocol.unclassified_drift"
                for finding in saved_errors
            ),
            3,
        )
        self.assertEqual(
            sum(
                finding.code == "parity.feature.core_drift"
                for finding in saved_errors
            ),
            2,
        )

        normalized = self.comparison(normalized=True)
        coverage = self.coverage(normalized)
        self.assertEqual(len(coverage), 7)
        dependencies = frozenset({"thread/resume.params.history"})
        eligibility = self.evaluate(
            protocol=normalized,
            features=self.feature_comparison(
                "item_ids",
                "multi_agent_v2",
            ),
            method_coverage=coverage,
            evaluation_stage="eligibility",
            multi_agent_v2_evidence=self.v2_evidence(),
            item_ids_observed_dependencies=dependencies,
        )
        self.assertFalse(eligibility.healthy)
        self.assertTrue(eligibility.probe_eligible)
        self.assertEqual(
            eligibility.provisional_capabilities,
            ("multi_agent_v2",),
        )

        final = self.evaluate(
            protocol=normalized,
            features=self.feature_comparison(
                "item_ids",
                "multi_agent_v2",
            ),
            method_coverage=coverage,
            multi_agent_v2_evidence=self.v2_evidence(
                typed_probe_result="passed",
                fingerprints_revalidated=True,
            ),
            item_ids_observed_dependencies=dependencies,
        )
        self.assertTrue(final.healthy)
        self.assertFalse(
            any(
                finding.severity == "error"
                for finding in final.findings
            )
        )
        self.assertEqual(
            [
                {
                    "category": item.category,
                    "finding_code": item.finding_code,
                    "identifier": item.identifier,
                }
                for item in final.synchronization_queue
            ],
            fixture["optional_queue"],
        )

        for index, record in enumerate(coverage):
            with self.subTest(missing_coverage=record.method):
                partial = coverage[:index] + coverage[index + 1 :]
                self.assertFalse(
                    self.evaluate(
                        protocol=normalized,
                        features=self.feature_comparison(
                            "item_ids",
                            "multi_agent_v2",
                        ),
                        method_coverage=partial,
                        multi_agent_v2_evidence=self.v2_evidence(
                            typed_probe_result="passed",
                            fingerprints_revalidated=True,
                        ),
                        item_ids_observed_dependencies=dependencies,
                    ).healthy
                )
        self.assertFalse(
            self.evaluate(
                protocol=normalized,
                features=self.feature_comparison(
                    "item_ids",
                    "multi_agent_v2",
                ),
                method_coverage=coverage,
                multi_agent_v2_evidence=self.v2_evidence(
                    typed_probe_result="passed",
                    fingerprints_revalidated=True,
                ),
            ).healthy
        )
        self.assertFalse(
            self.evaluate(
                protocol=normalized,
                features=self.feature_comparison(
                    "item_ids",
                    "multi_agent_v2",
                ),
                method_coverage=coverage,
                multi_agent_v2_evidence=self.v2_evidence(),
                item_ids_observed_dependencies=dependencies,
            ).healthy
        )

    def test_global_method_reason_and_schema_pair_only_evidence_fail_closed(
        self,
    ) -> None:
        method = frozenset({("client_request", "thread/resume")})
        comparison = self.comparison(
            normalized=True,
            methods=method,
        )
        coverage = self.coverage(comparison)
        self.assertEqual(len(coverage), 1)
        self.assertRegex(
            protocol_adapter_rule_set_digest(),
            r"^[0-9a-f]{64}$",
        )
        self.assertFalse(
            self.evaluate(
                protocol=comparison,
                method_coverage=(),
            ).healthy
        )
        manifest_without_resume = tuple(
            rule
            for rule
            in protocol_adapter_module.protocol_adapter_rule_manifest()
            if rule.rule_id
            != protocol_adapter_module.THREAD_RESUME_HISTORY_RULE_ID
        )
        missing_rule_coverage = self.coverage(
            comparison,
            adapter_rules=manifest_without_resume,
        )
        self.assertEqual(
            [record.disposition for record in missing_rule_coverage],
            ["uncovered"],
        )
        self.assertFalse(
            self.evaluate(
                protocol=comparison,
                method_coverage=missing_rule_coverage,
            ).healthy
        )

        record = coverage[0]
        rule_id = record.adapter_rule_digests[0][0]
        invalid_records = (
            replace(record, official_schema_sha256=SHA_A),
            replace(
                record,
                reason_codes=("parity.protocol.type_incompatible",),
            ),
            replace(record, method="thread/start"),
            replace(
                record,
                adapter_rule_digests=((rule_id, SHA_A),),
            ),
        )
        for invalid in invalid_records:
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    self.evaluate(
                        protocol=comparison,
                        method_coverage=(invalid,),
                    ).healthy
                )

    def test_exact_optional_extensions_escalate_when_observed_or_changed(
        self,
    ) -> None:
        key = ("client_request", "account/login/start")
        comparison = self.comparison(
            normalized=True,
            methods=frozenset({key}),
        )
        coverage = self.coverage(comparison)
        optional = self.evaluate(
            protocol=comparison,
            method_coverage=coverage,
        )
        self.assertTrue(optional.healthy)
        self.assertEqual(
            [item.identifier for item in optional.synchronization_queue],
            [
                "client_request:account/login/start#amazon_bedrock"
            ],
        )

        observed = self.evaluate(
            protocol=comparison,
            method_coverage=coverage,
            observed_protocol_extensions=frozenset(
                {
                    (
                        "client_request",
                        "account/login/start",
                        "amazon_bedrock",
                    )
                }
            ),
        )
        self.assertFalse(observed.healthy)

        entry = comparison.entry(*key)
        changed_official = _FixtureProtocolMethodRecord(
            direction=key[0],
            method=key[1],
            schema_sha256=SHA_A,
        )
        changed = ProtocolInventoryComparison(
            entries=(
                replace(entry, official=changed_official),
            )
        )
        changed_coverage = self.coverage(changed)
        self.assertEqual(
            [record.disposition for record in changed_coverage],
            ["uncovered"],
        )
        self.assertFalse(
            self.evaluate(
                protocol=changed,
                method_coverage=changed_coverage,
            ).healthy
        )

    def test_item_ids_requires_exact_resume_rule_and_no_other_dependency(
        self,
    ) -> None:
        comparison = self.comparison(
            normalized=True,
            methods=frozenset(
                {("client_request", "thread/resume")}
            ),
        )
        coverage = self.coverage(comparison)
        features = self.feature_comparison("item_ids")
        exact = frozenset({"thread/resume.params.history"})

        self.assertTrue(
            self.evaluate(
                protocol=comparison,
                features=features,
                method_coverage=coverage,
                item_ids_observed_dependencies=exact,
            ).healthy
        )
        self.assertFalse(
            self.evaluate(
                protocol=comparison,
                features=features,
                method_coverage=coverage,
                item_ids_observed_dependencies=(
                    exact
                    | frozenset({"turn/start.params.items"})
                ),
            ).healthy
        )
        self.assertFalse(
            self.evaluate(
                protocol=comparison,
                features=features,
                item_ids_observed_dependencies=exact,
            ).healthy
        )

    def test_item_ids_accepts_exact_native_resume_compatibility_without_adapter_coverage(
        self,
    ) -> None:
        comparison = self.comparison(
            normalized=True,
            methods=frozenset(
                {("client_request", "thread/resume")}
            ),
        )
        resume = comparison.entry(
            "client_request",
            "thread/resume",
        )
        native_compatible = ProtocolInventoryComparison(
            entries=(
                replace(
                    resume,
                    compatible=True,
                    reason_codes=(),
                ),
            )
        )
        exact = frozenset({"thread/resume.params.history"})

        self.assertTrue(
            self.evaluate(
                protocol=native_compatible,
                features=self.feature_comparison("item_ids"),
                item_ids_observed_dependencies=exact,
            ).healthy
        )

    def test_item_ids_native_resume_compatibility_proof_is_exact_and_consistent(
        self,
    ) -> None:
        comparison = self.comparison(
            normalized=True,
            methods=frozenset(
                {("client_request", "thread/resume")}
            ),
        )
        resume = comparison.entry(
            "client_request",
            "thread/resume",
        )
        native_compatible = replace(
            resume,
            compatible=True,
            reason_codes=(),
        )
        exact = frozenset({"thread/resume.params.history"})

        def assert_item_ids_core_drift(
            protocol: ProtocolInventoryComparison,
            dependencies: frozenset[str] = exact,
        ) -> None:
            evaluation = self.evaluate(
                protocol=protocol,
                features=self.feature_comparison("item_ids"),
                item_ids_observed_dependencies=dependencies,
            )
            self.assertFalse(evaluation.healthy)
            self.assertTrue(
                any(
                    finding.category == "feature"
                    and finding.code == "parity.feature.core_drift"
                    and finding.message
                    == "item_ids does not match parity policy."
                    for finding in evaluation.findings
                )
            )

        for missing_side in ("official", "internal"):
            with self.subTest(missing_side=missing_side):
                missing = ProtocolInventoryComparison(
                    entries=(
                        replace(
                            native_compatible,
                            official=(
                                None
                                if missing_side == "official"
                                else resume.official
                            ),
                            internal=(
                                None
                                if missing_side == "internal"
                                else resume.internal
                            ),
                        ),
                    )
                )
                assert_item_ids_core_drift(missing)

        contradictory = ProtocolInventoryComparison(
            entries=(
                replace(
                    native_compatible,
                    reason_codes=(
                        "parity.protocol.required_incompatible",
                    ),
                ),
            )
        )
        assert_item_ids_core_drift(contradictory)

        self.assertIsNotNone(resume.official)
        self.assertIsNotNone(resume.internal)
        assert resume.official is not None
        assert resume.internal is not None
        for direction, method in (
            ("client_notification", "thread/resume"),
            ("client_request", "thread/not-resume"),
        ):
            with self.subTest(direction=direction, method=method):
                wrong_method = ProtocolInventoryComparison(
                    entries=(
                        replace(
                            native_compatible,
                            direction=direction,
                            method=method,
                            official=_FixtureProtocolMethodRecord(
                                direction=direction,
                                method=method,
                                schema_sha256=(
                                    resume.official.schema_sha256
                                ),
                            ),
                            internal=_FixtureProtocolMethodRecord(
                                direction=direction,
                                method=method,
                                schema_sha256=(
                                    resume.internal.schema_sha256
                                ),
                            ),
                        ),
                    )
                )
                assert_item_ids_core_drift(wrong_method)

        assert_item_ids_core_drift(comparison)
        assert_item_ids_core_drift(
            ProtocolInventoryComparison(
                entries=(native_compatible,)
            ),
            exact | frozenset({"turn/start.params.items"}),
        )
        assert_item_ids_core_drift(
            ProtocolInventoryComparison(entries=())
        )

    def test_multi_agent_v2_requires_final_typed_probe(self) -> None:
        protocol = ProtocolInventoryComparison(entries=())
        features = self.feature_comparison("multi_agent_v2")
        missing = self.evaluate(
            protocol=protocol,
            features=features,
            evaluation_stage="eligibility",
        )
        self.assertFalse(missing.probe_eligible)
        self.assertFalse(missing.healthy)

        eligibility = self.evaluate(
            protocol=protocol,
            features=features,
            evaluation_stage="eligibility",
            multi_agent_v2_evidence=self.v2_evidence(),
        )
        self.assertTrue(eligibility.probe_eligible)
        self.assertFalse(eligibility.healthy)
        self.assertEqual(
            eligibility.provisional_capabilities,
            ("multi_agent_v2",),
        )

        self.assertFalse(
            self.evaluate(
                protocol=protocol,
                features=features,
                multi_agent_v2_evidence=self.v2_evidence(),
            ).healthy
        )
        self.assertFalse(
            self.evaluate(
                protocol=protocol,
                features=features,
                multi_agent_v2_evidence=self.v2_evidence(
                    typed_probe_result="passed",
                ),
            ).healthy
        )
        final = self.evaluate(
            protocol=protocol,
            features=features,
            multi_agent_v2_evidence=self.v2_evidence(
                typed_probe_result="passed",
                fingerprints_revalidated=True,
            ),
        )
        self.assertTrue(final.healthy)
        self.assertEqual(final.provisional_capabilities, ())


class ParitySerializationTests(unittest.TestCase):
    def sensitive_values(self) -> tuple[str, ...]:
        return (
            "credential-value-parity-secret",
            "d" * 64,
            "Authorization: Bearer parity-bearer-secret",
            "https://example.test/path?api-key=parity-query-secret",
            '[model_providers.azure]\napi_key = "raw-config-secret"',
            "raw prompt: disclose the private parity instruction",
            "raw model output: private-parity-completion",
            "/var/folders/parity/private-probe-root/output.json",
            "unbounded-process-output-" + ("x" * 128 * 1024),
        )

    def assert_sensitive_values_absent(self, payload: bytes) -> None:
        for value in self.sensitive_values():
            with self.subTest(value=value[:64]):
                self.assertNotIn(value.encode(), payload)

    def test_inventory_payloads_exclude_external_context(self) -> None:
        sensitive_blob = "\n".join(self.sensitive_values())
        feature_output = "multi_agent_v2  stable  true\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            feature_inventory = collect_feature_inventory(
                side="internal",
                cli_path=root / "bin" / "codex",
                isolated_home=root / "private-probe-root" / "isolated",
                effective_home=root / "private-probe-root" / "effective",
                runner=RecordingFeatureRunner(
                    [
                        feature_result(
                            feature_output,
                            stderr=sensitive_blob,
                        ),
                        feature_result(
                            feature_output,
                            stderr=sensitive_blob,
                        ),
                    ]
                ),
            )

        documents = protocol_documents()
        for document in documents.values():
            document["$comment"] = sensitive_blob
            document["description"] = sensitive_blob
            document["examples"] = [sensitive_blob]
            document["title"] = sensitive_blob
            variants = document["oneOf"]
            assert isinstance(variants, list)
            variant = variants[0]
            assert isinstance(variant, dict)
            variant["description"] = sensitive_blob
            variant["examples"] = [sensitive_blob]
        protocol_inventory = collect_protocol_inventory(
            side="official",
            schema_documents=documents,
        )

        self.assert_sensitive_values_absent(
            feature_inventory.canonical_bytes
        )
        self.assert_sensitive_values_absent(
            protocol_inventory.canonical_bytes
        )
        self.assertLess(len(feature_inventory.canonical_bytes), 4096)
        self.assertLess(len(protocol_inventory.canonical_bytes), 64 * 1024)

    def test_policy_receipt_payload_excludes_free_text_and_is_bounded(
        self,
    ) -> None:
        sensitive_blob = "\n".join(self.sensitive_values())
        evaluation = ParityPolicyEvaluation(
            healthy=False,
            policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
            findings=(
                ParityFinding(
                    category="feature",
                    code="parity.feature.unclassified_drift",
                    severity="error",
                    message=sensitive_blob,
                    expected=sensitive_blob,
                    observed=sensitive_blob,
                ),
            ),
            synchronization_queue=(
                ParityQueueItem(
                    category="feature",
                    identifier="skill_search",
                    finding_code="parity.feature.optional_missing",
                ),
            ),
        )
        self.assertTrue(
            hasattr(evaluation, "canonical_bytes"),
            "Parity policy receipt-facing serialization seam is missing",
        )

        payload = evaluation.canonical_bytes

        self.assert_sensitive_values_absent(payload)
        self.assertNotIn(b'"message"', payload)
        self.assertNotIn(b'"expected"', payload)
        self.assertNotIn(b'"observed"', payload)
        self.assertLess(len(payload), 1024)

    def test_policy_receipt_payload_is_deterministic_and_rejects_unsafe_identity(
        self,
    ) -> None:
        first = ParityPolicyEvaluation(
            healthy=True,
            policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
            findings=(
                ParityFinding(
                    category="feature",
                    code="parity.feature.optional_missing",
                    severity="warning",
                    message="first free text",
                ),
            ),
            synchronization_queue=(
                ParityQueueItem(
                    category="protocol",
                    identifier="client_request:app/read",
                    finding_code="parity.protocol.optional_missing",
                ),
                ParityQueueItem(
                    category="feature",
                    identifier="skill_search",
                    finding_code="parity.feature.optional_missing",
                ),
            ),
        )
        second = ParityPolicyEvaluation(
            healthy=True,
            policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
            findings=(
                ParityFinding(
                    category="feature",
                    code="parity.feature.optional_missing",
                    severity="warning",
                    message="different free text",
                ),
            ),
            synchronization_queue=tuple(
                reversed(first.synchronization_queue)
            ),
        )
        self.assertTrue(
            hasattr(first, "canonical_bytes"),
            "Parity policy receipt-facing serialization seam is missing",
        )

        self.assertEqual(first.canonical_bytes, second.canonical_bytes)

        unsafe = ParityPolicyEvaluation(
            healthy=True,
            policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
            synchronization_queue=(
                ParityQueueItem(
                    category="protocol",
                    identifier="/var/folders/private-probe/output.json",
                    finding_code="parity.protocol.optional_missing",
                ),
            ),
        )
        with self.assertRaises(ParityValidationError) as raised:
            _ = unsafe.canonical_bytes
        self.assertEqual(
            raised.exception.code,
            "parity.policy.serialization_invalid",
        )


class ParityReceiptTests(unittest.TestCase):
    RECEIPT_SEAMS = (
        "PARITY_RECEIPT_SCHEMA_VERSION",
        "ParityReceipt",
        "ParityReceiptArtifact",
        "current_parity_acceptance_trace",
        "resolve_parity_artifact_paths",
        "write_parity_receipt_artifact",
        "load_parity_receipt_artifact",
    )

    def receipt_seams(self) -> dict[str, object]:
        seams = {
            name: getattr(parity_module, name, None)
            for name in self.RECEIPT_SEAMS
        }
        missing = [
            name
            for name, value in seams.items()
            if value is None
        ]
        self.assertFalse(
            missing,
            "Parity receipt seams are missing: " + ", ".join(missing),
        )
        return seams

    def reference(self, root: Path) -> OfficialReference:
        bundle_root = root / "Applications" / "ChatGPT.app"
        return OfficialReference(
            authority="chatgpt-bundle",
            bundle_root=bundle_root,
            bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
            bundle_version="1.2026.196",
            bundled_cli=bundle_root / "Contents" / "Resources" / "codex",
            cli_version="0.146.0-alpha.3.1",
            binary_sha256=SHA_A,
            schema_sha256=SHA_B,
            feature_inventory_sha256=SHA_C,
        )

    def internal_fingerprint(self, root: Path) -> InternalFingerprint:
        return InternalFingerprint(
            backend_cli=root / "internal" / "codex",
            cli_version="0.144.6",
            binary_sha256=SHA_D,
            active_model="gpt-5.6-sol",
            provider_id="azure",
            wire_api="responses",
            endpoint_sha256=SHA_E,
            auth_source_kind="env",
            capability_receipt_sha256=SHA_F,
            source_catalog=root / "internal" / "models.json",
            source_catalog_sha256=SHA_A,
            config_sha256s=(
                ("profile", SHA_B),
                ("runtime", SHA_C),
            ),
        )

    def optional_policy(self) -> ParityPolicyEvaluation:
        return ParityPolicyEvaluation(
            healthy=True,
            policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
            findings=(
                ParityFinding(
                    category="feature",
                    code="parity.feature.optional_missing",
                    severity="warning",
                    message="Optional feature is not available.",
                ),
            ),
            synchronization_queue=(
                ParityQueueItem(
                    category="feature",
                    identifier="skill_search",
                    finding_code="parity.feature.optional_missing",
                ),
            ),
        )

    def receipt_fixture(
        self,
        root: Path,
    ) -> tuple[
        object,
        OfficialReference,
        InternalFingerprint,
        Path,
        object,
    ]:
        seams = self.receipt_seams()
        receipt_type = seams["ParityReceipt"]
        resolve_paths = seams["resolve_parity_artifact_paths"]
        self.assertTrue(callable(receipt_type))
        self.assertTrue(callable(resolve_paths))
        current_trace = seams["current_parity_acceptance_trace"]
        self.assertTrue(callable(current_trace))

        profile_dir = root / "profiles" / "internal"
        profile_dir.mkdir(parents=True, mode=0o700)
        paths = resolve_paths(profile_dir=profile_dir)
        reference = self.reference(root)
        internal = self.internal_fingerprint(root)
        receipt = receipt_type(
            schema_version=seams["PARITY_RECEIPT_SCHEMA_VERSION"],
            official_reference=reference,
            internal_fingerprint=internal,
            feature_inventory_sha256s=(
                ("official", SHA_C),
                ("internal", SHA_D),
            ),
            protocol_inventory_sha256s=(
                ("official", SHA_E),
                ("internal", SHA_F),
            ),
            adapter_rule_set_sha256=protocol_adapter_rule_set_digest(),
            acceptance_trace=current_trace(),
            overlay_path=paths.overlay_path,
            overlay_sha256=SHA_A,
            overlay_changes=(
                {
                    "op": "add",
                    "path": "/models/0/multi_agent_version",
                    "value": "v2",
                },
            ),
            probe_results=(
                ("core_protocol", "passed", SHA_B),
                ("typed_subagent_v2", "passed", SHA_C),
            ),
            policy_evaluation=self.optional_policy(),
        )
        return receipt, reference, internal, profile_dir, paths

    def write_fixture(
        self,
        root: Path,
    ) -> tuple[
        object,
        object,
        OfficialReference,
        InternalFingerprint,
        Path,
        object,
    ]:
        seams = self.receipt_seams()
        write_receipt = seams["write_parity_receipt_artifact"]
        self.assertTrue(callable(write_receipt))
        receipt, reference, internal, profile_dir, paths = (
            self.receipt_fixture(root)
        )
        artifact = write_receipt(
            receipt=receipt,
            profile_dir=profile_dir,
        )
        return artifact, receipt, reference, internal, profile_dir, paths

    def load_fixture(
        self,
        *,
        profile_dir: Path,
        payload_sha256: str,
        reference: OfficialReference,
        internal: InternalFingerprint,
        adapter_rule_set_sha256: str | None = None,
    ) -> object:
        seams = self.receipt_seams()
        load_receipt = seams["load_parity_receipt_artifact"]
        self.assertTrue(callable(load_receipt))
        return load_receipt(
            profile_dir=profile_dir,
            expected_payload_sha256=payload_sha256,
            expected_official_reference=reference,
            expected_internal_fingerprint=internal,
            expected_adapter_rule_set_sha256=(
                adapter_rule_set_sha256
                or protocol_adapter_rule_set_digest()
            ),
        )

    def assert_load_error(
        self,
        *,
        code: str,
        profile_dir: Path,
        payload_sha256: str,
        reference: OfficialReference,
        internal: InternalFingerprint,
    ) -> None:
        with self.assertRaises(ParityValidationError) as raised:
            self.load_fixture(
                profile_dir=profile_dir,
                payload_sha256=payload_sha256,
                reference=reference,
                internal=internal,
            )
        self.assertEqual(raised.exception.code, code)

    def test_receipt_payload_is_canonical_and_binds_complete_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt, reference, internal, _profile_dir, paths = (
                self.receipt_fixture(root)
            )
            policy_payload = json.loads(self.optional_policy().canonical_bytes)
            acceptance_trace = (
                parity_module.current_parity_acceptance_trace()
            )
            expected = {
                "acceptance_trace": dict(
                    acceptance_trace.canonical_payload()
                ),
                "adapter_rule_set_sha256": (
                    protocol_adapter_rule_set_digest()
                ),
                "evaluation_stage": "final",
                "feature_inventory_sha256s": [
                    {"sha256": SHA_D, "side": "internal"},
                    {"sha256": SHA_C, "side": "official"},
                ],
                "findings": policy_payload["findings"],
                "healthy": True,
                "internal_fingerprint": dict(
                    internal.canonical_payload()
                ),
                "internal_fingerprint_sha256": (
                    internal.fingerprint_sha256
                ),
                "method_coverage": [],
                "official_reference": dict(
                    reference.canonical_payload()
                ),
                "official_reference_sha256": (
                    reference.fingerprint_sha256
                ),
                "overlay": {
                    "changes": [
                        {
                            "op": "add",
                            "path": "/models/0/multi_agent_version",
                            "value": "v2",
                        }
                    ],
                    "path": str(paths.overlay_path),
                    "sha256": SHA_A,
                },
                "policy_version": PARITY_POLICY_VERSION,
                "probe_results": [
                    {
                        "evidence_sha256": SHA_B,
                        "name": "core_protocol",
                        "result_code": "passed",
                    },
                    {
                        "evidence_sha256": SHA_C,
                        "name": "typed_subagent_v2",
                        "result_code": "passed",
                    },
                ],
                "provisional_capabilities": [],
                "protocol_inventory_sha256s": [
                    {"sha256": SHA_F, "side": "internal"},
                    {"sha256": SHA_E, "side": "official"},
                ],
                "schema_version": 2,
                "synchronization_queue": policy_payload[
                    "synchronization_queue"
                ],
            }
            expected_bytes = json.dumps(
                expected,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

            self.assertEqual(receipt.canonical_bytes, expected_bytes)
            self.assertEqual(
                receipt.payload_sha256,
                hashlib.sha256(expected_bytes).hexdigest(),
            )
            self.assertEqual(
                receipt.policy_version,
                ParityPolicyVersion(PARITY_POLICY_VERSION),
            )
            self.assertEqual(
                receipt.acceptance_trace,
                acceptance_trace,
            )

    def test_profile_local_paths_modes_and_manifest_metadata_are_exact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, _receipt, reference, internal, _profile_dir, paths = (
                self.write_fixture(root)
            )

            self.assertEqual(
                paths.directory,
                root / "profiles" / "internal" / "parity",
            )
            self.assertEqual(paths.receipt_path, paths.directory / "receipt.json")
            self.assertEqual(
                paths.overlay_path,
                paths.directory / "model-catalog.json",
            )
            self.assertEqual(paths.directory.stat().st_mode & 0o777, 0o700)
            self.assertTrue(paths.receipt_path.is_file())
            self.assertFalse(paths.receipt_path.is_symlink())
            self.assertEqual(
                paths.receipt_path.stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                artifact.manifest_metadata,
                {
                    "parity_internal_fingerprint_sha256": (
                        internal.fingerprint_sha256
                    ),
                    "parity_official_reference_sha256": (
                        reference.fingerprint_sha256
                    ),
                    "parity_policy_version": PARITY_POLICY_VERSION,
                    "parity_receipt_path": str(paths.receipt_path),
                    "parity_receipt_schema_version": 2,
                    "parity_receipt_sha256": artifact.payload_sha256,
                },
            )

    def test_optional_queue_round_trip_remains_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, receipt, reference, internal, profile_dir, _paths = (
                self.write_fixture(root)
            )

            loaded = self.load_fixture(
                profile_dir=profile_dir,
                payload_sha256=artifact.payload_sha256,
                reference=reference,
                internal=internal,
            )

            self.assertEqual(loaded.payload, receipt.canonical_bytes)
            self.assertEqual(
                loaded.receipt.canonical_bytes,
                receipt.canonical_bytes,
            )
            self.assertTrue(loaded.receipt.healthy)
            self.assertEqual(
                [
                    (
                        item.category,
                        item.identifier,
                        item.finding_code,
                    )
                    for item in loaded.receipt.synchronization_queue
                ],
                [
                    (
                        "feature",
                        "skill_search",
                        "parity.feature.optional_missing",
                    )
                ],
            )

    def test_receipt_v2_round_trip_binds_sorted_method_coverage(
        self,
    ) -> None:
        coverage_type = getattr(
            parity_module,
            "MethodCoverageRecord",
            None,
        )
        self.assertTrue(
            callable(coverage_type),
            "Receipt-v2 method coverage record is missing",
        )
        coverage = (
            coverage_type(
                direction="client_request",
                method="turn/start",
                official_schema_sha256=SHA_E,
                internal_schema_sha256=SHA_F,
                reason_codes=(
                    "parity.protocol.enum_incompatible",
                    "parity.protocol.items_incompatible",
                ),
                disposition="optional_extension",
                optional_extension_ids=("local_audio_and_audio",),
            ),
            coverage_type(
                direction="client_request",
                method="account/login/start",
                official_schema_sha256=SHA_C,
                internal_schema_sha256=SHA_D,
                reason_codes=(
                    "parity.protocol.enum_incompatible",
                ),
                disposition="optional_extension",
                optional_extension_ids=("amazon_bedrock",),
            ),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt, reference, internal, profile_dir, _paths = (
                self.receipt_fixture(root)
            )
            receipt = replace(
                receipt,
                policy_evaluation=replace(
                    receipt.policy_evaluation,
                    method_coverage=coverage,
                ),
            )
            artifact = parity_module.write_parity_receipt_artifact(
                receipt=receipt,
                profile_dir=profile_dir,
            )
            loaded = self.load_fixture(
                profile_dir=profile_dir,
                payload_sha256=artifact.payload_sha256,
                reference=reference,
                internal=internal,
            )

            self.assertEqual(
                [
                    (record.direction, record.method)
                    for record in loaded.receipt.method_coverage
                ],
                [
                    ("client_request", "account/login/start"),
                    ("client_request", "turn/start"),
                ],
            )
            payload = json.loads(loaded.payload)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["evaluation_stage"], "final")
            self.assertEqual(payload["provisional_capabilities"], [])
            self.assertEqual(
                [
                    entry["method"]
                    for entry in payload["method_coverage"]
                ],
                ["account/login/start", "turn/start"],
            )
            invalid_probe_results = (
                (),
                (("core_protocol", "passed", SHA_B),),
                (
                    ("core_protocol", "passed", SHA_B),
                    ("typed_subagent", "passed", SHA_C),
                ),
                (
                    ("core_protocol", "passed", SHA_B),
                    ("typed_subagent_v2", "failed", SHA_C),
                ),
            )
            for probe_results in invalid_probe_results:
                with self.subTest(probe_results=probe_results):
                    with self.assertRaises(
                        ParityValidationError
                    ) as raised:
                        replace(
                            receipt,
                            probe_results=probe_results,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "parity.receipt.invalid",
                    )
            uncovered = coverage_type(
                direction="client_request",
                method="thread/start",
                official_schema_sha256=SHA_A,
                internal_schema_sha256=SHA_B,
                reason_codes=(
                    "parity.protocol.enum_incompatible",
                ),
                disposition="uncovered",
            )
            with self.assertRaises(
                ParityValidationError
            ) as raised:
                replace(
                    receipt.policy_evaluation,
                    method_coverage=(uncovered,),
                )
            self.assertEqual(
                raised.exception.code,
                "parity.policy.result_invalid",
            )
            changed_trace = replace(
                receipt.acceptance_trace,
                observed_protocol_extensions=(
                    (
                        "client_request",
                        "turn/start",
                        "local_audio_and_audio",
                    ),
                ),
            )
            with self.assertRaises(
                ParityValidationError
            ) as raised:
                replace(
                    receipt,
                    acceptance_trace=changed_trace,
                )
            self.assertEqual(
                raised.exception.code,
                "parity.receipt.invalid",
            )

    def test_receipt_v1_cannot_imply_coverage(self) -> None:
        self.assertEqual(
            parity_module.PARITY_RECEIPT_SCHEMA_VERSION,
            2,
            "Parity receipt schema v2 is not implemented",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt, _reference, _internal, _profile_dir, _paths = (
                self.receipt_fixture(Path(temp_dir))
            )
            legacy = json.loads(receipt.canonical_bytes)
            legacy["schema_version"] = 1
            legacy.pop("evaluation_stage")
            legacy.pop("method_coverage")
            legacy.pop("provisional_capabilities")

            with self.assertRaises(ParityValidationError) as raised:
                parity_module.ParityReceipt.from_payload(legacy)

            self.assertEqual(
                raised.exception.code,
                "parity.receipt.schema_unsupported",
            )

    def test_missing_receipt_fails_with_stable_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _receipt, reference, internal, profile_dir, _paths = (
                self.receipt_fixture(root)
            )

            self.assert_load_error(
                code="parity.receipt.missing",
                profile_dir=profile_dir,
                payload_sha256=SHA_A,
                reference=reference,
                internal=internal,
            )

    def test_symlink_non_regular_and_wrong_mode_receipts_are_rejected(
        self,
    ) -> None:
        cases = (
            ("symlink", "parity.receipt.unsafe"),
            ("directory", "parity.receipt.unsafe"),
            ("wrong-mode", "parity.receipt.mode_invalid"),
        )
        for case, code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                receipt, reference, internal, profile_dir, paths = (
                    self.receipt_fixture(root)
                )
                paths.directory.mkdir(parents=True, mode=0o700)
                payload = receipt.canonical_bytes
                if case == "symlink":
                    target = root / "outside-receipt.json"
                    target.write_bytes(payload)
                    target.chmod(0o600)
                    paths.receipt_path.symlink_to(target)
                elif case == "directory":
                    paths.receipt_path.mkdir()
                else:
                    paths.receipt_path.write_bytes(payload)
                    paths.receipt_path.chmod(0o644)

                self.assert_load_error(
                    code=code,
                    profile_dir=profile_dir,
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    reference=reference,
                    internal=internal,
                )

    def test_oversized_malformed_and_unsupported_receipts_are_rejected(
        self,
    ) -> None:
        cases = (
            (
                "oversized",
                b"x" * (1024 * 1024),
                "parity.receipt.oversized",
            ),
            (
                "malformed",
                b"{not-json\n",
                "parity.receipt.malformed",
            ),
        )
        for case, payload, code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _receipt, reference, internal, profile_dir, paths = (
                    self.receipt_fixture(root)
                )
                paths.directory.mkdir(parents=True, mode=0o700)
                paths.receipt_path.write_bytes(payload)
                paths.receipt_path.chmod(0o600)

                self.assert_load_error(
                    code=code,
                    profile_dir=profile_dir,
                    payload_sha256=hashlib.sha256(payload).hexdigest(),
                    reference=reference,
                    internal=internal,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt, reference, internal, profile_dir, paths = (
                self.receipt_fixture(root)
            )
            unsupported = json.loads(receipt.canonical_bytes)
            unsupported["schema_version"] = 999
            payload = json.dumps(
                unsupported,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            paths.directory.mkdir(parents=True, mode=0o700)
            paths.receipt_path.write_bytes(payload)
            paths.receipt_path.chmod(0o600)

            self.assert_load_error(
                code="parity.receipt.schema_unsupported",
                profile_dir=profile_dir,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                reference=reference,
                internal=internal,
            )

    def test_payload_digest_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, _receipt, reference, internal, profile_dir, paths = (
                self.write_fixture(root)
            )
            paths.receipt_path.write_bytes(
                paths.receipt_path.read_bytes() + b" "
            )
            paths.receipt_path.chmod(0o600)

            self.assert_load_error(
                code="parity.receipt.digest_mismatch",
                profile_dir=profile_dir,
                payload_sha256=artifact.payload_sha256,
                reference=reference,
                internal=internal,
            )

    def test_official_provider_and_runtime_drift_make_receipt_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, _receipt, reference, internal, profile_dir, _paths = (
                self.write_fixture(root)
            )
            cases = (
                (
                    "official-reference",
                    replace(reference, bundle_version="1.2026.197"),
                    internal,
                ),
                (
                    "provider",
                    reference,
                    replace(internal, provider_id="azure-next"),
                ),
                (
                    "runtime",
                    reference,
                    replace(internal, binary_sha256=SHA_E),
                ),
            )
            for case, expected_reference, expected_internal in cases:
                with self.subTest(case=case):
                    with self.assertRaises(
                        ParityValidationError
                    ) as raised:
                        self.load_fixture(
                            profile_dir=profile_dir,
                            payload_sha256=artifact.payload_sha256,
                            reference=expected_reference,
                            internal=expected_internal,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "parity.receipt.stale",
                    )


class ParityOverlayTests(unittest.TestCase):
    OVERLAY_SEAMS = (
        "ParityOverlayArtifact",
        "prepare_parity_overlay",
        "validate_parity_overlay",
    )
    ACTIVE_MODEL = "gpt-5.6-sol"

    def overlay_seams(self) -> dict[str, object]:
        seams = {
            name: getattr(parity_module, name, None)
            for name in self.OVERLAY_SEAMS
        }
        missing = [
            name
            for name, value in seams.items()
            if value is None
        ]
        self.assertFalse(
            missing,
            "Parity overlay seams are missing: " + ", ".join(missing),
        )
        return seams

    def catalog(self) -> dict[str, object]:
        return {
            "catalog_version": 7,
            "metadata": {
                "owner": "azure",
                "flags": [True, None, 3],
            },
            "models": [
                {
                    "slug": "gpt-5.5-sol",
                    "display_name": "GPT-5.5",
                    "provider": "azure",
                    "visibility": "hidden",
                },
                {
                    "slug": self.ACTIVE_MODEL,
                    "display_name": "GPT-5.6 SOL",
                    "provider": {
                        "id": "azure",
                        "deployment": "gpt-5.6-sol",
                    },
                    "wire_api": "responses",
                    "reasoning": {
                        "default": "high",
                        "supported": ["low", "medium", "high"],
                    },
                    "base_instructions": "Preserve provider instructions.",
                    "developer_instructions": "Preserve developer instructions.",
                    "input_modalities": ["text", "image"],
                    "visibility": "list",
                    "supported_in_api": True,
                    "nested": {
                        "objects": [{"key": "value"}],
                        "numbers": [1, 2, 3],
                    },
                },
                {
                    "slug": "gpt-5.7-sol",
                    "display_name": "GPT-5.7",
                    "provider": "azure",
                    "visibility": "list",
                },
            ],
        }

    def write_catalog(
        self,
        path: Path,
        document: object,
        *,
        mode: int = 0o640,
    ) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                document,
                ensure_ascii=True,
                indent=2,
                sort_keys=False,
            ).encode("utf-8")
            + b"\n"
        )
        path.write_bytes(payload)
        path.chmod(mode)
        return payload

    def prepare(
        self,
        *,
        source_catalog: Path,
        expected_source_sha256: str,
        _source_observer: object | None = None,
    ) -> object:
        prepare_overlay = self.overlay_seams()["prepare_parity_overlay"]
        self.assertTrue(callable(prepare_overlay))
        return prepare_overlay(
            source_catalog=source_catalog,
            expected_source_sha256=expected_source_sha256,
            active_model_slug=self.ACTIVE_MODEL,
            _source_observer=_source_observer,
        )

    def validate(
        self,
        *,
        source_document: object,
        overlay_document: object,
    ) -> object:
        validate_overlay = self.overlay_seams()["validate_parity_overlay"]
        self.assertTrue(callable(validate_overlay))
        return validate_overlay(
            source_document=source_document,
            overlay_document=overlay_document,
            active_model_slug=self.ACTIVE_MODEL,
        )

    def test_unique_active_model_missing_field_adds_only_v2_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "models.json"
            source = self.catalog()
            source_payload = self.write_catalog(source_path, source)
            expected_source_sha256 = hashlib.sha256(
                source_payload
            ).hexdigest()

            artifact = self.prepare(
                source_catalog=source_path,
                expected_source_sha256=expected_source_sha256,
            )

            overlay_type = self.overlay_seams()["ParityOverlayArtifact"]
            self.assertIsInstance(artifact, overlay_type)
            expected_document = copy.deepcopy(source)
            expected_document["models"][1][
                "multi_agent_version"
            ] = "v2"
            expected_payload = json.dumps(
                expected_document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            self.assertEqual(artifact.source_catalog, source_path)
            self.assertEqual(
                artifact.source_catalog_sha256,
                expected_source_sha256,
            )
            self.assertEqual(artifact.source_mode, 0o640)
            self.assertEqual(artifact.active_model_slug, self.ACTIVE_MODEL)
            self.assertEqual(artifact.active_model_index, 1)
            self.assertEqual(artifact.overlay_payload, expected_payload)
            self.assertEqual(
                artifact.overlay_sha256,
                hashlib.sha256(expected_payload).hexdigest(),
            )
            self.assertEqual(
                artifact.changes,
                (
                    {
                        "op": "add",
                        "path": "/models/1/multi_agent_version",
                        "value": "v2",
                    },
                ),
            )
            noncanonical = artifact.overlay_payload + b"\n"
            with self.assertRaises(ParityValidationError) as payload_error:
                replace(
                    artifact,
                    overlay_payload=noncanonical,
                    overlay_sha256=hashlib.sha256(
                        noncanonical
                    ).hexdigest(),
                )
            self.assertEqual(
                payload_error.exception.code,
                "parity.overlay.artifact_invalid",
            )
            with self.assertRaises(ParityValidationError) as changes_error:
                replace(
                    artifact,
                    changes=(
                        {
                            "op": "replace",
                            "path": "/models/1/multi_agent_version",
                            "value": "v2",
                        },
                    ),
                )
            self.assertEqual(
                changes_error.exception.code,
                "parity.overlay.artifact_invalid",
            )

    def test_existing_v2_has_zero_semantic_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "models.json"
            source = self.catalog()
            source["models"][1]["multi_agent_version"] = "v2"
            source_payload = self.write_catalog(source_path, source)

            artifact = self.prepare(
                source_catalog=source_path,
                expected_source_sha256=hashlib.sha256(
                    source_payload
                ).hexdigest(),
            )

            self.assertEqual(artifact.changes, ())
            self.assertEqual(
                json.loads(artifact.overlay_payload),
                source,
            )
            self.assertEqual(
                artifact.overlay_payload,
                json.dumps(
                    source,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8"),
            )

    def test_deep_structure_and_source_bytes_and_mode_are_preserved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "models.json"
            source = self.catalog()
            source_payload = self.write_catalog(
                source_path,
                source,
                mode=0o600,
            )

            artifact = self.prepare(
                source_catalog=source_path,
                expected_source_sha256=hashlib.sha256(
                    source_payload
                ).hexdigest(),
            )

            self.assertEqual(source_path.read_bytes(), source_payload)
            self.assertEqual(source_path.stat().st_mode & 0o777, 0o600)
            overlay_document = json.loads(artifact.overlay_payload)
            removed = overlay_document["models"][1].pop(
                "multi_agent_version"
            )
            self.assertEqual(removed, "v2")
            self.assertEqual(overlay_document, source)
            self.assertNotIn(
                "multi_agent_version",
                source["models"][1],
            )

    def test_missing_and_duplicate_active_model_slugs_are_rejected(
        self,
    ) -> None:
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            (
                "missing",
                {
                    **self.catalog(),
                    "models": [
                        {
                            "slug": "other-model",
                            "provider": "azure",
                        }
                    ],
                },
                "parity.overlay.active_model_missing",
            ),
            (
                "duplicate",
                {
                    **self.catalog(),
                    "models": [
                        *self.catalog()["models"],
                        {
                            "slug": self.ACTIVE_MODEL,
                            "provider": "azure",
                        },
                    ],
                },
                "parity.overlay.active_model_ambiguous",
            ),
        )
        for case, document, code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                source_path = Path(temp_dir) / "models.json"
                payload = self.write_catalog(source_path, document)
                with self.assertRaises(ParityValidationError) as raised:
                    self.prepare(
                        source_catalog=source_path,
                        expected_source_sha256=hashlib.sha256(
                            payload
                        ).hexdigest(),
                    )
                self.assertEqual(raised.exception.code, code)

    def test_unsupported_roots_and_unsafe_sources_are_rejected(self) -> None:
        invalid_documents: tuple[object, ...] = (
            [],
            {},
            {"models": "not-an-array"},
            {"models": [42]},
            {"models": [{"slug": 42}]},
            {
                "models": [
                    {
                        "slug": self.ACTIVE_MODEL,
                        "multi_agent_version": "v1",
                    }
                ]
            },
            {
                "models": [
                    {
                        "slug": self.ACTIVE_MODEL,
                        "multi_agent_version": None,
                    }
                ]
            },
        )
        for document in invalid_documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as temp_dir:
                source_path = Path(temp_dir) / "models.json"
                payload = self.write_catalog(source_path, document)
                with self.assertRaises(ParityValidationError) as raised:
                    self.prepare(
                        source_catalog=source_path,
                        expected_source_sha256=hashlib.sha256(
                            payload
                        ).hexdigest(),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "parity.overlay.source_invalid",
                )

        for source_kind in ("symlink", "directory"):
            with self.subTest(source_kind=source_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source_path = root / "models.json"
                if source_kind == "symlink":
                    target = root / "target.json"
                    payload = self.write_catalog(target, self.catalog())
                    source_path.symlink_to(target)
                    expected_digest = hashlib.sha256(payload).hexdigest()
                else:
                    source_path.mkdir()
                    expected_digest = SHA_A
                with self.assertRaises(ParityValidationError) as raised:
                    self.prepare(
                        source_catalog=source_path,
                        expected_source_sha256=expected_digest,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "parity.overlay.source_unsafe",
                )

    def test_tool_mode_addition_or_change_is_rejected(self) -> None:
        cases = ("add", "replace")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                source_path = Path(temp_dir) / "models.json"
                source = self.catalog()
                if case == "replace":
                    source["models"][1]["tool_mode"] = "provider-native"
                payload = self.write_catalog(source_path, source)
                artifact = self.prepare(
                    source_catalog=source_path,
                    expected_source_sha256=hashlib.sha256(
                        payload
                    ).hexdigest(),
                )
                candidate = json.loads(artifact.overlay_payload)
                candidate["models"][1]["tool_mode"] = "code_mode"

                with self.assertRaises(ParityValidationError) as raised:
                    self.validate(
                        source_document=source,
                        overlay_document=candidate,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "parity.overlay.mutation_forbidden",
                )

    def test_every_broader_overlay_mutation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "models.json"
            source = self.catalog()
            payload = self.write_catalog(source_path, source)
            artifact = self.prepare(
                source_catalog=source_path,
                expected_source_sha256=hashlib.sha256(payload).hexdigest(),
            )
            approved = json.loads(artifact.overlay_payload)
            cases = (
                ("provider", ("models", 1, "provider"), "other-provider"),
                ("wire-api", ("models", 1, "wire_api"), "chat"),
                (
                    "reasoning",
                    ("models", 1, "reasoning", "default"),
                    "low",
                ),
                (
                    "instructions",
                    ("models", 1, "base_instructions"),
                    "Changed.",
                ),
                (
                    "modalities",
                    ("models", 1, "input_modalities"),
                    ["text"],
                ),
                ("visibility", ("models", 1, "visibility"), "hidden"),
                (
                    "another-model",
                    ("models", 0, "visibility"),
                    "list",
                ),
                ("top-level", ("catalog_version",), 8),
            )
            for case, path, value in cases:
                with self.subTest(case=case):
                    candidate = copy.deepcopy(approved)
                    target = candidate
                    for segment in path[:-1]:
                        target = target[segment]
                    target[path[-1]] = value
                    with self.assertRaises(
                        ParityValidationError
                    ) as raised:
                        self.validate(
                            source_document=source,
                            overlay_document=candidate,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "parity.overlay.mutation_forbidden",
                    )

    def test_source_digest_and_identity_races_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "models.json"
            payload = self.write_catalog(source_path, self.catalog())
            original_digest = hashlib.sha256(payload).hexdigest()
            changed = payload.replace(
                b'"catalog_version": 7',
                b'"catalog_version": 8',
            )
            self.assertEqual(len(changed), len(payload))
            source_path.write_bytes(changed)
            source_path.chmod(0o640)

            with self.assertRaises(ParityValidationError) as raised:
                self.prepare(
                    source_catalog=source_path,
                    expected_source_sha256=original_digest,
                )
            self.assertEqual(
                raised.exception.code,
                "parity.overlay.source_stale",
            )

        for race_kind in ("replace", "rewrite"):
            with self.subTest(race_kind=race_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source_path = root / "models.json"
                payload = self.write_catalog(source_path, self.catalog())
                expected_digest = hashlib.sha256(payload).hexdigest()

                def observe(phase: str, path: Path) -> None:
                    if phase != "after_read":
                        return
                    if race_kind == "replace":
                        path.rename(root / "models.original.json")
                        path.write_bytes(payload)
                    else:
                        changed = payload.replace(
                            b'"catalog_version": 7',
                            b'"catalog_version": 8',
                        )
                        path.write_bytes(changed)
                    path.chmod(0o640)

                with self.assertRaises(ParityValidationError) as raised:
                    self.prepare(
                        source_catalog=source_path,
                        expected_source_sha256=expected_digest,
                        _source_observer=observe,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "parity.overlay.source_stale",
                )


class ParityBundleManifestTests(unittest.TestCase):
    BUNDLE_SEAMS = (
        "ParityBundle",
        "prepare_parity_bundle_artifacts",
    )
    REQUIRED_MANIFEST_KEYS = frozenset(
        {
            "parity_adapter_rule_set_sha256",
            "parity_capability_receipt_sha256",
            "parity_internal_fingerprint_sha256",
            "parity_official_reference_sha256",
            "parity_overlay_path",
            "parity_overlay_sha256",
            "parity_policy_version",
            "parity_receipt_path",
            "parity_receipt_schema_version",
            "parity_receipt_sha256",
            "parity_source_catalog_path",
            "parity_source_catalog_sha256",
        }
    )
    ACTIVE_MODEL = "gpt-5.6-sol"

    def bundle_seams(self) -> dict[str, object]:
        seams = {
            name: getattr(parity_module, name, None)
            for name in self.BUNDLE_SEAMS
        }
        missing = [
            name
            for name, value in seams.items()
            if value is None
        ]
        self.assertFalse(
            missing,
            "Parity bundle seams are missing: " + ", ".join(missing),
        )
        return seams

    def source_catalog(self) -> dict[str, object]:
        return {
            "catalog_version": 7,
            "models": [
                {
                    "slug": self.ACTIVE_MODEL,
                    "display_name": "GPT-5.6 SOL",
                    "provider": {
                        "id": "azure",
                        "deployment": "gpt-5.6-sol",
                    },
                    "wire_api": "responses",
                    "visibility": "list",
                }
            ],
        }

    def fixture(
        self,
        root: Path,
    ) -> tuple[object, object, bytes, int, object]:
        seams = self.bundle_seams()
        prepare_bundle = seams["prepare_parity_bundle_artifacts"]
        self.assertTrue(callable(prepare_bundle))

        profile_dir = root / "profiles" / "internal"
        profile_dir.mkdir(parents=True, mode=0o700)
        paths = parity_module.resolve_parity_artifact_paths(
            profile_dir=profile_dir
        )
        source_path = root / "configured" / "azure-models.json"
        source_path.parent.mkdir(parents=True)
        source_payload = (
            json.dumps(
                self.source_catalog(),
                ensure_ascii=True,
                indent=2,
                sort_keys=False,
            ).encode("utf-8")
            + b"\n"
        )
        source_path.write_bytes(source_payload)
        source_path.chmod(0o640)
        source_mode = source_path.stat().st_mode & 0o777
        source_sha256 = hashlib.sha256(source_payload).hexdigest()
        overlay = parity_module.prepare_parity_overlay(
            source_catalog=source_path,
            expected_source_sha256=source_sha256,
            active_model_slug=self.ACTIVE_MODEL,
        )
        reference = OfficialReference(
            authority="chatgpt-bundle",
            bundle_root=root / "Applications" / "ChatGPT.app",
            bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
            bundle_version="1.2026.196",
            bundled_cli=(
                root
                / "Applications"
                / "ChatGPT.app"
                / "Contents"
                / "Resources"
                / "codex"
            ),
            cli_version="0.146.0-alpha.3.1",
            binary_sha256=SHA_A,
            schema_sha256=SHA_B,
            feature_inventory_sha256=SHA_C,
        )
        internal = InternalFingerprint(
            backend_cli=root / "internal" / "codex",
            cli_version="0.144.6",
            binary_sha256=SHA_D,
            active_model=self.ACTIVE_MODEL,
            provider_id="azure",
            wire_api="responses",
            endpoint_sha256=SHA_E,
            auth_source_kind="env",
            capability_receipt_sha256=SHA_F,
            source_catalog=source_path,
            source_catalog_sha256=source_sha256,
            config_sha256s=(
                ("profile", SHA_B),
                ("runtime", SHA_C),
            ),
        )
        receipt = parity_module.ParityReceipt(
            schema_version=parity_module.PARITY_RECEIPT_SCHEMA_VERSION,
            official_reference=reference,
            internal_fingerprint=internal,
            feature_inventory_sha256s=(
                ("official", SHA_C),
                ("internal", SHA_D),
            ),
            protocol_inventory_sha256s=(
                ("official", SHA_E),
                ("internal", SHA_F),
            ),
            adapter_rule_set_sha256=protocol_adapter_rule_set_digest(),
            acceptance_trace=(
                parity_module.current_parity_acceptance_trace()
            ),
            overlay_path=paths.overlay_path,
            overlay_sha256=overlay.overlay_sha256,
            overlay_changes=overlay.changes,
            probe_results=(
                ("core_protocol", "passed", SHA_B),
                ("typed_subagent_v2", "passed", SHA_C),
            ),
            policy_evaluation=ParityPolicyEvaluation(
                healthy=True,
                policy_version=ParityPolicyVersion(
                    PARITY_POLICY_VERSION
                ),
            ),
        )
        work_root = root / "private-work"
        work_root.mkdir(mode=0o700)
        bundle = prepare_bundle(
            receipt=receipt,
            overlay=overlay,
            work_root=work_root,
        )
        return bundle, paths, source_payload, source_mode, work_root

    def test_manifest_candidate_binds_every_required_parity_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle, paths, _source_payload, _source_mode, _work_root = (
                self.fixture(root)
            )

            self.assertEqual(
                set(bundle.manifest_metadata),
                self.REQUIRED_MANIFEST_KEYS,
            )
            self.assertEqual(
                dict(bundle.manifest_metadata),
                {
                    "parity_adapter_rule_set_sha256": (
                        protocol_adapter_rule_set_digest()
                    ),
                    "parity_capability_receipt_sha256": SHA_F,
                    "parity_internal_fingerprint_sha256": (
                        bundle.receipt.internal_fingerprint.fingerprint_sha256
                    ),
                    "parity_official_reference_sha256": (
                        bundle.receipt.official_reference.fingerprint_sha256
                    ),
                    "parity_overlay_path": str(paths.overlay_path),
                    "parity_overlay_sha256": bundle.overlay.overlay_sha256,
                    "parity_policy_version": PARITY_POLICY_VERSION,
                    "parity_receipt_path": str(paths.receipt_path),
                    "parity_receipt_schema_version": (
                        parity_module.PARITY_RECEIPT_SCHEMA_VERSION
                    ),
                    "parity_receipt_sha256": (
                        hashlib.sha256(bundle.receipt_payload).hexdigest()
                    ),
                    "parity_source_catalog_path": str(
                        bundle.overlay.source_catalog
                    ),
                    "parity_source_catalog_sha256": (
                        bundle.overlay.source_catalog_sha256
                    ),
                },
            )

    def test_incomplete_or_mismatched_manifest_candidate_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle, _paths, _source_payload, _source_mode, _work_root = (
                self.fixture(Path(temp_dir))
            )
            bundle_type = self.bundle_seams()["ParityBundle"]
            self.assertIsInstance(bundle, bundle_type)

            for key in sorted(self.REQUIRED_MANIFEST_KEYS):
                with self.subTest(missing=key):
                    incomplete = dict(bundle.manifest_metadata)
                    incomplete.pop(key)
                    with self.assertRaises(
                        ParityValidationError
                    ) as raised:
                        replace(bundle, manifest_metadata=incomplete)
                    self.assertEqual(
                        raised.exception.code,
                        "parity.bundle.manifest_incomplete",
                    )

            for key, value in (
                ("parity_adapter_rule_set_sha256", SHA_A),
                ("parity_capability_receipt_sha256", SHA_A),
                ("parity_internal_fingerprint_sha256", SHA_A),
                ("parity_official_reference_sha256", SHA_A),
                (
                    "parity_overlay_path",
                    str(bundle.staging_root / "wrong-overlay.json"),
                ),
                ("parity_overlay_sha256", SHA_A),
                ("parity_policy_version", "999"),
                (
                    "parity_receipt_path",
                    str(bundle.staging_root / "wrong-receipt.json"),
                ),
                ("parity_receipt_schema_version", 999),
                ("parity_receipt_sha256", SHA_A),
                (
                    "parity_source_catalog_path",
                    str(bundle.staging_root / "wrong-source.json"),
                ),
                ("parity_source_catalog_sha256", SHA_A),
            ):
                with self.subTest(mismatched=key):
                    mismatched = dict(bundle.manifest_metadata)
                    mismatched[key] = value
                    with self.assertRaises(
                        ParityValidationError
                    ) as raised:
                        replace(bundle, manifest_metadata=mismatched)
                    self.assertEqual(
                        raised.exception.code,
                        "parity.bundle.manifest_mismatch",
                    )

            mismatched_receipts = (
                replace(bundle.receipt, overlay_sha256=SHA_A),
                replace(
                    bundle.receipt,
                    internal_fingerprint=replace(
                        bundle.receipt.internal_fingerprint,
                        active_model="gpt-5.7-sol",
                    ),
                ),
                replace(bundle.receipt, overlay_changes=()),
            )
            for mismatched_receipt in mismatched_receipts:
                with self.subTest(
                    receipt_mismatch=mismatched_receipt.payload_sha256
                ):
                    with self.assertRaises(
                        ParityValidationError
                    ) as raised:
                        replace(bundle, receipt=mismatched_receipt)
                    self.assertEqual(
                        raised.exception.code,
                        "parity.bundle.artifact_mismatch",
                    )

    def test_generated_artifacts_remain_in_private_staging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle, paths, source_payload, source_mode, work_root = (
                self.fixture(Path(temp_dir))
            )

            self.assertEqual(bundle.staging_root.parent, work_root)
            self.assertEqual(
                bundle.staging_root.stat().st_mode & 0o777,
                0o700,
            )
            self.assertEqual(
                sorted(path.name for path in bundle.staging_root.iterdir()),
                ["model-catalog.json", "receipt.json"],
            )
            for staged_path in (
                bundle.staged_receipt_path,
                bundle.staged_overlay_path,
            ):
                self.assertEqual(staged_path.parent, bundle.staging_root)
                self.assertTrue(staged_path.is_file())
                self.assertFalse(staged_path.is_symlink())
                self.assertEqual(staged_path.stat().st_mode & 0o777, 0o600)

            self.assertEqual(
                bundle.staged_receipt_path.read_bytes(),
                bundle.receipt_payload,
            )
            self.assertEqual(
                bundle.staged_overlay_path.read_bytes(),
                bundle.overlay.overlay_payload,
            )
            self.assertFalse(paths.receipt_path.exists())
            self.assertFalse(paths.overlay_path.exists())
            self.assertFalse(paths.directory.exists())
            self.assertEqual(
                bundle.overlay.source_catalog.read_bytes(),
                source_payload,
            )
            self.assertEqual(
                bundle.overlay.source_catalog.stat().st_mode & 0o777,
                source_mode,
            )

    def test_bundle_is_immutable_and_manifest_metadata_is_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle, _paths, _source_payload, _source_mode, _work_root = (
                self.fixture(Path(temp_dir))
            )

            with self.assertRaises(FrozenInstanceError):
                bundle.receipt_payload = b"changed"
            with self.assertRaises(TypeError):
                bundle.manifest_metadata[
                    "parity_policy_version"
                ] = "changed"


class ParityBundleTests(unittest.TestCase):
    def test_uncovered_drift_stops_before_probe_and_final_policy_precedes_receipt(
        self,
    ) -> None:
        build = getattr(parity_module, "build_method_coverage", None)
        self.assertTrue(
            callable(build),
            "Parity preparation method coverage is missing",
        )
        official = ProtocolMethodRecord(
            direction="client_request",
            method="thread/start",
            schema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["official"],
                    }
                },
            },
        )
        internal = ProtocolMethodRecord(
            direction="client_request",
            method="thread/start",
            schema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["internal"],
                    }
                },
            },
        )
        comparison = ProtocolInventoryComparison(
            entries=(
                ProtocolInventoryComparisonEntry(
                    direction="client_request",
                    method="thread/start",
                    official=official,
                    internal=internal,
                    producer_side="official",
                    consumer_side="internal",
                    compatible=False,
                    reason_codes=(
                        "parity.protocol.enum_incompatible",
                    ),
                ),
            )
        )
        coverage = build(comparison)
        self.assertEqual(
            [record.disposition for record in coverage],
            ["uncovered"],
        )
        eligibility = parity_module.evaluate_parity_policy(
            feature_comparison=FeatureInventoryComparison(entries=()),
            protocol_comparison=comparison,
            official_model_metadata={"multi_agent_version": "v2"},
            internal_model_metadata={"multi_agent_version": "v2"},
            method_coverage=coverage,
            evaluation_stage="eligibility",
        )
        self.assertFalse(eligibility.probe_eligible)
        self.assertFalse(eligibility.healthy)

        source = inspect.getsource(parity_module.prepare_parity_bundle)
        ordered_markers = (
            "acceptance_trace = current_parity_acceptance_trace()",
            "method_coverage = build_method_coverage(",
            "eligibility_evaluation = evaluate_parity_policy(",
            "probe_report = run_parity_probes(",
            "_revalidate_preparation_fingerprints(",
            "final_policy_evaluation = evaluate_parity_policy(",
            "receipt = ParityReceipt(",
        )
        positions = []
        for marker in ordered_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
            positions.append(source.index(marker))
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            source.count("acceptance_trace=acceptance_trace"),
            3,
        )
        receipt_source = source[source.index("receipt = ParityReceipt(") :]
        self.assertIn(
            "acceptance_trace=acceptance_trace",
            receipt_source,
        )


class ParityPreparationTests(unittest.TestCase):
    CORE_SUCCESS = (
        '{"id":"parity-probe-initialize","result":{"userAgent":"candidate"}}\n'
        '{"id":"parity-probe-collaboration","result":{"data":[]}}\n'
        '{"id":"parity-probe-thread","result":{"thread":{"id":"parent"}}}\n'
    )
    TYPED_SUCCESS = (
        '{"type":"thread.started","thread_id":"parent"}\n'
        '{"type":"item.completed","item":{"type":"subagent_spawn",'
        '"multi_agent_version":"v2","agentRole":"explorer",'
        '"source":"thread_spawn"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"agentRole":"explorer","text":"parity-subagent-ok"}}\n'
        '{"type":"turn.completed","result":"parity-parent-ok"}\n'
    )

    def setUp(self) -> None:
        if sys.version_info >= (3, 11):
            return
        python = shutil.which("python3.12") or shutil.which("python3.11")
        if python is None:
            self.fail("Python 3.11+ is required for parity preparation tests")

        class SubprocessTomllib:
            TOMLDecodeError = ValueError

            @staticmethod
            def loads(text: str) -> object:
                result = subprocess.run(
                    [
                        python,
                        "-c",
                        (
                            "import json, sys, tomllib; "
                            "print(json.dumps(tomllib.loads(sys.stdin.read())))"
                        ),
                    ],
                    input=text,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if result.returncode != 0:
                    raise ValueError(result.stderr.strip())
                return json.loads(result.stdout)

        for target in (
            "codex_switch_config_document.tomllib",
            "codex_switch_toml_validate.tomllib",
        ):
            patcher = mock.patch(target, SubprocessTomllib)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_prepare_parity_bundle_orchestrates_complete_staged_candidate(
        self,
    ) -> None:
        prepare = getattr(parity_module, "prepare_parity_bundle", None)
        timeouts_type = getattr(parity_module, "ParityTimeouts", None)
        self.assertTrue(callable(prepare), "Parity preparation seam is missing")
        self.assertTrue(
            callable(timeouts_type),
            "Parity preparation timeout contract is missing",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store_root = root / "store"
            profile_dir = store_root / "profiles" / "internal"
            profile_dir.mkdir(parents=True)
            official_home = root / "official-home"
            internal_home = root / "internal-home"
            official_home.mkdir()
            internal_home.mkdir()
            source_catalog = root / "configured" / "azure-models.json"
            source_catalog.parent.mkdir()
            source_payload = (
                b'{"models":[{"provider":"azure",'
                b'"slug":"provider-private-model"}]}\n'
            )
            source_catalog.write_bytes(source_payload)
            source_catalog.chmod(0o640)

            profile_config = profile_dir / "config.toml"
            profile_config.write_text(
                'model = "provider-private-model"\n'
                'model_provider = "azure"\n'
                f"model_catalog_json = {json.dumps(str(source_catalog))}\n"
                "\n"
                "[model_providers.azure]\n"
                'base_url = "https://internal.example/openai"\n'
                'wire_api = "responses"\n'
                'env_key = "AZURE_OPENAI_API_KEY"\n'
            )
            profile_config.chmod(0o600)
            shared_config = official_home / "config.toml"
            shared_config.write_text(
                "[agents]\n"
                "max_threads = 4\n"
                "\n"
                "[notice]\n"
                "keep = true\n"
            )
            shared_config.chmod(0o600)
            active_runtime_config = internal_home / "config.toml"
            active_runtime_config.write_text('profile = "old-runtime"\n')
            active_runtime_config.chmod(0o600)
            # An explicit custom catalog does not create an official cache.

            bundle_root = root / "Applications" / "ChatGPT.app"
            contents = bundle_root / "Contents"
            contents.mkdir(parents=True)
            with (contents / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": CURRENT_CHATGPT_BUNDLE_ID,
                        "CFBundleShortVersionString": "1.2026.196",
                    },
                    handle,
                )
            official_cli = write_executable(contents / "Resources" / "codex")
            official_main = write_executable(contents / "MacOS" / "ChatGPT")
            internal_cli = write_executable(root / "internal" / "codex")
            canonical_internal_cli = write_executable(
                root / "bound" / "codex"
            )
            canonical_internal_cli.write_text(
                "#!/bin/sh\nprintf 'old-bound\\n'\n"
            )
            internal_launcher = store_root / "bin" / "codex-internal-app"
            official_binding = RuntimeBinding(
                profile="openai-official",
                shell_cli=official_cli,
                desktop_cli=official_cli,
                backend_cli=official_cli,
                codex_home=official_home,
                desktop_host=ChatGPTDesktopHost(
                    kind="chatgpt",
                    bundle_root=bundle_root,
                    bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                    main_executable=official_main,
                    bundled_cli=official_cli,
                    healthy=True,
                    migration_only=False,
                ),
                requires_proxy=False,
            )
            internal_binding = RuntimeBinding(
                profile="internal",
                shell_cli=internal_cli,
                desktop_cli=internal_launcher,
                backend_cli=internal_cli,
                codex_home=internal_home,
                desktop_host=None,
                requires_proxy=True,
            )
            canonical_internal_binding = replace(
                internal_binding,
                shell_cli=canonical_internal_cli,
                backend_cli=canonical_internal_cli,
            )
            config_inputs = parity_module.ConfigInputs.capture(
                profile_config=profile_config,
                source_paths=(profile_config, shared_config),
            )
            schema_payload = json.dumps(
                {"schema_documents": protocol_documents()},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            schema_sha256 = hashlib.sha256(schema_payload).hexdigest()
            capability_receipt = CapabilityReceipt(
                backend_sha256=hashlib.sha256(
                    internal_cli.read_bytes()
                ).hexdigest(),
                schema_sha256=schema_sha256,
                capabilities=BackendCapabilities(True, True, True),
            )
            capability_payload = (
                json.dumps(
                    capability_receipt.to_dict(),
                    indent=2,
                    sort_keys=True,
                ).encode()
                + b"\n"
            )
            candidate = ParityCandidate(
                official_binding=official_binding,
                internal_binding=internal_binding,
                internal_manifest={
                    "name": "internal",
                    "codex_bin": str(canonical_internal_cli),
                    "app_cli_path": str(internal_launcher),
                },
                capability_receipt=CapabilityReceiptArtifact(
                    receipt=capability_receipt,
                    payload=capability_payload,
                    payload_sha256=hashlib.sha256(
                        capability_payload
                    ).hexdigest(),
                    reused=False,
                ),
                source_config=config_inputs,
                adapter_rule_set_sha256=protocol_adapter_rule_set_digest(),
                active_runtime_config_path=active_runtime_config,
                canonical_internal_binding=canonical_internal_binding,
            )
            feature_runner = RecordingFeatureRunner(
                [
                    feature_result("multi_agent_v2  stable  true\n"),
                    feature_result("multi_agent_v2  stable  true\n"),
                    feature_result("multi_agent_v2  stable  true\n"),
                    feature_result("multi_agent_v2  stable  true\n"),
                ]
            )

            def probe_runner(request: object) -> object:
                name = getattr(request, "name", "")
                return parity_module.ParityProbeCommandResult(
                    returncode=0,
                    stdout=(
                        self.CORE_SUCCESS
                        if name == "core_protocol"
                        else self.TYPED_SUCCESS
                    ),
                    stderr="",
                )

            work_root = root / "work"
            work_root.mkdir(mode=0o700)
            bundle = prepare(
                candidate,
                work_root=work_root,
                timeouts=timeouts_type(
                    command_seconds=1.0,
                    probe_seconds=1.0,
                ),
                _schema_loader=lambda _path, _timeout: schema_payload,
                _version_loader=lambda path, _timeout: (
                    "codex-cli 0.146.0-alpha.3.1"
                    if path == official_cli
                    else "codex-cli 0.144.6"
                ),
                _feature_runner=feature_runner,
                _probe_runner=probe_runner,
            )

            self.assertTrue(bundle.healthy)
            finding_code = "parity.model.custom_catalog_not_applicable"
            self.assertIn(finding_code, {f.code for f in bundle.findings})
            restored = parity_module.ParityReceipt.from_payload(
                json.loads(bundle.receipt.canonical_bytes)
            )
            self.assertEqual(restored.canonical_bytes, bundle.receipt.canonical_bytes)
            old_policy = json.loads(bundle.receipt.canonical_bytes)
            old_policy["policy_version"] = "1"
            with self.assertRaises(ParityValidationError) as raised:
                parity_module.ParityReceipt.from_payload(old_policy)
            self.assertEqual(raised.exception.code, "parity.receipt.policy_unsupported")

            def prepare_custom(probe=probe_runner, selected_candidate=candidate):
                return prepare(
                    selected_candidate,
                    work_root=work_root,
                    timeouts=timeouts_type(command_seconds=1.0, probe_seconds=1.0),
                    _schema_loader=lambda _path, _timeout: schema_payload,
                    _version_loader=lambda _path, _timeout: "codex-cli 0.144.6",
                    _feature_runner=lambda _request: feature_result(
                        "multi_agent_v2  stable  true\n"
                    ),
                    _probe_runner=probe,
                )

            cache = official_home / "models_cache.json"
            cache_payloads = (
                b"invalid-json",
                b'{"models":[{"slug":"different-official-model"}]}',
                b'{"models":[{"slug":"provider-private-model",'
                b'"multi_agent_version":"v1","tool_mode":"official-only"}]}',
            )
            for payload in cache_payloads:
                with self.subTest(unrelated_official_cache=payload):
                    cache.write_bytes(payload)
                    prepared = prepare_custom()
                    self.assertTrue(prepared.healthy)
                    self.assertEqual(cache.read_bytes(), payload)
                    cache.write_bytes(b"changed after preparation")
                    parity_module.revalidate_parity_bundle_inputs(prepared)
                    parity_module.revalidate_parity_bundle_immutable_inputs(prepared)
            cache.unlink()
            cache.symlink_to(official_home / "absent-cache-target")
            self.assertTrue(prepare_custom().healthy)
            self.assertTrue(cache.is_symlink())
            cache.unlink()

            bad_catalogs = (
                b"not-json", b"{}", b'{"models":[]}',
                b'{"models":[{"slug":"wrong-model"}]}',
                b'{"models":[{"slug":"provider-private-model"},'
                b'{"slug":"provider-private-model"}]}',
            )
            for payload in bad_catalogs:
                with self.subTest(invalid_custom_catalog=payload):
                    source_catalog.write_bytes(payload)
                    with self.assertRaises(ParityValidationError):
                        prepare_custom()
            source_catalog.write_bytes(source_payload)
            source_catalog.chmod(0o640)
            with self.assertRaises(ParityValidationError) as raised:
                prepare_custom(lambda _request: parity_module.ParityProbeCommandResult(
                    returncode=1, stdout="", stderr="fixture probe failed",
                ))
            self.assertEqual(raised.exception.code, "parity.preparation.probe_unhealthy")
            self.assertTrue(bundle.config_projection.healthy)
            self.assertEqual(
                bundle.active_runtime_config_path,
                active_runtime_config,
            )
            self.assertEqual(
                bundle.staged_runtime_config_path,
                bundle.staging_root / "config.toml",
            )
            staged = parity_module.ConfigDocument.parse(
                bundle.staged_runtime_config_payload.decode(),
                "staged parity runtime",
            )
            self.assertEqual(
                staged.data["model_catalog_json"],
                str(bundle.staged_overlay_path),
            )
            self.assertTrue(staged.data["features"]["multi_agent_v2"])
            self.assertNotIn("max_threads", staged.data.get("agents", {}))
            profile_projection = parity_module.ConfigDocument.parse(
                bundle.config_projection.payload_for(
                    profile_config
                ).decode(),
                "final parity profile",
            )
            self.assertEqual(
                profile_projection.data["model_catalog_json"],
                str(
                    profile_dir / "parity" / "model-catalog.json"
                ),
            )
            self.assertEqual(source_catalog.read_bytes(), source_payload)
            self.assertEqual(source_catalog.stat().st_mode & 0o777, 0o640)
            self.assertEqual(
                bundle.receipt.internal_fingerprint.backend_cli,
                canonical_internal_cli,
            )
            staging_before_stale_probe = set(work_root.iterdir())

            def mutating_probe_runner(request: object) -> object:
                if getattr(request, "name", "") == "typed_subagent_v2":
                    source_catalog.write_bytes(
                        b'{"models":[{"slug":"changed-during-probe"}]}\n'
                    )
                return probe_runner(request)

            with self.assertRaises(ParityValidationError) as raised:
                prepare(
                    candidate,
                    work_root=work_root,
                    timeouts=timeouts_type(
                        command_seconds=1.0,
                        probe_seconds=1.0,
                    ),
                    _schema_loader=lambda _path, _timeout: schema_payload,
                    _version_loader=lambda path, _timeout: (
                        "codex-cli 0.146.0-alpha.3.1"
                        if path == official_cli
                        else "codex-cli 0.144.6"
                    ),
                    _feature_runner=RecordingFeatureRunner(
                        [
                            feature_result(
                                "multi_agent_v2  stable  true\n"
                            ),
                            feature_result(
                                "multi_agent_v2  stable  true\n"
                            ),
                            feature_result(
                                "multi_agent_v2  stable  true\n"
                            ),
                            feature_result(
                                "multi_agent_v2  stable  true\n"
                            ),
                        ]
                    ),
                    _probe_runner=mutating_probe_runner,
                )
            self.assertEqual(
                raised.exception.code,
                "parity.preparation.candidate_stale",
            )
            self.assertEqual(
                set(work_root.iterdir()),
                staging_before_stale_probe,
            )
            source_catalog.write_bytes(source_payload)
            source_catalog.chmod(0o640)

            backup_cli = canonical_internal_cli.with_name("codex.backup")
            canonical_internal_cli.rename(backup_cli)
            internal_cli.rename(canonical_internal_cli)
            try:
                self.assertFalse(internal_cli.exists())
                parity_module.revalidate_parity_bundle_immutable_inputs(
                    bundle
                )
            finally:
                canonical_internal_cli.rename(internal_cli)
                backup_cli.rename(canonical_internal_cli)
            inactive_bundle = prepare(
                replace(
                    candidate,
                    active_runtime_config_path=None,
                ),
                work_root=work_root,
                timeouts=timeouts_type(
                    command_seconds=1.0,
                    probe_seconds=1.0,
                ),
                _schema_loader=lambda _path, _timeout: schema_payload,
                _version_loader=lambda path, _timeout: (
                    "codex-cli 0.146.0-alpha.3.1"
                    if path == official_cli
                    else "codex-cli 0.144.6"
                ),
                _feature_runner=RecordingFeatureRunner(
                    [
                        feature_result(
                            "multi_agent_v2  stable  true\n"
                        ),
                        feature_result(
                            "multi_agent_v2  stable  true\n"
                        ),
                        feature_result(
                            "multi_agent_v2  stable  true\n"
                        ),
                        feature_result(
                            "multi_agent_v2  stable  true\n"
                        ),
                    ]
                ),
                _probe_runner=probe_runner,
            )
            self.assertIsNone(
                inactive_bundle.active_runtime_config_path
            )
            self.assertIsNone(
                inactive_bundle.active_runtime_config_payload
            )
            self.assertEqual(
                b'profile = "old-runtime"\n',
                active_runtime_config.read_bytes(),
            )
            info_plist = contents / "Info.plist"
            original_info = {
                "CFBundleIdentifier": CURRENT_CHATGPT_BUNDLE_ID,
                "CFBundleShortVersionString": "1.2026.196",
            }
            for field, value in (
                ("CFBundleIdentifier", "com.example.foreign"),
                ("CFBundleShortVersionString", "1.2026.197"),
            ):
                with self.subTest(stale_official_field=field):
                    changed_info = dict(original_info)
                    changed_info[field] = value
                    with info_plist.open("wb") as handle:
                        plistlib.dump(changed_info, handle)
                    with self.assertRaises(ParityValidationError) as raised:
                        parity_module.revalidate_parity_bundle_inputs(bundle)
                    self.assertEqual(
                        raised.exception.code,
                        "parity.bundle.reference_stale",
                    )
                    with info_plist.open("wb") as handle:
                        plistlib.dump(original_info, handle)

            final_overlay = (
                profile_dir / "parity" / "model-catalog.json"
            )
            final_overlay.parent.mkdir(mode=0o700)
            final_overlay.write_bytes(bundle.overlay.overlay_payload)
            final_overlay.chmod(0o600)
            profile_config.write_bytes(
                bundle.config_projection.payload_for(profile_config)
            )
            profile_config.chmod(0o600)
            fresh_source_payload = (
                json.dumps(
                    {
                        "models": [
                            {
                                "provider": "azure",
                                "provider_revision": "fresh",
                                "slug": "provider-private-model",
                            }
                        ]
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                + b"\n"
            )
            source_catalog.write_bytes(fresh_source_payload)
            source_catalog.chmod(0o640)
            rebound_manifest = dict(candidate.internal_manifest)
            rebound_manifest.update(bundle.manifest_metadata)
            rebound_candidate = replace(
                candidate,
                internal_manifest=rebound_manifest,
                source_config=parity_module.ConfigInputs.capture(
                    profile_config=profile_config,
                    source_paths=(profile_config, shared_config),
                ),
            )
            rebound_bundle = prepare(
                rebound_candidate,
                work_root=work_root,
                timeouts=timeouts_type(
                    command_seconds=1.0,
                    probe_seconds=1.0,
                ),
                _schema_loader=lambda _path, _timeout: schema_payload,
                _version_loader=lambda path, _timeout: (
                    "codex-cli 0.146.0-alpha.3.1"
                    if path == official_cli
                    else "codex-cli 0.144.6"
                ),
                _feature_runner=RecordingFeatureRunner(
                    [
                        feature_result("multi_agent_v2  stable  true\n"),
                        feature_result("multi_agent_v2  stable  true\n"),
                        feature_result("multi_agent_v2  stable  true\n"),
                        feature_result("multi_agent_v2  stable  true\n"),
                    ]
                ),
                _probe_runner=probe_runner,
            )
            self.assertEqual(
                rebound_bundle.overlay.source_catalog,
                source_catalog,
            )
            rebound_overlay = json.loads(
                rebound_bundle.overlay.overlay_payload
            )
            self.assertEqual(
                rebound_overlay["models"][0]["provider_revision"],
                "fresh",
            )

            with self.assertRaises(ParityValidationError) as raised:
                prepare_custom(selected_candidate=replace(
                    rebound_candidate, internal_manifest={},
                ))
            self.assertEqual(raised.exception.code, "parity.preparation.config_invalid")

            original_profile_config = profile_config.with_name(
                "config.original.toml"
            )
            projected_profile_payload = profile_config.read_bytes()
            profile_config.rename(original_profile_config)
            profile_config.write_bytes(projected_profile_payload)
            profile_config.chmod(0o600)
            self.assertNotEqual(
                profile_config.stat().st_ino,
                original_profile_config.stat().st_ino,
            )
            with self.assertRaises(ParityValidationError) as raised:
                parity_module.revalidate_parity_bundle_inputs(
                    rebound_bundle
                )
            self.assertEqual(
                raised.exception.code,
                "parity.bundle.candidate_stale",
            )
            parity_module.revalidate_parity_bundle_immutable_inputs(
                rebound_bundle
            )
            profile_config.unlink()
            original_profile_config.rename(profile_config)

            original_source = source_catalog.with_name(
                "azure-models.original.json"
            )
            source_catalog.rename(original_source)
            source_catalog.write_bytes(fresh_source_payload)
            source_catalog.chmod(0o640)
            self.assertNotEqual(
                source_catalog.stat().st_ino,
                original_source.stat().st_ino,
            )
            with self.assertRaises(ParityValidationError) as raised:
                parity_module.revalidate_parity_bundle_inputs(
                    rebound_bundle
                )
            self.assertEqual(
                raised.exception.code,
                "parity.bundle.candidate_stale",
            )


class RecordingParityProbeRunner:
    def __init__(
        self,
        results: list[object],
        *,
        on_call: object | None = None,
    ) -> None:
        self.results = list(results)
        self.on_call = on_call
        self.requests: list[object] = []

    def __call__(self, request: object) -> object:
        self.requests.append(request)
        if callable(self.on_call):
            self.on_call(request, len(self.requests))
        if not self.results:
            raise AssertionError("unexpected parity probe request")
        return self.results.pop(0)


class ParityProbeTests(unittest.TestCase):
    RETAINED_FIXTURE = (
        Path(__file__).resolve().parents[1]
        / "testdata"
        / "parity"
        / "retained-v2-probe-redacted.json"
    )
    PROBE_SEAMS = (
        "ParityProbeInputs",
        "ParityProbeRequest",
        "ParityProbeCommandResult",
        "ParityProbeResult",
        "ParityProbeReport",
        "run_parity_probes",
    )
    CORE_SUCCESS = (
        '{"id":"parity-probe-initialize","result":{"userAgent":"candidate"}}\n'
        '{"id":"parity-probe-collaboration","result":{"data":[]}}\n'
        '{"id":"parity-probe-thread","result":{"thread":{"id":"parent"}}}\n'
    )
    TYPED_SUCCESS = (
        '{"type":"thread.started","thread_id":"parent"}\n'
        '{"type":"item.completed","item":{"type":"subagent_spawn",'
        '"multi_agent_version":"v2","agentRole":"explorer",'
        '"source":"thread_spawn"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"agentRole":"explorer","text":"parity-subagent-ok"}}\n'
        '{"type":"turn.completed","result":"parity-parent-ok"}\n'
    )

    def probe_seams(self) -> dict[str, object]:
        seams = {
            name: getattr(parity_module, name, None)
            for name in self.PROBE_SEAMS
        }
        missing = [
            name
            for name, value in seams.items()
            if value is None
        ]
        self.assertFalse(
            missing,
            "Parity probe seams are missing: " + ", ".join(missing),
        )
        return seams

    def write_probe_artifact(
        self,
        path: Path,
        payload: bytes,
        *,
        mode: int,
    ) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
        return hashlib.sha256(payload).hexdigest()

    def inputs(
        self,
        seams: dict[str, object],
        root: Path,
        *,
        backend_payload: bytes = b"#!/bin/sh\nexit 0\n",
    ) -> object:
        inputs_type = seams["ParityProbeInputs"]
        self.assertTrue(callable(inputs_type))
        candidate_root = root / "candidate"
        codex_home = candidate_root / "home"
        workspace = candidate_root / "workspace"
        codex_home.mkdir(parents=True, mode=0o700)
        workspace.mkdir(mode=0o700)
        backend_cli = candidate_root / "bin" / "codex"
        backend_sha256 = self.write_probe_artifact(
            backend_cli,
            backend_payload,
            mode=0o755,
        )
        overlay_path = codex_home / "parity" / "model-catalog.json"
        overlay_sha256 = self.write_probe_artifact(
            overlay_path,
            b'{"models":[{"slug":"provider-private-model",'
            b'"multi_agent_version":"v2"}]}\n',
            mode=0o600,
        )
        config_path = codex_home / "config.toml"
        config_sha256 = self.write_probe_artifact(
            config_path,
            (
                'model = "provider-private-model"\n'
                f"model_catalog_json = {json.dumps(str(overlay_path))}\n"
                "\n"
                "[features]\n"
                "multi_agent_v2 = true\n"
            ).encode(),
            mode=0o600,
        )
        capability_receipt_path = (
            codex_home / "parity" / "capability-receipt.json"
        )
        capability_receipt_sha256 = self.write_probe_artifact(
            capability_receipt_path,
            b'{"schema_version":1,"multi_agent_v2":true}\n',
            mode=0o600,
        )
        return inputs_type(
            backend_cli=backend_cli,
            backend_sha256=backend_sha256,
            codex_home=codex_home,
            workspace=workspace,
            config_path=config_path,
            config_sha256=config_sha256,
            overlay_path=overlay_path,
            overlay_sha256=overlay_sha256,
            capability_receipt_path=capability_receipt_path,
            capability_receipt_sha256=capability_receipt_sha256,
        )

    def command_result(
        self,
        seams: dict[str, object],
        *,
        stdout: str,
        stderr: str = "",
        returncode: int | None = 0,
        timed_out: bool = False,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        process_group_terminated: bool = False,
    ) -> object:
        result_type = seams["ParityProbeCommandResult"]
        self.assertTrue(callable(result_type))
        return result_type(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            process_group_terminated=process_group_terminated,
        )

    def run_success(
        self,
        seams: dict[str, object],
        inputs: object,
        *,
        core_stderr: str = "",
        typed_stderr: str = "",
    ) -> tuple[object, RecordingParityProbeRunner]:
        runner = RecordingParityProbeRunner(
            [
                self.command_result(
                    seams,
                    stdout=self.CORE_SUCCESS,
                    stderr=core_stderr,
                ),
                self.command_result(
                    seams,
                    stdout=self.TYPED_SUCCESS,
                    stderr=typed_stderr,
                ),
            ]
        )
        run_probes = seams["run_parity_probes"]
        self.assertTrue(callable(run_probes))
        report = run_probes(
            inputs=inputs,
            runner=runner,
            timeout_seconds=0.5,
            max_output_bytes=4096,
        )
        return report, runner

    def result_codes(self, report: object) -> list[tuple[str, str]]:
        return [
            (result.name, result.result_code)
            for result in report.results
        ]

    def test_candidate_artifacts_and_core_method_order_are_exact(
        self,
    ) -> None:
        seams = self.probe_seams()
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))
            report, runner = self.run_success(seams, inputs)

        self.assertTrue(report.healthy)
        self.assertEqual(
            ["core_protocol", "typed_subagent_v2"],
            [request.name for request in runner.requests],
        )
        for request in runner.requests:
            self.assertEqual(inputs.backend_cli, Path(request.command[0]))
            self.assertEqual(inputs.codex_home, request.codex_home)
        core_request = runner.requests[0]
        self.assertEqual(
            ("app-server", "--analytics-default-enabled"),
            core_request.command[1:],
        )
        self.assertEqual(
            [
                "initialize",
                "initialized",
                "collaborationMode/list",
                "thread/start",
            ],
            [
                message["method"]
                for message in core_request.stdin_messages
            ],
        )
        typed_request = runner.requests[1]
        self.assertEqual("exec", typed_request.command[1])
        self.assertIn("explorer", typed_request.command[-1])
        self.assertIn("parity-subagent-ok", typed_request.command[-1])

    def test_typed_explorer_v2_markers_are_required_for_success(self) -> None:
        seams = self.probe_seams()
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))
            report, _runner = self.run_success(seams, inputs)

        self.assertTrue(report.healthy)
        self.assertEqual(
            [
                ("core_protocol", "passed"),
                ("typed_subagent_v2", "passed"),
            ],
            self.result_codes(report),
        )
        self.assertEqual((), report.findings)
        self.assertEqual(
            tuple(
                (
                    result.name,
                    result.result_code,
                    result.evidence_sha256,
                )
                for result in report.results
            ),
            report.receipt_results,
        )

    def test_v1_or_nickname_only_subagent_never_passes(self) -> None:
        seams = self.probe_seams()
        outputs = (
            (
                '{"type":"item.completed","item":{"type":"subagent_spawn",'
                '"multi_agent_version":"v1","nickname":"random-name"}}\n'
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"parity-subagent-ok"}}\n'
                '{"type":"turn.completed","result":"parity-parent-ok"}\n'
            ),
            (
                '{"type":"item.completed","item":{"type":"subagent_spawn",'
                '"multi_agent_version":"v1","nickname":"random-name"}}\n'
                + self.TYPED_SUCCESS
            ),
        )
        for output in outputs:
            with self.subTest(output=output):
                with tempfile.TemporaryDirectory() as temp_dir:
                    inputs = self.inputs(seams, Path(temp_dir))
                    runner = RecordingParityProbeRunner(
                        [
                            self.command_result(
                                seams,
                                stdout=self.CORE_SUCCESS,
                            ),
                            self.command_result(seams, stdout=output),
                        ]
                    )
                    report = seams["run_parity_probes"](
                        inputs=inputs,
                        runner=runner,
                        timeout_seconds=0.5,
                        max_output_bytes=4096,
                    )

                self.assertFalse(report.healthy)
                self.assertEqual(
                    ("typed_subagent_v2", "v1_fallback"),
                    self.result_codes(report)[-1],
                )
                self.assertEqual(
                    ["parity.probe.v1_fallback"],
                    [finding.code for finding in report.findings],
                )

    def test_multiple_v2_subagent_spawns_never_pass(self) -> None:
        seams = self.probe_seams()
        repeated_spawn = (
            '{"type":"item.completed","item":{"type":"subagent_spawn",'
            '"multi_agent_version":"v2","agentRole":"explorer",'
            '"source":"thread_spawn"}}\n'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))
            runner = RecordingParityProbeRunner(
                [
                    self.command_result(seams, stdout=self.CORE_SUCCESS),
                    self.command_result(
                        seams,
                        stdout=repeated_spawn + self.TYPED_SUCCESS,
                    ),
                ]
            )
            report = seams["run_parity_probes"](
                inputs=inputs,
                runner=runner,
                timeout_seconds=0.5,
                max_output_bytes=4096,
            )

        self.assertFalse(report.healthy)
        self.assertEqual(
            ("typed_subagent_v2", "typed_subagent_missing"),
            self.result_codes(report)[-1],
        )

    def test_typed_subagent_completion_order_is_required(self) -> None:
        seams = self.probe_seams()
        spawn = (
            '{"type":"item.completed","item":{"type":"subagent_spawn",'
            '"multi_agent_version":"v2","agentRole":"explorer",'
            '"source":"thread_spawn"}}\n'
        )
        child = (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"agentRole":"explorer","text":"parity-subagent-ok"}}\n'
        )
        parent = (
            '{"type":"turn.completed","result":"parity-parent-ok"}\n'
        )
        for output in (child + spawn + parent, spawn + parent + child):
            with self.subTest(output=output):
                with tempfile.TemporaryDirectory() as temp_dir:
                    inputs = self.inputs(seams, Path(temp_dir))
                    runner = RecordingParityProbeRunner(
                        [
                            self.command_result(
                                seams,
                                stdout=self.CORE_SUCCESS,
                            ),
                            self.command_result(seams, stdout=output),
                        ]
                    )
                    report = seams["run_parity_probes"](
                        inputs=inputs,
                        runner=runner,
                        timeout_seconds=0.5,
                        max_output_bytes=4096,
                    )

                self.assertFalse(report.healthy)
                self.assertEqual(
                    ("typed_subagent_v2", "typed_subagent_missing"),
                    self.result_codes(report)[-1],
                )

    def test_timeout_stops_before_later_probe(self) -> None:
        seams = self.probe_seams()
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))
            runner = RecordingParityProbeRunner(
                [
                    self.command_result(
                        seams,
                        stdout="",
                        returncode=None,
                        timed_out=True,
                        process_group_terminated=True,
                    )
                ]
            )
            report = seams["run_parity_probes"](
                inputs=inputs,
                runner=runner,
                timeout_seconds=0.1,
                max_output_bytes=4096,
            )

        self.assertFalse(report.healthy)
        self.assertEqual(1, len(runner.requests))
        self.assertEqual(
            [("core_protocol", "timeout")],
            self.result_codes(report),
        )

    def test_malformed_or_missing_core_responses_fail_closed(self) -> None:
        seams = self.probe_seams()
        cases = (
            ("not-json\n", "malformed_output"),
            (
                '{"id":"parity-probe-initialize","result":{}}\n',
                "missing_response",
            ),
        )
        for stdout, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as temp_dir:
                    inputs = self.inputs(seams, Path(temp_dir))
                    runner = RecordingParityProbeRunner(
                        [self.command_result(seams, stdout=stdout)]
                    )
                    report = seams["run_parity_probes"](
                        inputs=inputs,
                        runner=runner,
                        timeout_seconds=0.5,
                        max_output_bytes=4096,
                    )
                self.assertFalse(report.healthy)
                self.assertEqual(
                    [("core_protocol", expected_code)],
                    self.result_codes(report),
                )

    def test_early_exit_is_distinct_from_missing_response(self) -> None:
        seams = self.probe_seams()
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))
            runner = RecordingParityProbeRunner(
                [
                    self.command_result(
                        seams,
                        stdout=(
                            '{"id":"parity-probe-initialize",'
                            '"result":{}}\n'
                        ),
                        returncode=17,
                    )
                ]
            )
            report = seams["run_parity_probes"](
                inputs=inputs,
                runner=runner,
                timeout_seconds=0.5,
                max_output_bytes=4096,
            )

        self.assertEqual(
            [("core_protocol", "early_exit")],
            self.result_codes(report),
        )

    def test_oversized_output_is_rejected(self) -> None:
        seams = self.probe_seams()
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))
            runner = RecordingParityProbeRunner(
                [
                    self.command_result(
                        seams,
                        stdout=self.CORE_SUCCESS,
                        stdout_truncated=True,
                    )
                ]
            )
            report = seams["run_parity_probes"](
                inputs=inputs,
                runner=runner,
                timeout_seconds=0.5,
                max_output_bytes=64,
            )

        self.assertEqual(
            [("core_protocol", "output_oversized")],
            self.result_codes(report),
        )

    def test_default_runner_terminates_the_complete_process_group(self) -> None:
        seams = self.probe_seams()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_path = root / "child.pid"
            backend_payload = (
                "#!/bin/sh\n"
                "trap '' TERM\n"
                "/bin/sh -c \"trap '' TERM; "
                "while :; do sleep 60; done\" &\n"
                "child=$!\n"
                f"printf '%s' \"$child\" > "
                f"{shlex.quote(str(child_pid_path))}\n"
                "while :; do\n"
                "    sleep 60\n"
                "done\n"
            ).encode()
            inputs = self.inputs(
                seams,
                root,
                backend_payload=backend_payload,
            )
            report = seams["run_parity_probes"](
                inputs=inputs,
                timeout_seconds=1.0,
                max_output_bytes=4096,
            )
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text())
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("parity probe descendant survived timeout")

        self.assertEqual(
            [("core_protocol", "timeout")],
            self.result_codes(report),
        )

    def test_candidate_input_change_during_probe_is_stale(self) -> None:
        seams = self.probe_seams()
        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))

            def mutate(_request: object, call_count: int) -> None:
                if call_count == 1:
                    inputs.overlay_path.write_bytes(b'{"changed":true}\n')

            runner = RecordingParityProbeRunner(
                [
                    self.command_result(
                        seams,
                        stdout=self.CORE_SUCCESS,
                    )
                ],
                on_call=mutate,
            )
            report = seams["run_parity_probes"](
                inputs=inputs,
                runner=runner,
                timeout_seconds=0.5,
                max_output_bytes=4096,
            )

        self.assertFalse(report.healthy)
        self.assertEqual(
            [("core_protocol", "candidate_stale")],
            self.result_codes(report),
        )
        self.assertEqual(1, len(runner.requests))

    def test_sanitized_evidence_is_bounded_and_secret_stable(self) -> None:
        seams = self.probe_seams()
        reports: list[object] = []
        for secret in ("first-secret", "second-secret"):
            with tempfile.TemporaryDirectory() as temp_dir:
                inputs = self.inputs(seams, Path(temp_dir))
                report, _runner = self.run_success(
                    seams,
                    inputs,
                    core_stderr=(
                        f"Authorization: Bearer {secret}\n"
                        f"api-key={secret}\n"
                        f'client_secret="{secret}"\n'
                        f'{{"refresh_token":"{secret}"}}\n'
                        f"https://user:{secret}@example.test/path"
                        f"?x-amz-signature={secret}"
                        f"&x-goog-signature={secret}"
                        f"&access_token={secret}"
                        f"&api_key={secret}"
                        f"&apikey={secret}"
                        f"&credential={secret}\n"
                        "x-account-id: safe-route-123\n"
                    ),
                    typed_stderr=f"password={secret}\n",
                )
                reports.append(report)

        first, second = reports
        self.assertEqual(first.receipt_results, second.receipt_results)
        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        self.assertLess(len(first.canonical_bytes), 4096)
        for secret in ("first-secret", "second-secret"):
            self.assertNotIn(secret.encode(), first.canonical_bytes)
            self.assertNotIn(secret.encode(), second.canonical_bytes)

        with tempfile.TemporaryDirectory() as temp_dir:
            inputs = self.inputs(seams, Path(temp_dir))
            changed_route, _runner = self.run_success(
                seams,
                inputs,
                core_stderr=(
                    "Authorization: Bearer third-secret\n"
                    "x-account-id: different-safe-route\n"
                ),
            )
        self.assertNotEqual(
            first.receipt_results,
            changed_route.receipt_results,
        )

    def test_retained_redacted_provider_fixture_never_copies_transient_config(
        self,
    ) -> None:
        seams = self.probe_seams()
        self.assertTrue(
            self.RETAINED_FIXTURE.is_file(),
            "redacted retained parity probe fixture is missing",
        )
        fixture_payload = self.RETAINED_FIXTURE.read_bytes()
        fixture = json.loads(fixture_payload)
        self.assertEqual(
            {
                "expected_result_codes",
                "probe_outputs",
                "redacted_config",
                "schema",
                "schema_version",
            },
            set(fixture),
        )
        self.assertEqual(
            "codex-switch.parity-retained-probe-fixture",
            fixture["schema"],
        )
        self.assertEqual(1, fixture["schema_version"])
        redacted_config = fixture["redacted_config"]
        self.assertIsInstance(redacted_config, str)
        self.assertIn("[model_providers.azure-redacted]", redacted_config)
        self.assertIn('wire_api = "responses"', redacted_config)
        self.assertIn(
            'env_key = "AZURE_OPENAI_API_KEY"',
            redacted_config,
        )
        credential_lines = [
            line.strip()
            for line in redacted_config.splitlines()
            if line.strip().lower().startswith(
                ("api_key =", "password =", "access_token =")
            )
        ]
        self.assertEqual(['api_key = "[REDACTED]"'], credential_lines)
        fixture_text = fixture_payload.decode()
        for forbidden in (
            "Bearer ",
            "raw-config-secret",
            "/Users/",
            "/private/tmp/",
            "/var/folders/",
        ):
            self.assertNotIn(forbidden, fixture_text)

        outputs = fixture["probe_outputs"]
        self.assertIsInstance(outputs, dict)
        expected_codes = [
            tuple(item)
            for item in fixture["expected_result_codes"]
        ]
        report_payloads: list[bytes] = []
        for secret in (
            "transient-provider-secret-one",
            "transient-provider-secret-two",
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                inputs = self.inputs(seams, root)
                config_text = redacted_config.replace(
                    "__MODEL_CATALOG_JSON__",
                    json.dumps(str(inputs.overlay_path)),
                ).replace(
                    'api_key = "[REDACTED]"',
                    f"api_key = {json.dumps(secret)}",
                )
                config_payload = config_text.encode()
                inputs.config_path.write_bytes(config_payload)
                inputs.config_path.chmod(0o600)
                inputs = replace(
                    inputs,
                    config_sha256=hashlib.sha256(
                        config_payload
                    ).hexdigest(),
                )
                before_files = {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                runner = RecordingParityProbeRunner(
                    [
                        self.command_result(
                            seams,
                            stdout=outputs["core_stdout"],
                            stderr=outputs["core_stderr"],
                        ),
                        self.command_result(
                            seams,
                            stdout=outputs["typed_stdout"],
                            stderr=outputs["typed_stderr"],
                        ),
                    ]
                )
                report = seams["run_parity_probes"](
                    inputs=inputs,
                    runner=runner,
                    timeout_seconds=0.5,
                    max_output_bytes=4096,
                )
                after_files = {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }

                self.assertTrue(report.healthy)
                self.assertEqual(expected_codes, self.result_codes(report))
                self.assertEqual(before_files, after_files)
                self.assertNotIn(secret.encode(), report.canonical_bytes)
                self.assertNotIn(config_payload, report.canonical_bytes)
                self.assertNotIn(
                    str(inputs.config_path).encode(),
                    report.canonical_bytes,
                )
                report_payloads.append(report.canonical_bytes)

        self.assertEqual(report_payloads[0], report_payloads[1])


class ParityConfigProjectionTests(unittest.TestCase):
    PROJECTION_SEAMS = (
        "ConfigInputs",
        "ConfigProjection",
        "prepare_parity_config_projection",
    )

    def supported_python_for_config_test(self) -> str:
        if sys.version_info >= (3, 11):
            return sys.executable
        python = shutil.which("python3.12") or shutil.which("python3.11")
        if python is None:
            self.fail("Python 3.11+ is required for parity config tests")
        return python

    def tomllib_parser_for_config_test(self) -> object:
        if sys.version_info >= (3, 11):
            import tomllib

            return tomllib
        python = self.supported_python_for_config_test()

        class SubprocessTomllib:
            TOMLDecodeError = ValueError

            @staticmethod
            def loads(text: str) -> object:
                result = subprocess.run(
                    [
                        python,
                        "-c",
                        (
                            "import json, sys, tomllib; "
                            "print(json.dumps(tomllib.loads(sys.stdin.read())))"
                        ),
                    ],
                    input=text,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if result.returncode != 0:
                    raise ValueError(result.stderr.strip())
                return json.loads(result.stdout)

        return SubprocessTomllib

    def setUp(self) -> None:
        if sys.version_info >= (3, 11):
            return
        parser = self.tomllib_parser_for_config_test()
        patcher = mock.patch(
            "codex_switch_config_document.tomllib",
            parser,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def projection_seams(self) -> dict[str, object]:
        seams = {
            name: getattr(parity_module, name, None)
            for name in self.PROJECTION_SEAMS
        }
        missing = [
            name
            for name, value in seams.items()
            if value is None
        ]
        self.assertFalse(
            missing,
            "Parity config projection seams are missing: "
            + ", ".join(missing),
        )
        return seams

    def write_config(self, path: Path, text: str) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = text.encode("utf-8")
        path.write_bytes(payload)
        path.chmod(0o600)
        return payload

    def prepare(
        self,
        *,
        profile_config: Path,
        sources: tuple[tuple[Path, bytes], ...],
        overlay_path: Path,
        _source_observer: object | None = None,
    ) -> object:
        seams = self.projection_seams()
        inputs_type = seams["ConfigInputs"]
        prepare_projection = seams["prepare_parity_config_projection"]
        self.assertTrue(callable(inputs_type))
        self.assertTrue(callable(prepare_projection))
        config_inputs = inputs_type(
            profile_config=profile_config,
            sources=tuple(
                (
                    path,
                    hashlib.sha256(payload).hexdigest(),
                )
                for path, payload in sources
            ),
        )
        kwargs = {
            "config_inputs": config_inputs,
            "overlay_path": overlay_path,
        }
        if _source_observer is not None:
            kwargs["_source_observer"] = _source_observer
        return prepare_projection(
            **kwargs,
        )

    def projection_payload(self, projection: object, path: Path) -> bytes:
        method = getattr(projection, "payload_for", None)
        self.assertTrue(
            callable(method),
            "ConfigProjection.payload_for is required",
        )
        payload = method(path)
        self.assertIsInstance(payload, bytes)
        return payload

    def assert_unhealthy_projection(
        self,
        projection: object,
        *,
        code: str,
    ) -> None:
        projection_type = self.projection_seams()["ConfigProjection"]
        self.assertIsInstance(projection, projection_type)
        self.assertIs(projection.healthy, False)
        self.assertEqual((), projection.changed_paths)
        self.assertIsNone(projection.max_threads_source)
        self.assertEqual(
            [("config", code, "error")],
            [
                (
                    finding.category,
                    finding.code,
                    finding.severity,
                )
                for finding in projection.findings
            ],
        )

    def failure_profile(
        self,
        root: Path,
    ) -> tuple[Path, Path, bytes]:
        profile_config = root / "profiles" / "internal" / "config.toml"
        overlay_path = (
            root
            / "profiles"
            / "internal"
            / "parity"
            / "model-catalog.json"
        )
        profile_payload = self.write_config(
            profile_config,
            (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "azure"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            ),
        )
        return profile_config, overlay_path, profile_payload

    def test_internal_profile_projects_overlay_and_v2_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config = root / "profiles" / "internal" / "config.toml"
            shared_config = root / "shared" / "config.toml"
            overlay_path = (
                root
                / "profiles"
                / "internal"
                / "parity"
                / "model-catalog.json"
            )
            profile_payload = self.write_config(
                profile_config,
                (
                    "# keep internal profile heading\n"
                    'model = "gpt-5.6-sol"\n'
                    'model_provider = "azure"\n'
                    'cli_auth_credentials_store = "file"\n'
                    "\n"
                    "[features]\n"
                    "memory = true\n"
                    "\n"
                    "[model_providers.azure]\n"
                    'name = "Azure"\n'
                ),
            )
            shared_payload = self.write_config(
                shared_config,
                (
                    "# keep shared heading\n"
                    "[agents]\n"
                    "max_threads = 6 # stale v1 tuning\n"
                    "max_depth = 3\n"
                    'role = "explorer"\n'
                    "\n"
                    "[tui]\n"
                    'theme = "dark"\n'
                ),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            projection_type = self.projection_seams()["ConfigProjection"]
            self.assertIsInstance(projection, projection_type)
            self.assertIs(projection.healthy, True)
            self.assertEqual((), projection.findings)
            projected_profile = self.projection_payload(
                projection,
                profile_config,
            )
            projected_shared = self.projection_payload(
                projection,
                shared_config,
            )
            from codex_switch_config_document import ConfigDocument

            profile_document = ConfigDocument.parse(
                projected_profile.decode("utf-8"),
                "projected internal profile",
            )
            shared_document = ConfigDocument.parse(
                projected_shared.decode("utf-8"),
                "projected shared config",
            )

            self.assertEqual(
                str(overlay_path),
                profile_document.data["model_catalog_json"],
            )
            self.assertIs(
                profile_document.data["features"]["multi_agent_v2"],
                True,
            )
            self.assertEqual(
                "gpt-5.6-sol",
                profile_document.data["model"],
            )
            self.assertEqual(
                "azure",
                profile_document.data["model_provider"],
            )
            self.assertEqual(
                "file",
                profile_document.data["cli_auth_credentials_store"],
            )
            self.assertIs(profile_document.data["features"]["memory"], True)
            self.assertEqual(
                "Azure",
                profile_document.data["model_providers"]["azure"]["name"],
            )
            self.assertNotIn(
                "model_catalog_json",
                shared_document.data,
            )
            self.assertNotIn(
                "multi_agent_v2",
                shared_document.data.get("features", {}),
            )
            self.assertNotIn(
                "max_threads",
                shared_document.data["agents"],
            )
            self.assertEqual(3, shared_document.data["agents"]["max_depth"])
            self.assertEqual(
                "explorer",
                shared_document.data["agents"]["role"],
            )
            self.assertEqual("dark", shared_document.data["tui"]["theme"])
            self.assertEqual(
                {profile_config, shared_config},
                set(projection.changed_paths),
            )
            self.assertEqual(
                shared_config,
                projection.max_threads_source,
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_already_clean_projection_is_byte_identical_and_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config = root / "profiles" / "internal" / "config.toml"
            shared_config = root / "shared" / "config.toml"
            overlay_path = (
                root
                / "profiles"
                / "internal"
                / "parity"
                / "model-catalog.json"
            )
            profile_payload = self.write_config(
                profile_config,
                (
                    "# already projected\n"
                    'model = "gpt-5.6-sol"\n'
                    f"model_catalog_json = {json.dumps(str(overlay_path))}\n"
                    "\n"
                    "[features]\n"
                    "multi_agent_v2 = true\n"
                    "memory = true\n"
                ),
            )
            shared_payload = self.write_config(
                shared_config,
                (
                    "[agents]\n"
                    "max_depth = 3\n"
                    'role = "explorer"\n'
                ),
            )
            sources = (
                (profile_config, profile_payload),
                (shared_config, shared_payload),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=sources,
                overlay_path=overlay_path,
            )
            repeated = self.prepare(
                profile_config=profile_config,
                sources=sources,
                overlay_path=overlay_path,
            )

            self.assertEqual(projection, repeated)
            self.assertIs(projection.healthy, True)
            self.assertEqual((), projection.findings)
            self.assertEqual((), projection.changed_paths)
            self.assertIsNone(projection.max_threads_source)
            self.assertEqual(
                profile_payload,
                self.projection_payload(projection, profile_config),
            )
            self.assertEqual(
                shared_payload,
                self.projection_payload(projection, shared_config),
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_duplicate_max_threads_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "[agents]\n"
                    "max_threads = 4\n"
                    "max_threads = 6\n"
                ),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            self.assert_unhealthy_projection(
                projection,
                code="parity.config.max_threads_ambiguous",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_dotted_max_threads_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "agents.max_threads = 6\n"
                    'mode = "keep"\n'
                ),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            self.assert_unhealthy_projection(
                projection,
                code="parity.config.max_threads_ambiguous",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_non_scalar_max_threads_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "[agents]\n"
                    "max_threads = [6]\n"
                    "max_depth = 3\n"
                ),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            self.assert_unhealthy_projection(
                projection,
                code="parity.config.max_threads_ambiguous",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_inline_parent_max_threads_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "agents = { max_threads = 6, max_depth = 3 }\n"
                    'mode = "keep"\n'
                ),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            self.assert_unhealthy_projection(
                projection,
                code="parity.config.max_threads_ambiguous",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_invalid_toml_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "[agents\n"
                    "max_threads = 6\n"
                ),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            self.assert_unhealthy_projection(
                projection,
                code="parity.config.source_invalid",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_multiply_sourced_max_threads_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, _profile_payload = (
                self.failure_profile(root)
            )
            profile_payload = self.write_config(
                profile_config,
                (
                    'model = "gpt-5.6-sol"\n'
                    "\n"
                    "[agents]\n"
                    "max_threads = 4\n"
                ),
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "[agents]\n"
                    "max_threads = 6\n"
                ),
            )

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            self.assert_unhealthy_projection(
                projection,
                code="parity.config.max_threads_ambiguous",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())

    def test_symlinked_config_source_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_config.parent.mkdir(parents=True)
            shared_target = root / "outside-shared.toml"
            shared_payload = self.write_config(
                shared_target,
                (
                    "[agents]\n"
                    "max_threads = 6\n"
                ),
            )
            shared_config.symlink_to(shared_target)

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
            )

            self.assert_unhealthy_projection(
                projection,
                code="parity.config.source_unsafe",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertTrue(shared_config.is_symlink())
            self.assertEqual(shared_payload, shared_target.read_bytes())

    def test_concurrent_source_replacement_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "[agents]\n"
                    "max_threads = 6\n"
                    "max_depth = 3\n"
                ),
            )
            replaced = root / "shared.original.toml"
            observed = False

            def observe(phase: str, path: Path) -> None:
                nonlocal observed
                if observed or phase != "after_read" or path != shared_config:
                    return
                observed = True
                path.rename(replaced)
                path.write_bytes(shared_payload)
                path.chmod(0o600)

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
                _source_observer=observe,
            )

            self.assertTrue(observed)
            self.assert_unhealthy_projection(
                projection,
                code="parity.config.source_stale",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())
            self.assertEqual(shared_payload, replaced.read_bytes())

    def test_previously_read_source_replacement_is_unhealthy_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_config, overlay_path, profile_payload = (
                self.failure_profile(root)
            )
            shared_config = root / "shared" / "config.toml"
            shared_payload = self.write_config(
                shared_config,
                (
                    "[agents]\n"
                    "max_threads = 6\n"
                    "max_depth = 3\n"
                ),
            )
            replaced = root / "profile.original.toml"
            observed = False

            def observe(phase: str, path: Path) -> None:
                nonlocal observed
                if observed or phase != "after_read" or path != shared_config:
                    return
                observed = True
                profile_config.rename(replaced)
                profile_config.write_bytes(profile_payload)
                profile_config.chmod(0o600)

            projection = self.prepare(
                profile_config=profile_config,
                sources=(
                    (profile_config, profile_payload),
                    (shared_config, shared_payload),
                ),
                overlay_path=overlay_path,
                _source_observer=observe,
            )

            self.assertTrue(observed)
            self.assert_unhealthy_projection(
                projection,
                code="parity.config.source_stale",
            )
            self.assertEqual(profile_payload, profile_config.read_bytes())
            self.assertEqual(profile_payload, replaced.read_bytes())
            self.assertEqual(shared_payload, shared_config.read_bytes())


if __name__ == "__main__":
    unittest.main()
