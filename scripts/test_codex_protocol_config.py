#!/usr/bin/env python3

from __future__ import annotations

import ast
import copy
import json
import hashlib
import os
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import codex_switch_protocol_adapter as protocol_adapter_module
from codex_switch_app_wrapper import write_profile_app_wrapper
from codex_switch_constants import SwitchError
from codex_switch_protocol_adapter import (
    BackendCapabilities,
    CapabilityReceipt,
    PendingRequestTracker,
    ProtocolAdapter,
    extract_schema_capabilities,
    generate_app_server_schema,
    prepare_capability_receipt_artifact,
    probe_config_write_capability,
)
from codex_switch_store import Store


ACTUAL_MODEL = "gpt-5.5-2026-04-24"
DESKTOP_MODEL = "gpt-5.5"


def write_probe_backend(path: Path, mode: str = "success") -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        f"MODE = {mode!r}\n"
        "if 'generate-json-schema' in sys.argv:\n"
        "    output = Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "    output.mkdir(parents=True, exist_ok=True)\n"
        "    (output / 'protocol.json').write_text(json.dumps({\n"
        "        '$defs': {\n"
        "            'ThreadStartParams': {'type': 'object', 'properties': {'dynamicTools': {'type': 'array'}}},\n"
        "            'PluginMarketplaceKind': {'enum': ['local', 'created-by-me-remote']},\n"
        "        }\n"
        "    }, sort_keys=True))\n"
        "    nested = output / 'nested'\n"
        "    nested.mkdir()\n"
        "    (nested / 'write.json').write_text(json.dumps({'title': 'ConfigWriteResponse'}, sort_keys=True))\n"
        "    if MODE == 'mutate-during-schema':\n"
        "        with Path(sys.argv[0]).open('a') as stream:\n"
        "            stream.write('\\n# changed-during-schema\\n')\n"
        "    raise SystemExit(0)\n"
        "if MODE == 'mutate-during-probe':\n"
        "    marker = Path(str(Path(sys.argv[0])) + '.probe-mutated')\n"
        "    if not marker.exists():\n"
        "        with Path(sys.argv[0]).open('a') as stream:\n"
        "            stream.write('\\n# changed-during-probe\\n')\n"
        "        marker.write_text('mutated\\n')\n"
        "initialized = False\n"
        "write_count = 0\n"
        "for raw in sys.stdin:\n"
        "    message = json.loads(raw)\n"
        "    method = message.get('method')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'id': message['id'], 'result': {'userAgent': 'probe-test'}}), flush=True)\n"
        "        continue\n"
        "    if method == 'initialized':\n"
        "        initialized = True\n"
        "        continue\n"
        "    if method != 'config/value/write':\n"
        "        continue\n"
        "    if MODE == 'timeout':\n"
        "        time.sleep(10)\n"
        "        continue\n"
        "    if MODE == 'error':\n"
        "        print(json.dumps({'id': message['id'], 'error': {'code': -32000, 'message': 'rejected'}}), flush=True)\n"
        "        continue\n"
        "    params = message.get('params', {})\n"
        "    config_path = Path(params['filePath'])\n"
        "    if MODE == 'current-marketplace-config':\n"
        "        config_text = config_path.read_text()\n"
        "        if (\n"
        "            'source_type = \"github\"' in config_text\n"
        "            or 'source_type = \"local\"' not in config_text\n"
        "        ):\n"
        "            print(json.dumps({'id': message['id'], 'error': {'code': -32600, 'message': 'invalid marketplace source type'}}), flush=True)\n"
        "            continue\n"
        "    expected_version = None if write_count == 0 else 'probe-version-' + str(write_count)\n"
        "    if (\n"
        "        not initialized\n"
        "        or config_path.parent.resolve() != Path(os.environ['CODEX_HOME']).resolve()\n"
        "        or params.get('keyPath') != 'features.codex_switch_config_write_probe'\n"
        "        or params.get('value') is not True\n"
        "        or params.get('mergeStrategy') != 'replace'\n"
        "        or 'expectedVersion' not in params\n"
        "        or params.get('expectedVersion') != expected_version\n"
        "    ):\n"
        "        print(json.dumps({'id': message['id'], 'error': {'code': -32002, 'message': 'invalid probe state'}}), flush=True)\n"
        "        continue\n"
        "    version_log = os.environ.get('CODEX_SWITCH_PROBE_VERSION_LOG')\n"
        "    if version_log:\n"
        "        with Path(version_log).open('a') as stream:\n"
        "            stream.write(json.dumps(params.get('expectedVersion')) + '\\n')\n"
        "    text = config_path.read_text()\n"
        "    text = text.replace('codex_switch_config_write_probe = false', 'codex_switch_config_write_probe = true')\n"
        "    if MODE == 'drops-unrelated':\n"
        "        text = '[features]\\ncodex_switch_config_write_probe = true\\n'\n"
        "    if MODE == 'changes-unrelated':\n"
        "        text = text.replace(\n"
        "            '[plugins.\"codex-switch-probe@local\"]\\nenabled = false',\n"
        "            '[plugins.\"codex-switch-probe@local\"]\\nenabled = true',\n"
        "        )\n"
        "    config_path.write_text(text)\n"
        "    write_count += 1\n"
        "    response = {\n"
        "        'id': message['id'],\n"
        "        'result': {\n"
        "            'filePath': str(config_path.resolve()),\n"
        "            'status': 'ok',\n"
        "            'version': 'probe-version-' + str(write_count),\n"
        "        },\n"
        "    }\n"
        "    if MODE == 'malformed':\n"
        "        response['result'].pop('version')\n"
        "    if MODE == 'wrong-path':\n"
        "        response['result']['filePath'] = str(config_path.parent / 'other.toml')\n"
        "    print(json.dumps(response), flush=True)\n"
    )
    path.chmod(0o755)
    return path


def write_leaky_backend(path: Path, phase: str) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        f"PHASE = {phase!r}\n"
        "pid_path = Path(os.environ['CODEX_SWITCH_LEAK_PID'])\n"
        "def spawn_child():\n"
        "    child = subprocess.Popen(\n"
        "        [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "        stdin=subprocess.DEVNULL,\n"
        "        stdout=subprocess.DEVNULL,\n"
        "        stderr=subprocess.DEVNULL,\n"
        "    )\n"
        "    pid_path.write_text(str(child.pid))\n"
        "if 'generate-json-schema' in sys.argv:\n"
        "    if PHASE == 'schema':\n"
        "        spawn_child()\n"
        "        time.sleep(30)\n"
        "    raise SystemExit(0)\n"
        "if PHASE == 'probe':\n"
        "    spawn_child()\n"
        "raise SystemExit(0)\n"
    )
    path.chmod(0o755)
    return path


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_process_exit(pid: int, timeout_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return True
        time.sleep(0.02)
    return not process_is_alive(pid)


def write_proxy_backend(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print(os.environ.get('PROXY_BACKEND_VERSION', 'codex-cli 0.142.4'))\n"
        "    raise SystemExit(0)\n"
        "log_path = Path(os.environ['PROXY_BACKEND_LOG'])\n"
        "mode = os.environ.get('PROXY_BACKEND_MODE', 'success')\n"
        "actual_model = os.environ.get('PROXY_BACKEND_ACTUAL_MODEL', 'gpt-5.5-2026-04-24')\n"
        "raw_log = os.environ.get('PROXY_BACKEND_RAW_LOG')\n"
        "metadata_path = os.environ.get('PROXY_BACKEND_METADATA')\n"
        "if metadata_path:\n"
        "    Path(metadata_path).write_text(json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'codex_home': os.environ.get('CODEX_HOME'),\n"
        "        'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "    }, sort_keys=True) + '\\n')\n"
        "if mode == 'exit-before-read':\n"
        "    sys.stderr.buffer.write(b'backend-exit-before-read\\n')\n"
        "    sys.stderr.buffer.flush()\n"
        "    time.sleep(0.05)\n"
        "    raise SystemExit(int(os.environ.get('PROXY_BACKEND_EXIT_CODE', '29')))\n"
        "pending = []\n"
        "def write_json(message):\n"
        "    sys.stdout.buffer.write((json.dumps(message, separators=(',', ':')) + '\\n').encode())\n"
        "    sys.stdout.buffer.flush()\n"
        "for raw in sys.stdin.buffer:\n"
        "    if raw_log:\n"
        "        with Path(raw_log).open('ab') as stream:\n"
        "            stream.write(raw)\n"
        "    message = json.loads(raw)\n"
        "    with log_path.open('a') as stream:\n"
        "        stream.write(json.dumps(message, sort_keys=True) + '\\n')\n"
        "    method = message.get('method')\n"
        "    if method == 'initialize':\n"
        "        write_json({'id': message['id'], 'result': {'userAgent': 'proxy-chain-test'}})\n"
        "        continue\n"
        "    if method == 'thread/start':\n"
        "        write_json({'id': message['id'], 'result': {'thread': {'model': actual_model}}})\n"
        "        continue\n"
        "    if method == 'plugin/list':\n"
        "        write_json({'id': message['id'], 'result': {\n"
        "            'marketplaceKinds': message.get('params', {}).get('marketplaceKinds', []),\n"
        "        }})\n"
        "        continue\n"
        "    if method == 'model/list':\n"
        "        write_json({'id': message['id'], 'result': {'data': [{\n"
        "            'id': actual_model,\n"
        "            'model': actual_model,\n"
        "            'metadata': {'model': actual_model},\n"
        "        }]}})\n"
        "        continue\n"
        "    if method == 'unknown/raw':\n"
        "        response = (\n"
        "            b' { \"id\" : \"raw\", \"result\" : { \"model\" : \"'\n"
        "            + actual_model.encode()\n"
        "            + b'\", \"payload\" : { \"model\" : \"opaque\" } } }\\r\\n'\n"
        "        )\n"
        "        sys.stdout.buffer.write(response)\n"
        "        sys.stdout.buffer.flush()\n"
        "        continue\n"
        "    if method == 'config/value/write' or method == 'config/batchWrite':\n"
        "        if mode == 'backend-error':\n"
        "            write_json({'id': message['id'], 'error': {'code': -32001, 'message': 'backend rejected'}})\n"
        "            continue\n"
        "        if mode == 'reverse':\n"
        "            pending.append(message)\n"
        "            if len(pending) == 2:\n"
        "                write_json({'id': pending[0]['id'], 'method': 'server/request', 'params': {'probe': True}})\n"
        "                for item in reversed(pending):\n"
        "                    write_json({\n"
        "                        'id': item['id'],\n"
        "                        'result': {\n"
        "                            'filePath': item.get('params', {}).get('filePath'),\n"
        "                            'status': 'ok',\n"
        "                            'version': 'version-' + str(item['id']),\n"
        "                        },\n"
        "                    })\n"
        "            continue\n"
        "        result = {\n"
        "            'filePath': message.get('params', {}).get('filePath'),\n"
        "            'status': 'ok',\n"
        "            'version': 'backend-version-7',\n"
        "        }\n"
        "        if mode == 'invalid-path':\n"
        "            result['filePath'] = '/wrong/config.toml'\n"
        "        elif mode == 'invalid-status':\n"
        "            result['status'] = 'rejected'\n"
        "        elif mode == 'invalid-version':\n"
        "            result['version'] = None\n"
        "        write_json({'id': message['id'], 'result': result})\n"
        "if mode == 'hold-streams':\n"
        "    child = subprocess.Popen(\n"
        "        [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "        stdin=subprocess.DEVNULL,\n"
        "    )\n"
        "    hold_pid_path = os.environ.get('PROXY_BACKEND_HOLD_PID')\n"
        "    if hold_pid_path:\n"
        "        Path(hold_pid_path).write_text(str(child.pid))\n"
        "elif mode == 'eof-exit':\n"
        "    marker_path = os.environ.get('PROXY_BACKEND_EOF_MARKER')\n"
        "    if marker_path:\n"
        "        Path(marker_path).write_text('eof\\n')\n"
        "    blob_size = int(os.environ.get('PROXY_BACKEND_EOF_BLOB_SIZE', '2000000'))\n"
        "    write_json({'id': 'eof-final', 'result': {'blob': 'x' * blob_size}})\n"
        "    sys.stderr.buffer.write(b'backend-eof-stderr-final\\n')\n"
        "    sys.stderr.buffer.flush()\n"
        "raise SystemExit(int(os.environ.get('PROXY_BACKEND_EXIT_CODE', '0')))\n"
    )
    path.chmod(0o755)
    return path


