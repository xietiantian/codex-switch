#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import os
import plistlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import codex_switch_bindings as bindings_module
import codex_switch_doctor as doctor_module
import codex_switch_doctor_active as doctor_active_module
import codex_switch_status_active as status_active_module
from codex_switch_capture import capture_profile
from codex_switch_bindings import (
    cmd_promote_internal_update,
    cmd_set_app_bin,
    cmd_set_bin,
)
from codex_switch_doctor_active import active_profile_problems
from codex_switch_lifecycle import cmd_init
from codex_switch_home_sync import build_internal_home_config
from codex_switch_parity import (
    ConfigInputs,
    prepare_parity_config_projection,
    project_parity_runtime_config_payload,
)
from codex_switch_protocol_adapter import CapabilityReceipt
from codex_switch_running_app import (
    RunningCodexProcess,
    app_server_command_path,
    collect_runtime_observation,
    print_running_desktop_status,
    running_codex_processes,
    running_desktop_problems,
)
from codex_switch_shim import write_codex_shim
from codex_switch_status_app import print_app_codex_status
from codex_switch_runtime_binding import (
    CURRENT_CHATGPT_BUNDLE_ID,
    DesktopInventory,
    DesktopRoots,
    RuntimeObservation,
    RuntimeBindingContext,
    RuntimeBindingError,
    attest_runtime_binding,
    discover_desktop_hosts,
    resolve_runtime_binding,
)
from codex_switch_store import Store
from codex_switch_switching import switch_profile
from codex_switch_constants import SwitchError
from codex_switch_selection import ProfileSelection
from codex_switch_verify import collect_active_state_problems, run_app_server_smoke
from codex_switch_transaction import (
    TransactionRequest,
    execute_transaction,
    locked_store_mutation,
)


def write_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class ActiveProfileSelectionTests(unittest.TestCase):
    def selection_module(self):
        import codex_switch_selection

        return codex_switch_selection

    def test_requested_internal_cli_official_app_normalizes_alias(self) -> None:
        selection = self.selection_module().requested_profile_selection(
            "internal",
            "official",
        )

        self.assertEqual("internal", selection.cli_profile)
        self.assertEqual("openai-official", selection.app_profile)
        self.assertTrue(selection.is_split)

    def test_requested_profile_without_override_remains_synchronized(self) -> None:
        selection = self.selection_module().requested_profile_selection(
            "internal",
            None,
        )

        self.assertEqual("internal", selection.cli_profile)
        self.assertEqual("internal", selection.app_profile)
        self.assertFalse(selection.is_split)

    def test_requested_unsupported_split_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            SwitchError,
            "selection.unsupported.*supported split: internal/openai-official",
        ):
            self.selection_module().requested_profile_selection(
                "openai-official",
                "internal",
            )

    def test_requested_explicit_app_and_skip_app_cli_conflict(self) -> None:
        with self.assertRaisesRegex(SwitchError, "selection.app_skip_conflict"):
            self.selection_module().requested_profile_selection(
                "internal",
                "official",
                skip_app_cli=True,
            )

    def test_legacy_active_record_maps_both_surfaces_to_profile(self) -> None:
        selection = self.selection_module().active_profile_selection(
            {"profile": "openai-official"}
        )

        self.assertEqual("openai-official", selection.cli_profile)
        self.assertEqual("openai-official", selection.app_profile)

    def test_explicit_active_record_preserves_split_and_cli_alias(self) -> None:
        selection = self.selection_module().active_profile_selection(
            {
                "profile": "internal",
                "cli_profile": "internal",
                "app_profile": "openai-official",
            }
        )

        self.assertEqual("internal", selection.cli_profile)
        self.assertEqual("openai-official", selection.app_profile)
        self.assertEqual(
            {
                "profile": "internal",
                "cli_profile": "internal",
                "app_profile": "openai-official",
            },
            self.selection_module().active_profile_fields(selection),
        )

    def test_partial_explicit_active_record_fails_closed(self) -> None:
        with self.assertRaisesRegex(SwitchError, "active.selection.partial"):
            self.selection_module().active_profile_selection(
                {"profile": "internal", "cli_profile": "internal"}
            )

    def test_conflicting_legacy_cli_alias_fails_closed(self) -> None:
        with self.assertRaisesRegex(SwitchError, "active.selection.cli_conflict"):
            self.selection_module().active_profile_selection(
                {
                    "profile": "openai-official",
                    "cli_profile": "internal",
                    "app_profile": "openai-official",
                }
            )