def write_proxy_receipt(
    path: Path,
    backend: Path,
    *,
    schema_sha256: str,
    config_write: bool | None,
    dynamic_tools: bool | None = True,
    marketplace: bool | None = True,
    schema_version: int = 2,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "backend_sha256": hashlib.sha256(backend.read_bytes()).hexdigest(),
                "schema_sha256": schema_sha256,
                "capabilities": BackendCapabilities(
                    dynamic_tools,
                    marketplace,
                    config_write,
                ).to_dict(),
            },
            sort_keys=True,
        )
        + "\n"
    )
    path.chmod(0o600)
    return path


def prepare_generated_proxy_chain(
    root: Path,
    *,
    version: str,
    dynamic_tools: bool | None = None,
    marketplace: bool | None = None,
    config_write: bool | None = None,
    with_receipt: bool,
) -> dict[str, object]:
    live_home = root / "live"
    app_home = root / "store" / "homes" / "internal"
    live_home.mkdir(parents=True)
    (live_home / "config.toml").write_text(
        f'model = "{ACTUAL_MODEL}"\n'
        'model_provider = "azure"\n'
        "\n"
        "[desktop]\n"
        'followUpQueueMode = "off"\n'
    )
    (live_home / "AGENTS.md").write_text("shared\n")

    store = Store(
        root / "store",
        live_home,
        root / "agent.plist",
        internal_codex_home=app_home,
        internal_codex_home_source="explicit",
    )
    store.ensure()
    profile_dir = store.profile_dir("internal")
    profile_dir.mkdir(parents=True)
    profile_config = profile_dir / "config.toml"
    profile_config.write_text(
        f'model = "{ACTUAL_MODEL}"\n'
        'model_provider = "azure"\n'
    )

    backend = write_proxy_backend(root / "codex")
    wrapper = store.bin_dir / "codex-internal-app"
    (profile_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "internal",
                "codex_bin": str(backend),
                "codex_home": str(app_home),
                "app_cli_path": str(wrapper),
                "app_cli_binding": "launchagent",
            },
            sort_keys=True,
        )
        + "\n"
    )

    schema_sha256 = "9" * 64
    receipt_path: Path | None = None
    receipt_sha256 = ""
    if with_receipt:
        receipt_path = write_proxy_receipt(
            root / "capabilities.json",
            backend,
            schema_sha256=schema_sha256,
            dynamic_tools=dynamic_tools,
            marketplace=marketplace,
            config_write=config_write,
        )
        receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()

    python_path = shutil.which("python3.12")
    if python_path is None and sys.version_info >= (3, 11):
        python_path = sys.executable
    if python_path is None:
        raise unittest.SkipTest("generated wrapper requires Python 3.11+")
    with mock.patch.dict(
        os.environ,
        {"CODEX_SWITCH_PYTHON": python_path},
        clear=False,
    ):
        write_profile_app_wrapper(
            store=store,
            name="internal",
            app_cli_path=str(wrapper),
            codex_bin=str(backend),
            switch_scripts=Path(__file__).resolve().parent,
            capability_receipt_path=receipt_path,
            schema_sha256=schema_sha256 if with_receipt else "",
            capability_receipt_sha256=receipt_sha256,
        )

    backend_log = root / "backend.jsonl"
    raw_log = root / "backend.raw.jsonl"
    metadata_path = root / "backend-metadata.json"
    eof_marker = root / "backend-eof.txt"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = "/original/proxy-chain"
    environment["PROXY_BACKEND_VERSION"] = version
    environment["PROXY_BACKEND_ACTUAL_MODEL"] = ACTUAL_MODEL
    environment["PROXY_BACKEND_LOG"] = str(backend_log)
    environment["PROXY_BACKEND_RAW_LOG"] = str(raw_log)
    environment["PROXY_BACKEND_METADATA"] = str(metadata_path)
    environment["PROXY_BACKEND_EOF_MARKER"] = str(eof_marker)
    return {
        "wrapper": wrapper,
        "app_home": app_home,
        "environment": environment,
        "backend_log": backend_log,
        "raw_log": raw_log,
        "metadata_path": metadata_path,
        "eof_marker": eof_marker,
    }


def adapter(
    *,
    dynamic_tools: bool | None = None,
    marketplace: bool | None = None,
    config_write: bool | None = None,
) -> ProtocolAdapter:
    return ProtocolAdapter(
        actual_model=ACTUAL_MODEL,
        desktop_model=DESKTOP_MODEL,
        capabilities=BackendCapabilities(
            canonical_dynamic_tools=dynamic_tools,
            remote_marketplace_kind=marketplace,
            versioned_config_write_preserves_unrelated=config_write,
        ),
    )