class RuntimeBindingTests(unittest.TestCase):
    def supported_python_for_runtime_test(self) -> str:
        if sys.version_info >= (3, 11):
            return sys.executable
        python = shutil.which("python3.12") or shutil.which("python3.11")
        if python is None:
            self.fail("Python 3.11+ is required for transaction runtime tests")
        return python

    def tomllib_parser_for_runtime_test(self) -> object:
        if sys.version_info >= (3, 11):
            import tomllib

            return tomllib
        python = self.supported_python_for_runtime_test()

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
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(result.stdout)

        return SimpleNamespace(loads=loads)

    def setUp(self) -> None:
        if sys.version_info >= (3, 11):
            return
        tomllib_parser = self.tomllib_parser_for_runtime_test()
        for target in (
            "codex_switch_toml_validate.tomllib",
            "codex_switch_config_document.tomllib",
        ):
            patcher = patch(target, tomllib_parser)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(
            os.environ,
            {"CODEX_SWITCH_PYTHON": self.supported_python_for_runtime_test()},
        )
        environment.start()
        self.addCleanup(environment.stop)

    def make_roots(self, root: Path) -> DesktopRoots:
        applications = root / "Applications"
        return DesktopRoots(
            chatgpt=applications / "ChatGPT.app",
            legacy_codex=applications / "Codex.app",
            chatgpt_classic=applications / "ChatGPT Classic.app",
        )

    def write_bundle(
        self,
        bundle: Path,
        *,
        bundle_id: str,
        main_name: str,
        include_main: bool = True,
        include_cli: bool = True,
    ) -> tuple[Path, Path]:
        contents = bundle / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleIdentifier": bundle_id}, handle)
        main = contents / "MacOS" / main_name
        cli = contents / "Resources" / "codex"
        if include_main:
            write_executable(main)
        if include_cli:
            write_executable(cli)
        return main, cli

    def make_context(
        self,
        root: Path,
        profile: str,
        manifest: dict[str, object],
        *,
        path_codex: Path | None = None,
        active_record: dict[str, object] | None = None,
    ) -> RuntimeBindingContext:
        store_root = root / "store"
        return RuntimeBindingContext(
            profile=profile,
            manifest=manifest,
            store_root=store_root,
            bin_dir=store_root / "bin",
            profile_home=store_root / "homes" / profile,
            path_codex=path_codex,
            active_record=active_record or {},
        )

    def make_store(self, root: Path) -> Store:
        return Store(
            root=root / "store",
            live_codex_home=root / "official-home",
            launch_agent_path=root / "agent.plist",
            launch_agent_label="test",
            internal_codex_home=root / "internal-home",
            internal_codex_home_source="explicit",
        )

    def write_internal_store_fixture(
        self,
        root: Path,
    ) -> tuple[Store, Path, Path, object]:
        store = self.make_store(root)
        store.ensure()
        backend = write_executable(root / "internal-bin" / "codex")
        launcher = store.bin_dir / "codex-internal-app"
        write_executable(launcher)
        profile_dir = store.profile_dir("internal")
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "config.toml").write_text('profile = "internal"\n')
        (profile_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "internal",
                    "codex_bin": str(backend),
                    "app_cli_path": str(launcher),
                    "app_cli_binding": "launchagent",
                }
            )
        )
        home = store.internal_codex_home
        assert home is not None
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('profile = "internal"\n')
        active = {
            "profile": "internal",
            "codex_home": str(home),
            "shell_cli_path": str(backend),
            "app_cli_path": str(launcher),
        }
        store.active_path.write_text(json.dumps(active))
        binding = resolve_runtime_binding(
            self.make_context(root, "internal", store.load_manifest("internal")),
            DesktopInventory(current=None),
        )
        return store, backend, launcher, binding

    def write_split_store_fixture(
        self,
        root: Path,
    ) -> tuple[Store, Path, Path, object, object, RuntimeObservation]:
        from codex_switch_launch import launch_agent_payload

        store, backend, launcher, internal_binding = (
            self.write_internal_store_fixture(root)
        )
        roots = self.make_roots(root)
        _main, official_cli = self.write_bundle(
            roots.chatgpt,
            bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
            main_name="ChatGPT",
        )
        official_profile = store.profile_dir("openai-official")
        official_profile.mkdir(parents=True, exist_ok=True)
        (official_profile / "config.toml").write_text('profile = "openai-official"\n')
        official_manifest = {
            "name": "openai-official",
            "codex_bin": str(official_cli),
            "app_cli_path": str(official_cli),
            "runtime_binding": "canonical",
            "app_cli_binding": "launchagent",
        }
        (official_profile / "manifest.json").write_text(
            json.dumps(official_manifest)
        )
        official_binding = resolve_runtime_binding(
            self.make_context(root, "openai-official", official_manifest),
            discover_desktop_hosts(roots),
        )
        home = store.internal_codex_home
        assert home is not None
        internal_binding = replace(internal_binding, codex_home=home)
        write_codex_shim(
            store,
            str(internal_binding.shell_cli),
            home,
            profile_name="internal",
        )
        store.launch_agent_path.write_bytes(
            launch_agent_payload(store.launch_agent_label, official_cli)
        )
        store.active_path.write_text(
            json.dumps(
                {
                    "profile": "internal",
                    "cli_profile": "internal",
                    "app_profile": "openai-official",
                    "codex_home": str(home),
                    "shim_path": str(store.bin_dir / "codex"),
                    "shell_cli_path": str(backend),
                    "app_cli_path": str(official_cli),
                }
            )
        )
        observation = RuntimeObservation(
            gui_app_cli=str(official_cli),
            launch_agent_cli=str(official_cli),
        )
        return (
            store,
            backend,
            official_cli,
            internal_binding,
            official_binding,
            observation,
        )

    def rebind_args(
        self,
        store: Store,
        backend: Path,
        *,
        fault_hook: object | None = None,
    ) -> SimpleNamespace:
        roots = self.make_roots(store.root.parent)
        if not roots.chatgpt.exists():
            self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
        return SimpleNamespace(
            store_dir=store.root,
            official_codex_home=store.official_codex_home,
            official_codex_home_source="explicit",
            internal_codex_home=store.internal_codex_home,
            internal_codex_home_source="explicit",
            launch_agent_path=store.launch_agent_path,
            launch_agent_label=store.launch_agent_label,
            name="internal",
            codex_bin=str(backend),
            preserve_app_cli=False,
            rebind_commit_fault_hook=fault_hook,
            rebind_desktop_inventory=discover_desktop_hosts(roots),
        )

    def write_rebind_backend(
        self,
        path: Path,
        *,
        initialize_error: bool = False,
        runtime_label: str = "candidate",
        version: str = "0.142.4",
    ) -> Path:
        initialize_response = (
            "{'id': message['id'], 'error': {'code': -32000, 'message': 'rejected'}}"
            if initialize_error
            else "{'id': message['id'], 'result': {'userAgent': 'rebind-test'}}"
        )
        return write_executable(
            path,
            "#!/usr/bin/env python3\n"
            "import hashlib, json, os, re, sys\n"
            "from pathlib import Path\n"
            f"RUNTIME_LABEL = {runtime_label!r}\n"
            "if '--version' in sys.argv:\n"
            f"    print('codex-cli {version}')\n"
            "    raise SystemExit(0)\n"
            "if '--runtime-contract' in sys.argv:\n"
            "    home = Path(os.environ.get('CODEX_HOME', '')).expanduser()\n"
            "    config_path = home / 'config.toml'\n"
            "    config_payload = config_path.read_bytes()\n"
            "    config_text = config_payload.decode()\n"
            "    catalog_match = re.search(\n"
            "        r'^model_catalog_json\\s*=\\s*\"([^\"]+)\"',\n"
            "        config_text,\n"
            "        re.MULTILINE,\n"
            "    )\n"
            "    overlay_path = (\n"
            "        Path(catalog_match.group(1)).expanduser()\n"
            "        if catalog_match is not None\n"
            "        else Path()\n"
            "    )\n"
            "    overlay_payload = (\n"
            "        overlay_path.read_bytes()\n"
            "        if catalog_match is not None and overlay_path.is_file()\n"
            "        else b''\n"
            "    )\n"
            "    marker = os.environ.get('RUNTIME_CONTRACT_MARKER', '')\n"
            "    if marker:\n"
            "        Path(marker).write_text(RUNTIME_LABEL + '\\n')\n"
            "    print(json.dumps({\n"
            "        'runtime_label': RUNTIME_LABEL,\n"
            "        'codex_home': str(home.resolve()),\n"
            "        'config_sha256': hashlib.sha256(config_payload).hexdigest(),\n"
            "        'overlay_path': str(overlay_path.resolve()),\n"
            "        'overlay_sha256': hashlib.sha256(overlay_payload).hexdigest(),\n"
            "        'multi_agent_v2': bool(re.search(\n"
            "            r'^multi_agent_v2\\s*=\\s*true\\s*$',\n"
            "            config_text,\n"
            "            re.MULTILINE,\n"
            "        )),\n"
            "        'capability_receipt_path': os.environ.get(\n"
            "            'CODEX_SWITCH_CAPABILITY_RECEIPT',\n"
            "            '',\n"
            "        ),\n"
            "        'expected_receipt_sha256': os.environ.get(\n"
            "            'CODEX_SWITCH_EXPECTED_RECEIPT_SHA256',\n"
            "            '',\n"
            "        ),\n"
            "    }, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "if 'generate-json-schema' in sys.argv:\n"
            "    output = Path(sys.argv[sys.argv.index('--out') + 1])\n"
            "    output.mkdir(parents=True, exist_ok=True)\n"
            "    (output / 'protocol.json').write_text(json.dumps({\n"
            "        '$defs': {\n"
            "            'ThreadStartParams': {'type': 'object', 'properties': {'dynamicTools': {'type': 'array'}}},\n"
            "            'PluginMarketplaceKind': {'enum': ['local', 'created-by-me-remote']},\n"
            "        }\n"
            "    }, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "for raw in sys.stdin:\n"
            "    message = json.loads(raw)\n"
            "    if message.get('method') == 'initialize':\n"
            f"        print(json.dumps({initialize_response}), flush=True)\n"
            "    elif message.get('method') == 'plugin/list':\n"
            "        print(json.dumps({'id': 'plugin-list-smoke', 'result': {'marketplaces': []}}), flush=True)\n"
            "    elif message.get('method') == 'config/value/write':\n"
            "        params = message['params']\n"
            "        config_path = Path(params['filePath'])\n"
            "        text = config_path.read_text().replace(\n"
            "            'codex_switch_config_write_probe = false',\n"
            "            'codex_switch_config_write_probe = true',\n"
            "        )\n"
            "        config_path.write_text(text)\n"
            "        print(json.dumps({\n"
            "            'id': message['id'],\n"
            "            'result': {\n"
            "                'filePath': str(config_path.resolve()),\n"
            "                'status': 'ok',\n"
            "                'version': 'runtime-binding-probe-v1',\n"
            "            },\n"
            "        }), flush=True)\n",
        )

    def parity_rebind_bundle_fixture(
        self,
        root: Path,
        store: Store,
        *,
        generation: str = "current",
        config_marker: str | None = None,
    ) -> tuple[SimpleNamespace, Path, bytes, int, dict[str, bytes]]:
        profile_dir = store.profile_dir("internal")
        profile_config = profile_dir / "config.toml"
        shared_config = store.official_codex_home / "config.toml"
        active_runtime_config = store.internal_codex_home / "config.toml"
        source_catalog = root / "configured" / "azure-models.json"
        source_catalog.parent.mkdir(parents=True, exist_ok=True)
        source_payload = (
            json.dumps(
                {
                    "catalog_generation": generation,
                    "models": [
                        {
                            "provider": "azure",
                            "slug": "gpt-5.6-sol",
                        }
                    ],
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        source_catalog.write_bytes(source_payload)
        source_catalog.chmod(0o640)
        source_mode = source_catalog.stat().st_mode & 0o777

        shared_config.parent.mkdir(parents=True, exist_ok=True)
        marker_setting = (
            f'fixture_generation = "{config_marker}"\n'
            if config_marker is not None
            else ""
        )
        shared_config.write_text(
            "[agents]\nmax_threads = 4\n\n[notice]\nkeep = true\n"
            + marker_setting
        )
        shared_config.chmod(0o600)
        profile_config.write_text(
            'model = "gpt-5.6-sol"\n'
            'model_provider = "azure"\n'
            f'model_catalog_json = "{source_catalog}"\n'
            + marker_setting
        )
        profile_config.chmod(0o600)
        active_runtime_config.write_text(
            'profile = "old-runtime"\n' + marker_setting
        )
        active_runtime_config.chmod(0o600)

        parity_dir = profile_dir / "parity"
        final_receipt = parity_dir / "receipt.json"
        final_overlay = parity_dir / "model-catalog.json"
        receipt_payload = (
            json.dumps(
                {
                    "generation": generation,
                    "healthy": True,
                    "schema_version": 1,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        overlay_payload = (
            json.dumps(
                {
                    "catalog_generation": generation,
                    "models": [
                        {
                            "multi_agent_version": "v2",
                            "provider": "azure",
                            "slug": "gpt-5.6-sol",
                        }
                    ],
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        config_projection = prepare_parity_config_projection(
            config_inputs=ConfigInputs.capture(
                profile_config=profile_config,
                source_paths=(profile_config, shared_config),
            ),
            overlay_path=final_overlay,
        )
        self.assertTrue(
            config_projection.healthy,
            config_projection.findings,
        )
        projection_payloads = dict(config_projection.payloads)
        runtime_payload = build_internal_home_config(
            store.official_codex_home,
            "internal",
            active_runtime_config,
            profile_config,
            config_projection=config_projection,
        ).encode()
        projected_payloads = {
            "profile_config": projection_payloads[profile_config],
            "shared_config": projection_payloads[shared_config],
            "active_runtime_config": runtime_payload,
        }
        staging_root = root / f"parity-stage-{generation}"
        staging_root.mkdir(mode=0o700)
        staged_receipt = staging_root / "receipt.json"
        staged_overlay = staging_root / "model-catalog.json"
        staged_runtime_config = staging_root / "config.toml"
        staged_runtime_payload = project_parity_runtime_config_payload(
            runtime_payload,
            overlay_path=staged_overlay,
        )
        staged_receipt.write_bytes(receipt_payload)
        staged_overlay.write_bytes(overlay_payload)
        staged_runtime_config.write_bytes(staged_runtime_payload)
        staged_receipt.chmod(0o600)
        staged_overlay.chmod(0o600)
        staged_runtime_config.chmod(0o600)
        source_sha256 = hashlib.sha256(source_payload).hexdigest()
        receipt_sha256 = hashlib.sha256(receipt_payload).hexdigest()
        overlay_sha256 = hashlib.sha256(overlay_payload).hexdigest()
        manifest_metadata = {
            "parity_adapter_rule_set_sha256": "a" * 64,
            "parity_capability_receipt_sha256": "b" * 64,
            "parity_internal_fingerprint_sha256": "c" * 64,
            "parity_official_reference_sha256": "d" * 64,
            "parity_overlay_path": str(final_overlay),
            "parity_overlay_sha256": overlay_sha256,
            "parity_policy_version": "1",
            "parity_receipt_path": str(final_receipt),
            "parity_receipt_schema_version": 1,
            "parity_receipt_sha256": receipt_sha256,
            "parity_source_catalog_path": str(source_catalog),
            "parity_source_catalog_sha256": source_sha256,
        }
        bundle = SimpleNamespace(
            healthy=True,
            findings=(),
            synchronization_queue=(),
            receipt=SimpleNamespace(
                healthy=True,
                findings=(),
                canonical_bytes=receipt_payload,
            ),
            receipt_payload=receipt_payload,
            overlay=SimpleNamespace(
                overlay_payload=overlay_payload,
                source_catalog=source_catalog,
                source_catalog_sha256=source_sha256,
                overlay_sha256=overlay_sha256,
            ),
            overlay_payload=overlay_payload,
            config_projection=config_projection,
            active_runtime_config=(
                active_runtime_config,
                projected_payloads["active_runtime_config"],
            ),
            active_runtime_config_path=active_runtime_config,
            active_runtime_config_payload=projected_payloads[
                "active_runtime_config"
            ],
            staging_root=staging_root,
            staged_receipt_path=staged_receipt,
            staged_overlay_path=staged_overlay,
            staged_runtime_config_path=staged_runtime_config,
            staged_runtime_config_payload=staged_runtime_payload,
            config_sha256s=(
                (
                    "profile",
                    hashlib.sha256(
                        projected_payloads["profile_config"]
                    ).hexdigest(),
                ),
                (
                    "shared",
                    hashlib.sha256(
                        projected_payloads["shared_config"]
                    ).hexdigest(),
                ),
                (
                    "runtime",
                    hashlib.sha256(
                        projected_payloads["active_runtime_config"]
                    ).hexdigest(),
                ),
            ),
            manifest_metadata=manifest_metadata,
        )
        return bundle, source_catalog, source_payload, source_mode, projected_payloads

    def bind_staged_capability_receipt(
        self,
        bundle: SimpleNamespace,
        candidate: object,
    ) -> SimpleNamespace:
        artifact = getattr(candidate, "capability_receipt")
        payload = getattr(artifact, "payload")
        self.assertIsInstance(payload, bytes)
        staged_path = bundle.staging_root / "capability-receipt.json"
        staged_path.write_bytes(payload)
        staged_path.chmod(0o600)
        bundle.staged_capability_receipt_path = staged_path
        bundle.staged_capability_receipt_payload = payload
        capability_sha256 = hashlib.sha256(payload).hexdigest()
        receipt = json.loads(bundle.receipt_payload)
        receipt["internal_fingerprint"] = {
            "capability_receipt_sha256": capability_sha256,
            "config_sha256s": [
                {"name": name, "sha256": digest}
                for name, digest in bundle.config_sha256s
            ],
        }
        receipt["overlay"] = {
            "path": bundle.manifest_metadata["parity_overlay_path"],
            "sha256": bundle.manifest_metadata["parity_overlay_sha256"],
        }
        receipt_payload = (
            json.dumps(
                receipt,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        bundle.receipt_payload = receipt_payload
        bundle.receipt.canonical_bytes = receipt_payload
        bundle.staged_receipt_path.write_bytes(receipt_payload)
        bundle.manifest_metadata["parity_capability_receipt_sha256"] = (
            capability_sha256
        )
        bundle.manifest_metadata["parity_receipt_sha256"] = hashlib.sha256(
            receipt_payload
        ).hexdigest()
        return bundle

    @contextmanager
    def prepared_parity_rebind(
        self,
        root: Path,
        store: Store,
        *,
        generation: str = "current",
    ) -> object:
        bundle, *_fixture = self.parity_rebind_bundle_fixture(
            root,
            store,
            generation=generation,
        )
        with patch.object(
            bindings_module,
            "prepare_parity_bundle",
            side_effect=lambda candidate, **_kwargs: (
                self.bind_staged_capability_receipt(bundle, candidate)
            ),
        ), patch.object(
            bindings_module,
            "revalidate_parity_bundle_inputs",
            return_value=None,
        ), patch.object(
            bindings_module,
            "revalidate_parity_bundle_immutable_inputs",
            return_value=None,
        ):
            yield bundle

    def internal_update_promotion_fixture(
        self,
        root: Path,
    ) -> SimpleNamespace:
        store, bound, launcher, _binding = self.write_internal_store_fixture(
            root
        )
        self.write_rebind_backend(
            bound,
            runtime_label="old",
            version="0.100.0",
        )
        old_bundle, source_catalog, source_payload, source_mode, _payloads = (
            self.parity_rebind_bundle_fixture(
                root,
                store,
                generation="old",
                config_marker="old",
            )
        )

        def prepare_old_generation(
            candidate: object,
            **_kwargs: object,
        ) -> object:
            return self.bind_staged_capability_receipt(
                old_bundle,
                candidate,
            )

        with patch.object(
            bindings_module,
            "prepare_parity_bundle",
            side_effect=prepare_old_generation,
        ), patch.object(
            bindings_module,
            "revalidate_parity_bundle_inputs",
            return_value=None,
        ), patch.object(
            bindings_module,
            "revalidate_parity_bundle_immutable_inputs",
            return_value=None,
        ), redirect_stdout(io.StringIO()):
            cmd_set_bin(self.rebind_args(store, bound))

        runtime_paths = {
            "manifest": store.manifest_path("internal"),
            "launcher": launcher,
            "capability_receipt": (
                store.bin_dir / "codex-internal-app.capabilities.json"
            ),
            "parity_receipt": (
                store.profile_dir("internal") / "parity" / "receipt.json"
            ),
            "parity_overlay": (
                store.profile_dir("internal")
                / "parity"
                / "model-catalog.json"
            ),
            "profile_config": (
                store.profile_dir("internal") / "config.toml"
            ),
            "shared_config": store.official_codex_home / "config.toml",
            "active_runtime_config": (
                store.internal_codex_home / "config.toml"
            ),
        }
        old_generation = {
            "bound_binary": (
                bound.read_bytes(),
                bound.stat().st_mode & 0o777,
            ),
            **{
                role: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                )
                for role, path in runtime_paths.items()
            },
        }
        bundle, *_new_fixture = self.parity_rebind_bundle_fixture(
            root,
            store,
            generation="new",
            config_marker="new",
        )
        for role, path in runtime_paths.items():
            payload, mode = old_generation[role]
            path.write_bytes(payload)
            path.chmod(mode)
        source_catalog.write_bytes(source_payload)
        source_catalog.chmod(source_mode)
        new_projection_payloads = dict(bundle.config_projection.payloads)
        new_payloads = {
            "parity_overlay": bundle.overlay.overlay_payload,
            "profile_config": new_projection_payloads[
                runtime_paths["profile_config"]
            ],
            "shared_config": new_projection_payloads[
                runtime_paths["shared_config"]
            ],
            "active_runtime_config": (
                bundle.active_runtime_config_payload
            ),
        }
        for role, payload in new_payloads.items():
            self.assertNotEqual(
                old_generation[role][0],
                payload,
                f"{role} fixture generations must differ",
            )

        candidate_dir = bound.parent / ".codex-internal-update-test"
        candidate_dir.mkdir(mode=0o700)
        candidate = self.write_rebind_backend(
            candidate_dir / "codex",
            runtime_label="new",
            version="0.142.4",
        )
        backup = bound.parent / ".codex-internal-backup-test"
        promotion_values = vars(self.rebind_args(store, bound)).copy()
        for key in ("name", "codex_bin", "preserve_app_cli"):
            promotion_values.pop(key)
        args = SimpleNamespace(**promotion_values)
        args.bound_bin = str(bound)
        args.candidate_bin = str(candidate)
        args.backup_bin = str(backup)
        args.target_version = "0.142.4"
        candidate_receipt: dict[str, CapabilityReceipt] = {}

        def prepare_candidate_generation(
            parity_candidate: object,
            **_kwargs: object,
        ) -> object:
            candidate_receipt["value"] = getattr(
                getattr(parity_candidate, "capability_receipt"),
                "receipt",
            )
            prepared = self.bind_staged_capability_receipt(
                bundle,
                parity_candidate,
            )
            canonical_binding = getattr(
                parity_candidate,
                "canonical_internal_binding",
            )
            internal_binding = getattr(
                parity_candidate,
                "internal_binding",
            )
            prepared.receipt.internal_fingerprint = SimpleNamespace(
                backend_cli=canonical_binding.backend_cli,
                binary_sha256=hashlib.sha256(
                    internal_binding.backend_cli.read_bytes()
                ).hexdigest(),
            )
            return prepared

        return SimpleNamespace(
            args=args,
            backup=backup,
            bound=bound,
            bundle=bundle,
            candidate=candidate,
            candidate_receipt=candidate_receipt,
            old_generation=old_generation,
            prepare_candidate_generation=prepare_candidate_generation,
            runtime_paths=runtime_paths,
            store=store,
        )

    def current_internal_runtime_generation(
        self,
        fixture: SimpleNamespace,
    ) -> dict[str, tuple[bytes, int]]:
        return {
            "bound_binary": (
                fixture.bound.read_bytes(),
                fixture.bound.stat().st_mode & 0o777,
            ),
            **{
                role: (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                )
                for role, path in fixture.runtime_paths.items()
            },
        }

    def assert_failed_internal_update_handshake_restores_generation(
        self,
        failure_kind: str,
    ) -> None:
        expected_errors = {
            "version": "injected installed version verification failure",
            "binding": (
                "Canonical Runtime Binding does not match the promoted binary"
            ),
            "app_server": "Promoted internal app-server smoke failed",
            "receipt": (
                "Promoted capability receipt does not match the bound binary"
            ),
            "overlay": (
                "Promoted runtime artifact is invalid: parity_overlay"
            ),
            "config": (
                "Promoted runtime artifact is invalid: profile_config"
            ),
            "parity": (
                "Promoted parity receipt does not match the canonical binding"
            ),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self.internal_update_promotion_fixture(root)
            output = io.StringIO()

            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        bindings_module,
                        "prepare_parity_bundle",
                        side_effect=(
                            fixture.prepare_candidate_generation
                        ),
                    )
                )
                stack.enter_context(
                    patch.object(
                        bindings_module,
                        "revalidate_parity_bundle_inputs",
                        return_value=None,
                    )
                )
                stack.enter_context(
                    patch.object(
                        bindings_module,
                        "revalidate_parity_bundle_immutable_inputs",
                        return_value=None,
                    )
                )

                if failure_kind == "version":
                    real_version_probe = (
                        bindings_module._exact_executable_version
                    )

                    def fail_promoted_version(
                        path: Path,
                        *,
                        expected_version: str,
                        label: str,
                    ) -> str:
                        if label == "Promoted internal binary":
                            raise SwitchError(
                                "injected installed version verification "
                                "failure"
                            )
                        return real_version_probe(
                            path,
                            expected_version=expected_version,
                            label=label,
                        )

                    stack.enter_context(
                        patch.object(
                            bindings_module,
                            "_exact_executable_version",
                            side_effect=fail_promoted_version,
                        )
                    )
                elif failure_kind == "binding":
                    real_resolve_binding = (
                        bindings_module.resolve_store_runtime_binding
                    )

                    def fail_promoted_binding(
                        *args: object,
                        **kwargs: object,
                    ) -> object:
                        binding = real_resolve_binding(*args, **kwargs)
                        profile = args[1] if len(args) > 1 else None
                        if (
                            profile == "internal"
                            and fixture.backup.exists()
                            and not fixture.candidate.exists()
                        ):
                            return replace(
                                binding,
                                backend_cli=fixture.backup,
                            )
                        return binding

                    stack.enter_context(
                        patch.object(
                            bindings_module,
                            "resolve_store_runtime_binding",
                            side_effect=fail_promoted_binding,
                        )
                    )
                elif failure_kind == "app_server":
                    real_smoke = bindings_module.run_app_server_smoke

                    def fail_promoted_smoke(
                        executable: str,
                        *args: object,
                        **kwargs: object,
                    ) -> tuple[int, str]:
                        if Path(executable) == fixture.runtime_paths["launcher"]:
                            return 1, "injected app-server handshake failure"
                        return real_smoke(executable, *args, **kwargs)

                    stack.enter_context(
                        patch.object(
                            bindings_module,
                            "run_app_server_smoke",
                            side_effect=fail_promoted_smoke,
                        )
                    )
                elif failure_kind in {"overlay", "config"}:
                    role = (
                        "parity_overlay"
                        if failure_kind == "overlay"
                        else "profile_config"
                    )
                    target = fixture.runtime_paths[role]
                    real_read_bytes = Path.read_bytes

                    def fail_promoted_artifact_read(path: Path) -> bytes:
                        payload = real_read_bytes(path)
                        if (
                            path == target
                            and fixture.backup.exists()
                            and not fixture.candidate.exists()
                        ):
                            return payload + b"\ninjected-handshake-drift\n"
                        return payload

                    stack.enter_context(
                        patch.object(
                            Path,
                            "read_bytes",
                            autospec=True,
                            side_effect=fail_promoted_artifact_read,
                        )
                    )
                else:
                    real_identity = (
                        bindings_module._stable_executable_identity
                    )
                    injected = False

                    def inject_handshake_failure(
                        path: Path,
                        *,
                        label: str,
                    ) -> tuple[Path, int, str]:
                        nonlocal injected
                        identity = real_identity(path, label=label)
                        if (
                            label == "Last-known-good internal backup"
                            and not injected
                        ):
                            injected = True
                            if failure_kind == "parity":
                                receipt = fixture.bundle.receipt
                                receipt.internal_fingerprint = (
                                    SimpleNamespace(
                                        backend_cli=fixture.bound,
                                        binary_sha256="0" * 64,
                                    )
                                )
                            elif failure_kind == "receipt":
                                receipt = fixture.candidate_receipt["value"]
                                object.__setattr__(
                                    receipt,
                                    "backend_sha256",
                                    "0" * 64,
                                )
                        return identity

                    stack.enter_context(
                        patch.object(
                            bindings_module,
                            "_stable_executable_identity",
                            side_effect=inject_handshake_failure,
                        )
                    )

                with self.assertRaises(SwitchError) as raised, redirect_stdout(
                    output
                ):
                    cmd_promote_internal_update(fixture.args)

            self.assertIn(
                expected_errors[failure_kind],
                str(raised.exception),
            )
            self.assertNotIn(
                "update-internal: verified installed version",
                output.getvalue(),
            )
            self.assertNotIn("Restart required", output.getvalue())
            current_generation = self.current_internal_runtime_generation(
                fixture
            )
            unrestored = sorted(
                role
                for role, expected in fixture.old_generation.items()
                if current_generation.get(role) != expected
            )
            self.assertEqual(
                [],
                unrestored,
                (
                    f"{failure_kind} handshake failure must restore the old "
                    "binary and complete runtime bundle"
                ),
            )
            self.assertFalse(
                (fixture.store.root / ".runtime-binding-rebind.json").exists()
            )
            self.assertFalse(fixture.backup.exists())
            self.assertTrue(fixture.candidate.is_file())

    def run_runtime_contract(
        self,
        executable: Path,
        *,
        marker: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in (
            "CODEX_SWITCH_CAPABILITY_RECEIPT",
            "CODEX_SWITCH_EXPECTED_SCHEMA_SHA256",
            "CODEX_SWITCH_EXPECTED_RECEIPT_SHA256",
        ):
            environment.pop(name, None)
        if marker is not None:
            environment["RUNTIME_CONTRACT_MARKER"] = str(marker)
        return subprocess.run(
            [str(executable), "--runtime-contract"],
            input="",
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )

    def test_discovery_accepts_current_chatgpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            main, cli = self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )

            inventory = discover_desktop_hosts(roots)

            self.assertIsNotNone(inventory.current)
            assert inventory.current is not None
            self.assertEqual(inventory.current.kind, "chatgpt")
            self.assertEqual(inventory.current.main_executable, main)
            self.assertEqual(inventory.current.bundled_cli, cli)
            self.assertTrue(inventory.current.healthy)
            self.assertFalse(inventory.current.migration_only)
            self.assertNotIn(
                "desktop.current.invalid",
                {finding.code for finding in inventory.findings},
            )

    def test_discovery_rejects_current_chatgpt_with_wrong_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            roots = self.make_roots(Path(temp_dir))
            self.write_bundle(
                roots.chatgpt,
                bundle_id="com.openai.chat",
                main_name="ChatGPT",
            )

            inventory = discover_desktop_hosts(roots)

            self.assertIsNone(inventory.current)
            self.assertIn(
                "desktop.current.bundle_id_mismatch",
                {finding.code for finding in inventory.findings},
            )

    def test_discovery_rejects_current_chatgpt_without_main_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            roots = self.make_roots(Path(temp_dir))
            self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
                include_main=False,
            )

            inventory = discover_desktop_hosts(roots)

            self.assertIsNone(inventory.current)
            self.assertIn(
                "desktop.current.main_invalid",
                {finding.code for finding in inventory.findings},
            )

    def test_discovery_rejects_current_chatgpt_without_bundled_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            roots = self.make_roots(Path(temp_dir))
            self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
                include_cli=False,
            )

            inventory = discover_desktop_hosts(roots)

            self.assertIsNone(inventory.current)
            self.assertIn(
                "desktop.current.cli_invalid",
                {finding.code for finding in inventory.findings},
            )

    def test_discovery_rejects_directory_and_non_executable_members(self) -> None:
        cases = (
            "main-directory",
            "main-non-executable",
            "main-symlink",
            "cli-directory",
            "cli-non-executable",
            "cli-symlink",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                roots = self.make_roots(root)
                main, cli = self.write_bundle(
                    roots.chatgpt,
                    bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                    main_name="ChatGPT",
                )
                target = main if case.startswith("main") else cli
                if case.endswith("directory"):
                    target.unlink()
                    target.mkdir()
                elif case.endswith("symlink"):
                    replacement = write_executable(root / f"{case}-target")
                    target.unlink()
                    target.symlink_to(replacement)
                else:
                    target.chmod(0o644)

                inventory = discover_desktop_hosts(roots)

                self.assertIsNone(inventory.current)
                expected = (
                    "desktop.current.main_invalid"
                    if case.startswith("main")
                    else "desktop.current.cli_invalid"
                )
                self.assertIn(expected, {finding.code for finding in inventory.findings})

    def test_discovery_excludes_chatgpt_classic_even_with_codex_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            roots = self.make_roots(Path(temp_dir))
            self.write_bundle(
                roots.chatgpt_classic,
                bundle_id="com.openai.chat",
                main_name="ChatGPT",
            )

            inventory = discover_desktop_hosts(roots)

            self.assertIsNone(inventory.current)
            self.assertEqual(len(inventory.excluded), 1)
            self.assertEqual(inventory.excluded[0].kind, "chatgpt-classic")
            self.assertIn(
                "desktop.classic.excluded",
                {finding.code for finding in inventory.findings},
            )

    def test_discovery_marks_legacy_codex_as_migration_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            roots = self.make_roots(Path(temp_dir))
            self.write_bundle(
                roots.legacy_codex,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="Codex",
            )

            inventory = discover_desktop_hosts(roots)

            self.assertIsNone(inventory.current)
            self.assertEqual(len(inventory.legacy), 1)
            self.assertTrue(inventory.legacy[0].migration_only)
            self.assertFalse(inventory.legacy[0].healthy)
            self.assertIn(
                "desktop.legacy.migration_only",
                {finding.code for finding in inventory.findings},
            )

    def test_discovery_current_chatgpt_wins_over_legacy_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            roots = self.make_roots(Path(temp_dir))
            self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            self.write_bundle(
                roots.legacy_codex,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="Codex",
            )

            inventory = discover_desktop_hosts(roots)

            self.assertIsNotNone(inventory.current)
            assert inventory.current is not None
            self.assertEqual(inventory.current.bundle_root, roots.chatgpt)
            self.assertEqual(len(inventory.legacy), 1)

    def test_official_resolution_fails_closed_without_verified_current_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            self.write_bundle(
                roots.legacy_codex,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="Codex",
            )
            inventory = discover_desktop_hosts(roots)
            context = self.make_context(root, "openai-official", {})

            with self.assertRaises(RuntimeBindingError) as raised:
                resolve_runtime_binding(context, inventory)

            self.assertEqual(raised.exception.code, "binding.official.current_host_unavailable")

    def test_official_resolution_uses_bundled_cli_for_full_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            main, cli = self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            inventory = discover_desktop_hosts(roots)
            context = self.make_context(
                root,
                "openai-official",
                {"codex_bin": "/stale/official", "app_cli_path": "/stale/app"},
            )

            binding = resolve_runtime_binding(context, inventory)

            self.assertEqual(binding.profile, "openai-official")
            self.assertEqual(binding.shell_cli, cli)
            self.assertEqual(binding.desktop_cli, cli)
            self.assertEqual(binding.backend_cli, cli)
            self.assertEqual(binding.desktop_host.main_executable, main)
            self.assertFalse(binding.requires_proxy)

    def test_official_alias_normalizes_without_broadening_other_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            inventory = discover_desktop_hosts(roots)

            binding = resolve_runtime_binding(
                self.make_context(root, "official", {}), inventory
            )
            self.assertEqual(binding.profile, "openai-official")

            with self.assertRaises(RuntimeBindingError) as raised:
                resolve_runtime_binding(self.make_context(root, "preview", {}), inventory)
            self.assertEqual(raised.exception.code, "binding.profile.unsupported")

    def test_official_resolution_rejects_managed_shim_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            context = self.make_context(
                root,
                "openai-official",
                {},
                path_codex=root / "store" / "bin" / "codex",
            )

            with self.assertRaises(RuntimeBindingError) as raised:
                resolve_runtime_binding(context, discover_desktop_hosts(roots))

            self.assertEqual(raised.exception.code, "binding.official.managed_shim_rejected")

    def test_internal_resolution_uses_managed_launcher_and_manifest_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            context = self.make_context(
                root,
                "internal",
                {"codex_bin": str(backend), "app_cli_path": str(backend)},
            )

            binding = resolve_runtime_binding(
                context, discover_desktop_hosts(self.make_roots(root))
            )

            launcher = root / "store" / "bin" / "codex-internal-app"
            self.assertEqual(binding.shell_cli, backend.resolve())
            self.assertEqual(binding.desktop_cli, launcher)
            self.assertEqual(binding.backend_cli, backend.resolve())
            self.assertTrue(binding.requires_proxy)
            self.assertIsNone(binding.desktop_host)

    def test_internal_raw_app_path_is_migration_drift_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            context = self.make_context(
                root,
                "internal",
                {"codex_bin": str(backend), "app_cli_path": str(backend)},
            )

            binding = resolve_runtime_binding(
                context, discover_desktop_hosts(self.make_roots(root))
            )

            self.assertNotEqual(binding.desktop_cli, backend)
            self.assertIn(
                "binding.internal.raw_app_cli_migration_drift",
                {finding.code for finding in binding.findings},
            )

    def test_internal_resolution_rejects_invalid_backends(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directory = root / "directory"
            directory.mkdir()
            non_executable = root / "non-executable"
            non_executable.write_text("not executable")
            cases = {
                "missing": root / "missing",
                "relative": Path("relative-codex"),
                "directory": directory,
                "non-executable": non_executable,
            }
            for label, backend in cases.items():
                with self.subTest(label=label):
                    context = self.make_context(
                        root,
                        "internal",
                        {"codex_bin": str(backend)},
                    )
                    with self.assertRaises(RuntimeBindingError) as raised:
                        resolve_runtime_binding(
                            context, discover_desktop_hosts(self.make_roots(root))
                        )
                    self.assertEqual(raised.exception.code, "binding.internal.backend_invalid")

    def test_internal_resolution_rejects_recursive_managed_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bin_dir = root / "store" / "bin"
            recursive_paths = (
                write_executable(bin_dir / "codex"),
                write_executable(bin_dir / "codex-internal-app"),
                write_executable(bin_dir / "nested" / "backend"),
            )
            for backend in recursive_paths:
                with self.subTest(backend=backend):
                    context = self.make_context(
                        root,
                        "internal",
                        {"codex_bin": str(backend)},
                    )
                    with self.assertRaises(RuntimeBindingError) as raised:
                        resolve_runtime_binding(
                            context, discover_desktop_hosts(self.make_roots(root))
                        )
                    self.assertEqual(
                        raised.exception.code, "binding.internal.recursive_backend"
                    )

    def test_internal_resolution_rejects_invalid_launcher_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            context = self.make_context(
                root,
                "internal",
                {
                    "codex_bin": str(backend),
                    "app_launcher_sha256": "NOT-A-SHA256",
                },
            )

            with self.assertRaises(RuntimeBindingError) as raised:
                resolve_runtime_binding(
                    context, discover_desktop_hosts(self.make_roots(root))
                )

            self.assertEqual(
                raised.exception.code,
                "binding.internal.launcher_fingerprint_invalid",
            )

    def test_stale_active_record_is_not_binding_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            stale = write_executable(root / "stale" / "codex")
            context = self.make_context(
                root,
                "internal",
                {"codex_bin": str(backend), "app_cli_path": str(backend)},
                active_record={
                    "profile": "internal",
                    "shell_cli_path": str(stale),
                    "app_cli_path": str(stale),
                },
            )

            binding = resolve_runtime_binding(
                context, discover_desktop_hosts(self.make_roots(root))
            )

            self.assertEqual(binding.backend_cli, backend.resolve())
            self.assertEqual(
                binding.desktop_cli, root / "store" / "bin" / "codex-internal-app"
            )
            self.assertIn(
                "binding.observation.active_stale",
                {finding.code for finding in binding.findings},
            )

    def test_app_server_parser_accepts_global_config_before_subcommand(self) -> None:
        command = (
            "/Applications/ChatGPT.app/Contents/Resources/codex "
            "-c dynamic_tools=true app-server --analytics-default-enabled"
        )

        self.assertEqual(
            app_server_command_path(command),
            "/Applications/ChatGPT.app/Contents/Resources/codex",
        )

    def test_app_server_parser_accepts_multiple_global_options(self) -> None:
        command = (
            "/Applications/ChatGPT.app/Contents/Resources/codex "
            "--model gpt-5.6 -c model_reasoning_effort=high "
            "--enable dynamic_tools app-server --analytics-default-enabled"
        )

        self.assertEqual(
            app_server_command_path(command),
            "/Applications/ChatGPT.app/Contents/Resources/codex",
        )

    def test_app_server_parser_rejects_exec_payload_mentions(self) -> None:
        commands = (
            "/usr/local/bin/codex exec --json 'please run app-server --stdio'",
            "/bin/sh -c 'echo codex app-server --stdio'",
            "/usr/bin/node app-server --analytics-default-enabled",
        )

        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(app_server_command_path(command), "")

    def test_running_desktop_uses_host_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            main, _cli = self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            inventory = discover_desktop_hosts(roots)

            observations = running_codex_processes(
                inventory=inventory,
                process_output=f"101 1 {main}\n",
                env_reader=lambda _pid: "/tmp/managed-app-cli",
            )

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].kind, "desktop")
            self.assertEqual(observations[0].command_path, str(main))
            self.assertEqual(observations[0].host_kind, "chatgpt")

    def test_attestation_accepts_official_direct_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            main, cli = self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            binding = resolve_runtime_binding(
                self.make_context(root, "openai-official", {}),
                discover_desktop_hosts(roots),
            )
            observation = RuntimeObservation(
                processes=(
                    RunningCodexProcess(
                        pid=10,
                        ppid=1,
                        kind="desktop",
                        command_path=str(main),
                        app_cli_env=str(cli),
                        host_kind="chatgpt",
                    ),
                    RunningCodexProcess(
                        pid=11,
                        ppid=10,
                        kind="app-server",
                        command_path=str(cli),
                        app_cli_env=str(cli),
                        parent_command=str(main),
                    ),
                ),
                gui_app_cli=str(cli),
                launch_agent_cli=str(cli),
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertTrue(attestation.healthy)
            self.assertEqual([], [f.code for f in attestation.findings if f.severity == "error"])

    def test_attestation_accepts_internal_proxy_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            launcher = root / "store" / "bin" / "codex-internal-app"
            binding = resolve_runtime_binding(
                self.make_context(
                    root,
                    "internal",
                    {"codex_bin": str(backend), "app_cli_path": str(launcher)},
                ),
                discover_desktop_hosts(self.make_roots(root)),
            )
            observation = RuntimeObservation(
                processes=(
                    RunningCodexProcess(
                        pid=20,
                        ppid=1,
                        kind="desktop",
                        command_path="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
                        app_cli_env=str(launcher),
                        host_kind="chatgpt",
                    ),
                    RunningCodexProcess(
                        pid=21,
                        ppid=20,
                        kind="app-server",
                        command_path=str(backend),
                        app_cli_env=str(launcher),
                        parent_command=f"python3 codex_switch_app_proxy.py {backend} app-server",
                    ),
                ),
                gui_app_cli=str(launcher),
                launch_agent_cli=str(launcher),
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertTrue(attestation.healthy)
            self.assertEqual([], [f.code for f in attestation.findings if f.severity == "error"])

    def test_attestation_rejects_correct_launcher_with_wrong_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            stale_backend = write_executable(root / "stale" / "codex")
            launcher = root / "store" / "bin" / "codex-internal-app"
            binding = resolve_runtime_binding(
                self.make_context(root, "internal", {"codex_bin": str(backend)}),
                discover_desktop_hosts(self.make_roots(root)),
            )
            observation = RuntimeObservation(
                processes=(
                    RunningCodexProcess(
                        pid=31,
                        ppid=30,
                        kind="app-server",
                        command_path=str(stale_backend),
                        app_cli_env=str(launcher),
                        parent_command=(
                            f"python3 codex_switch_app_proxy.py {stale_backend} app-server"
                        ),
                    ),
                ),
                gui_app_cli=str(launcher),
                launch_agent_cli=str(launcher),
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertFalse(attestation.healthy)
            self.assertIn(
                "attestation.app_server.backend_mismatch",
                {finding.code for finding in attestation.findings},
            )

    def test_attestation_rejects_backend_without_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            launcher = root / "store" / "bin" / "codex-internal-app"
            binding = resolve_runtime_binding(
                self.make_context(root, "internal", {"codex_bin": str(backend)}),
                discover_desktop_hosts(self.make_roots(root)),
            )
            observation = RuntimeObservation(
                processes=(
                    RunningCodexProcess(
                        pid=41,
                        ppid=40,
                        kind="app-server",
                        command_path=str(backend),
                        app_cli_env=str(launcher),
                        parent_command="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
                    ),
                ),
                gui_app_cli=str(launcher),
                launch_agent_cli=str(launcher),
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertFalse(attestation.healthy)
            self.assertIn(
                "attestation.internal.proxy_bypass",
                {finding.code for finding in attestation.findings},
            )

    def test_attestation_rejects_stale_launch_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            launcher = root / "store" / "bin" / "codex-internal-app"
            binding = resolve_runtime_binding(
                self.make_context(root, "internal", {"codex_bin": str(backend)}),
                discover_desktop_hosts(self.make_roots(root)),
            )
            observation = RuntimeObservation(
                gui_app_cli=str(launcher),
                launch_agent_cli=str(root / "stale-launcher"),
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertFalse(attestation.healthy)
            self.assertIn(
                "attestation.launch_agent.cli_mismatch",
                {finding.code for finding in attestation.findings},
            )

    def test_attestation_rejects_unset_gui_env_for_running_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            binding = resolve_runtime_binding(
                self.make_context(root, "internal", {"codex_bin": str(backend)}),
                discover_desktop_hosts(self.make_roots(root)),
            )
            observation = RuntimeObservation(
                processes=(
                    RunningCodexProcess(
                        pid=50,
                        ppid=1,
                        kind="desktop",
                        command_path="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
                        app_cli_env="",
                        host_kind="chatgpt",
                    ),
                ),
                gui_app_cli="",
                launch_agent_cli=str(binding.desktop_cli),
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertFalse(attestation.healthy)
            self.assertIn(
                "attestation.gui_env.unset",
                {finding.code for finding in attestation.findings},
            )

    def test_attestation_rejects_running_legacy_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            roots = self.make_roots(root)
            main, cli = self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            legacy_main, _legacy_cli = self.write_bundle(
                roots.legacy_codex,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="Codex",
            )
            binding = resolve_runtime_binding(
                self.make_context(root, "openai-official", {}),
                discover_desktop_hosts(roots),
            )
            observation = RuntimeObservation(
                processes=(
                    RunningCodexProcess(
                        pid=60,
                        ppid=1,
                        kind="desktop",
                        command_path=str(legacy_main),
                        app_cli_env=str(cli),
                        host_kind="legacy-codex",
                    ),
                ),
                gui_app_cli=str(cli),
                launch_agent_cli=str(cli),
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertFalse(attestation.healthy)
            self.assertIn(
                "attestation.desktop.legacy_running",
                {finding.code for finding in attestation.findings},
            )
            self.assertNotEqual(str(main), str(legacy_main))

    def test_attestation_rejects_managed_launcher_fingerprint_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_executable(root / "internal" / "codex")
            binding = replace(
                resolve_runtime_binding(
                    self.make_context(root, "internal", {"codex_bin": str(backend)}),
                    discover_desktop_hosts(self.make_roots(root)),
                ),
                launcher_fingerprint="expected-fingerprint",
            )
            observation = RuntimeObservation(
                gui_app_cli=str(binding.desktop_cli),
                launch_agent_cli=str(binding.desktop_cli),
                managed_launcher_fingerprint="stale-fingerprint",
            )

            attestation = attest_runtime_binding(binding, observation)

            self.assertFalse(attestation.healthy)
            self.assertIn(
                "attestation.internal.launcher_fingerprint_mismatch",
                {finding.code for finding in attestation.findings},
            )

    def test_diagnostic_process_inventory_is_collected_once(self) -> None:
        store_root = Path("/tmp/codex-switch-runtime-observation-store")
        store = Store(
            root=store_root,
            live_codex_home=Path("/tmp/codex-switch-runtime-observation-live"),
            launch_agent_path=Path("/tmp/codex-switch-runtime-observation-agent"),
            launch_agent_label="test",
        )
        expected = "/tmp/codex-switch-runtime-observation-cli"
        process = RunningCodexProcess(
            pid=90,
            ppid=1,
            kind="app-server",
            command_path=expected,
            app_cli_env=expected,
        )

        with patch(
            "codex_switch_running_app.running_codex_processes",
            return_value=[process],
        ) as inventory_scan:
            observation = collect_runtime_observation(
                inventory=DesktopInventory(current=None),
                gui_app_cli=expected,
                launch_agent_cli=expected,
            )
            verify_problems = running_desktop_problems(
                store,
                "internal",
                expected,
                runtime_observation=observation,
                enforce_default_context=False,
            )
            doctor_problems = running_desktop_problems(
                store,
                "internal",
                expected,
                runtime_observation=observation,
                enforce_default_context=False,
            )
            with redirect_stdout(io.StringIO()):
                print_running_desktop_status(
                    store,
                    expected,
                    runtime_observation=observation,
                    enforce_default_context=False,
                )

        self.assertEqual([], verify_problems)
        self.assertEqual([], doctor_problems)
        self.assertEqual(inventory_scan.call_count, 1)

    def test_init_defaults_official_to_chatgpt_bundled_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            roots = self.make_roots(root)
            _main, bundled_cli = self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            args = SimpleNamespace(
                store_dir=store.root,
                official_codex_home=store.official_codex_home,
                official_codex_home_source="explicit",
                internal_codex_home=store.internal_codex_home,
                internal_codex_home_source="explicit",
                launch_agent_path=store.launch_agent_path,
                launch_agent_label=store.launch_agent_label,
                codex_bin=None,
                app_cli_path=None,
                capture_current=None,
                overwrite_capture=False,
                desktop_inventory=discover_desktop_hosts(roots),
            )

            with redirect_stdout(io.StringIO()):
                cmd_init(args)

            manifest = store.load_manifest("openai-official")
            self.assertEqual(manifest["codex_bin"], str(bundled_cli))
            self.assertEqual(manifest["app_cli_path"], str(bundled_cli))

    def test_capture_internal_records_managed_launcher_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            source_home = root / "source-home"
            source_home.mkdir()
            (source_home / "config.toml").write_text('profile = "internal"\n')
            backend = write_executable(root / "internal-bin" / "codex")

            with redirect_stdout(io.StringIO()):
                capture_profile(
                    store=store,
                    name="internal",
                    source_home=source_home,
                    codex_bin=str(backend),
                    app_cli_path=str(backend),
                    allow_missing_auth=True,
                    overwrite=False,
                )

            manifest = store.load_manifest("internal")
            self.assertEqual(manifest["codex_bin"], str(backend.resolve()))
            self.assertEqual(
                manifest["app_cli_path"], str(store.bin_dir / "codex-internal-app")
            )

    def test_fresh_internal_one_key_requires_explicit_parity_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "live"
            live.mkdir()
            (live / "config.toml").write_text('profile = "internal"\n')
            backend = write_executable(root / "path" / "codex")
            official = write_executable(root / "official" / "codex")
            env = dict(os.environ)
            env["PATH"] = f"{backend.parent}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP"] = "1"
            wrapper = Path(__file__).with_name("codex-switch")
            common = [
                str(wrapper),
                "--store-dir",
                str(root / "store"),
                "--live-codex-home",
                str(live),
                "--launch-agent-path",
                str(root / "agent.plist"),
            ]
            init = subprocess.run(
                [
                    *common,
                    "init",
                    "--app-cli-path",
                    str(official),
                    "--capture-current",
                    "internal",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)

            result = subprocess.run(
                [
                    *common,
                    "--skip-self-update",
                    "internal",
                    "--skip-update-check",
                    "--skip-plugin-repair",
                    "--skip-verify",
                    "--no-status",
                    "--skip-launchctl",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1, output)
            self.assertIn("Switched to profile internal", output)
            self.assertIn("Parity health: unhealthy", output)
            self.assertIn(
                "Parity finding: parity.receipt.missing",
                output,
            )
            self.assertIn("Outcome: ACTION REQUIRED", output)
            self.assertIn("Switch: succeeded", output)
            self.assertIn("Doctor: failed (exit 1)", output)
            manifest = json.loads(
                (root / "store" / "profiles" / "internal" / "manifest.json").read_text()
            )
            self.assertEqual(
                manifest["app_cli_path"],
                str(root / "store" / "bin" / "codex-internal-app"),
            )
            self.assertNotIn("parity_receipt_path", manifest)
            self.assertFalse(
                (root / "store" / "profiles" / "internal" / "parity").exists()
            )

    def test_status_without_gui_env_prints_expected_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, launcher, binding = self.write_internal_store_fixture(root)
            output = io.StringIO()

            with redirect_stdout(output):
                print_app_codex_status(
                    store,
                    "internal",
                    runtime_observation=RuntimeObservation(),
                )

            rendered = output.getvalue()
            self.assertIn("GUI CODEX_CLI_PATH: <unset>", rendered)
            self.assertIn(f"Expected Desktop CLI: {launcher}", rendered)

    def test_split_status_reports_separate_cli_and_app_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                store,
                backend,
                official_cli,
                internal_binding,
                official_binding,
                _observation,
            ) = self.write_split_store_fixture(root)
            output = io.StringIO()

            def resolve_binding(_store, profile, **_kwargs):
                return (
                    internal_binding
                    if profile == "internal"
                    else official_binding
                )

            with (
                patch.object(
                    status_active_module,
                    "resolve_store_runtime_binding",
                    side_effect=resolve_binding,
                ),
                redirect_stdout(output),
            ):
                selection = status_active_module.print_active_profile_status(store)

            self.assertIsNotNone(selection)
            self.assertEqual("internal", selection.cli_profile)
            self.assertEqual("openai-official", selection.app_profile)
            rendered = output.getvalue()
            self.assertIn("CLI profile: internal", rendered)
            self.assertIn("App profile: openai-official", rendered)
            self.assertIn(f"Active configured CLI: {backend}", rendered)
            self.assertIn(f"Active configured App CLI: {official_cli}", rendered)

    def test_split_doctor_uses_official_app_binding_without_cross_surface_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                store,
                _backend,
                _official_cli,
                internal_binding,
                official_binding,
                observation,
            ) = self.write_split_store_fixture(root)

            def resolve_binding(_store, profile, **_kwargs):
                return (
                    internal_binding
                    if profile == "internal"
                    else official_binding
                )

            with patch.object(
                doctor_module,
                "resolve_store_runtime_binding",
                side_effect=resolve_binding,
            ) as resolve_doctor:
                observed_binding = doctor_module.active_runtime_binding_for_observation(
                    store
                )
            self.assertEqual("openai-official", observed_binding.profile)
            self.assertEqual(
                "openai-official",
                resolve_doctor.call_args.args[1],
            )

            with patch.object(
                doctor_active_module,
                "resolve_store_runtime_binding",
                side_effect=resolve_binding,
            ):
                problems = active_profile_problems(
                    store,
                    runtime_observation=observation,
                )
            self.assertEqual([], problems)

    def test_split_doctor_reports_only_the_drifted_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                store,
                _backend,
                _official_cli,
                internal_binding,
                official_binding,
                observation,
            ) = self.write_split_store_fixture(root)

            def resolve_binding(_store, profile, **_kwargs):
                return (
                    internal_binding
                    if profile == "internal"
                    else official_binding
                )

            shim = store.bin_dir / "codex"
            shim.write_text("#!/bin/sh\nexit 42\n")
            shim.chmod(0o755)
            with patch.object(
                doctor_active_module,
                "resolve_store_runtime_binding",
                side_effect=resolve_binding,
            ):
                shell_problems = active_profile_problems(
                    store,
                    runtime_observation=observation,
                )
            self.assertTrue(
                any("switch shim mismatch" in problem for problem in shell_problems),
                shell_problems,
            )
            self.assertFalse(
                any("attestation." in problem for problem in shell_problems),
                shell_problems,
            )

            home = store.internal_codex_home
            assert home is not None
            write_codex_shim(
                store,
                str(internal_binding.shell_cli),
                home,
                profile_name="internal",
            )
            app_drift = RuntimeObservation(
                gui_app_cli=str(root / "wrong-app"),
                launch_agent_cli=str(root / "wrong-app"),
            )
            with patch.object(
                doctor_active_module,
                "resolve_store_runtime_binding",
                side_effect=resolve_binding,
            ):
                app_problems = active_profile_problems(
                    store,
                    runtime_observation=app_drift,
                )
            self.assertTrue(
                any("attestation." in problem for problem in app_problems),
                app_problems,
            )
            self.assertFalse(
                any("switch shim mismatch" in problem for problem in app_problems),
                app_problems,
            )

    def test_split_doctor_rejects_active_cli_home_and_matching_shim_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                store,
                _backend,
                _official_cli,
                internal_binding,
                official_binding,
                observation,
            ) = self.write_split_store_fixture(root)
            drift_home = root / "drifted-internal-home"
            drift_home.mkdir()
            active = json.loads(store.active_path.read_text())
            active["codex_home"] = str(drift_home)
            store.active_path.write_text(json.dumps(active))
            write_codex_shim(
                store,
                str(internal_binding.shell_cli),
                drift_home,
                profile_name="internal",
            )

            def resolve_binding(_store, profile, **_kwargs):
                return (
                    internal_binding
                    if profile == "internal"
                    else official_binding
                )

            with patch.object(
                doctor_active_module,
                "resolve_store_runtime_binding",
                side_effect=resolve_binding,
            ):
                problems = active_profile_problems(
                    store,
                    runtime_observation=observation,
                )

            self.assertTrue(
                any(
                    "recorded CODEX_HOME" in problem
                    and str(internal_binding.codex_home) in problem
                    for problem in problems
                ),
                problems,
            )

    def test_malformed_split_active_selection_is_reported_stably(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, _launcher, _binding = self.write_internal_store_fixture(
                root
            )
            active = json.loads(store.active_path.read_text())
            active["cli_profile"] = "internal"
            store.active_path.write_text(json.dumps(active))
            output = io.StringIO()

            with redirect_stdout(output):
                selection = status_active_module.print_active_profile_status(store)

            self.assertIsNone(selection)
            self.assertIn("active.selection.partial", output.getvalue())
            problems = active_profile_problems(store)
            self.assertTrue(
                any("active.selection.partial" in problem for problem in problems),
                problems,
            )

    def test_status_doctor_and_verify_share_finding_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, launcher, binding = self.write_internal_store_fixture(root)
            observation = RuntimeObservation(
                gui_app_cli=str(launcher),
                launch_agent_cli=str(root / "stale-launcher"),
            )
            output = io.StringIO()

            with redirect_stdout(output):
                print_app_codex_status(
                    store,
                    "internal",
                    runtime_observation=observation,
                )
            doctor = active_profile_problems(
                store,
                runtime_observation=observation,
            )
            home = store.internal_codex_home
            assert home is not None
            verify = collect_active_state_problems(
                store,
                "internal",
                home,
                runtime_observation=observation,
            )

            finding = "attestation.launch_agent.cli_mismatch"
            self.assertIn(finding, output.getvalue())
            self.assertTrue(any(finding in problem for problem in doctor), doctor)
            self.assertTrue(any(finding in problem for problem in verify), verify)

    def test_verify_manifest_expectation_wins_over_stale_active_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, backend, launcher, _binding = self.write_internal_store_fixture(root)
            stale = write_executable(root / "stale" / "codex")
            active = json.loads(store.active_path.read_text())
            active["shell_cli_path"] = str(stale)
            active["app_cli_path"] = str(stale)
            store.active_path.write_text(json.dumps(active))
            binding = resolve_runtime_binding(
                self.make_context(
                    root,
                    "internal",
                    store.load_manifest("internal"),
                    active_record=active,
                ),
                DesktopInventory(current=None),
            )
            observation = RuntimeObservation(
                gui_app_cli=str(launcher),
                launch_agent_cli=str(launcher),
                processes=(
                    RunningCodexProcess(
                        pid=100,
                        ppid=99,
                        kind="app-server",
                        command_path=str(backend),
                        app_cli_env=str(launcher),
                        parent_command=f"python3 codex_switch_app_proxy.py {backend}",
                    ),
                ),
            )
            home = store.internal_codex_home
            assert home is not None

            problems = collect_active_state_problems(
                store,
                "internal",
                home,
                runtime_observation=observation,
            )

            self.assertTrue(
                any("binding.observation.active_stale" in problem for problem in problems),
                problems,
            )
            self.assertFalse(
                any(f"expected {stale}" in problem for problem in problems),
                problems,
            )

    def test_initialize_error_fails_app_server_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            home.mkdir()
            backend = write_executable(
                root / "codex",
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for raw in sys.stdin:\n"
                "    message = json.loads(raw)\n"
                "    if message.get('method') == 'initialize':\n"
                "        print(json.dumps({'id': '__codex_initialize__', 'error': {'code': -32000, 'message': 'rejected'}}), flush=True)\n"
                "    elif message.get('method') == 'plugin/list':\n"
                "        print(json.dumps({'id': 'plugin-list-smoke', 'result': {'marketplaces': []}}), flush=True)\n",
            )

            code, output = run_app_server_smoke(str(backend), home)

            self.assertNotEqual(code, 0)
            self.assertIn("initialize", output)
            self.assertIn("error", output)

    def test_internal_snapshot_switch_uses_canonical_managed_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "live"
            live.mkdir()
            (live / "config.toml").write_text('profile = "internal"\n')
            backend = write_executable(root / "path" / "codex")
            official = write_executable(root / "official" / "codex")
            env = dict(os.environ)
            env["PATH"] = f"{backend.parent}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP"] = "1"
            script = Path(__file__).with_name("codex_profile_switch.py")
            common = [
                self.supported_python_for_runtime_test(),
                str(script),
                "--store-dir",
                str(root / "store"),
                "--live-codex-home",
                str(live),
                "--launch-agent-path",
                str(root / "agent.plist"),
            ]
            init = subprocess.run(
                [
                    *common,
                    "init",
                    "--app-cli-path",
                    str(official),
                    "--capture-current",
                    "internal",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)

            result = subprocess.run(
                [
                    *common,
                    "switch",
                    "internal",
                    "--config-mode",
                    "snapshot",
                    "--skip-launchctl",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            expected = root / "store" / "bin" / "codex-internal-app"
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["app_cli_path"], str(expected))
            self.assertTrue(expected.exists())
            self.assertIn("codex_switch_app_proxy.py", expected.read_text())
            manifest = json.loads(
                (root / "store" / "profiles" / "internal" / "manifest.json").read_text()
            )
            self.assertEqual(
                manifest["app_launcher_sha256"],
                hashlib.sha256(expected.read_bytes()).hexdigest(),
            )

    def test_canonical_official_switch_repairs_manifest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            roots = self.make_roots(root)
            _main, bundled_cli = self.write_bundle(
                roots.chatgpt,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                main_name="ChatGPT",
            )
            stale = write_executable(root / "stale" / "codex")
            official_home = store.official_codex_home
            official_home.mkdir(parents=True)
            (official_home / "config.toml").write_text('profile = "official"\n')
            profile = store.profile_dir("openai-official")
            profile.mkdir(parents=True)
            (profile / "config.toml").write_text(
                'cli_auth_credentials_store = "file"\n'
            )
            (profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "openai-official",
                        "codex_bin": str(stale),
                        "app_cli_path": str(stale),
                        "runtime_binding": "canonical",
                    }
                )
            )
            inventory = discover_desktop_hosts(roots)
            tomllib_parser = self.tomllib_parser_for_runtime_test()
            shell_profile = root / ".zshrc"

            with patch.dict(
                os.environ,
                {"CODEX_SWITCH_SHELL_PROFILE": str(shell_profile)},
            ), patch(
                "codex_switch_runtime_binding.discover_desktop_hosts",
                return_value=inventory,
            ), patch(
                "codex_switch_toml_validate.tomllib",
                tomllib_parser,
            ), patch(
                "codex_switch_config_document.tomllib",
                tomllib_parser,
            ):
                switch_profile(
                    store,
                    "openai-official",
                    dry_run=False,
                    clear_missing_auth=False,
                    config_mode="shared",
                    shared_config_base=None,
                    skip_shim=False,
                    skip_app_cli=False,
                    skip_launchctl=True,
                )

            manifest = store.load_manifest("openai-official")
            active = json.loads(store.active_path.read_text())
            self.assertEqual(str(bundled_cli), manifest["codex_bin"])
            self.assertEqual(str(bundled_cli), manifest["app_cli_path"])
            self.assertEqual("canonical", manifest["runtime_binding"])
            self.assertEqual(str(bundled_cli), active["shell_cli_path"])
            self.assertEqual(str(bundled_cli), active["app_cli_path"])
            self.assertIn(
                str(store.bin_dir),
                shell_profile.read_text(),
            )

    def test_set_bin_internal_rejects_invalid_and_recursive_backend_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, launcher, _binding = self.write_internal_store_fixture(root)
            manifest_path = store.manifest_path("internal")
            manifest_before = manifest_path.read_bytes()
            launcher_before = launcher.read_bytes()
            non_executable = root / "non-executable"
            non_executable.write_text("not executable")
            candidates = (root / "missing", non_executable, launcher)

            for candidate in candidates:
                with self.subTest(candidate=candidate):
                    with self.assertRaises(SwitchError):
                        cmd_set_bin(self.rebind_args(store, candidate))
                    self.assertEqual(manifest_path.read_bytes(), manifest_before)
                    self.assertEqual(launcher.read_bytes(), launcher_before)

    def test_set_app_bin_internal_rejects_raw_backend_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, backend, _launcher, _binding = self.write_internal_store_fixture(root)
            args = self.rebind_args(store, backend)
            args.app_cli_path = str(backend)

            with self.assertRaises(SwitchError) as raised:
                cmd_set_app_bin(args)

            self.assertIn("managed launcher", str(raised.exception))

    def test_staged_internal_launcher_retains_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")

            with self.prepared_parity_rebind(
                root,
                store,
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            launcher_text = launcher.read_text()
            self.assertIn("codex_switch_app_proxy.py", launcher_text)
            self.assertIn(str(candidate), launcher_text)
            self.assertNotIn("CODEX_BIN=\"" + str(launcher) + "\"", launcher_text)

    def test_failed_rebind_smoke_preserves_manifest_and_launcher_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex",
                initialize_error=True,
            )
            manifest_path = store.manifest_path("internal")
            manifest_before = manifest_path.read_bytes()
            launcher_before = launcher.read_bytes()
            real_smoke = bindings_module.run_app_server_smoke
            bearer_secret = "codex-runtime-secret"
            assignment_secret = "codex-api-key-secret"

            def secret_bearing_smoke(
                *args: object,
                **kwargs: object,
            ) -> tuple[int, str]:
                code, output = real_smoke(*args, **kwargs)
                return (
                    code,
                    f"{output}\n"
                    f"Authorization: Bearer {bearer_secret}\n"
                    f"api_key={assignment_secret}",
                )

            with self.prepared_parity_rebind(
                root,
                store,
            ), patch.object(
                bindings_module,
                "run_app_server_smoke",
                side_effect=secret_bearing_smoke,
            ), self.assertRaises(SwitchError) as raised:
                cmd_set_bin(self.rebind_args(store, candidate))

            error_text = str(raised.exception)
            self.assertIn("compatibility smoke failed", error_text)
            self.assertIn("[REDACTED]", error_text)
            self.assertNotIn(bearer_secret, error_text)
            self.assertNotIn(assignment_secret, error_text)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(launcher.read_bytes(), launcher_before)
            self.assertFalse((store.root / ".runtime-binding-rebind.json").exists())

    def test_rebind_attests_requested_backend_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, old_backend, _launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")

            with self.prepared_parity_rebind(
                root,
                store,
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            manifest = store.load_manifest("internal")
            self.assertEqual(manifest["codex_bin"], str(candidate.resolve()))
            self.assertNotEqual(manifest["codex_bin"], str(old_backend))
            self.assertEqual(manifest["runtime_binding"], "canonical")
            self.assertRegex(manifest["app_launcher_sha256"], r"^[0-9a-f]{64}$")

    def test_rebind_promotes_receipt_consistently_with_launcher_and_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")

            with self.prepared_parity_rebind(
                root,
                store,
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            receipt_path = store.bin_dir / "codex-internal-app.capabilities.json"
            self.assertTrue(receipt_path.is_file())
            self.assertFalse(receipt_path.is_symlink())
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
            receipt = CapabilityReceipt.from_dict(
                json.loads(receipt_path.read_text())
            )
            self.assertEqual(
                receipt.backend_sha256,
                hashlib.sha256(candidate.read_bytes()).hexdigest(),
            )
            self.assertTrue(
                receipt.capabilities.versioned_config_write_preserves_unrelated
            )
            manifest = store.load_manifest("internal")
            receipt_payload = receipt_path.read_bytes()
            self.assertEqual(
                manifest["app_capability_receipt_path"],
                str(receipt_path),
            )
            self.assertEqual(
                manifest["app_capability_receipt_sha256"],
                hashlib.sha256(receipt_payload).hexdigest(),
            )
            self.assertEqual(
                manifest["app_schema_sha256"],
                receipt.schema_sha256,
            )
            launcher_text = launcher.read_text()
            self.assertIn(str(receipt_path), launcher_text)
            self.assertIn(receipt.schema_sha256, launcher_text)
            self.assertIn(
                manifest["app_capability_receipt_sha256"],
                launcher_text,
            )
            self.assertNotIn(str(root), receipt_path.read_text())

    def test_internal_rebind_prepares_parity_before_launcher_smoke(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex"
            )
            (
                bundle,
                source_catalog,
                source_payload,
                source_mode,
                _projected_payloads,
            ) = self.parity_rebind_bundle_fixture(root, store)
            events: list[str] = []
            real_smoke = bindings_module.run_app_server_smoke

            def prepare_parity(*_args: object, **_kwargs: object) -> object:
                events.append("parity")
                return self.bind_staged_capability_receipt(
                    bundle,
                    _args[0],
                )

            def observe_smoke(*args: object, **kwargs: object) -> object:
                events.append("smoke")
                return real_smoke(*args, **kwargs)

            def capture_bundle(*_args: object, **_kwargs: object) -> None:
                events.append("commit")

            with patch.object(
                bindings_module,
                "prepare_parity_bundle",
                side_effect=prepare_parity,
                create=True,
            ), patch.object(
                bindings_module,
                "run_app_server_smoke",
                side_effect=observe_smoke,
            ), patch.object(
                bindings_module,
                "commit_runtime_binding_bundle",
                side_effect=capture_bundle,
                create=True,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_inputs",
                return_value=None,
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            self.assertEqual(
                {
                    "events": events,
                    "source_bytes": source_catalog.read_bytes(),
                    "source_mode": source_catalog.stat().st_mode & 0o777,
                },
                {
                    "events": ["parity", "smoke", "commit"],
                    "source_bytes": source_payload,
                    "source_mode": source_mode,
                },
            )

    def test_successful_internal_rebind_supplies_complete_schema_v3_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex"
            )
            (
                bundle,
                source_catalog,
                source_payload,
                source_mode,
                projected_payloads,
            ) = self.parity_rebind_bundle_fixture(root, store)
            captured_artifacts: list[tuple[object, ...]] = []
            captured_input_validators: list[object] = []

            def capture_bundle(
                _locked_store: object,
                *,
                artifacts: tuple[object, ...],
                executable_swap: object | None = None,
                input_validator: object | None = None,
                fault_hook: object | None = None,
            ) -> None:
                del fault_hook
                self.assertIsNone(executable_swap)
                captured_artifacts.append(artifacts)
                captured_input_validators.append(input_validator)
                self.assertTrue(callable(input_validator))
                input_validator()
                manifest_artifact = next(
                    artifact
                    for artifact in artifacts
                    if getattr(artifact, "role", "") == "manifest"
                )
                marker_path = store.root / ".runtime-binding-rebind.json"
                marker_path.write_text('{"state":"prepared"}\n')
                try:
                    Path(getattr(manifest_artifact, "path")).write_bytes(
                        getattr(manifest_artifact, "payload")
                    )
                    input_validator()
                finally:
                    marker_path.unlink()

            with patch.object(
                bindings_module,
                "prepare_parity_bundle",
                side_effect=lambda parity_candidate, **_kwargs: (
                    self.bind_staged_capability_receipt(
                        bundle,
                        parity_candidate,
                    )
                ),
                create=True,
            ), patch.object(
                bindings_module,
                "commit_runtime_binding_bundle",
                side_effect=capture_bundle,
                create=True,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_inputs",
                return_value=None,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_immutable_inputs",
                return_value=None,
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            artifacts = captured_artifacts[0] if captured_artifacts else ()
            self.assertEqual(1, len(captured_input_validators))
            self.assertTrue(callable(captured_input_validators[0]))
            by_role = {
                getattr(artifact, "role", ""): artifact
                for artifact in artifacts
            }
            expected_paths = {
                "manifest": store.manifest_path("internal"),
                "official_manifest": store.manifest_path("openai-official"),
                "launcher": store.bin_dir / "codex-internal-app",
                "capability_receipt": (
                    store.bin_dir
                    / "codex-internal-app.capabilities.json"
                ),
                "parity_receipt": (
                    store.profile_dir("internal")
                    / "parity"
                    / "receipt.json"
                ),
                "parity_overlay": (
                    store.profile_dir("internal")
                    / "parity"
                    / "model-catalog.json"
                ),
                "profile_config": (
                    store.profile_dir("internal") / "config.toml"
                ),
                "shared_config": (
                    store.official_codex_home / "config.toml"
                ),
                "active_runtime_config": (
                    store.internal_codex_home / "config.toml"
                ),
            }
            actual_payloads = {
                role: getattr(artifact, "payload", None)
                for role, artifact in by_role.items()
            }
            self.assertEqual(
                json.loads(actual_payloads["official_manifest"])["codex_home"],
                str(store.official_codex_home),
            )
            manifest_payload = actual_payloads.get("manifest")
            manifest = (
                json.loads(manifest_payload)
                if isinstance(manifest_payload, bytes)
                else {}
            )
            self.assertEqual(
                {
                    "roles": set(by_role),
                    "paths": {
                        role: getattr(artifact, "path", None)
                        for role, artifact in by_role.items()
                    },
                    "modes": {
                        role: getattr(artifact, "mode", None)
                        for role, artifact in by_role.items()
                    },
                    "parity_receipt": actual_payloads.get(
                        "parity_receipt"
                    ),
                    "parity_overlay": actual_payloads.get(
                        "parity_overlay"
                    ),
                    "profile_config": actual_payloads.get(
                        "profile_config"
                    ),
                    "shared_config": actual_payloads.get(
                        "shared_config"
                    ),
                    "active_runtime_config": actual_payloads.get(
                        "active_runtime_config"
                    ),
                    "manifest_metadata": {
                        key: manifest.get(key)
                        for key in bundle.manifest_metadata
                    },
                    "source_bytes": source_catalog.read_bytes(),
                    "source_mode": source_catalog.stat().st_mode & 0o777,
                },
                {
                    "roles": set(expected_paths),
                    "paths": expected_paths,
                    "modes": {
                        role: 0o755 if role == "launcher" else 0o600
                        for role in expected_paths
                    },
                    "parity_receipt": bundle.receipt_payload,
                    "parity_overlay": bundle.overlay.overlay_payload,
                    "profile_config": projected_payloads[
                        "profile_config"
                    ],
                    "shared_config": projected_payloads["shared_config"],
                    "active_runtime_config": projected_payloads[
                        "active_runtime_config"
                    ],
                    "manifest_metadata": dict(bundle.manifest_metadata),
                    "source_bytes": source_payload,
                    "source_mode": source_mode,
                },
            )

    def test_set_bin_internal_preserves_external_backend_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            external_parent = root / "external-install"
            external_backend = self.write_rebind_backend(
                external_parent / "codex",
                runtime_label="external",
            )
            before_info = external_backend.lstat()
            before_identity = (
                before_info.st_dev,
                before_info.st_ino,
                before_info.st_mode,
                before_info.st_nlink,
                before_info.st_size,
                before_info.st_mtime_ns,
                before_info.st_ctime_ns,
            )
            before_payload = external_backend.read_bytes()
            before_entries = tuple(
                sorted(path.name for path in external_parent.iterdir())
            )
            marker_snapshots: list[dict[str, object]] = []

            def capture_prepared_marker(phase: str) -> None:
                if phase != "after_marker":
                    return
                marker_path = (
                    store.root / ".runtime-binding-rebind.json"
                )
                marker = json.loads(marker_path.read_text())
                self.assertIsInstance(marker, dict)
                marker_snapshots.append(marker)

            with self.prepared_parity_rebind(
                root,
                store,
            ), redirect_stdout(io.StringIO()):
                result = cmd_set_bin(
                    self.rebind_args(
                        store,
                        external_backend,
                        fault_hook=capture_prepared_marker,
                    )
                )

            after_info = external_backend.lstat()
            after_identity = (
                after_info.st_dev,
                after_info.st_ino,
                after_info.st_mode,
                after_info.st_nlink,
                after_info.st_size,
                after_info.st_mtime_ns,
                after_info.st_ctime_ns,
            )
            self.assertEqual(before_identity, after_identity)
            self.assertEqual(before_payload, external_backend.read_bytes())
            self.assertEqual(
                before_entries,
                tuple(
                    sorted(path.name for path in external_parent.iterdir())
                ),
            )
            self.assertEqual(1, len(marker_snapshots))
            marker = marker_snapshots[0]
            self.assertEqual(3, marker.get("schema_version"))
            self.assertEqual("prepared", marker.get("state"))
            self.assertNotIn("executable_swap", marker)
            entries = marker.get("artifacts")
            self.assertIsInstance(entries, list)
            artifact_entries = [
                entry for entry in entries if isinstance(entry, dict)
            ]
            self.assertEqual(
                {
                    "active_runtime_config",
                    "capability_receipt",
                    "launcher",
                    "manifest",
                    "official_manifest",
                    "parity_overlay",
                    "parity_receipt",
                    "profile_config",
                    "shared_config",
                },
                {
                    entry.get("role")
                    for entry in artifact_entries
                },
            )
            self.assertNotIn(
                external_backend,
                {
                    Path(str(entry.get("path")))
                    for entry in artifact_entries
                },
            )
            managed_copies = []
            for path in store.root.rglob("*"):
                info = path.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and path.read_bytes() == before_payload
                ):
                    managed_copies.append(path)
            self.assertEqual([], managed_copies)
            self.assertEqual(
                external_backend.resolve(),
                getattr(getattr(result, "binding"), "backend_cli"),
            )
            self.assertEqual(
                str(external_backend.resolve()),
                store.load_manifest("internal")["codex_bin"],
            )
            self.assertFalse(
                (store.root / ".runtime-binding-rebind.json").exists()
            )

    def test_active_internal_shell_and_desktop_use_same_parity_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, old_backend, launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            self.write_rebind_backend(
                old_backend,
                runtime_label="old",
                version="0.100.0",
            )
            home = store.internal_codex_home
            assert home is not None
            shell = write_codex_shim(
                store,
                str(old_backend),
                home,
            )
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex",
                runtime_label="new",
            )

            with self.prepared_parity_rebind(
                root,
                store,
                generation="new",
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            desktop_result = self.run_runtime_contract(launcher)
            shell_result = self.run_runtime_contract(shell)
            self.assertEqual(
                0,
                desktop_result.returncode,
                desktop_result.stdout + desktop_result.stderr,
            )
            self.assertEqual(
                0,
                shell_result.returncode,
                shell_result.stdout + shell_result.stderr,
            )
            desktop_contract = json.loads(desktop_result.stdout)
            shell_contract = json.loads(shell_result.stdout)
            manifest = store.load_manifest("internal")
            overlay_path = Path(str(manifest["parity_overlay_path"]))
            config_path = home / "config.toml"
            expected = {
                "runtime_label": "new",
                "codex_home": str(home.resolve()),
                "config_sha256": hashlib.sha256(
                    config_path.read_bytes()
                ).hexdigest(),
                "overlay_path": str(overlay_path.resolve()),
                "overlay_sha256": str(
                    manifest["parity_overlay_sha256"]
                ),
                "multi_agent_v2": True,
                "capability_receipt_path": str(
                    manifest["app_capability_receipt_path"]
                ),
                "expected_receipt_sha256": str(
                    manifest["app_capability_receipt_sha256"]
                ),
            }
            self.assertEqual(expected, desktop_contract)
            self.assertEqual(expected, shell_contract)

    def test_internal_launch_rejects_runtime_config_digest_drift_before_backend(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, old_backend, launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            self.write_rebind_backend(
                old_backend,
                runtime_label="old",
                version="0.100.0",
            )
            home = store.internal_codex_home
            assert home is not None
            shell = write_codex_shim(
                store,
                str(old_backend),
                home,
            )
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex",
                runtime_label="bound",
            )

            with self.prepared_parity_rebind(
                root,
                store,
                generation="bound",
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            drift = "\n[unexpected]\nvalue = true\n"
            profile_config = store.profile_dir("internal") / "config.toml"
            profile_config.write_text(profile_config.read_text() + drift)
            runtime_config = home / "config.toml"
            runtime_config.write_text(runtime_config.read_text() + drift)

            for name, executable in (
                ("desktop", launcher),
                ("shell", shell),
            ):
                with self.subTest(route=name):
                    marker = root / f"{name}.backend-started"
                    result = self.run_runtime_contract(
                        executable,
                        marker=marker,
                    )

                    self.assertNotEqual(
                        0,
                        result.returncode,
                        result.stdout + result.stderr,
                    )
                    self.assertRegex(
                        result.stderr.lower(),
                        r"config|digest|generation",
                    )
                    self.assertFalse(marker.exists())

    def test_internal_launch_rejects_partial_parity_manifest_before_backend(
        self,
    ) -> None:
        from codex_switch_app_wrapper import write_profile_app_wrapper

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, backend, launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            self.write_rebind_backend(
                backend,
                runtime_label="legacy",
                version="0.100.0",
            )
            store.official_codex_home.mkdir(parents=True, exist_ok=True)
            (store.official_codex_home / "config.toml").write_text(
                "[notice]\nkeep = true\n"
            )
            manifest = store.load_manifest("internal")
            manifest["parity_adapter_rule_set_sha256"] = "a" * 64
            store.manifest_path("internal").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            write_profile_app_wrapper(
                store=store,
                name="internal",
                app_cli_path=str(launcher),
                codex_bin=str(backend),
                switch_scripts=Path(__file__).resolve().parent,
                manifest_override=manifest,
            )
            home = store.internal_codex_home
            assert home is not None
            shell = write_codex_shim(
                store,
                str(backend),
                home,
            )

            for name, executable in (
                ("desktop", launcher),
                ("shell", shell),
            ):
                with self.subTest(route=name):
                    marker = root / f"{name}.backend-started"
                    result = self.run_runtime_contract(
                        executable,
                        marker=marker,
                    )

                    self.assertNotEqual(
                        0,
                        result.returncode,
                        result.stdout + result.stderr,
                    )
                    self.assertRegex(
                        result.stderr.lower(),
                        r"generation|manifest|parity",
                    )
                    self.assertFalse(marker.exists())

    def test_internal_launch_rejects_mixed_parity_generations_before_backend(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            first_candidate = self.write_rebind_backend(
                root / "candidate-old" / "codex",
                runtime_label="old",
            )
            with self.prepared_parity_rebind(
                root,
                store,
                generation="old",
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, first_candidate))
            old_launcher_payload = launcher.read_bytes()
            old_overlay_payload = (
                store.profile_dir("internal")
                / "parity"
                / "model-catalog.json"
            ).read_bytes()

            second_candidate = self.write_rebind_backend(
                root / "candidate-new" / "codex",
                runtime_label="new",
            )
            with self.prepared_parity_rebind(
                root,
                store,
                generation="new",
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, second_candidate))
            new_launcher_payload = launcher.read_bytes()
            manifest = store.load_manifest("internal")
            overlay_path = Path(str(manifest["parity_overlay_path"]))
            new_overlay_payload = overlay_path.read_bytes()
            self.assertNotEqual(old_launcher_payload, new_launcher_payload)
            self.assertNotEqual(old_overlay_payload, new_overlay_payload)

            cases = (
                (
                    "old-launcher-new-manifest",
                    old_launcher_payload,
                    new_overlay_payload,
                ),
                (
                    "new-launcher-old-overlay",
                    new_launcher_payload,
                    old_overlay_payload,
                ),
            )
            for name, launcher_payload, overlay_payload in cases:
                with self.subTest(case=name):
                    launcher.write_bytes(launcher_payload)
                    launcher.chmod(0o755)
                    overlay_path.write_bytes(overlay_payload)
                    overlay_path.chmod(0o600)
                    marker = root / f"{name}.backend-started"

                    result = self.run_runtime_contract(
                        launcher,
                        marker=marker,
                    )

                    self.assertNotEqual(
                        0,
                        result.returncode,
                        result.stdout + result.stderr,
                    )
                    self.assertRegex(
                        result.stderr.lower(),
                        r"generation|digest|launcher|overlay|receipt",
                    )
                    self.assertFalse(marker.exists())

    def test_internal_launch_rejects_unbound_parity_receipt_before_backend(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex",
                runtime_label="bound",
            )
            with self.prepared_parity_rebind(
                root,
                store,
                generation="bound",
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))

            manifest_path = store.manifest_path("internal")
            receipt_path = (
                store.profile_dir("internal") / "parity" / "receipt.json"
            )
            baseline_manifest = store.load_manifest("internal")
            baseline_receipt = json.loads(receipt_path.read_bytes())
            cases = (
                ("missing-overlay-binding", "overlay"),
                (
                    "missing-capability-binding",
                    "internal_fingerprint",
                ),
            )
            for name, removed_key in cases:
                with self.subTest(case=name):
                    receipt = dict(baseline_receipt)
                    receipt.pop(removed_key)
                    receipt_payload = (
                        json.dumps(
                            receipt,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode()
                        + b"\n"
                    )
                    receipt_path.write_bytes(receipt_payload)
                    receipt_path.chmod(0o600)
                    manifest = dict(baseline_manifest)
                    manifest["parity_receipt_sha256"] = hashlib.sha256(
                        receipt_payload
                    ).hexdigest()
                    manifest_path.write_text(
                        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
                    )
                    manifest_path.chmod(0o600)
                    marker = root / f"{name}.backend-started"

                    result = self.run_runtime_contract(
                        launcher,
                        marker=marker,
                    )

                    self.assertNotEqual(
                        0,
                        result.returncode,
                        result.stdout + result.stderr,
                    )
                    self.assertRegex(
                        result.stderr.lower(),
                        r"generation|receipt|overlay|capability",
                    )
                    self.assertFalse(marker.exists())

    def test_internal_rebind_only_promotes_materialized_active_internal_config(
        self,
    ) -> None:
        for active_profile in ("internal", "openai-official"):
            with (
                self.subTest(active_profile=active_profile),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                store, _old_backend, _launcher, _binding = (
                    self.write_internal_store_fixture(root)
                )
                candidate = self.write_rebind_backend(
                    root / "candidate" / "codex"
                )
                bundle, *_fixture = self.parity_rebind_bundle_fixture(
                    root,
                    store,
                )
                if active_profile == "openai-official":
                    store.active_path.write_text(
                        json.dumps(
                            {
                                "profile": "openai-official",
                                "codex_home": str(
                                    store.official_codex_home
                                ),
                                "shell_cli_path": str(candidate),
                                "app_cli_path": str(candidate),
                            }
                        )
                    )
                requested_active_paths: list[object] = []
                captured_roles: list[set[str]] = []

                def prepare_parity(
                    parity_candidate: object,
                    **_kwargs: object,
                ) -> object:
                    requested = getattr(
                        parity_candidate,
                        "active_runtime_config_path",
                        "missing",
                    )
                    requested_active_paths.append(requested)
                    if requested is None:
                        bundle.active_runtime_config = None
                        bundle.active_runtime_config_path = None
                        bundle.active_runtime_config_payload = None
                    return self.bind_staged_capability_receipt(
                        bundle,
                        parity_candidate,
                    )

                def capture_bundle(
                    _locked_store: object,
                    *,
                    artifacts: tuple[object, ...],
                    **_kwargs: object,
                ) -> None:
                    captured_roles.append(
                        {
                            str(getattr(artifact, "role", ""))
                            for artifact in artifacts
                        }
                    )

                with patch.object(
                    bindings_module,
                    "prepare_parity_bundle",
                    side_effect=prepare_parity,
                ), patch.object(
                    bindings_module,
                    "revalidate_parity_bundle_inputs",
                    return_value=None,
                ), patch.object(
                    bindings_module,
                    "commit_runtime_binding_bundle",
                    side_effect=capture_bundle,
                ), redirect_stdout(io.StringIO()):
                    cmd_set_bin(self.rebind_args(store, candidate))

                expected_active_path = (
                    store.internal_codex_home / "config.toml"
                    if active_profile == "internal"
                    else None
                )
                self.assertEqual(
                    [expected_active_path],
                    requested_active_paths,
                )
                self.assertEqual(1, len(captured_roles))
                self.assertEqual(
                    active_profile == "internal",
                    "active_runtime_config" in captured_roles[0],
                )

    def test_active_internal_record_symlink_swap_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            binding = SimpleNamespace(codex_home=store.internal_codex_home)
            active_path = store.active_path
            original_active = active_path.with_name("active.original.json")
            foreign_active = root / "foreign-active.json"
            foreign_payload = json.dumps(
                {
                    "profile": "internal",
                    "codex_home": str(store.internal_codex_home),
                }
            )
            foreign_active.write_text(foreign_payload)
            real_lstat = Path.lstat
            replaced = False

            def replace_after_active_snapshot(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                nonlocal replaced
                info = real_lstat(path, *args, **kwargs)
                if path == active_path and not replaced:
                    active_path.rename(original_active)
                    active_path.symlink_to(foreign_active)
                    replaced = True
                return info

            with patch.object(
                Path,
                "lstat",
                replace_after_active_snapshot,
            ), self.assertRaisesRegex(
                SwitchError,
                "Active profile record",
            ):
                bindings_module._materialized_active_internal_config(
                    store,
                    binding,
                )

            self.assertTrue(replaced)
            self.assertTrue(active_path.is_symlink())
            self.assertEqual(foreign_payload, foreign_active.read_text())

    def test_internal_rebind_rejects_manifest_change_after_child_attestation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex"
            )
            bundle, *_fixture = self.parity_rebind_bundle_fixture(root, store)
            manifest_path = store.manifest_path("internal")
            foreign_payload = b'{"foreign":"post-attestation"}\n'
            bundle_commit = unittest.mock.Mock()

            def prepare_parity(
                parity_candidate: object,
                **_kwargs: object,
            ) -> object:
                return self.bind_staged_capability_receipt(
                    bundle,
                    parity_candidate,
                )

            def replace_manifest(_bundle: object) -> None:
                manifest_path.write_bytes(foreign_payload)

            with patch.object(
                bindings_module,
                "prepare_parity_bundle",
                side_effect=prepare_parity,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_inputs",
                side_effect=replace_manifest,
            ), patch.object(
                bindings_module,
                "commit_runtime_binding_bundle",
                bundle_commit,
            ), self.assertRaises(SwitchError) as raised, redirect_stdout(
                io.StringIO()
            ):
                cmd_set_bin(self.rebind_args(store, candidate))

            self.assertIn(
                "Internal manifest changed before runtime rebind",
                str(raised.exception),
            )
            self.assertEqual(bundle_commit.call_count, 0)
            self.assertEqual(manifest_path.read_bytes(), foreign_payload)
            self.assertFalse(
                (store.root / ".runtime-binding-rebind.json").exists()
            )

    def test_full_rebind_rejects_active_selection_drift_before_preparation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            candidate = self.write_rebind_backend(
                root / "candidate" / "codex"
            )
            args = self.rebind_args(store, candidate)
            args.expected_active_selection_payload = (
                store.active_path.read_bytes()
            )
            changed = json.loads(store.active_path.read_text())
            changed["cli_profile"] = "internal"
            changed["app_profile"] = "openai-official"
            store.active_path.write_text(json.dumps(changed) + "\n")
            manifest_before = store.manifest_path("internal").read_bytes()

            with patch.object(
                bindings_module,
                "prepare_parity_bundle",
                side_effect=AssertionError(
                    "selection CAS must fail before parity preparation"
                ),
            ) as prepare_parity, self.assertRaisesRegex(
                SwitchError,
                "active.selection.changed_before_repair",
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(args)

            prepare_parity.assert_not_called()
            self.assertEqual(
                manifest_before,
                store.manifest_path("internal").read_bytes(),
            )
            self.assertFalse(
                (store.root / ".runtime-binding-rebind.json").exists()
            )

    def test_doctor_uses_one_active_selection_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, _launcher, _binding = (
                self.write_internal_store_fixture(root)
            )
            split = {
                "profile": "internal",
                "cli_profile": "internal",
                "app_profile": "openai-official",
            }
            snapshot = SimpleNamespace(
                record=split,
                selection=ProfileSelection(
                    cli_profile="internal",
                    app_profile="openai-official",
                ),
                problem=None,
                payload=(json.dumps(split) + "\n").encode(),
            )
            args = SimpleNamespace()
            output = io.StringIO()
            with (
                patch.object(doctor_module, "make_store", return_value=store),
                patch.object(
                    doctor_module,
                    "read_active_profile_selection_snapshot",
                    return_value=snapshot,
                    create=True,
                ) as read_snapshot,
                patch.object(
                    doctor_module,
                    "active_runtime_binding_for_observation",
                    return_value=None,
                ) as resolve_app,
                patch.object(
                    doctor_module,
                    "collect_store_runtime_observation",
                    return_value=None,
                ),
                patch.object(
                    doctor_module,
                    "active_shared_configuration_report",
                    return_value=None,
                ) as shared_report,
                patch.object(
                    doctor_module,
                    "collect_doctor_problems",
                    return_value=[],
                ) as collect_problems,
                patch.object(
                    doctor_module,
                    "active_cli_runtime_binding_for_parity",
                    return_value=None,
                ) as parity_binding,
                patch.object(
                    doctor_module,
                    "active_internal_app_profile",
                    return_value="openai-official",
                ) as app_profile,
                redirect_stdout(output),
            ):
                doctor_module.cmd_doctor(args)

            read_snapshot.assert_called_once_with(store.active_path)
            resolve_app.assert_called_once_with(store, snapshot=snapshot)
            shared_report.assert_called_once_with(store, snapshot=snapshot)
            collect_problems.assert_called_once_with(
                store,
                None,
                None,
                shared_configuration=None,
                snapshot=snapshot,
            )
            parity_binding.assert_called_once_with(store, snapshot=snapshot)
            app_profile.assert_called_once_with(store, snapshot=snapshot)
            self.assertIn("Doctor passed", output.getvalue())
            self.assertIn(
                "Internal App parity: not applicable "
                "(App profile: openai-official)",
                output.getvalue(),
            )

    def test_rebind_commit_failure_restores_manifest_and_launcher_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")
            manifest_path = store.manifest_path("internal")
            manifest_before = manifest_path.read_bytes()
            launcher_before = launcher.read_bytes()

            def fail_after_launcher(phase: str) -> None:
                if phase == "after_launcher":
                    raise OSError("injected rebind commit failure")

            with self.prepared_parity_rebind(
                root,
                store,
            ), self.assertRaises(OSError):
                cmd_set_bin(
                    self.rebind_args(
                        store,
                        candidate,
                        fault_hook=fail_after_launcher,
                    )
                )

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(launcher.read_bytes(), launcher_before)
            self.assertFalse((store.root / ".runtime-binding-rebind.json").exists())

    def test_promote_internal_update_rolls_back_failed_version_handshake(
        self,
    ) -> None:
        self.assert_failed_internal_update_handshake_restores_generation(
            "version"
        )

    def test_promote_internal_update_rolls_back_failed_binding_handshake(
        self,
    ) -> None:
        self.assert_failed_internal_update_handshake_restores_generation(
            "binding"
        )

    def test_promote_internal_update_rolls_back_failed_app_server_handshake(
        self,
    ) -> None:
        self.assert_failed_internal_update_handshake_restores_generation(
            "app_server"
        )

    def test_promote_internal_update_rolls_back_failed_receipt_handshake(
        self,
    ) -> None:
        self.assert_failed_internal_update_handshake_restores_generation(
            "receipt"
        )

    def test_promote_internal_update_rolls_back_failed_overlay_handshake(
        self,
    ) -> None:
        self.assert_failed_internal_update_handshake_restores_generation(
            "overlay"
        )

    def test_promote_internal_update_rolls_back_failed_config_handshake(
        self,
    ) -> None:
        self.assert_failed_internal_update_handshake_restores_generation(
            "config"
        )

    def test_promote_internal_update_rolls_back_failed_parity_handshake(
        self,
    ) -> None:
        self.assert_failed_internal_update_handshake_restores_generation(
            "parity"
        )

    def test_promote_internal_update_retires_backup_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self.internal_update_promotion_fixture(root)
            candidate_payload = fixture.candidate.read_bytes()

            with patch.object(
                bindings_module,
                "prepare_parity_bundle",
                side_effect=fixture.prepare_candidate_generation,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_inputs",
                return_value=None,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_immutable_inputs",
                return_value=None,
            ), redirect_stdout(
                io.StringIO()
            ):
                cmd_promote_internal_update(fixture.args)

            self.assertEqual(candidate_payload, fixture.bound.read_bytes())
            self.assertFalse(fixture.candidate.exists())
            self.assertFalse(fixture.backup.exists())
            self.assertFalse(
                (fixture.store.root / ".runtime-binding-rebind.json").exists()
            )

    def test_promote_internal_update_reports_restart_only_after_durable_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self.internal_update_promotion_fixture(root)
            candidate_payload = fixture.candidate.read_bytes()
            marker_path = (
                fixture.store.root / ".runtime-binding-rebind.json"
            )
            output: list[str] = []

            def observe_print(*values: object, **_kwargs: object) -> None:
                message = " ".join(str(value) for value in values)
                output.append(message)
                if "Restart required" in message:
                    self.assertFalse(marker_path.exists())
                    self.assertFalse(fixture.backup.exists())
                    self.assertEqual(
                        candidate_payload,
                        fixture.bound.read_bytes(),
                    )

            with patch.object(
                bindings_module,
                "prepare_parity_bundle",
                side_effect=fixture.prepare_candidate_generation,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_inputs",
                return_value=None,
            ), patch.object(
                bindings_module,
                "revalidate_parity_bundle_immutable_inputs",
                return_value=None,
            ), patch(
                "builtins.print",
                side_effect=observe_print,
            ):
                cmd_promote_internal_update(fixture.args)

            receipt_index = next(
                index
                for index, message in enumerate(output)
                if "capability and parity receipts verified" in message
            )
            restart_indexes = [
                index
                for index, message in enumerate(output)
                if "Restart required" in message
            ]
            self.assertEqual(1, len(restart_indexes))
            self.assertGreater(restart_indexes[0], receipt_index)

    def test_successful_rebind_reports_restart_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")
            output = io.StringIO()

            with self.prepared_parity_rebind(
                root,
                store,
            ), redirect_stdout(output):
                cmd_set_bin(self.rebind_args(store, candidate))

            self.assertIn("Restart required", output.getvalue())
            self.assertIn("quit ChatGPT completely", output.getvalue())
            self.assertEqual(
                store.load_manifest("internal")["app_cli_path"],
                str(launcher),
            )

    def test_rebind_commit_rejects_concurrent_manifest_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, _launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")
            manifest_path = store.manifest_path("internal")
            foreign_payload = b'{"foreign":"manifest"}\n'

            def replace_manifest_after_launcher(phase: str) -> None:
                if phase == "after_launcher":
                    manifest_path.write_bytes(foreign_payload)

            with self.prepared_parity_rebind(
                root,
                store,
            ), self.assertRaises(SwitchError) as raised:
                cmd_set_bin(
                    self.rebind_args(
                        store,
                        candidate,
                        fault_hook=replace_manifest_after_launcher,
                    )
                )

            self.assertIn("foreign target state", str(raised.exception))
            self.assertEqual(manifest_path.read_bytes(), foreign_payload)
            self.assertTrue((store.root / ".runtime-binding-rebind.json").exists())

    def test_custom_set_bin_preserves_direct_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            profile = store.profile_dir("custom")
            profile.mkdir(parents=True)
            (profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "custom",
                        "codex_bin": "/old/codex",
                        "app_cli_path": "/old/codex",
                    }
                )
            )
            backend = write_executable(root / "custom" / "codex")
            args = self.rebind_args(store, backend)
            args.name = "custom"

            with redirect_stdout(io.StringIO()):
                cmd_set_bin(args)

            manifest = store.load_manifest("custom")
            self.assertEqual(manifest["codex_bin"], str(backend))
            self.assertEqual(manifest["app_cli_path"], str(backend))

    def test_official_app_override_is_rejected_with_chatgpt_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.ensure()
            profile = store.profile_dir("openai-official")
            profile.mkdir(parents=True)
            backend = write_executable(root / "official" / "codex")
            (profile / "manifest.json").write_text(
                json.dumps(
                    {
                        "name": "openai-official",
                        "codex_bin": str(backend),
                        "app_cli_path": str(backend),
                    }
                )
            )
            args = self.rebind_args(store, backend)
            args.name = "openai-official"
            args.app_cli_path = str(backend)

            with self.assertRaises(SwitchError) as set_bin_error:
                cmd_set_bin(args)
            with self.assertRaises(SwitchError) as set_app_error:
                cmd_set_app_bin(args)

            self.assertIn("ChatGPT.app", str(set_bin_error.exception))
            self.assertIn("ChatGPT.app", str(set_app_error.exception))

    def test_pending_rebind_dry_run_is_non_mutating_then_lock_recovers(self) -> None:
        class RebindCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")
            manifest_path = store.manifest_path("internal")
            manifest_before = manifest_path.read_bytes()
            launcher_before = launcher.read_bytes()

            def interrupt_after_launcher(phase: str) -> None:
                if phase == "after_launcher":
                    raise RebindCrash()

            with self.prepared_parity_rebind(
                root,
                store,
            ), self.assertRaises(RebindCrash):
                cmd_set_bin(
                    self.rebind_args(
                        store,
                        candidate,
                        fault_hook=interrupt_after_launcher,
                    )
                )
            marker = store.root / ".runtime-binding-rebind.json"
            self.assertTrue(marker.exists())
            partial_launcher = launcher.read_bytes()
            self.assertNotEqual(partial_launcher, launcher_before)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)

            receipt = execute_transaction(
                store,
                TransactionRequest(
                    operation="switch",
                    profile="internal",
                    options={},
                ),
                dry_run=True,
            )

            self.assertEqual(receipt.outcome, "dry_run")
            self.assertIn("pending runtime rebind", receipt.preview_lines[0])
            self.assertTrue(marker.exists())
            self.assertEqual(launcher.read_bytes(), partial_launcher)
            with locked_store_mutation(store, operation="test recovery"):
                pass
            self.assertFalse(marker.exists())
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(launcher.read_bytes(), launcher_before)

    def test_doctor_detects_rebound_launcher_fingerprint_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _old_backend, launcher, _binding = self.write_internal_store_fixture(root)
            candidate = self.write_rebind_backend(root / "candidate" / "codex")
            with self.prepared_parity_rebind(
                root,
                store,
            ), redirect_stdout(io.StringIO()):
                cmd_set_bin(self.rebind_args(store, candidate))
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)

            problems = active_profile_problems(store)

            self.assertTrue(
                any(
                    "attestation.internal.launcher_fingerprint_mismatch" in problem
                    for problem in problems
                ),
                problems,
            )

    def test_rebind_recovery_rejects_symlink_marker_without_following(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, _launcher, _binding = self.write_internal_store_fixture(root)
            external = root / "external-marker.json"
            external_payload = b'{"schema_version":1}\n'
            external.write_bytes(external_payload)
            marker = store.root / ".runtime-binding-rebind.json"
            marker.symlink_to(external)

            with self.assertRaises(SwitchError) as raised:
                with locked_store_mutation(store, operation="test recovery"):
                    pass

            self.assertIn("marker must be a regular file", str(raised.exception))
            self.assertTrue(marker.is_symlink())
            self.assertEqual(external.read_bytes(), external_payload)

    def test_rebind_recovery_rejects_dangling_symlink_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store, _backend, _launcher, _binding = self.write_internal_store_fixture(root)
            marker = store.root / ".runtime-binding-rebind.json"
            missing_target = root / "missing-marker.json"
            marker.symlink_to(missing_target)

            with self.assertRaisesRegex(
                SwitchError,
                "marker must be a regular file",
            ):
                with locked_store_mutation(store, operation="test recovery"):
                    pass

            self.assertTrue(marker.is_symlink())
            self.assertFalse(missing_target.exists())


if __name__ == "__main__":
    unittest.main()