class ProtocolAdapterEvidenceTests(unittest.TestCase):
    def rule_set_digest(self):
        digest_function = getattr(
            protocol_adapter_module,
            "protocol_adapter_rule_set_digest",
            None,
        )
        self.assertTrue(
            callable(digest_function),
            "Protocol Adapter rule-set digest seam is missing",
        )
        return digest_function

    def test_rule_set_digest_is_deterministic_and_policy_free(self) -> None:
        digest_function = self.rule_set_digest()

        first = digest_function()
        second = digest_function()

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

        module_path = Path(protocol_adapter_module.__file__).resolve()
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        self.assertNotIn("codex_switch_parity", imported_modules)

    def test_rule_set_digest_covers_exact_tables_canonically(self) -> None:
        digest_function = self.rule_set_digest()
        baseline = digest_function()

        reordered = {
            name: dict(reversed(tuple(getattr(protocol_adapter_module, name).items())))
            for name in (
                "REQUEST_MODEL_PATHS",
                "RESPONSE_MODEL_PATHS",
                "NOTIFICATION_MODEL_PATHS",
            )
        }
        with mock.patch.multiple(protocol_adapter_module, **reordered):
            self.assertEqual(baseline, digest_function())

        mutations = {
            "REQUEST_MODEL_PATHS": {
                **protocol_adapter_module.REQUEST_MODEL_PATHS,
                "__digest_probe__": (("params", "model"),),
            },
            "RESPONSE_MODEL_PATHS": {
                **protocol_adapter_module.RESPONSE_MODEL_PATHS,
                "__digest_probe__": (("result", "model"),),
            },
            "NOTIFICATION_MODEL_PATHS": {
                **protocol_adapter_module.NOTIFICATION_MODEL_PATHS,
                "__digest_probe__": (("params", "model"),),
            },
            "CONFIG_WRITE_METHODS": frozenset(
                {
                    *protocol_adapter_module.CONFIG_WRITE_METHODS,
                    "__digest_probe__",
                }
            ),
            "REMOTE_MARKETPLACE_KIND": "__digest_probe__",
        }
        for name, value in mutations.items():
            with self.subTest(name=name), mock.patch.object(
                protocol_adapter_module,
                name,
                value,
            ):
                if name == "CONFIG_WRITE_METHODS":
                    with self.assertRaises(SwitchError):
                        digest_function()
                else:
                    self.assertNotEqual(baseline, digest_function())

    def test_rule_manifest_binds_every_actual_transform(self) -> None:
        manifest_function = getattr(
            protocol_adapter_module,
            "protocol_adapter_rule_manifest",
            None,
        )
        self.assertTrue(
            callable(manifest_function),
            "Protocol Adapter structured rule manifest is missing",
        )

        rules = manifest_function()
        expected_rule_ids = {
            "client_request.config_batch_write_model_alias",
            "client_request.config_value_write_model_alias",
            "client_request.dynamic_tools_legacy",
            "client_request.model_alias.realtime_start",
            "client_request.model_alias.thread_start",
            "client_request.model_alias.turn_start",
            "client_request.remote_marketplace_kind_filter",
            "client_request.thread_resume_history_portability",
            "server_notification.model_alias.item_completed",
            "server_notification.model_alias.item_started",
            "server_notification.model_alias.thread_started",
            "server_notification.model_alias.thread_updated",
            "server_notification.model_alias.turn_completed",
            "server_notification.model_alias.turn_started",
            "server_response.model_alias.config_read",
            "server_response.model_alias.realtime_start",
            "server_response.model_alias.thread_load",
            "server_response.model_alias.thread_read",
            "server_response.model_alias.thread_start",
            "server_response.model_alias.turn_start",
            "server_response.model_list_alias",
        }

        self.assertEqual(
            {rule.rule_id for rule in rules},
            expected_rule_ids,
        )
        self.assertEqual(
            tuple(rule.rule_id for rule in rules),
            tuple(
                rule.rule_id
                for rule in sorted(
                    rules,
                    key=lambda rule: (
                        rule.direction,
                        rule.method,
                        rule.rule_id,
                    ),
                )
            ),
        )
        for rule in rules:
            with self.subTest(rule_id=rule.rule_id):
                self.assertRegex(rule.sha256, r"^[0-9a-f]{64}$")
                self.assertTrue(rule.paths)
                self.assertTrue(rule.variants)
                self.assertTrue(rule.capability_predicate)

        manifest_payload = {
            "rules": [
                dict(rule.canonical_payload())
                for rule in rules
            ],
            "schema_version": 1,
        }
        expected_digest = hashlib.sha256(
            json.dumps(
                manifest_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            protocol_adapter_module.protocol_adapter_rule_set_digest(),
            expected_digest,
        )

    def test_thread_resume_transform_and_manifest_share_one_rule(self) -> None:
        manifest_function = getattr(
            protocol_adapter_module,
            "protocol_adapter_rule_manifest",
            None,
        )
        rule_id = getattr(
            protocol_adapter_module,
            "THREAD_RESUME_HISTORY_RULE_ID",
            None,
        )
        self.assertTrue(callable(manifest_function))
        self.assertEqual(
            rule_id,
            "client_request.thread_resume_history_portability",
        )
        rules = {
            rule.rule_id: rule
            for rule in manifest_function()
        }
        rule = rules[rule_id]
        self.assertEqual(rule.direction, "client_request")
        self.assertEqual(rule.method, "thread/resume")
        self.assertEqual(rule.paths, (("params", "history"),))
        self.assertEqual(
            rule.variants,
            (
                "drop_nonportable_opaque_reasoning",
                "remove_top_level_item_id",
            ),
        )
        self.assertEqual(rule.capability_predicate, "always")

        original_message = {
            "id": 9,
            "method": "thread/resume",
            "params": {
                "history": [
                    {
                        "type": "message",
                        "id": "message-id",
                        "content": [],
                    },
                    {
                        "type": "reasoning",
                        "id": "opaque-id",
                        "summary": [],
                        "encrypted_content": None,
                    },
                ]
            },
        }
        result = adapter().client_request(original_message)
        self.assertTrue(result.changed)
        self.assertEqual(
            result.message["params"]["history"],
            [{"type": "message", "content": []}],
        )

        patched_rule = replace(rule, method="thread/resume-probe")
        with mock.patch.object(
            protocol_adapter_module,
            "THREAD_RESUME_HISTORY_RULE",
            patched_rule,
        ):
            patched_manifest = {
                item.rule_id: item
                for item in manifest_function()
            }
            self.assertEqual(
                patched_manifest[rule_id].method,
                "thread/resume-probe",
            )
            self.assertFalse(
                adapter().client_request(original_message).changed
            )
            probe_message = copy.deepcopy(original_message)
            probe_message["method"] = "thread/resume-probe"
            self.assertTrue(adapter().client_request(probe_message).changed)


class ProtocolAdapterTests(unittest.TestCase):
    def test_config_value_model_write_changes_only_exact_value(self) -> None:
        message = {
            "id": 1,
            "method": "config/value/write",
            "params": {
                "keyPath": "model",
                "value": DESKTOP_MODEL,
                "metadata": {"model": DESKTOP_MODEL},
            },
        }

        result = adapter(config_write=True).client_request(message)

        self.assertTrue(result.changed)
        self.assertEqual(result.message["params"]["value"], ACTUAL_MODEL)
        self.assertEqual(
            result.message["params"]["metadata"]["model"], DESKTOP_MODEL
        )
        self.assertEqual(message["params"]["value"], DESKTOP_MODEL)

    def test_config_value_non_model_write_is_byte_semantically_unchanged(self) -> None:
        message = {
            "id": 2,
            "method": "config/value/write",
            "params": {"keyPath": "desktop.model", "value": DESKTOP_MODEL},
        }

        result = adapter(config_write=True).client_request(message)

        self.assertFalse(result.changed)
        self.assertIs(result.message, message)

    def test_config_batch_model_edit_changes_only_adjacent_value(self) -> None:
        message = {
            "id": 3,
            "method": "config/batchWrite",
            "params": {
                "edits": [
                    {"keyPath": "model", "value": DESKTOP_MODEL},
                    {
                        "keyPath": "desktop.model",
                        "value": DESKTOP_MODEL,
                        "metadata": {"model": DESKTOP_MODEL},
                    },
                ]
            },
        }

        result = adapter(config_write=True).client_request(message)

        edits = result.message["params"]["edits"]
        self.assertEqual(edits[0]["value"], ACTUAL_MODEL)
        self.assertEqual(edits[1]["value"], DESKTOP_MODEL)
        self.assertEqual(edits[1]["metadata"]["model"], DESKTOP_MODEL)

    def test_documented_thread_and_turn_request_paths_are_translated(self) -> None:
        for method in ("thread/start", "turn/start", "realtime/start"):
            with self.subTest(method=method):
                message = {
                    "id": method,
                    "method": method,
                    "params": {
                        "model": DESKTOP_MODEL,
                        "payload": {"model": DESKTOP_MODEL},
                    },
                }

                result = adapter().client_request(message)

                self.assertEqual(result.message["params"]["model"], ACTUAL_MODEL)
                self.assertEqual(
                    result.message["params"]["payload"]["model"], DESKTOP_MODEL
                )

    def test_unknown_and_tool_payload_model_fields_are_not_rewritten(self) -> None:
        fixtures = (
            {
                "id": 4,
                "method": "custom/tool",
                "params": {"model": DESKTOP_MODEL, "type": "namespace"},
            },
            {
                "id": 5,
                "method": "thread/start",
                "params": {
                    "dynamicTools": [
                        {
                            "type": "function",
                            "name": "echo",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"model": {"const": DESKTOP_MODEL}},
                            },
                        }
                    ]
                },
            },
        )
        for message in fixtures:
            with self.subTest(method=message["method"]):
                original = json.loads(json.dumps(message))
                result = adapter().client_request(message)
                self.assertFalse(result.changed)
                self.assertIs(result.message, message)
                self.assertEqual(result.message, original)

    def test_thread_resume_history_removes_only_top_level_item_ids(self) -> None:
        message = {
            "id": 6,
            "method": "thread/resume",
            "params": {
                "threadId": "thread-1",
                "history": [
                    {
                        "type": "message",
                        "id": "019f8dfe-5fb3-7443-9889-6d89991bd9e8",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hook"}],
                        "metadata": {"id": "preserve-nested"},
                    },
                    {
                        "type": "function_call_output",
                        "id": "fco_local",
                        "call_id": "call_preserve",
                        "output": "ok",
                    },
                ],
                "metadata": {"id": "preserve-outside-history"},
            },
        }

        result = adapter().client_request(message)

        self.assertTrue(result.changed)
        history = result.message["params"]["history"]
        self.assertNotIn("id", history[0])
        self.assertNotIn("id", history[1])
        self.assertEqual(history[0]["metadata"]["id"], "preserve-nested")
        self.assertEqual(history[1]["call_id"], "call_preserve")
        self.assertEqual(
            result.message["params"]["metadata"]["id"],
            "preserve-outside-history",
        )
        self.assertEqual(
            message["params"]["history"][0]["id"],
            "019f8dfe-5fb3-7443-9889-6d89991bd9e8",
        )

    def test_thread_resume_history_drops_only_opaque_reasoning_items(self) -> None:
        message = {
            "id": 7,
            "method": "thread/resume",
            "params": {
                "threadId": "thread-1",
                "history": [
                    {
                        "type": "reasoning",
                        "id": "rs_unavailable",
                        "summary": [],
                        "encrypted_content": None,
                    },
                    {
                        "type": "reasoning",
                        "id": "rs_portable",
                        "summary": [],
                        "encrypted_content": "encrypted",
                    },
                    {
                        "type": "message",
                        "id": "msg_server",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "visible"}],
                    },
                ],
            },
        }

        result = adapter().client_request(message)

        history = result.message["params"]["history"]
        self.assertEqual(["reasoning", "message"], [item["type"] for item in history])
        self.assertEqual("encrypted", history[0]["encrypted_content"])
        self.assertNotIn("id", history[0])
        self.assertEqual("visible", history[1]["content"][0]["text"])
        self.assertNotIn("id", history[1])
        self.assertEqual(3, len(message["params"]["history"]))

    def test_non_resume_history_is_unchanged(self) -> None:
        message = {
            "id": 8,
            "method": "custom/resume",
            "params": {"history": [{"type": "message", "id": "local-id"}]},
        }

        result = adapter().client_request(message)

        self.assertFalse(result.changed)
        self.assertIs(result.message, message)

    def test_model_list_and_config_read_mask_only_schema_paths(self) -> None:
        tracker = PendingRequestTracker()
        model_request = {"id": 6, "method": "model/list", "params": {}}
        tracker.observe_client(model_request)
        model_response = {
            "id": 6,
            "result": {
                "data": [
                    {
                        "id": ACTUAL_MODEL,
                        "model": ACTUAL_MODEL,
                        "metadata": {"model": ACTUAL_MODEL},
                    }
                ],
                "models": [
                    {
                        "id": ACTUAL_MODEL,
                        "model": ACTUAL_MODEL,
                        "metadata": {"model": ACTUAL_MODEL},
                    }
                ],
            },
        }
        result = adapter().server_message(
            model_response,
            pending_method=tracker.consume_backend_response(model_response),
        )
        [entry] = result.message["result"]["data"]
        self.assertEqual(entry["id"], DESKTOP_MODEL)
        self.assertEqual(entry["model"], DESKTOP_MODEL)
        self.assertEqual(entry["metadata"]["model"], ACTUAL_MODEL)
        [legacy_entry] = result.message["result"]["models"]
        self.assertEqual(legacy_entry["id"], DESKTOP_MODEL)
        self.assertEqual(legacy_entry["model"], DESKTOP_MODEL)
        self.assertEqual(legacy_entry["metadata"]["model"], ACTUAL_MODEL)

        config_response = {
            "id": 7,
            "result": {
                "config": {
                    "model": ACTUAL_MODEL,
                    "metadata": {"model": ACTUAL_MODEL},
                }
            },
        }
        masked = adapter().server_message(
            config_response,
            pending_method="config/read",
        )
        self.assertEqual(masked.message["result"]["config"]["model"], DESKTOP_MODEL)
        self.assertEqual(
            masked.message["result"]["config"]["metadata"]["model"],
            ACTUAL_MODEL,
        )

    def test_documented_thread_response_fields_are_masked_without_writes(self) -> None:
        message = {
            "id": 8,
            "result": {
                "conversation": {
                    "model": ACTUAL_MODEL,
                    "latestModel": ACTUAL_MODEL,
                    "previousTurnModel": ACTUAL_MODEL,
                    "settings": {"model": ACTUAL_MODEL},
                },
                "writes": [{"key": "model", "value": ACTUAL_MODEL}],
            },
        }

        result = adapter().server_message(message, pending_method="thread/load")

        conversation = result.message["result"]["conversation"]
        self.assertEqual(conversation["model"], DESKTOP_MODEL)
        self.assertEqual(conversation["latestModel"], DESKTOP_MODEL)
        self.assertEqual(conversation["previousTurnModel"], DESKTOP_MODEL)
        self.assertEqual(conversation["settings"]["model"], DESKTOP_MODEL)
        self.assertEqual(
            result.message["result"]["writes"][0]["value"], ACTUAL_MODEL
        )

    def test_error_payload_is_unchanged(self) -> None:
        message = {
            "id": 9,
            "error": {
                "code": -32000,
                "message": "failed",
                "data": {"model": ACTUAL_MODEL, "type": "namespace"},
            },
        }

        result = adapter().server_message(message, pending_method="thread/load")

        self.assertFalse(result.changed)
        self.assertIs(result.message, message)

    def test_pending_tracker_is_direction_aware_and_rejects_boolean_ids(self) -> None:
        tracker = PendingRequestTracker()
        tracker.observe_client({"id": 10, "method": "thread/load"})
        tracker.observe_client({"id": True, "method": "config/read"})

        self.assertIsNone(
            tracker.consume_backend_response({"id": 10, "method": "server/request"})
        )
        self.assertIsNone(tracker.consume_backend_response({"id": True, "result": {}}))
        self.assertEqual(
            tracker.consume_backend_response({"id": 10, "result": {}}),
            "thread/load",
        )
        self.assertIsNone(tracker.consume_backend_response({"id": 10, "result": {}}))

    def test_orphan_response_does_not_consume_another_pending_method(self) -> None:
        tracker = PendingRequestTracker()
        tracker.observe_client({"id": "a", "method": "model/list"})

        self.assertIsNone(
            tracker.consume_backend_response({"id": "b", "result": {}})
        )
        self.assertEqual(
            tracker.consume_backend_response({"id": "a", "error": {"code": 1}}),
            "model/list",
        )

    def test_legacy_dynamic_tools_transform_is_exact_and_independent(self) -> None:
        dynamic_tools = [
            {
                "type": "namespace",
                "name": "search",
                "tools": [
                    {
                        "type": "function",
                        "name": "query",
                        "inputSchema": {"type": "object"},
                    }
                ],
            }
        ]
        message = {
            "id": 11,
            "method": "thread/start",
            "params": {"dynamicTools": dynamic_tools},
        }

        legacy = adapter(dynamic_tools=False, marketplace=True).client_request(message)
        self.assertEqual(
            legacy.message["params"]["dynamicTools"],
            [
                {
                    "namespace": "search",
                    "type": "function",
                    "name": "query",
                    "inputSchema": {"type": "object"},
                }
            ],
        )
        for state in (True, None):
            with self.subTest(state=state):
                preserved = adapter(dynamic_tools=state).client_request(message)
                self.assertFalse(preserved.changed)
                self.assertIs(preserved.message, message)

    def test_marketplace_capability_is_independent_and_unknown_preserves(self) -> None:
        message = {
            "id": 12,
            "method": "plugin/list",
            "params": {
                "marketplaceKinds": ["local", "created-by-me-remote"]
            },
        }

        legacy = adapter(dynamic_tools=True, marketplace=False).client_request(message)
        self.assertEqual(
            legacy.message["params"]["marketplaceKinds"], ["local"]
        )
        for state in (True, None):
            with self.subTest(state=state):
                preserved = adapter(marketplace=state).client_request(message)
                self.assertFalse(preserved.changed)

    def test_schema_capabilities_are_independent_three_state_values(self) -> None:
        modern = {
            "$defs": {
                "ThreadStartParams": {
                    "type": "object",
                    "properties": {"dynamicTools": {"type": "array"}},
                },
                "PluginMarketplaceKind": {
                    "enum": ["local", "created-by-me-remote"]
                },
            }
        }
        legacy = {
            "$defs": {
                "ThreadStartParams": {"type": "object", "properties": {}},
                "PluginMarketplaceKind": {"enum": ["local"]},
            }
        }

        self.assertEqual(
            extract_schema_capabilities(modern),
            BackendCapabilities(True, True, None),
        )
        self.assertEqual(
            extract_schema_capabilities(legacy),
            BackendCapabilities(False, False, None),
        )
        current = {
            "$defs": {
                "ThreadStartParams": {
                    "type": "object",
                    "properties": {"dynamicTools": {"type": "array"}},
                },
                "PluginListMarketplaceKind": {
                    "enum": ["local", "created-by-me-remote"]
                },
            }
        }
        self.assertEqual(
            extract_schema_capabilities(current),
            BackendCapabilities(True, True, None),
        )
        self.assertEqual(
            extract_schema_capabilities({"$defs": {}}),
            BackendCapabilities(None, None, None),
        )

    def test_behavioral_probe_uses_current_valid_marketplace_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = write_probe_backend(
                Path(temp_dir) / "codex",
                "current-marketplace-config",
            )

            self.assertTrue(
                probe_config_write_capability(
                    backend,
                    timeout_seconds=5.0,
                )
            )

    def test_capability_receipt_is_bound_to_backend_and_schema_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = root / "codex"
            backend.write_bytes(b"backend-v1")
            schema = json.dumps(
                {
                    "$defs": {
                        "ThreadStartParams": {
                            "type": "object",
                            "properties": {"dynamicTools": {"type": "array"}},
                        }
                    }
                },
                sort_keys=True,
            ).encode()

            receipt = CapabilityReceipt.from_schema(backend, schema)

            self.assertTrue(receipt.matches(backend, schema))
            backend.write_bytes(b"backend-v2")
            self.assertFalse(receipt.matches(backend, schema))
            backend.write_bytes(b"backend-v1")
            self.assertFalse(receipt.matches(backend, schema + b"\n"))

    def test_capability_receipt_artifact_reuses_valid_bound_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = root / "codex"
            backend.write_bytes(b"backend-v1")
            schema = json.dumps({"$defs": {}}, sort_keys=True).encode()
            receipt = CapabilityReceipt.from_schema(backend, schema)
            payload = (
                json.dumps(receipt.to_dict(), indent=2, sort_keys=True).encode()
                + b"\n"
            )
            receipt_path = root / "receipt.json"
            receipt_path.write_bytes(payload)

            with mock.patch.object(
                CapabilityReceipt,
                "from_backend",
                side_effect=AssertionError("valid receipt must be reused"),
            ):
                artifact = prepare_capability_receipt_artifact(
                    backend,
                    receipt_path=receipt_path,
                    expected_payload_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_schema_sha256=receipt.schema_sha256,
                    schema_timeout_seconds=0.1,
                    probe_timeout_seconds=0.1,
                )

            self.assertTrue(artifact.reused)
            self.assertEqual(payload, artifact.payload)
            self.assertEqual(receipt, artifact.receipt)
            self.assertEqual(
                hashlib.sha256(payload).hexdigest(),
                artifact.payload_sha256,
            )

    def test_capability_receipt_artifact_refreshes_after_in_place_backend_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = root / "codex"
            backend.write_bytes(b"backend-v1")
            schema = json.dumps({"$defs": {}}, sort_keys=True).encode()
            old_receipt = CapabilityReceipt.from_schema(backend, schema)
            old_payload = (
                json.dumps(old_receipt.to_dict(), indent=2, sort_keys=True).encode()
                + b"\n"
            )
            receipt_path = root / "receipt.json"
            receipt_path.write_bytes(old_payload)
            backend.write_bytes(b"backend-v2")

            artifact = prepare_capability_receipt_artifact(
                backend,
                receipt_path=receipt_path,
                expected_payload_sha256=hashlib.sha256(old_payload).hexdigest(),
                expected_schema_sha256=old_receipt.schema_sha256,
                schema_timeout_seconds=0.1,
                probe_timeout_seconds=0.1,
            )

            self.assertFalse(artifact.reused)
            self.assertNotEqual(old_payload, artifact.payload)
            self.assertEqual(
                hashlib.sha256(b"backend-v2").hexdigest(),
                artifact.receipt.backend_sha256,
            )
            self.assertEqual(
                BackendCapabilities(None, None, None),
                artifact.receipt.capabilities,
            )
            self.assertRegex(artifact.receipt.schema_sha256, r"^[0-9a-f]{64}$")

    def test_behavioral_receipt_proves_safe_write_and_persists_only_sanitized_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_probe_backend(root / "codex")
            schema = json.dumps(
                {
                    "$defs": {
                        "ThreadStartParams": {
                            "type": "object",
                            "properties": {"dynamicTools": {"type": "array"}},
                        },
                        "PluginMarketplaceKind": {
                            "enum": ["local", "created-by-me-remote"]
                        },
                    }
                },
                sort_keys=True,
            ).encode()
            factory = getattr(
                CapabilityReceipt,
                "from_backend_probe",
                None,
            )
            self.assertIsNotNone(factory, "behavioral receipt factory is missing")

            receipt = factory(backend, schema, timeout_seconds=1.0)

            self.assertTrue(receipt.matches(backend, schema))
            self.assertEqual(
                receipt.capabilities,
                BackendCapabilities(True, True, True),
            )
            payload = receipt.to_dict()
            self.assertEqual(
                set(payload),
                {
                    "schema_version",
                    "backend_sha256",
                    "schema_sha256",
                    "capabilities",
                },
            )
            encoded = json.dumps(payload, sort_keys=True)
            self.assertNotIn(str(root), encoded)
            self.assertNotIn("probe-version-1", encoded)
            self.assertNotIn("cy-codex-skills", encoded)
            self.assertNotIn("CODEX_HOME", encoded)
            self.assertNotIn(os.environ.get("HOME", "<missing-home>"), encoded)

    def test_behavioral_receipt_classifies_unproven_and_unsafe_results(
        self,
    ) -> None:
        schema = json.dumps({"$defs": {}}, sort_keys=True).encode()
        cases = (
            ("timeout", None),
            ("error", None),
            ("malformed", None),
            ("wrong-path", False),
            ("drops-unrelated", False),
            ("changes-unrelated", False),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                backend = write_probe_backend(Path(temp_dir) / "codex", mode)

                receipt = CapabilityReceipt.from_backend_probe(
                    backend,
                    schema,
                    timeout_seconds=1.0,
                )

                self.assertIs(
                    receipt.capabilities.versioned_config_write_preserves_unrelated,
                    expected,
                )

    def test_backend_schema_generation_and_behavior_probe_form_one_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_probe_backend(root / "codex")
            factory = getattr(CapabilityReceipt, "from_backend", None)
            self.assertIsNotNone(factory, "backend receipt factory is missing")

            first = factory(
                backend,
                schema_timeout_seconds=3.0,
                probe_timeout_seconds=3.0,
            )
            second = factory(
                backend,
                schema_timeout_seconds=3.0,
                probe_timeout_seconds=3.0,
            )

            self.assertEqual(first, second)
            self.assertEqual(
                first.capabilities,
                BackendCapabilities(True, True, True),
            )
            self.assertRegex(first.backend_sha256, r"^[0-9a-f]{64}$")
            self.assertRegex(first.schema_sha256, r"^[0-9a-f]{64}$")

    def test_capability_receipt_rejects_backend_drift_during_generation(
        self,
    ) -> None:
        for mode in ("mutate-during-schema", "mutate-during-probe"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                backend = write_probe_backend(Path(temp_dir) / "codex", mode)

                with self.assertRaisesRegex(
                    SwitchError,
                    "backend changed during capability receipt generation",
                ):
                    CapabilityReceipt.from_backend(
                        backend,
                        schema_timeout_seconds=1.0,
                        probe_timeout_seconds=1.0,
                    )

    def test_behavioral_probe_reuses_returned_version_for_followup_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_probe_backend(root / "codex")
            version_log = root / "expected-versions.jsonl"

            with mock.patch.dict(
                os.environ,
                {"CODEX_SWITCH_PROBE_VERSION_LOG": str(version_log)},
            ):
                result = probe_config_write_capability(
                    backend,
                    timeout_seconds=1.0,
                )

            self.assertTrue(result)
            self.assertEqual(
                [
                    json.loads(line)
                    for line in version_log.read_text().splitlines()
                ],
                [None, "probe-version-1"],
            )

    def test_schema_and_behavior_probe_terminate_descendant_processes(
        self,
    ) -> None:
        cases = ("schema", "probe")
        for phase in cases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                backend = write_leaky_backend(root / "codex", phase)
                pid_path = root / "child.pid"
                child_pid = 0
                processes = []
                popen_kwargs = []
                popen = subprocess.Popen

                def tracked_popen(*args, **kwargs):
                    popen_kwargs.append(dict(kwargs))
                    process = popen(*args, **kwargs)
                    processes.append(process)
                    return process

                try:
                    with mock.patch.dict(
                        os.environ,
                        {"CODEX_SWITCH_LEAK_PID": str(pid_path)},
                    ), mock.patch.object(
                        protocol_adapter_module.subprocess,
                        "Popen",
                        side_effect=tracked_popen,
                    ):
                        if phase == "schema":
                            with self.assertRaises(SwitchError):
                                generate_app_server_schema(
                                    backend,
                                    # Leave enough time to launch and record the child
                                    # under a full-suite load before exercising cleanup.
                                    timeout_seconds=5.0,
                                )
                        else:
                            self.assertIsNone(
                                probe_config_write_capability(
                                    backend,
                                    timeout_seconds=5.0,
                                )
                            )
                    pid_deadline = time.monotonic() + 1.0
                    while (
                        not pid_path.exists()
                        and time.monotonic() < pid_deadline
                    ):
                        time.sleep(0.02)
                    self.assertTrue(
                        pid_path.exists(),
                        f"{phase} fixture did not record its child process",
                    )
                    child_pid = int(pid_path.read_text())
                    self.assertTrue(
                        wait_for_process_exit(child_pid),
                        f"{phase} probe left child process {child_pid} running",
                    )
                    if phase == "schema":
                        self.assertEqual(len(popen_kwargs), 1)
                        self.assertIs(
                            popen_kwargs[0].get("stdout"),
                            subprocess.DEVNULL,
                        )
                        self.assertIs(
                            popen_kwargs[0].get("stderr"),
                            subprocess.DEVNULL,
                        )
                    for process in processes:
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                self.assertTrue(
                                    stream.closed,
                                    f"{phase} probe left a process pipe open",
                                )
                finally:
                    if child_pid and process_is_alive(child_pid):
                        os.kill(child_pid, signal.SIGKILL)

    def test_proxy_rejects_receipt_capability_tampering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_proxy_backend(root / "codex")
            config_path = root / "config.toml"
            config_payload = b'[desktop]\nfollowUpQueueMode = "off"\n'
            config_path.write_bytes(config_payload)
            schema_sha256 = "e" * 64
            receipt_path = write_proxy_receipt(
                root / "receipt.json",
                backend,
                schema_sha256=schema_sha256,
                config_write=False,
            )
            expected_receipt_sha256 = hashlib.sha256(
                receipt_path.read_bytes()
            ).hexdigest()
            payload = json.loads(receipt_path.read_text())
            payload["capabilities"][
                "versioned_config_write_preserves_unrelated"
            ] = True
            receipt_path.write_text(json.dumps(payload, sort_keys=True) + "\n")
            backend_log = root / "backend.jsonl"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parent)
            environment["PROXY_BACKEND_LOG"] = str(backend_log)
            environment["CODEX_SWITCH_CAPABILITY_RECEIPT"] = str(receipt_path)
            environment["CODEX_SWITCH_EXPECTED_SCHEMA_SHA256"] = schema_sha256
            environment["CODEX_SWITCH_EXPECTED_RECEIPT_SHA256"] = (
                expected_receipt_sha256
            )
            request = {
                "id": "tampered",
                "method": "config/value/write",
                "params": {
                    "filePath": str(config_path),
                    "keyPath": "desktop.followUpQueueMode",
                    "value": "queue",
                },
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("codex_switch_app_proxy.py")),
                    str(backend),
                    str(config_path),
                    "app-server",
                ],
                input=json.dumps(request) + "\n",
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertIn("error", response)
            self.assertEqual(response["error"]["code"], -32096)
            self.assertFalse(backend_log.exists())
            self.assertEqual(config_path.read_bytes(), config_payload)

    def test_proxy_forwards_only_with_valid_behavioral_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_proxy_backend(root / "codex")
            config_path = root / "config.toml"
            config_payload = b'[desktop]\nfollowUpQueueMode = "off"\n'
            config_path.write_bytes(config_payload)
            schema_sha256 = "a" * 64
            receipt_path = write_proxy_receipt(
                root / "receipt.json",
                backend,
                schema_sha256=schema_sha256,
                config_write=True,
            )
            request = {
                "id": 41,
                "method": "config/value/write",
                "params": {
                    "filePath": str(config_path),
                    "keyPath": "desktop.followUpQueueMode",
                    "value": "queue",
                },
            }
            base_env = os.environ.copy()
            base_env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
            base_env["CODEX_SWITCH_EXPECTED_SCHEMA_SHA256"] = schema_sha256

            valid_log = root / "valid-backend.jsonl"
            valid_env = dict(base_env)
            valid_env["PROXY_BACKEND_LOG"] = str(valid_log)
            valid_env["CODEX_SWITCH_CAPABILITY_RECEIPT"] = str(receipt_path)
            valid_env["CODEX_SWITCH_EXPECTED_RECEIPT_SHA256"] = hashlib.sha256(
                receipt_path.read_bytes()
            ).hexdigest()
            valid = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("codex_switch_app_proxy.py")),
                    str(backend),
                    str(config_path),
                    "app-server",
                ],
                input=json.dumps(request) + "\n",
                capture_output=True,
                text=True,
                timeout=5,
                env=valid_env,
                check=False,
            )

            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(
                json.loads(valid.stdout),
                {
                    "id": 41,
                    "result": {
                        "filePath": str(config_path),
                        "status": "ok",
                        "version": "backend-version-7",
                    },
                },
            )
            valid_messages = [
                json.loads(line) for line in valid_log.read_text().splitlines()
            ]
            self.assertEqual(valid_messages, [request])
            self.assertEqual(config_path.read_bytes(), config_payload)

            missing_log = root / "missing-backend.jsonl"
            missing_env = dict(base_env)
            missing_env["PROXY_BACKEND_LOG"] = str(missing_log)
            missing = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("codex_switch_app_proxy.py")),
                    str(backend),
                    str(config_path),
                    "app-server",
                ],
                input=json.dumps(request) + "\n",
                capture_output=True,
                text=True,
                timeout=5,
                env=missing_env,
                check=False,
            )

            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertEqual(
                json.loads(missing.stdout),
                {
                    "id": 41,
                    "error": {
                        "code": -32096,
                        "message": (
                            "codex-switch: config write blocked because backend "
                            "capability receipt is not proven"
                        ),
                    },
                },
            )
            self.assertFalse(missing_log.exists())
            self.assertEqual(config_path.read_bytes(), config_payload)

    def test_proxy_rejects_every_unproven_receipt_before_backend_or_file_mutation(
        self,
    ) -> None:
        cases = (
            "stale-backend",
            "stale-schema",
            "failed",
            "timeout",
            "malformed-json",
            "extra-field",
            "old-generation",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                backend = write_proxy_backend(root / "codex")
                config_path = root / "config.toml"
                config_payload = b'[desktop]\nfollowUpQueueMode = "off"\n'
                config_path.write_bytes(config_payload)
                schema_sha256 = "b" * 64
                receipt_path = write_proxy_receipt(
                    root / "receipt.json",
                    backend,
                    schema_sha256=schema_sha256,
                    config_write=(
                        False
                        if case == "failed"
                        else None
                        if case == "timeout"
                        else True
                    ),
                    schema_version=1 if case == "old-generation" else 2,
                )
                if case == "stale-backend":
                    backend.write_text(backend.read_text() + "\n# changed\n")
                elif case == "malformed-json":
                    receipt_path.write_text("{not-json\n")
                elif case == "extra-field":
                    payload = json.loads(receipt_path.read_text())
                    payload["temporary_home"] = "/secret/config-probe"
                    receipt_path.write_text(json.dumps(payload) + "\n")

                backend_log = root / "backend.jsonl"
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parent)
                environment["PROXY_BACKEND_LOG"] = str(backend_log)
                environment["CODEX_SWITCH_CAPABILITY_RECEIPT"] = str(receipt_path)
                environment["CODEX_SWITCH_EXPECTED_SCHEMA_SHA256"] = (
                    "c" * 64 if case == "stale-schema" else schema_sha256
                )
                request = {
                    "id": case,
                    "method": "config/batchWrite",
                    "params": {
                        "filePath": str(config_path),
                        "edits": [
                            {
                                "keyPath": "desktop.followUpQueueMode",
                                "value": "queue",
                            }
                        ],
                    },
                }

                result = subprocess.run(
                    [
                        sys.executable,
                        str(
                            Path(__file__).with_name(
                                "codex_switch_app_proxy.py"
                            )
                        ),
                        str(backend),
                        str(config_path),
                        "app-server",
                    ],
                    input=json.dumps(request) + "\n",
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=environment,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    {
                        "id": case,
                        "error": {
                            "code": -32096,
                            "message": (
                                "codex-switch: config write blocked because "
                                "backend capability receipt is not proven"
                            ),
                        },
                    },
                )
                self.assertFalse(backend_log.exists())
                self.assertEqual(config_path.read_bytes(), config_payload)

    def test_proxy_preserves_concurrent_write_order_and_never_compensates_responses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = write_proxy_backend(root / "codex")
            config_path = root / "config.toml"
            config_payload = b'[desktop]\nfollowUpQueueMode = "off"\n'
            config_path.write_bytes(config_payload)
            schema_sha256 = "d" * 64
            receipt_path = write_proxy_receipt(
                root / "receipt.json",
                backend,
                schema_sha256=schema_sha256,
                config_write=True,
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parent)
            environment["CODEX_SWITCH_CAPABILITY_RECEIPT"] = str(receipt_path)
            environment["CODEX_SWITCH_EXPECTED_SCHEMA_SHA256"] = schema_sha256
            environment["CODEX_SWITCH_EXPECTED_RECEIPT_SHA256"] = (
                hashlib.sha256(receipt_path.read_bytes()).hexdigest()
            )
            environment["PROXY_BACKEND_MODE"] = "reverse"
            backend_log = root / "backend.jsonl"
            environment["PROXY_BACKEND_LOG"] = str(backend_log)
            requests = [
                {
                    "id": "write-1",
                    "method": "config/value/write",
                    "params": {
                        "filePath": str(config_path),
                        "keyPath": "desktop.followUpQueueMode",
                        "value": "queue",
                    },
                },
                {
                    "id": "write-2",
                    "method": "config/batchWrite",
                    "params": {
                        "filePath": str(config_path),
                        "edits": [
                            {
                                "keyPath": "desktop.followUpQueueMode",
                                "value": "off",
                            }
                        ],
                    },
                },
            ]

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("codex_switch_app_proxy.py")),
                    str(backend),
                    str(config_path),
                    "app-server",
                ],
                input="".join(json.dumps(item) + "\n" for item in requests),
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                [
                    json.loads(line)
                    for line in backend_log.read_text().splitlines()
                ],
                requests,
            )
            responses = [
                json.loads(line) for line in result.stdout.splitlines()
            ]
            self.assertEqual(
                responses,
                [
                    {
                        "id": "write-1",
                        "method": "server/request",
                        "params": {"probe": True},
                    },
                    {
                        "id": "write-2",
                        "result": {
                            "filePath": str(config_path),
                            "status": "ok",
                            "version": "version-write-2",
                        },
                    },
                    {
                        "id": "write-1",
                        "result": {
                            "filePath": str(config_path),
                            "status": "ok",
                            "version": "version-write-1",
                        },
                    },
                ],
            )
            self.assertEqual(config_path.read_bytes(), config_payload)

            for mode in (
                "backend-error",
                "invalid-path",
                "invalid-status",
                "invalid-version",
            ):
                with self.subTest(mode=mode):
                    case_log = root / f"{mode}.jsonl"
                    case_env = dict(environment)
                    case_env["PROXY_BACKEND_MODE"] = mode
                    case_env["PROXY_BACKEND_LOG"] = str(case_log)
                    case = subprocess.run(
                        [
                            sys.executable,
                            str(
                                Path(__file__).with_name(
                                    "codex_switch_app_proxy.py"
                                )
                            ),
                            str(backend),
                            str(config_path),
                            "app-server",
                        ],
                        input=json.dumps(requests[0]) + "\n",
                        capture_output=True,
                        text=True,
                        timeout=5,
                        env=case_env,
                        check=False,
                    )
                    self.assertEqual(case.returncode, 0, case.stderr)
                    [forwarded] = [
                        json.loads(line)
                        for line in case_log.read_text().splitlines()
                    ]
                    self.assertEqual(forwarded, requests[0])
                    response = json.loads(case.stdout)
                    self.assertEqual(response["id"], "write-1")
                    if mode == "backend-error":
                        self.assertEqual(
                            response["error"],
                            {
                                "code": -32001,
                                "message": "backend rejected",
                            },
                        )
                    else:
                        self.assertIn("result", response)
                    self.assertEqual(config_path.read_bytes(), config_payload)

    def test_generated_wrapper_proxy_chain_preserves_modern_protocol_and_masks_responses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chain = prepare_generated_proxy_chain(
                root,
                version="codex-cli 0.142.4",
                dynamic_tools=True,
                marketplace=True,
                config_write=True,
                with_receipt=True,
            )
            wrapper = chain["wrapper"]
            app_home = chain["app_home"]
            environment = chain["environment"]
            assert isinstance(wrapper, Path)
            assert isinstance(app_home, Path)
            assert isinstance(environment, dict)
            app_config = app_home / "config.toml"
            dynamic_tools = [
                {
                    "type": "namespace",
                    "name": "workspace",
                    "tools": [
                        {
                            "type": "function",
                            "name": "search",
                            "description": "search files",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                        }
                    ],
                }
            ]
            requests = [
                {
                    "id": "initialize",
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "Desktop"}},
                },
                {
                    "id": "thread",
                    "method": "thread/start",
                    "params": {
                        "model": DESKTOP_MODEL,
                        "dynamicTools": dynamic_tools,
                        "metadata": {"model": DESKTOP_MODEL},
                    },
                },
                {
                    "id": "plugins",
                    "method": "plugin/list",
                    "params": {
                        "marketplaceKinds": [
                            "local",
                            "created-by-me-remote",
                        ]
                    },
                },
                {
                    "id": "models",
                    "method": "model/list",
                    "params": {},
                },
                {
                    "id": "write",
                    "method": "config/value/write",
                    "params": {
                        "filePath": str(app_config),
                        "keyPath": "model",
                        "value": DESKTOP_MODEL,
                        "metadata": {"model": DESKTOP_MODEL},
                    },
                },
            ]
            raw_request = (
                b' { "id" : "raw", "method" : "unknown/raw", "params" : '
                b'{ "model" : "gpt-5.5", "type" : "namespace" } }\r\n'
            )
            payload = b"".join(
                (json.dumps(item, separators=(",", ":")) + "\n").encode()
                for item in requests
            ) + raw_request

            result = subprocess.run(
                [
                    str(wrapper),
                    "-c",
                    "features.code_mode_host=true",
                    "app-server",
                    "--analytics-default-enabled",
                ],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=5,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            backend_log = chain["backend_log"]
            raw_log = chain["raw_log"]
            metadata_path = chain["metadata_path"]
            assert isinstance(backend_log, Path)
            assert isinstance(raw_log, Path)
            assert isinstance(metadata_path, Path)
            backend_messages = [
                json.loads(line) for line in backend_log.read_text().splitlines()
            ]
            by_method = {
                message["method"]: message
                for message in backend_messages
                if isinstance(message.get("method"), str)
            }
            self.assertEqual(
                by_method["thread/start"]["params"]["model"],
                ACTUAL_MODEL,
            )
            self.assertEqual(
                by_method["thread/start"]["params"]["dynamicTools"],
                dynamic_tools,
            )
            self.assertEqual(
                by_method["thread/start"]["params"]["metadata"]["model"],
                DESKTOP_MODEL,
            )
            self.assertEqual(
                by_method["plugin/list"]["params"]["marketplaceKinds"],
                ["local", "created-by-me-remote"],
            )
            self.assertEqual(
                by_method["config/value/write"]["params"]["value"],
                ACTUAL_MODEL,
            )
            self.assertEqual(
                by_method["config/value/write"]["params"]["metadata"]["model"],
                DESKTOP_MODEL,
            )
            self.assertIn(raw_request, raw_log.read_bytes())

            responses = [
                json.loads(line)
                for line in result.stdout.splitlines()
                if line.strip()
            ]
            by_id = {
                response["id"]: response
                for response in responses
                if isinstance(response, dict) and "id" in response
            }
            self.assertEqual(
                by_id["thread"]["result"]["thread"]["model"],
                DESKTOP_MODEL,
            )
            [model] = by_id["models"]["result"]["data"]
            self.assertEqual(model["id"], DESKTOP_MODEL)
            self.assertEqual(model["model"], DESKTOP_MODEL)
            self.assertEqual(model["metadata"]["model"], ACTUAL_MODEL)
            self.assertEqual(
                by_id["write"]["result"],
                {
                    "filePath": str(app_config),
                    "status": "ok",
                    "version": "backend-version-7",
                },
            )
            raw_response = (
                b' { "id" : "raw", "result" : { "model" : "'
                + ACTUAL_MODEL.encode()
                + b'", "payload" : { "model" : "opaque" } } }\r\n'
            )
            self.assertIn(raw_response, result.stdout)
            metadata = json.loads(metadata_path.read_text())
            self.assertEqual(
                metadata["argv"],
                [
                    "-c",
                    "features.code_mode_host=true",
                    "app-server",
                    "--analytics-default-enabled",
                ],
            )
            self.assertEqual(metadata["codex_home"], str(app_home))
            self.assertEqual(metadata["pythonpath"], "/original/proxy-chain")
            self.assertEqual(
                (app_home / "AGENTS.md").read_text(),
                "shared\n",
            )
            self.assertIn(
                f'model = "{ACTUAL_MODEL}"',
                app_config.read_text(),
            )

    def test_generated_wrapper_proxy_chain_applies_only_exact_legacy_transforms(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chain = prepare_generated_proxy_chain(
                root,
                version="codex-cli 0.140.0",
                dynamic_tools=False,
                marketplace=False,
                config_write=False,
                with_receipt=True,
            )
            wrapper = chain["wrapper"]
            environment = chain["environment"]
            assert isinstance(wrapper, Path)
            assert isinstance(environment, dict)
            raw_request = (
                b' { "id" : "raw", "method" : "unknown/raw", "params" : '
                b'{ "tools" : [{ "type" : "namespace" }] } }\r\n'
            )
            requests = [
                {
                    "id": "thread",
                    "method": "thread/start",
                    "params": {
                        "model": DESKTOP_MODEL,
                        "dynamicTools": [
                            {
                                "type": "namespace",
                                "name": "workspace",
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "search",
                                        "description": "search files",
                                        "inputSchema": {"type": "object"},
                                    }
                                ],
                            }
                        ],
                    },
                },
                {
                    "id": "plugins",
                    "method": "plugin/list",
                    "params": {
                        "marketplaceKinds": [
                            "local",
                            "created-by-me-remote",
                        ]
                    },
                },
            ]
            payload = b"".join(
                (json.dumps(item, separators=(",", ":")) + "\n").encode()
                for item in requests
            ) + raw_request

            result = subprocess.run(
                [str(wrapper), "app-server"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=5,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            backend_log = chain["backend_log"]
            raw_log = chain["raw_log"]
            assert isinstance(backend_log, Path)
            assert isinstance(raw_log, Path)
            messages = [
                json.loads(line) for line in backend_log.read_text().splitlines()
            ]
            by_method = {message["method"]: message for message in messages}
            self.assertEqual(
                by_method["thread/start"]["params"]["dynamicTools"],
                [
                    {
                        "namespace": "workspace",
                        "type": "function",
                        "name": "search",
                        "description": "search files",
                        "inputSchema": {"type": "object"},
                    }
                ],
            )
            self.assertEqual(
                by_method["plugin/list"]["params"]["marketplaceKinds"],
                ["local"],
            )
            self.assertIn(raw_request, raw_log.read_bytes())
            raw_response = (
                b' { "id" : "raw", "result" : { "model" : "'
                + ACTUAL_MODEL.encode()
                + b'", "payload" : { "model" : "opaque" } } }\r\n'
            )
            self.assertIn(raw_response, result.stdout)

    def test_generated_wrapper_proxy_chain_preserves_unknown_and_blocks_unproven_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chain = prepare_generated_proxy_chain(
                root,
                version="codex-cli unknown",
                with_receipt=False,
            )
            wrapper = chain["wrapper"]
            app_home = chain["app_home"]
            environment = chain["environment"]
            assert isinstance(wrapper, Path)
            assert isinstance(app_home, Path)
            assert isinstance(environment, dict)
            app_config = app_home / "config.toml"
            raw_request = (
                b' { "id" : "raw", "method" : "unknown/raw", "params" : '
                b'{ "model" : "gpt-5.5", "namespace" : "opaque" } }\r\n'
            )
            requests = [
                {
                    "id": "thread",
                    "method": "thread/start",
                    "params": {
                        "model": DESKTOP_MODEL,
                        "dynamicTools": [
                            {
                                "type": "namespace",
                                "name": "workspace",
                                "tools": [],
                            }
                        ],
                    },
                },
                {
                    "id": "plugins",
                    "method": "plugin/list",
                    "params": {
                        "marketplaceKinds": [
                            "local",
                            "created-by-me-remote",
                        ]
                    },
                },
                {
                    "id": "blocked-write",
                    "method": "config/value/write",
                    "params": {
                        "filePath": str(app_config),
                        "keyPath": "model",
                        "value": DESKTOP_MODEL,
                    },
                },
            ]
            payload = b"".join(
                (json.dumps(item, separators=(",", ":")) + "\n").encode()
                for item in requests
            ) + raw_request

            result = subprocess.run(
                [str(wrapper), "app-server"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=5,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            backend_log = chain["backend_log"]
            raw_log = chain["raw_log"]
            assert isinstance(backend_log, Path)
            assert isinstance(raw_log, Path)
            messages = [
                json.loads(line) for line in backend_log.read_text().splitlines()
            ]
            by_method = {message["method"]: message for message in messages}
            self.assertNotIn("config/value/write", by_method)
            self.assertEqual(
                by_method["thread/start"]["params"]["dynamicTools"][0]["type"],
                "namespace",
            )
            self.assertEqual(
                by_method["plugin/list"]["params"]["marketplaceKinds"],
                ["local", "created-by-me-remote"],
            )
            self.assertIn(raw_request, raw_log.read_bytes())
            responses = [
                json.loads(line)
                for line in result.stdout.splitlines()
                if line.strip()
            ]
            by_id = {
                response["id"]: response
                for response in responses
                if isinstance(response, dict) and "id" in response
            }
            self.assertEqual(
                by_id["blocked-write"]["error"],
                {
                    "code": -32096,
                    "message": (
                        "codex-switch: config write blocked because backend "
                        "capability receipt is not proven"
                    ),
                },
            )
            self.assertIn(
                f'model = "{ACTUAL_MODEL}"',
                app_config.read_text(),
            )

    def test_generated_wrapper_proxy_chain_flushes_response_before_client_eof(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chain = prepare_generated_proxy_chain(
                root,
                version="codex-cli 0.142.4",
                dynamic_tools=True,
                marketplace=True,
                config_write=True,
                with_receipt=True,
            )
            wrapper = chain["wrapper"]
            environment = chain["environment"]
            assert isinstance(wrapper, Path)
            assert isinstance(environment, dict)
            process = subprocess.Popen(
                [str(wrapper), "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                assert process.stdin is not None
                assert process.stdout is not None
                process.stdin.write(
                    b'{"id":"initialize","method":"initialize","params":{}}\n'
                )
                process.stdin.flush()
                ready, _, _ = select.select([process.stdout], [], [], 2.0)
                self.assertTrue(ready, "proxy did not flush response before EOF")
                self.assertEqual(
                    json.loads(process.stdout.readline()),
                    {
                        "id": "initialize",
                        "result": {"userAgent": "proxy-chain-test"},
                    },
                )
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_generated_wrapper_proxy_chain_drains_eof_and_propagates_nonzero_exit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chain = prepare_generated_proxy_chain(
                root,
                version="codex-cli 0.142.4",
                dynamic_tools=True,
                marketplace=True,
                config_write=True,
                with_receipt=True,
            )
            wrapper = chain["wrapper"]
            environment = chain["environment"]
            eof_marker = chain["eof_marker"]
            assert isinstance(wrapper, Path)
            assert isinstance(environment, dict)
            assert isinstance(eof_marker, Path)
            lifecycle_environment = dict(environment)
            lifecycle_environment["PROXY_BACKEND_MODE"] = "eof-exit"
            lifecycle_environment["PROXY_BACKEND_EXIT_CODE"] = "23"
            lifecycle_environment["PROXY_BACKEND_EOF_BLOB_SIZE"] = "20000000"

            result = subprocess.run(
                [str(wrapper), "app-server"],
                input=b'{"id":"initialize","method":"initialize","params":{}}\n',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=lifecycle_environment,
                timeout=5,
                check=False,
            )

            self.assertEqual(result.returncode, 23)
            self.assertTrue(eof_marker.exists())
            self.assertIn(b'"id":"eof-final"', result.stdout)
            self.assertIn(b"backend-eof-stderr-final\n", result.stderr)
            self.assertNotIn(b"Exception in thread", result.stderr)

    def test_generated_wrapper_proxy_chain_bounds_inherited_stream_drain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chain = prepare_generated_proxy_chain(
                root,
                version="codex-cli 0.142.4",
                dynamic_tools=True,
                marketplace=True,
                config_write=True,
                with_receipt=True,
            )
            wrapper = chain["wrapper"]
            environment = chain["environment"]
            assert isinstance(wrapper, Path)
            assert isinstance(environment, dict)
            hold_pid_path = root / "hold-streams.pid"
            hold_environment = dict(environment)
            hold_environment["PROXY_BACKEND_MODE"] = "hold-streams"
            hold_environment["PROXY_BACKEND_EXIT_CODE"] = "19"
            hold_environment["PROXY_BACKEND_HOLD_PID"] = str(hold_pid_path)
            child_pid: int | None = None
            process = subprocess.Popen(
                [str(wrapper), "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=hold_environment,
            )
            started = time.monotonic()
            try:
                stdout, stderr = process.communicate(
                    input=(
                        b'{"id":"initialize","method":"initialize",'
                        b'"params":{}}\n'
                    ),
                    timeout=5,
                )
                elapsed = time.monotonic() - started
                self.assertEqual(process.returncode, 19)
                self.assertLess(elapsed, 4.5)
                self.assertIn(
                    b"codex-switch app proxy: backend stream drain timed out",
                    stderr,
                )
                self.assertIn(b'"id":"initialize"', stdout)
                deadline = time.monotonic() + 1.0
                while not hold_pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(hold_pid_path.exists())
                child_pid = int(hold_pid_path.read_text())
                self.assertTrue(process_is_alive(child_pid))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                if hold_pid_path.exists() and child_pid is None:
                    child_pid = int(hold_pid_path.read_text())
                if child_pid is not None and process_is_alive(child_pid):
                    os.kill(child_pid, signal.SIGKILL)
                    wait_for_process_exit(child_pid)

    def test_generated_wrapper_proxy_chain_handles_early_backend_exit_without_traceback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chain = prepare_generated_proxy_chain(
                root,
                version="codex-cli 0.142.4",
                dynamic_tools=True,
                marketplace=True,
                config_write=True,
                with_receipt=True,
            )
            wrapper = chain["wrapper"]
            environment = chain["environment"]
            assert isinstance(wrapper, Path)
            assert isinstance(environment, dict)
            early_environment = dict(environment)
            early_environment["PROXY_BACKEND_MODE"] = "exit-before-read"
            early_environment["PROXY_BACKEND_EXIT_CODE"] = "29"
            request = (
                b'{"method":"unknown/large","params":{"payload":"'
                + (b"x" * 4096)
                + b'"}}\n'
            )

            result = subprocess.run(
                [str(wrapper), "app-server"],
                input=request * 512,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=early_environment,
                timeout=5,
                check=False,
            )

            self.assertEqual(result.returncode, 29)
            self.assertIn(b"backend-exit-before-read\n", result.stderr)
            self.assertNotIn(b"Exception in thread", result.stderr)
            self.assertNotIn(b"BrokenPipeError", result.stderr)
            self.assertNotIn(b"Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
