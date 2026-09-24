#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import io
import inspect
import json
import os
import plistlib
import select
import shlex
import shutil
import tarfile
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from contextlib import redirect_stdout
from unittest import mock
from types import SimpleNamespace
from pathlib import Path

import codex_switch_bindings as bindings_module
import codex_switch_doctor as doctor_module
import codex_switch_status as status_module
import codex_switch_switching as switching_module
import codex_switch_verify as verify_module
try:
    import release_auto
except ModuleNotFoundError:
    from scripts import release_auto

from codex_switch_bindings import cmd_set_bin
from codex_switch_constants import SwitchError
from codex_switch_plugins import (
    apply_plugin_config_updates,
    available_plugin_catalog,
    build_plugin_config_updates,
    disable_unavailable_plugin_requirements,
    plugin_is_installed,
    plugin_requirement,
    plugin_tree_manifest,
    profile_plugin_config_paths,
    repair_profile_plugins,
)
from codex_switch_running_app import (
    RunningCodexProcess,
    app_server_command_path,
    parse_env_app_cli_path,
    parse_ps_processes,
    running_desktop_problems,
)
from codex_switch_runtime_binding import (
    CURRENT_CHATGPT_BUNDLE_ID,
    ChatGPTDesktopHost,
    DesktopInventory,
    RuntimeObservation,
)
from codex_switch_app_proxy import (
    adapt_backend_json_line,
    adapt_client_json_line,
    codex_version_supports_canonical_dynamic_tools,
    mask_backend_message_for_desktop,
    protocol_capabilities_for_version,
    translate_desktop_message_for_backend,
)
from codex_switch_protocol_adapter import (
    BackendCapabilities,
    PendingRequestTracker,
    ProtocolAdapter,
)
from codex_switch_release_bundle import build_release_bundle
from codex_switch_config import (
    build_base_config_text,
    build_profile_v2_config_text,
    merge_missing_shared_config_defaults,
)
from codex_switch_home_sync import (
    build_internal_home_config,
    refresh_profile_canonical_config,
    refresh_profile_plugin_support_snapshot,
    sync_shared_support,
)
from codex_switch_parity import (
    PARITY_POLICY_VERSION,
    ConfigInputs,
    ConfigProjection,
    InternalFingerprint,
    OfficialReference,
    ParityFinding,
    ParityPolicyVersion,
    ParityQueueItem,
    ParityReport,
)
from codex_switch_store import Store


SCRIPT = Path(__file__).with_name("codex_profile_switch.py")
WRAPPER = Path(__file__).with_name("codex-switch")
INSTALLER = Path(__file__).parents[1] / "install.sh"
REMOTE_RUNNER = Path(__file__).parents[1] / "run.sh"
RELEASE_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
AUTO_RELEASE_WORKFLOW = (
    Path(__file__).parents[1] / ".github" / "workflows" / "auto-release.yml"
)
RELEASE_AUTO = Path(__file__).with_name("release_auto.py")
RELEASE_BUNDLE_MODULE = Path(__file__).with_name(
    "codex_switch_release_bundle.py"
)
PROMOTION_MODULE = Path(__file__).with_name("codex_switch_promotion.py")
UPDATE_POLICY_MODULE = Path(__file__).with_name(
    "codex_switch_update_policy.py"
)
OFFICIAL_RELEASE_MODULE = Path(__file__).with_name(
    "codex_switch_official_release.py"
)


class FakeGitHubReleaseAdapter:
    def __init__(
        self,
        *,
        exists: bool = True,
        assets: dict[str, bytes] | None = None,
        draft: bool = False,
        fail_upload_once: str | None = None,
        fail_publish_once: bool = False,
        corrupt_after_publish: str | None = None,
    ) -> None:
        self.exists = exists
        self.assets = dict(assets or {})
        self.draft = draft
        self.fail_upload_once = fail_upload_once
        self.failed_upload = False
        self.fail_publish_once = fail_publish_once
        self.failed_publish = False
        self.corrupt_after_publish = corrupt_after_publish
        self.create_calls: list[str] = []
        self.publish_calls: list[str] = []
        self.download_calls: list[str] = []
        self.upload_attempts: list[str] = []

    def inspect_release(self, tag: str):
        return release_auto.ReleaseSnapshot(
            exists=self.exists,
            assets=tuple(sorted(self.assets)),
            draft=self.draft,
        )

    def create_release(self, tag: str) -> None:
        if self.exists:
            raise AssertionError(f"release already exists: {tag}")
        self.exists = True
        self.draft = True
        self.create_calls.append(tag)

    def download_asset(self, tag: str, name: str, destination: Path) -> None:
        if not self.exists or name not in self.assets:
            raise AssertionError(f"missing release asset: {tag}/{name}")
        self.download_calls.append(name)
        destination.write_bytes(self.assets[name])

    def upload_asset(self, tag: str, path: Path) -> None:
        name = path.name
        self.upload_attempts.append(name)
        if name in self.assets:
            raise AssertionError(f"upload would clobber existing asset: {name}")
        if self.fail_upload_once == name and not self.failed_upload:
            self.failed_upload = True
            raise release_auto.ReleaseError(f"injected upload failure: {name}")
        self.assets[name] = path.read_bytes()

    def publish_release(self, tag: str) -> None:
        if not self.exists or not self.draft:
            raise AssertionError(f"release is not a draft: {tag}")
        self.publish_calls.append(tag)
        if self.fail_publish_once and not self.failed_publish:
            self.failed_publish = True
            raise release_auto.ReleaseError(f"injected publish failure: {tag}")
        self.draft = False
        if self.corrupt_after_publish is not None:
            self.assets[self.corrupt_after_publish] = b"corrupted after publish"


def write_required_release_modules(scripts_dir: Path) -> None:
    shutil.copy2(
        RELEASE_BUNDLE_MODULE,
        scripts_dir / RELEASE_BUNDLE_MODULE.name,
    )
    shutil.copy2(
        PROMOTION_MODULE,
        scripts_dir / PROMOTION_MODULE.name,
    )
    shutil.copy2(
        UPDATE_POLICY_MODULE,
        scripts_dir / UPDATE_POLICY_MODULE.name,
    )
    shutil.copy2(WRAPPER.with_name("codex_switch_first_install.py"), scripts_dir / "codex_switch_first_install.py")
    (scripts_dir / OFFICIAL_RELEASE_MODULE.name).write_text(
        "VALUE = 1\n"
    )
    (scripts_dir / "codex_profile_switch.py").write_text("VALUE = 1\n")
    for name in (
        "codex_switch_update.py",
        "codex_switch_parity.py",
        "codex_switch_runtime_binding.py",
        "codex_switch_app_proxy.py",
        "codex_switch_home_sync.py",
        "codex_switch_selection.py",
        "codex_switch_shared_configuration.py",
    ):
        (scripts_dir / name).write_text("VALUE = 1\n")


def write_fake_codex(path: Path, label: str, *, version: str | None = None) -> None:
    path.write_text(
        "#!/usr/bin/env sh\n"
        + (
            f'if [ "${{1:-}}" = "--version" ]; then echo "codex-cli {version}"; exit 0; fi\n'
            if version is not None else ""
        )
        +
        "if [ \"${1:-}\" = \"login\" ]; then\n"
        "  if grep -q '^profile = ' \"$CODEX_HOME/config.toml\" 2>/dev/null; then\n"
        "    echo legacy-profile-config >&2\n"
        "    exit 42\n"
        "  fi\n"
        "  mkdir -p \"$CODEX_HOME\"\n"
        "  printf '{\"fake\":\"auth\"}\\n' > \"$CODEX_HOME/auth.json\"\n"
        f"  echo {label}-login\n"
        "  exit 0\n"
        "fi\n"
        f"echo {label}\n"
    )
    path.chmod(0o755)


def write_fake_script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def write_fake_release_redirects(
    path: Path,
    redirects: dict[str, str | None],
    *,
    args_log: Path | None = None,
) -> None:
    lines = ["#!/usr/bin/env sh"]
    if args_log is not None:
        lines.append(f'printf \'%s\\n\' "$*" >> {shlex.quote(str(args_log))}')
    lines.append('case "$*" in')
    for needle, location in redirects.items():
        lines.append(f'  *"{needle}"*)')
        if location is None:
            lines.append("    exit 22")
        else:
            lines.extend(
                (
                    "    cat <<'EOF'",
                    "HTTP/2 302",
                    f"location: {location}",
                    "EOF",
                )
            )
        lines.append("    ;;")
    lines.extend(("  *) exit 23 ;;", "esac", ""))
    write_fake_script(path, "\n".join(lines))


def filesystem_snapshot(root: Path) -> list[tuple[str, str, bytes | str]]:
    if not os.path.lexists(root):
        return [(".", "missing", b"")]
    entries: list[tuple[str, str, bytes | str]] = []
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        elif path.is_dir():
            entries.append((relative, "dir", b""))
        else:
            entries.append((relative, "other", b""))
    return entries


def desktop_global_state_payload(
    *,
    bounds_width: int,
    hotkey: str,
    auto_context: bool,
    prompt_history_label: str,
) -> dict[str, object]:
    return {
        "electron-main-window-bounds": {
            "x": 10,
            "y": 20,
            "width": bounds_width,
            "height": 800,
        },
        "appshotHotkey": hotkey,
        "queued-follow-ups": [{"thread": f"{prompt_history_label}-queued"}],
        "selected-remote-host-id": f"{prompt_history_label}-remote-host",
        "electron-local-remote-control-environment-id": f"{prompt_history_label}-env",
        "electron-persisted-atom-state": {
            "composer-auto-context-enabled": auto_context,
            "sidebar-width": 312,
            "diff-filter": {"mode": "all"},
            "prompt-history": {"entries": [f"{prompt_history_label}-prompt"]},
            "heartbeat-thread-permissions-by-id": {
                f"{prompt_history_label}-thread": "full"
            },
            "composer-prompt-drafts-v1": {
                f"{prompt_history_label}-thread": "draft"
            },
            "unread-thread-ids-by-host-v1": {
                f"{prompt_history_label}-host": [f"{prompt_history_label}-thread"]
            },
            "remote-thread-summaries:remote": {
                "title": f"{prompt_history_label}-remote-summary"
            },
        },
    }


def write_plugin_source(
    root: Path,
    *,
    name: str,
    version: str,
    payload: str,
) -> None:
    manifest = root / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": name, "version": version}, sort_keys=True) + "\n"
    )
    (root / "payload.txt").write_text(payload)
    executable = root / "scripts" / "run.sh"
    executable.parent.mkdir()
    executable.write_text("#!/usr/bin/env sh\nexit 0\n")
    executable.chmod(0o755)


def write_fake_plugin_refresh_codex(
    path: Path,
    *,
    catalog: dict[str, object],
    sources: dict[str, Path] | None = None,
    catalog_stdout: str | None = None,
    catalog_stderr: str = "",
    catalog_returncode: int = 0,
) -> None:
    catalog_json = json.dumps(catalog, sort_keys=True)
    catalog_output = (
        json.dumps(catalog, sort_keys=True) + "\n"
        if catalog_stdout is None
        else catalog_stdout
    )
    source_json = json.dumps(
        {selector: str(source) for selector, source in (sources or {}).items()},
        sort_keys=True,
    )
    write_fake_script(
        path,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import shutil\n"
        "import sys\n"
        f"catalog = json.loads({catalog_json!r})\n"
        f"catalog_stdout = {catalog_output!r}\n"
        f"catalog_stderr = {catalog_stderr!r}\n"
        f"catalog_returncode = {catalog_returncode!r}\n"
        f"sources = json.loads({source_json!r})\n"
        "home = Path(os.environ['CODEX_HOME'])\n"
        "home.mkdir(parents=True, exist_ok=True)\n"
        "args = sys.argv[1:]\n"
        "with (home / 'codex-calls.log').open('a') as handle:\n"
        "    handle.write(str(home) + '|' + ' '.join(args) + '\\n')\n"
        "if args == ['--version']:\n"
        "    print('codex-cli 9.9.9')\n"
        "    raise SystemExit(0)\n"
        "if args[:3] == ['plugin', 'marketplace', 'upgrade']:\n"
        "    print(json.dumps({'upgraded': []}))\n"
        "    raise SystemExit(0)\n"
        "if args[:3] == ['plugin', 'list', '--available']:\n"
        "    (home / '.tmp' / 'plugins' / 'plugins' / 'catalog-only').mkdir(\n"
        "        parents=True, exist_ok=True\n"
        "    )\n"
        "    sys.stdout.write(catalog_stdout)\n"
        "    sys.stderr.write(catalog_stderr)\n"
        "    raise SystemExit(catalog_returncode)\n"
        "if args[:2] == ['plugin', 'add']:\n"
        "    selector = args[2]\n"
        "    records = catalog.get('installed', []) + catalog.get('available', [])\n"
        "    record = next(item for item in records if item.get('pluginId') == selector)\n"
        "    plugin, marketplace = selector.rsplit('@', 1)\n"
        "    destination = home / 'plugins' / 'cache' / marketplace / plugin / record['version']\n"
        "    if destination.exists() or destination.is_symlink():\n"
        "        if destination.is_dir() and not destination.is_symlink():\n"
        "            shutil.rmtree(destination)\n"
        "        else:\n"
        "            destination.unlink()\n"
        "    source = sources.get(selector)\n"
        "    if source:\n"
        "        shutil.copytree(source, destination, symlinks=True)\n"
        "    else:\n"
        "        (destination / '.codex-plugin').mkdir(parents=True)\n"
        "        (destination / '.codex-plugin' / 'plugin.json').write_text(\n"
        "            json.dumps({'name': plugin, 'version': record['version']}) + '\\n'\n"
        "        )\n"
        "    print(json.dumps({'pluginId': selector, 'installedPath': str(destination)}))\n"
        "    raise SystemExit(0)\n"
        "print('fake-plugin-refresh-codex')\n",
    )


def write_fake_runtime_smoke_codex(path: Path) -> None:
    write_fake_script(
        path,
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        "mkdir -p \"$CODEX_HOME\"\n"
        "printf '%s|%s\\n' \"$CODEX_HOME\" \"$*\" >> \"$CODEX_HOME/runtime-smoke.log\"\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  echo codex-cli 9.9.9\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = \"plugin\" ] && [ \"${2:-}\" = \"list\" ] && [ \"${3:-}\" = \"--json\" ]; then\n"
        "  printf '{\"plugins\":[]}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = \"exec\" ] && [ \"${2:-}\" = \"--json\" ]; then\n"
        "  printf '{\"status\":\"ok\"}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "echo fake-runtime-smoke-codex\n",
    )


def write_fake_responses_tool_smoke_codex(
    path: Path,
    *,
    fail_resource_mismatch: bool = False,
    fail_reasoning_item_not_found: bool = False,
) -> None:
    mismatch_body = ""
    if fail_resource_mismatch:
        mismatch_body = (
            "  echo 'x-account-deployment: deployment-gpt-5.5-2026-04-24-platform-global'\n"
            "  echo 'x-account-id: globalttswedencentral010'\n"
            "  echo 'x-model-request-id: b1ce23f9-e838-47c5-a705-afa2564e4409'\n"
            "  echo 'x-tt-logid: 20260703112009D6B58AAAD12F032ED7AB'\n"
            "  echo 'The requested item was created under a different Azure OpenAI resource. Use the same resource that created the item to access it.'\n"
            "  echo 'x-account-id: globalttswedencentral053'\n"
            "  echo 'x-model-request-id: 741c1f3e-fad4-48be-abe0-d0c2e99b3506'\n"
            "  echo 'x-tt-logid: 202607031120158DCF6A7C87F2A6AF4908'\n"
            "  echo 'api-key: should-not-leak'\n"
            "  exit 1\n"
        )
    elif fail_reasoning_item_not_found:
        mismatch_body = (
            "  echo 'x-account-id: globalttswedencentral010'\n"
            "  echo \"code: ; message: Item with id 'rs_08926f6eb84342d1006a61d1f955e081938793643ece5c1c56' not found.\"\n"
            "  echo 'authorization: should-not-leak'\n"
            "  exit 1\n"
        )
    else:
        mismatch_body = "  printf '{\"type\":\"turn.completed\"}\\n'\n  exit 0\n"
    write_fake_script(
        path,
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        "mkdir -p \"$CODEX_HOME\"\n"
        "printf '%s|%s\\n' \"$CODEX_HOME\" \"$*\" >> \"$CODEX_HOME/responses-tool-smoke.log\"\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  echo codex-cli 9.9.9\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = \"plugin\" ] && [ \"${2:-}\" = \"list\" ] && [ \"${3:-}\" = \"--json\" ]; then\n"
        "  printf '{\"plugins\":[]}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"${1:-}\" = \"exec\" ] && [ \"${2:-}\" = \"--json\" ]; then\n"
        + mismatch_body +
        "fi\n"
        "echo fake-responses-tool-smoke-codex\n",
    )


def write_fake_secret_smoke_codex(path: Path) -> None:
    write_fake_script(
        path,
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        "if [ \"${1:-}\" = \"exec\" ]; then\n"
        "  echo 'Authorization: Bearer auth-secret'\n"
        "  echo 'Bearer standalone-secret'\n"
        "  echo 'api-key: api-secret'\n"
        "  echo 'Cookie: session=cookie-secret'\n"
        "  echo 'https://example.test/path?sig=signed-secret&safe=value'\n"
        "  echo 'x-account-id: safe-route-123'\n"
        "  echo 'exception password=exception-secret' >&2\n"
        "  exit 7\n"
        "fi\n"
        "echo fake-secret-smoke-codex\n",
    )


def write_fake_app_server_smoke_codex(
    path: Path,
    *,
    version: str = "codex-cli 9.9.9",
    exit_241_after_plugin_list: bool = False,
) -> None:
    typed_responses = json.loads(
        (SCRIPT.parent.parent / "evals/fixtures/typed-subagent-app-server.json").read_text()
    )
    write_fake_script(
        path,
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"VERSION = {version!r}\n"
        f"EXIT_241 = {exit_241_after_plugin_list!r}\n"
        f"TYPED_RESPONSES = {typed_responses!r}\n"
        "if '--version' in sys.argv:\n"
        "    print(VERSION)\n"
        "    raise SystemExit(0)\n"
        "if 'generate-json-schema' in sys.argv:\n"
        "    output = Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "    output.mkdir(parents=True, exist_ok=True)\n"
        "    (output / 'protocol.json').write_text(json.dumps({\n"
        "        '$defs': {\n"
        "            'ThreadStartParams': {'type': 'object', 'properties': {'dynamicTools': {'type': 'array'}}},\n"
        "            'PluginListMarketplaceKind': {'enum': ['local', 'created-by-me-remote']},\n"
        "        }\n"
        "    }, sort_keys=True))\n"
        "    roots = {\n"
        "        'ClientRequest.json': ['initialize', 'config/read', 'model/list', 'collaborationMode/list', 'thread/start', 'turn/start'],\n"
        "        'ClientNotification.json': ['initialized'],\n"
        "        'ServerRequest.json': ['item/tool/call', 'execCommandApproval'],\n"
        "        'ServerNotification.json': ['item/completed', 'turn/completed'],\n"
        "    }\n"
        "    for name, methods in roots.items():\n"
        "        variants = []\n"
        "        for method in methods:\n"
        "            properties = {'method': {'type': 'string', 'enum': [method]}, 'params': {'type': 'object', 'properties': {}}}\n"
        "            required = ['method', 'params']\n"
        "            if name.endswith('Request.json'):\n"
        "                properties['id'] = {'type': 'string'}\n"
        "                required.append('id')\n"
        "            variants.append({'type': 'object', 'required': required, 'properties': properties})\n"
        "        (output / name).write_text(json.dumps({'oneOf': variants}))\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:3] == ['features', 'list']:\n"
        "    print('multi_agent_v2  stable  true')\n"
        "    raise SystemExit(0)\n"
        "home = Path(os.environ['CODEX_HOME'])\n"
        "home.mkdir(parents=True, exist_ok=True)\n"
        "log_paths = [home / 'app-server-smoke.log']\n"
        "explicit_log = os.environ.get('CODEX_SWITCH_TEST_APP_SERVER_LOG')\n"
        "if explicit_log:\n"
        "    explicit_log_path = Path(explicit_log)\n"
        "    explicit_log_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    log_paths.append(explicit_log_path)\n"
        "def append_log(text):\n"
        "    for log_path in log_paths:\n"
        "        with log_path.open('a') as log:\n"
        "            log.write(text + '\\n')\n"
        "append_log(f\"{home}|{' '.join(sys.argv[1:])}\")\n"
        "if sys.argv[1:4] == ['plugin', 'list', '--json']:\n"
        "    print(json.dumps({'plugins': []}))\n"
        "    raise SystemExit(0)\n"
        "if 'app-server' in sys.argv[1:]:\n"
        "    for raw in sys.stdin:\n"
        "        append_log(f\"stdin:{raw.rstrip()}\")\n"
        "        message = json.loads(raw)\n"
        "        method = message.get('method')\n"
        "        if str(message.get('id', '')).startswith('parity-probe-'):\n"
        "            for reply in TYPED_RESPONSES:\n"
        "                if reply.get('id') == message['id'] or (method == 'turn/start' and 'method' in reply):\n"
        "                    print(json.dumps(reply), flush=True)\n"
        "            continue\n"
        "        if method == 'initialize':\n"
        "            print(json.dumps({\n"
        "                'id': message['id'],\n"
        "                'result': {\n"
        "                    'userAgent': 'Codex Desktop/9.9.9 (codex-switch-smoke)',\n"
        "                    'codexHome': '/tmp/fake',\n"
        "                    'platformFamily': 'unix',\n"
        "                    'platformOs': 'macos',\n"
        "                },\n"
        "            }), flush=True)\n"
        "        elif method == 'plugin/list':\n"
        "            if EXIT_241:\n"
        "                print(json.dumps({'id': message['id'], 'result': {'marketplaces': []}}), flush=True)\n"
        "                print(json.dumps({\n"
        "                    'timestamp': '2026-07-03T03:52:12.432642Z',\n"
        "                    'level': 'WARN',\n"
        "                    'fields': {'message': 'plugin/list featured plugin fetch failed; returning empty featured ids'},\n"
        "                    'target': 'codex_app_server::request_processors::plugins',\n"
        "                }), file=sys.stderr, flush=True)\n"
        "                raise SystemExit(241)\n"
        "            print(json.dumps({\n"
        "                'id': message['id'],\n"
        "                'error': {\n"
        "                    'code': -32600,\n"
        "                    'message': 'chatgpt authentication required for remote plugin catalog',\n"
        "                },\n"
        "            }), flush=True)\n"
        "        elif method == 'config/value/write':\n"
        "            params = message['params']\n"
        "            config_path = Path(params['filePath'])\n"
        "            config_path.write_text(config_path.read_text().replace(\n"
        "                'codex_switch_config_write_probe = false',\n"
        "                'codex_switch_config_write_probe = true',\n"
        "            ))\n"
        "            print(json.dumps({\n"
        "                'id': message['id'],\n"
        "                'result': {\n"
        "                    'filePath': str(config_path.resolve()),\n"
        "                    'status': 'ok',\n"
        "                    'version': 'profile-smoke-v1',\n"
        "                },\n"
        "            }), flush=True)\n"
        "    raise SystemExit(0)\n"
        "print('fake-app-server-smoke-codex')\n",
    )


def write_fake_staged_update_helper(
    path: Path,
    *,
    candidate_source: Path | None,
    args_log: Path,
    exit_status: int = 0,
) -> None:
    source_value = str(candidate_source) if candidate_source is not None else None
    write_fake_script(
        path,
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import shutil\n"
        "import sys\n"
        f"args_log = Path({str(args_log)!r})\n"
        f"source_value = {source_value!r}\n"
        f"exit_status = {exit_status}\n"
        "args = sys.argv[1:]\n"
        "args_log.write_text(' '.join(args) + '\\n')\n"
        "if exit_status:\n"
        "    raise SystemExit(exit_status)\n"
        "if source_value is None:\n"
        "    raise SystemExit(0)\n"
        "install_dir = Path(args[args.index('--install-dir') + 1])\n"
        "install_dir.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "install_dir.chmod(0o700)\n"
        "target = install_dir / 'codex'\n"
        "shutil.copy2(Path(source_value), target)\n"
        "target.chmod(0o755)\n",
    )


def write_update_command_observer(path: Path) -> None:
    """Observe wrapper verification arguments without faking publication or health."""
    write_fake_script(
        path,
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "log = os.environ.get('CODEX_SWITCH_TEST_VERIFY_ARGS_LOG')\n"
        "if log and 'verify' in args and 'internal' in args:\n"
        "    Path(log).write_text(' '.join(args) + '\\n')\n"
        "driver = os.environ['CODEX_SWITCH_TEST_DESKTOP_DRIVER']\n"
        "python = os.environ.get('CODEX_SWITCH_TEST_DELEGATE_PYTHON', sys.executable)\n"
        "raise SystemExit(subprocess.call([python, '-B', driver, *args]))\n",
    )


def write_update_python_launcher(path: Path, *, delegate_python: str, applications: Path) -> None:
    """Redirect only platform discovery while retaining the real shared engine."""
    write_fake_script(
        path,
        f"#!{sys.executable}\n"
        "import functools, os, runpy, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "entry = next((i for i, value in enumerate(args) "
        "if value.endswith('/codex_switch_update.py')), None)\n"
        "if entry is None:\n"
        f"    os.execv({delegate_python!r}, [{delegate_python!r}, *args])\n"
        "script = args[entry]\n"
        "sys.path.insert(0, str(Path(script).parent))\n"
        "import codex_switch_runtime_binding as binding\n"
        f"apps = Path({str(applications)!r})\n"
        "roots = binding.DesktopRoots(apps / 'ChatGPT.app', "
        "apps / 'Codex.app', apps / 'ChatGPT Classic.app')\n"
        "binding.discover_desktop_hosts = functools.partial("
        "binding.discover_desktop_hosts, roots=roots)\n"
        "sys.argv = [script, *args[entry + 1:]]\n"
        "runpy.run_path(script, run_name='__main__')\n",
    )


def write_fake_desktop_inventory_driver(path: Path) -> None:
    write_fake_script(
        path,
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import runpy\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "real_switcher = Path(os.environ['CODEX_SWITCH_TEST_REAL_SWITCHER'])\n"
        "chatgpt_app = Path(os.environ['CODEX_SWITCH_TEST_CHATGPT_APP'])\n"
        "sys.path.insert(0, str(real_switcher.parent))\n"
        "import codex_switch_runtime_binding as runtime_binding\n"
        "\n"
        "real_discover = runtime_binding.discover_desktop_hosts\n"
        "applications = chatgpt_app.parent\n"
        "fixture_inventory = real_discover(runtime_binding.DesktopRoots(\n"
        "    chatgpt=chatgpt_app,\n"
        "    legacy_codex=applications / 'Codex.app',\n"
        "    chatgpt_classic=applications / 'ChatGPT Classic.app',\n"
        "))\n"
        "if fixture_inventory.current is None:\n"
        "    raise SystemExit('invalid test ChatGPT.app fixture')\n"
        "\n"
        "def discover_desktop_hosts(roots=None):\n"
        "    if roots is None:\n"
        "        return fixture_inventory\n"
        "    return real_discover(roots)\n"
        "\n"
        "runtime_binding.discover_desktop_hosts = discover_desktop_hosts\n"
        "sys.argv = [str(real_switcher), *sys.argv[1:]]\n"
        "runpy.run_path(str(real_switcher), run_name='__main__')\n",
    )


def write_fake_capability_codex(path: Path) -> None:
    write_fake_script(
        path,
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli 0.142.4')\n"
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
        "        print(json.dumps({'id': message['id'], 'result': {'userAgent': 'profile-test'}}), flush=True)\n"
        "    elif message.get('method') == 'plugin/list':\n"
        "        print(json.dumps({'id': message['id'], 'result': {'marketplaces': []}}), flush=True)\n"
        "    elif message.get('method') == 'config/value/write':\n"
        "        params = message['params']\n"
        "        config_path = Path(params['filePath'])\n"
        "        config_path.write_text(config_path.read_text().replace(\n"
        "            'codex_switch_config_write_probe = false',\n"
        "            'codex_switch_config_write_probe = true',\n"
        "        ))\n"
        "        print(json.dumps({\n"
        "            'id': message['id'],\n"
        "            'result': {\n"
        "                'filePath': str(config_path.resolve()),\n"
        "                'status': 'ok',\n"
        "                'version': 'profile-test-v1',\n"
        "            },\n"
        "        }), flush=True)\n",
    )


class CodexProfileSwitchTests(unittest.TestCase):
    def test_switch_help_exposes_explicit_app_profile_selection(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "switch", "--help"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--app-profile", result.stdout)

    def make_workspace(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name).resolve()
        (root / "live").mkdir()
        (root / "live" / "config.toml").write_text(
            'profile = "internal"\n\n[profiles.internal]\n'
        )
        return temp_dir, root

    def make_parity_diagnostic_report(
        self,
        root: Path,
        *,
        findings: tuple[ParityFinding, ...],
        queue: tuple[ParityQueueItem, ...] = (),
    ) -> ParityReport:
        bundle_root = root / "Applications" / "ChatGPT.app"
        return ParityReport(
            healthy=not any(
                finding.severity == "error"
                for finding in findings
            ),
            policy_version=ParityPolicyVersion(PARITY_POLICY_VERSION),
            official_reference=OfficialReference(
                authority="chatgpt-bundle",
                bundle_root=bundle_root,
                bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                bundle_version="1.2026.196",
                bundled_cli=(
                    bundle_root / "Contents" / "Resources" / "codex"
                ),
                cli_version="0.146.0-alpha.3.1",
                binary_sha256="a" * 64,
                schema_sha256="b" * 64,
                feature_inventory_sha256="c" * 64,
            ),
            internal_fingerprint=InternalFingerprint(
                backend_cli=root / "internal" / "codex",
                cli_version="0.144.6",
                binary_sha256="d" * 64,
                active_model="gpt-5.6-sol",
                provider_id="azure",
                wire_api="responses",
                endpoint_sha256="e" * 64,
                auth_source_kind="env",
                capability_receipt_sha256="f" * 64,
                source_catalog=root / "internal" / "models.json",
                source_catalog_sha256="a" * 64,
                config_sha256s=(
                    ("profile", "b" * 64),
                    ("runtime", "c" * 64),
                ),
            ),
            findings=findings,
            synchronization_queue=queue,
        )

    def run_parity_diagnostic_command(
        self,
        command: str,
        store: Store,
        report: ParityReport,
    ) -> tuple[int, str, int]:
        output = io.StringIO()
        if command == "status":
            module = status_module
            args = SimpleNamespace()
            with (
                mock.patch.object(module, "make_store", return_value=store),
                mock.patch.object(
                    module,
                    "print_active_profile_status",
                    return_value=SimpleNamespace(
                        cli_profile="internal",
                        app_profile="internal",
                    ),
                ),
                mock.patch.object(module, "print_shell_codex_status"),
                mock.patch.object(module, "print_app_codex_status"),
                mock.patch.object(
                    module,
                    "collect_parity_report",
                    return_value=report,
                    create=True,
                ) as collect_parity,
                redirect_stdout(output),
            ):
                module.cmd_status(args)
            return 0, output.getvalue(), collect_parity.call_count

        if command == "doctor":
            module = doctor_module
            args = SimpleNamespace()
            with (
                mock.patch.object(module, "make_store", return_value=store),
                mock.patch.object(
                    module,
                    "active_runtime_binding_for_observation",
                    return_value=SimpleNamespace(profile="internal"),
                ),
                mock.patch.object(
                    module,
                    "active_cli_runtime_binding_for_parity",
                    return_value=SimpleNamespace(profile="internal"),
                ),
                mock.patch.object(
                    module,
                    "collect_store_runtime_observation",
                    return_value=None,
                ),
                mock.patch.object(
                    module,
                    "collect_doctor_problems",
                    return_value=[],
                ),
                mock.patch.object(
                    module,
                    "collect_parity_report",
                    return_value=report,
                    create=True,
                ) as collect_parity,
                redirect_stdout(output),
            ):
                try:
                    module.cmd_doctor(args)
                except SystemExit as exc:
                    code = int(exc.code)
                else:
                    code = 0
            return code, output.getvalue(), collect_parity.call_count

        if command == "verify":
            module = verify_module
            args = SimpleNamespace(
                name="internal",
                repair="none",
                app_server_smoke=False,
                runtime_smoke=False,
                exec_smoke=None,
                responses_tool_smoke=False,
                report=False,
            )
            with (
                mock.patch.object(module, "make_store", return_value=store),
                mock.patch.object(store, "load_manifest", return_value={}),
                mock.patch.object(
                    module,
                    "profile_home",
                    return_value=store.managed_home("internal"),
                ),
                mock.patch.object(
                    module,
                    "manifest_uses_canonical_binding",
                    return_value=False,
                ),
                mock.patch.object(
                    module,
                    "collect_store_runtime_observation",
                    return_value=None,
                ),
                mock.patch.object(
                    module,
                    "collect_active_state_problems",
                    return_value=[],
                ),
                mock.patch.object(
                    module,
                    "collect_runtime_config_problems",
                    return_value=[],
                ),
                mock.patch.object(
                    module,
                    "collect_parity_report",
                    return_value=report,
                ) as collect_parity,
                redirect_stdout(output),
            ):
                try:
                    module.cmd_verify(args)
                except SystemExit as exc:
                    code = int(exc.code)
                else:
                    code = 0
            return code, output.getvalue(), collect_parity.call_count

        raise AssertionError(f"unsupported parity diagnostic command: {command}")

    def tomllib_parser_for_config_test(self) -> object:
        if sys.version_info >= (3, 11):
            import tomllib

            return tomllib
        python = shutil.which("python3.12") or shutil.which("python3.11")
        if python is None:
            self.fail("Python 3.11+ is required for home-sync config tests")

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

    def parity_config_projection(
        self,
        *,
        profile_config: Path,
        profile_source: bytes,
        projected_profile: bytes,
        shared_config: Path,
        shared_source: bytes,
        projected_shared: bytes,
        overlay_path: Path,
    ) -> ConfigProjection:
        config_inputs = ConfigInputs(
            profile_config=profile_config,
            sources=(
                (
                    profile_config,
                    hashlib.sha256(profile_source).hexdigest(),
                ),
                (
                    shared_config,
                    hashlib.sha256(shared_source).hexdigest(),
                ),
            ),
        )
        return ConfigProjection(
            config_inputs=config_inputs,
            overlay_path=overlay_path,
            payloads=(
                (profile_config, projected_profile),
                (shared_config, projected_shared),
            ),
            healthy=True,
            findings=(),
            changed_paths=(profile_config, shared_config),
            max_threads_source=shared_config,
        )

    def internal_rebind_fixture(
        self,
        root: Path,
    ) -> tuple[Store, Path, SimpleNamespace, tuple[Path, ...]]:
        store = Store(
            root=root / "store",
            live_codex_home=root / "live",
            launch_agent_path=root / "agent.plist",
            launch_agent_label="test",
            internal_codex_home=root / "store" / "homes" / "internal",
            internal_codex_home_source="explicit",
        )
        store.ensure()
        old_backend = root / "old-internal" / "codex"
        candidate = root / "candidate-internal" / "codex"
        old_backend.parent.mkdir(parents=True)
        candidate.parent.mkdir(parents=True)
        write_fake_app_server_smoke_codex(old_backend)
        write_fake_app_server_smoke_codex(candidate)
        launcher = store.bin_dir / "codex-internal-app"
        write_fake_script(launcher, "#!/usr/bin/env sh\nexit 0\n")
        profile_dir = store.profile_dir("internal")
        profile_dir.mkdir(parents=True, exist_ok=True)
        source_catalog = root / "configured" / "azure-models.json"
        source_catalog.parent.mkdir(parents=True)
        source_catalog.write_bytes(
            b'{"models":[{"provider":"azure","slug":"gpt-5.6-sol"}]}\n'
        )
        source_catalog.chmod(0o640)
        profile_config = profile_dir / "config.toml"
        profile_config.write_text(
            'model = "gpt-5.6-sol"\n'
            'model_provider = "azure"\n'
            f'model_catalog_json = "{source_catalog}"\n'
        )
        profile_config.chmod(0o600)
        internal_home = store.internal_codex_home
        internal_home.mkdir(parents=True, exist_ok=True)
        active_runtime_config = internal_home / "config.toml"
        active_runtime_config.write_text('profile = "internal"\n')
        active_runtime_config.chmod(0o600)
        shared_config = store.official_codex_home / "config.toml"
        shared_config.write_text(
            '[agents]\nmax_threads = 4\n\n[notice]\nkeep = true\n'
        )
        shared_config.chmod(0o600)
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
        official_main = contents / "MacOS" / "ChatGPT"
        official_cli = contents / "Resources" / "codex"
        official_main.parent.mkdir(parents=True)
        official_cli.parent.mkdir(parents=True)
        write_fake_script(official_main, "#!/usr/bin/env sh\nexit 0\n")
        write_fake_script(official_cli, "#!/usr/bin/env sh\nexit 0\n")
        store.manifest_path("internal").write_text(
            json.dumps(
                {
                    "name": "internal",
                    "codex_bin": str(old_backend),
                    "app_cli_path": str(launcher),
                    "app_cli_binding": "launchagent",
                    "runtime_binding": "canonical",
                }
            )
        )
        store.active_path.write_text(
            json.dumps(
                {
                    "profile": "internal",
                    "codex_home": str(internal_home),
                    "shell_cli_path": str(old_backend),
                    "app_cli_path": str(launcher),
                }
            )
        )
        args = SimpleNamespace(
            store_dir=store.root,
            official_codex_home=store.official_codex_home,
            official_codex_home_source="explicit",
            internal_codex_home=store.internal_codex_home,
            internal_codex_home_source="explicit",
            launch_agent_path=store.launch_agent_path,
            launch_agent_label=store.launch_agent_label,
            name="internal",
            codex_bin=str(candidate),
            preserve_app_cli=False,
            rebind_commit_fault_hook=None,
            rebind_desktop_inventory=DesktopInventory(
                current=ChatGPTDesktopHost(
                    kind="chatgpt",
                    bundle_root=bundle_root,
                    bundle_id=CURRENT_CHATGPT_BUNDLE_ID,
                    main_executable=official_main,
                    bundled_cli=official_cli,
                    healthy=True,
                    migration_only=False,
                )
            ),
        )
        observed_paths = (
            store.manifest_path("internal"),
            launcher,
            store.bin_dir / "codex-internal-app.capabilities.json",
            profile_dir / "parity" / "receipt.json",
            profile_dir / "parity" / "model-catalog.json",
            profile_config,
            shared_config,
            active_runtime_config,
            store.root / ".runtime-binding-rebind.json",
            source_catalog,
        )
        return store, source_catalog, args, observed_paths

    def snapshot_rebind_paths(
        self,
        paths: tuple[Path, ...],
    ) -> dict[Path, tuple[str, bytes | None, int | None]]:
        snapshot: dict[Path, tuple[str, bytes | None, int | None]] = {}
        for path in paths:
            if path.is_symlink():
                snapshot[path] = ("symlink", os.readlink(path).encode(), None)
            elif path.exists():
                snapshot[path] = (
                    "file" if path.is_file() else "other",
                    path.read_bytes() if path.is_file() else None,
                    path.stat().st_mode & 0o777,
                )
            else:
                snapshot[path] = ("missing", None, None)
        return snapshot

    def build_internal_home_config_with_projection(
        self,
        official_home: Path,
        profile_name: str,
        target_runtime_config: Path,
        canonical_config: Path,
        config_projection: ConfigProjection,
    ) -> str:
        kwargs: dict[str, object] = {}
        if (
            "config_projection"
            in inspect.signature(build_internal_home_config).parameters
        ):
            kwargs["config_projection"] = config_projection
        return build_internal_home_config(
            official_home,
            profile_name,
            target_runtime_config,
            canonical_config,
            **kwargs,
        )

    def run_switcher(
        self,
        root: Path,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        python_executable: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        clean_env = dict(env or os.environ)
        if (
            "CODEX_SWITCH_SHELL_PROFILE" not in clean_env
            and "CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP" not in clean_env
        ):
            clean_env["CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP"] = "1"
        switch_script = SCRIPT
        desktop_driver = clean_env.pop(
            "CODEX_SWITCH_TEST_DESKTOP_DRIVER",
            None,
        )
        if desktop_driver:
            switch_script = Path(desktop_driver)
            clean_env["CODEX_SWITCH_TEST_REAL_SWITCHER"] = str(SCRIPT)
        command = [
            python_executable or sys.executable,
            str(switch_script),
            "--store-dir",
            str(root / "store"),
            "--live-codex-home",
            str(root / "live"),
            "--launch-agent-path",
            str(root / "agent.plist"),
            *args,
        ]
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env,
        )

    def run_wrapper(
        self,
        root: Path,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        cwd: Path | None = None,
        allow_switch_script: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(WRAPPER),
            "--store-dir",
            str(root / "store"),
            "--live-codex-home",
            str(root / "live"),
            "--launch-agent-path",
            str(root / "agent.plist"),
            *args,
        ]
        clean_env = dict(env or os.environ)
        clean_env.pop("CODEX_CLI_PATH", None)
        clean_env.setdefault("CODEX_SWITCH_SKIP_SELF_UPDATE", "1")
        if not allow_switch_script:
            clean_env.pop("CODEX_SWITCH_SCRIPT", None)
            clean_env.pop("CODEX_SWITCH_TEST_REAL_SWITCHER", None)
            desktop_driver = clean_env.pop(
                "CODEX_SWITCH_TEST_DESKTOP_DRIVER",
                None,
            )
            if desktop_driver:
                clean_env["CODEX_SWITCH_SCRIPT"] = desktop_driver
                clean_env["CODEX_SWITCH_TEST_REAL_SWITCHER"] = str(SCRIPT)
        clean_env.pop("CODEX_SWITCH_HOME", None)
        if (
            "CODEX_SWITCH_SHELL_PROFILE" not in clean_env
            and "CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP" not in clean_env
        ):
            clean_env["CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP"] = "1"
        desktop_fixture = clean_env.get("CODEX_SWITCH_TEST_CHATGPT_APP")
        if desktop_fixture:
            delegate_python = clean_env.get("CODEX_SWITCH_PYTHON") or sys.executable
            launcher = root / "update-fixture-python"
            write_update_python_launcher(
                launcher,
                delegate_python=delegate_python,
                applications=Path(desktop_fixture).parent,
            )
            clean_env.setdefault("CODEX_SWITCH_TEST_DELEGATE_PYTHON", delegate_python)
            clean_env["CODEX_SWITCH_PYTHON"] = str(launcher)
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env,
            cwd=cwd,
        )

    def enable_staged_internal_update_fixture(
        self,
        root: Path,
        env: dict[str, str],
    ) -> Path:
        driver = root / "update-command-observer.py"
        write_update_command_observer(driver)
        official = Path(env["CODEX_SWITCH_TEST_CHATGPT_APP"]) / "Contents/Resources/codex"
        write_fake_app_server_smoke_codex(official, version="codex-cli 1.0.0")
        catalog = root / "fixture-models.json"
        catalog.write_text('{"models":[{"slug":"fixture-model"}]}\n')
        (root / "live/config.toml").write_text(
            'model = "fixture-model"\nmodel_provider = "fixture"\n'
            f'model_catalog_json = {json.dumps(str(catalog))}\n'
            '[model_providers.fixture]\nname = "Local fixture"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
        )
        env["CODEX_SWITCH_SCRIPT"] = str(driver)
        env["CODEX_SWITCH_TEST_REAL_SWITCHER"] = str(SCRIPT)
        env["CODEX_SWITCH_TEST_APP_SERVER_LOG"] = str(root / "update-app-server.log")
        return driver

    def assert_update_scope(
        self, root: Path, env: dict[str, str], scope: str
    ) -> dict[str, object]:
        records = list((root / "store/updates").glob("*/record.json"))
        self.assertEqual(1, len(records))
        update_id = records[0].parent.name
        result = self.run_wrapper(root, "update-internal", "status", update_id, "--json", env=env)
        record = json.loads(result.stdout)
        self.assertEqual("applied", record["state"])
        self.assertEqual(scope, record["scope"])
        return record

    def assert_update_smoke_log(self, env: dict[str, str]) -> None:
        methods = {
            json.loads(line.removeprefix("stdin:"))["method"]
            for line in Path(env["CODEX_SWITCH_TEST_APP_SERVER_LOG"]).read_text().splitlines()
            if line.startswith("stdin:")
        }
        self.assertTrue(
            {"plugin/list", "collaborationMode/list", "thread/start", "turn/start", "thread/read"}
            <= methods,
            methods,
        )

    def assert_staged_update_helper_args(
        self,
        args_log: Path,
        *,
        bound_bin: Path,
        version: str,
        expected_flags: tuple[str, ...] = (),
    ) -> Path:
        args = shlex.split(args_log.read_text())
        self.assertEqual("update-internal", args[0])
        self.assertEqual(version, args[args.index("--version") + 1])
        self.assertEqual(
            str(bound_bin),
            args[args.index("--internal-bin") + 1],
        )
        candidate_dir = Path(args[args.index("--install-dir") + 1])
        self.assertEqual(bound_bin.parent.resolve(), candidate_dir.parent.resolve())
        self.assertNotEqual(bound_bin.parent.resolve(), candidate_dir.resolve())
        self.assertTrue(candidate_dir.name.startswith(".codex-internal-update-"))
        for flag in expected_flags:
            self.assertIn(flag, args)
        return candidate_dir

    def prepare_profiles(self, root: Path) -> tuple[Path, Path, dict[str, str]]:
        path_dir = root / "path"
        path_dir.mkdir()
        internal = root / "internal-bin" / "codex"
        chatgpt_app = root / "Applications" / "ChatGPT.app"
        contents = chatgpt_app / "Contents"
        official = contents / "Resources" / "codex"
        official_main = contents / "MacOS" / "ChatGPT"
        internal.parent.mkdir()
        official.parent.mkdir(parents=True)
        official_main.parent.mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleIdentifier": CURRENT_CHATGPT_BUNDLE_ID,
                    "CFBundleShortVersionString": "1.2026.196",
                },
                handle,
            )
        write_fake_codex(internal, "internal-codex")
        write_fake_codex(official, "official-codex", version="1.0.0")
        write_fake_script(official_main, "#!/usr/bin/env sh\nexit 0\n")
        (path_dir / "codex").symlink_to(internal)
        desktop_driver = root / "desktop-inventory-driver.py"
        write_fake_desktop_inventory_driver(desktop_driver)
        env = os.environ.copy()
        env.pop("CODEX_CLI_PATH", None)
        env["PATH"] = f"{path_dir}{os.pathsep}{env.get('PATH', '')}"
        env["CODEX_SWITCH_TEST_CHATGPT_APP"] = str(chatgpt_app)
        env["CODEX_SWITCH_TEST_DESKTOP_DRIVER"] = str(desktop_driver)
        return internal.resolve(), official, env

    def run_verified_split_switch(
        self,
        root: Path,
        official_codex: Path,
        *,
        dry_run: bool,
    ) -> str:
        store = Store(
            root / "store",
            root / "live",
            root / "agent.plist",
            "com.openai.codex-cli-path",
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                switching_module,
                "resolve_store_runtime_binding",
                return_value=SimpleNamespace(desktop_cli=official_codex),
            ),
            mock.patch.dict(
                os.environ,
                {"CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP": "1"},
            ),
            redirect_stdout(output),
        ):
            switching_module.switch_profile(
                store,
                "internal",
                dry_run,
                False,
                "shared",
                None,
                False,
                False,
                True,
                "official",
            )
        return output.getvalue()

    def prepare_internal_plugin_profile(
        self,
        root: Path,
        *,
        config_text: str,
        catalog: dict[str, object],
        sources: dict[str, Path] | None = None,
        python_executable: str | None = None,
    ) -> tuple[Path, dict[str, str], Path]:
        internal_codex, official_codex, env = self.prepare_profiles(root)
        write_fake_plugin_refresh_codex(
            internal_codex,
            catalog=catalog,
            sources=sources,
        )
        (root / "live" / "config.toml").write_text(config_text)
        setup_python = python_executable
        if setup_python is None and sys.version_info < (3, 11):
            setup_python = (
                shutil.which("python3.12")
                or shutil.which("python3.11")
            )
        self.run_switcher(
            root,
            "init",
            "--app-cli-path",
            str(official_codex),
            "--capture-current",
            "internal",
            env=env,
            python_executable=setup_python,
        )
        internal_home = root / "store" / "homes" / "internal"
        internal_home.mkdir(parents=True, exist_ok=True)
        (internal_home / "config.toml").write_text(config_text)
        return internal_codex, env, internal_home

    def read_manifest(self, root: Path, name: str) -> dict[str, str]:
        path = root / "store" / "profiles" / name / "manifest.json"
        return json.loads(path.read_text())

    def make_installed_wrapper(self, root: Path, version: str = "0.1.1") -> Path:
        source_root = self.write_adapter_source(
            root,
            version,
            "",
            source_name="installed-source",
        )
        scripts_dir = source_root / "scripts"
        shutil.copy2(WRAPPER, scripts_dir / "codex-switch")
        (scripts_dir / "codex-switch").chmod(0o755)
        fake_switcher = scripts_dir / "codex_profile_switch.py"
        fake_switcher.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('old-switcher:' + ' '.join(sys.argv[1:]))\n"
        )
        fake_switcher.chmod(0o755)
        receipt = build_release_bundle(source_root, root / "installed-release")
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run(
            [
                sys.executable,
                str(PROMOTION_MODULE),
                "--candidate-root",
                str(receipt.package_dir),
                "--layout-root",
                str(root / "lib"),
                "--expected-version",
                version,
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return root / "lib" / "current" / "scripts" / "codex-switch"

    def make_remote_wrapper_tarball(self, root: Path, version: str = "9.9.9") -> Path:
        return self.make_adapter_release_tarball(
            root,
            version,
            "#!/usr/bin/env sh\n"
            "printf 'synced-wrapper:%s\\n' \"$*\"\n"
            "printf 'skip-self-update:%s\\n' \"${CODEX_SWITCH_SKIP_SELF_UPDATE:-}\"\n"
        )

    def write_adapter_source(
        self,
        root: Path,
        version: str,
        wrapper_text: str,
        *,
        source_name: str = "adapter-source",
    ) -> Path:
        source_root = root / source_name
        scripts_dir = source_root / "scripts"
        (source_root / "agents").mkdir(parents=True)
        (source_root / "docs").mkdir()
        (source_root / "evals").mkdir()
        scripts_dir.mkdir()
        (source_root / "README.md").write_text("adapter source\n")
        (source_root / "SKILL.md").write_text("adapter source\n")
        (source_root / "VERSION").write_text(f"{version}\n")
        (source_root / "run.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (source_root / "agents" / "openai.yaml").write_text(
            "name: adapter-source\n"
        )
        (source_root / "docs" / "release.md").write_text("adapter source\n")
        (source_root / "evals" / "evals.json").write_text('{"evals": []}\n')
        wrapper_lines = wrapper_text.splitlines(keepends=True)
        if wrapper_lines and wrapper_lines[0].startswith("#!"):
            shebang = wrapper_lines[0]
            wrapper_body = "".join(wrapper_lines[1:])
        else:
            shebang = "#!/usr/bin/env sh\n"
            wrapper_body = wrapper_text
        (scripts_dir / "codex-switch").write_text(
            shebang
            + 'if [ "${1:-}" = "--version" ]; then\n'
            + f"  printf '%s\\n' '{version}'\n"
            + "  exit 0\n"
            + "fi\n"
            + wrapper_body
        )
        (scripts_dir / "package-release.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n"
        )
        write_required_release_modules(scripts_dir)
        for path in (
            source_root / "run.sh",
            scripts_dir / "codex-switch",
            scripts_dir / "package-release.sh",
        ):
            path.chmod(0o755)
        return source_root

    def make_adapter_release_tarball(
        self,
        root: Path,
        version: str,
        wrapper_text: str,
    ) -> Path:
        source_root = self.write_adapter_source(root, version, wrapper_text)
        output_root = root / "adapter-release"
        receipt = build_release_bundle(source_root, output_root)
        return receipt.archive

    def make_source_archive(
        self,
        root: Path,
        version: str = "9.9.9",
        *,
        remove_path: str | None = None,
    ) -> Path:
        source_root = root / "source-release" / f"codex-switch-{version}"
        scripts_dir = source_root / "scripts"
        (source_root / "agents").mkdir(parents=True)
        (source_root / "docs" / "troubleshooting").mkdir(parents=True)
        (source_root / "evals").mkdir(parents=True)
        (scripts_dir / "__pycache__").mkdir(parents=True)
        (source_root / "README.md").write_text("source archive\n")
        (source_root / "SKILL.md").write_text("source archive\n")
        (source_root / "VERSION").write_text(f"{version}\n")
        source_runner = source_root / "run.sh"
        source_runner.write_text("#!/usr/bin/env sh\nprintf 'source-runner\\n'\n")
        source_runner.chmod(0o741)
        (source_root / "agents" / "openai.yaml").write_text(
            "name: source-agent\n"
        )
        (
            source_root
            / "docs"
            / "troubleshooting"
            / "source-fallback.md"
        ).write_text("source fallback\n")
        (source_root / "evals" / "evals.json").write_text('{"evals": []}\n')
        raw_wrapper = scripts_dir / "codex-switch"
        raw_wrapper.write_text(
            "#!/usr/bin/env sh\n"
            "source_path=\"$0\"\n"
            "while [ -L \"$source_path\" ]; do\n"
            "  source_dir=\"$(cd -P -- \"$(dirname -- \"$source_path\")\" && pwd)\"\n"
            "  source_path=\"$(readlink \"$source_path\")\"\n"
            "  case \"$source_path\" in\n"
            "    /*) ;;\n"
            "    *) source_path=\"$source_dir/$source_path\" ;;\n"
            "  esac\n"
            "done\n"
            "if [ \"${1:-}\" = \"--version\" ]; then\n"
            "  sed -n '1p' \"$(dirname -- \"$source_path\")/../VERSION\"\n"
            "  exit 0\n"
            "fi\n"
            "printf 'raw-source:%s\\n' \"$*\"\n"
            "printf 'skip-self-update:%s\\n' "
            "\"${CODEX_SWITCH_SKIP_SELF_UPDATE:-}\"\n"
            "printf 'source-root:%s\\n' "
            "\"$(cd -P -- \"$(dirname -- \"$source_path\")/..\" && pwd)\"\n"
        )
        raw_wrapper.chmod(0o751)
        package_script = scripts_dir / "package-release.sh"
        package_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf 'executed\\n' > "
            "\"${CODEX_SWITCH_MALICIOUS_SENTINEL:?}\"\n"
            'out="${CODEX_SWITCH_DIST_DIR:-$PWD/dist}"\n'
            'pkg="$out/codex-switch"\n'
            'rm -rf "$pkg"\n'
            'mkdir -p "$pkg/scripts"\n'
            f"printf '{version}\\n' > \"$pkg/VERSION\"\n"
            "cat > \"$pkg/scripts/codex-switch\" <<'SH'\n"
            "#!/usr/bin/env sh\n"
            "printf 'packaged-source:%s\\n' \"$*\"\n"
            "printf 'skip-self-update:%s\\n' \"${CODEX_SWITCH_SKIP_SELF_UPDATE:-}\"\n"
            "SH\n"
            'chmod +x "$pkg/scripts/codex-switch"\n'
            'echo "$out/codex-switch.tar.gz"\n'
        )
        package_script.chmod(0o701)
        write_required_release_modules(scripts_dir)
        (scripts_dir / "__pycache__" / "poison.pyc").write_bytes(b"cache")
        (source_root / "DO_NOT_COPY.txt").write_text("outside allowlist\n")
        if remove_path is not None:
            removed = source_root / remove_path
            if removed.is_dir():
                shutil.rmtree(removed)
            else:
                removed.unlink()
        tarball = root / "source-codex-switch.tar.gz"
        with tarfile.open(tarball, "w:gz") as archive:
            archive.add(source_root, arcname=f"codex-switch-{version}")
        return tarball

    def assert_trusted_source_fallback(
        self,
        installed_root: Path,
        malicious_sentinel: Path,
        *,
        promoted: bool = False,
    ) -> None:
        self.assertFalse(malicious_sentinel.exists())
        expected_entries = {
            "README.md",
            "SKILL.md",
            "VERSION",
            "run.sh",
            "agents",
            "docs",
            "evals",
            "scripts",
        }
        if promoted:
            expected_entries.add("bundle-manifest.json")
        self.assertEqual(
            expected_entries,
            {path.name for path in installed_root.iterdir()},
        )
        self.assertFalse((installed_root / "DO_NOT_COPY.txt").exists())
        self.assertFalse((installed_root / "scripts" / "__pycache__").exists())
        self.assertTrue(
            (installed_root / "scripts" / "package-release.sh").is_file()
        )
        self.assertEqual(
            0o755 if promoted else 0o741,
            (installed_root / "run.sh").stat().st_mode & 0o777,
        )
        self.assertEqual(
            0o755 if promoted else 0o751,
            (installed_root / "scripts" / "codex-switch").stat().st_mode
            & 0o777,
        )
        self.assertEqual(
            0o755 if promoted else 0o701,
            (installed_root / "scripts" / "package-release.sh").stat().st_mode
            & 0o777,
        )

    def init_release_repo(self, root: Path, version: str = "0.1.3") -> Path:
        repo = root / "release-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "codex-switch").write_text("#!/usr/bin/env sh\necho old\n")
        (repo / "scripts" / "codex-switch").chmod(0o755)
        (repo / "VERSION").write_text(f"{version}\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial release"], cwd=repo, check=True)
        subprocess.run(["git", "tag", f"v{version}"], cwd=repo, check=True)
        return repo

    def run_release_auto(
        self,
        repo: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RELEASE_AUTO), "--repo", str(repo), *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def release_plan(self, repo: Path) -> dict[str, object]:
        result = self.run_release_auto(repo, "plan", "--json")
        return json.loads(result.stdout)

    def release_assets(
        self,
        root: Path,
        *,
        install: bytes = b"installer\n",
        runner: bytes = b"runner\n",
        archive: bytes = b"archive\n",
    ):
        paths = {
            "install.sh": root / "install.sh",
            "run.sh": root / "run.sh",
            "codex-switch.tar.gz": root / "codex-switch.tar.gz",
        }
        paths["install.sh"].write_bytes(install)
        paths["run.sh"].write_bytes(runner)
        paths["codex-switch.tar.gz"].write_bytes(archive)
        return tuple(
            release_auto.build_asset_evidence(name, path)
            for name, path in paths.items()
        )

    def self_update_env(self, root: Path, tarball: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_SWITCH_LIB_DIR"] = str(root / "lib")
        env["CODEX_SWITCH_TARBALL_URL"] = tarball.as_uri()
        return env

    def test_remote_runner_downloads_release_and_execs_command(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            tarball = self.make_adapter_release_tarball(
                root,
                "9.9.9",
                "#!/usr/bin/env sh\n"
                "printf 'fake-codex-switch:%s\\n' \"$*\"\n"
                "printf 'skip-self-update:%s\\n' \"${CODEX_SWITCH_SKIP_SELF_UPDATE:-}\"\n"
                "printf 'script-dir:%s\\n' \"$(cd -- \"$(dirname -- \"$0\")\" && pwd)\"\n"
            )

            install_dir = root / "bin"
            lib_dir = root / "lib"
            env = os.environ.copy()
            env["CODEX_SWITCH_TARBALL_URL"] = tarball.as_uri()
            env["CODEX_SWITCH_INSTALL_DIR"] = str(install_dir)
            env["CODEX_SWITCH_LIB_DIR"] = str(lib_dir)

            result = subprocess.run(
                [str(REMOTE_RUNNER), "status", "--verbose"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertEqual(
                1,
                result.stdout.count("fake-codex-switch:status --verbose"),
            )
            self.assertIn("skip-self-update:1", result.stdout)
            self.assertIn(
                f"script-dir:{(lib_dir / 'current' / 'scripts').resolve()}",
                result.stdout,
            )
            self.assertTrue((lib_dir / "current" / "scripts" / "codex-switch").exists())
            self.assertFalse((install_dir / "codex-switch").exists())

    def test_installer_falls_back_to_source_archive_and_installs_path_command(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            source_archive = self.make_source_archive(root)
            malicious_sentinel = root / "installer-malicious-executed"
            install_dir = root / "bin"
            lib_dir = root / "lib"
            missing_tarball = root / "missing-codex-switch.tar.gz"
            env = os.environ.copy()
            env["CODEX_SWITCH_TARBALL_URL"] = missing_tarball.as_uri()
            env["CODEX_SWITCH_SOURCE_TARBALL_URL"] = source_archive.as_uri()
            env["CODEX_SWITCH_INSTALL_DIR"] = str(install_dir)
            env["CODEX_SWITCH_LIB_DIR"] = str(lib_dir)
            env["CODEX_SWITCH_MALICIOUS_SENTINEL"] = str(malicious_sentinel)

            subprocess.run(
                [str(INSTALLER)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            result = subprocess.run(
                [str(install_dir / "codex-switch"), "status", "--verbose"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertEqual(
                1,
                result.stdout.count("raw-source:status --verbose"),
            )
            self.assertIn(
                f"source-root:{(lib_dir / 'current').resolve()}",
                result.stdout,
            )
            self.assertEqual("9.9.9\n", (lib_dir / "current" / "VERSION").read_text())
            self.assertTrue((install_dir / "codex-switch").exists())
            self.assert_trusted_source_fallback(
                lib_dir / "current",
                malicious_sentinel,
                promoted=True,
            )

    def test_installer_rejects_downloaded_source_missing_required_allowlist_path(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            source_archive = self.make_source_archive(root, remove_path="docs")
            malicious_sentinel = root / "installer-malicious-executed"
            install_dir = root / "bin"
            lib_dir = root / "lib"
            env = os.environ.copy()
            env["CODEX_SWITCH_TARBALL_URL"] = (
                root / "missing-codex-switch.tar.gz"
            ).as_uri()
            env["CODEX_SWITCH_SOURCE_TARBALL_URL"] = source_archive.as_uri()
            env["CODEX_SWITCH_INSTALL_DIR"] = str(install_dir)
            env["CODEX_SWITCH_LIB_DIR"] = str(lib_dir)
            env["CODEX_SWITCH_MALICIOUS_SENTINEL"] = str(malicious_sentinel)

            result = subprocess.run(
                [str(INSTALLER)],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn(
                "Downloaded source archive is missing required directory: docs",
                result.stderr,
            )
            self.assertFalse(malicious_sentinel.exists())
            self.assertFalse((lib_dir / "current").exists())
            self.assertFalse((install_dir / "codex-switch").is_symlink())

    def test_downloaded_source_cleanup_failure_is_not_reported_as_success(
        self,
    ) -> None:
        for label, entrypoint, args in (
            ("installer", INSTALLER, ()),
            ("runner", REMOTE_RUNNER, ("status",)),
        ):
            with self.subTest(label=label):
                temp_dir, root = self.make_workspace()
                with temp_dir:
                    source_archive = self.make_source_archive(root)
                    fake_bin = root / "fake-bin"
                    fake_bin.mkdir()
                    write_fake_script(
                        fake_bin / "rm",
                        "#!/bin/sh\n"
                        'case "$*" in\n'
                        '  *"/scripts/__pycache__") exit 17 ;;\n'
                        "esac\n"
                        'exec /bin/rm "$@"\n',
                    )
                    install_dir = root / "bin"
                    lib_dir = root / "lib"
                    env = os.environ.copy()
                    env["PATH"] = f"{fake_bin}:{env['PATH']}"
                    env["CODEX_SWITCH_TARBALL_URL"] = (
                        root / "missing-codex-switch.tar.gz"
                    ).as_uri()
                    env["CODEX_SWITCH_SOURCE_TARBALL_URL"] = (
                        source_archive.as_uri()
                    )
                    env["CODEX_SWITCH_INSTALL_DIR"] = str(install_dir)
                    env["CODEX_SWITCH_LIB_DIR"] = str(lib_dir)

                    result = subprocess.run(
                        [str(entrypoint), *args],
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )

                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse((lib_dir / "current").exists())
                    self.assertFalse((install_dir / "codex-switch").is_symlink())

    def test_installer_preserves_local_source_version(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            source_root = self.write_adapter_source(
                root,
                "0.1.13-dev",
                "#!/usr/bin/env sh\nprintf 'source-wrapper\\n'\n",
            )

            install_dir = root / "bin"
            lib_dir = root / "lib"
            env = os.environ.copy()
            env["CODEX_SWITCH_SOURCE_DIR"] = str(source_root)
            env["CODEX_SWITCH_INSTALL_DIR"] = str(install_dir)
            env["CODEX_SWITCH_LIB_DIR"] = str(lib_dir)

            subprocess.run(
                [str(INSTALLER)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertEqual("0.1.13-dev\n", (lib_dir / "current" / "VERSION").read_text())
            self.assertTrue((install_dir / "codex-switch").exists())

    def test_remote_runner_falls_back_to_source_archive_and_execs_command(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            source_archive = self.make_source_archive(root)
            malicious_sentinel = root / "runner-malicious-executed"
            install_dir = root / "bin"
            lib_dir = root / "lib"
            missing_tarball = root / "missing-codex-switch.tar.gz"
            env = os.environ.copy()
            env["CODEX_SWITCH_TARBALL_URL"] = missing_tarball.as_uri()
            env["CODEX_SWITCH_SOURCE_TARBALL_URL"] = source_archive.as_uri()
            env["CODEX_SWITCH_INSTALL_DIR"] = str(install_dir)
            env["CODEX_SWITCH_LIB_DIR"] = str(lib_dir)
            env["CODEX_SWITCH_MALICIOUS_SENTINEL"] = str(malicious_sentinel)

            result = subprocess.run(
                [str(REMOTE_RUNNER), "status", "--verbose"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertEqual(
                1,
                result.stdout.count("raw-source:status --verbose"),
            )
            self.assertIn("skip-self-update:1", result.stdout)
            self.assertIn(
                f"source-root:{(lib_dir / 'current').resolve()}",
                result.stdout,
            )
            self.assertEqual("9.9.9\n", (lib_dir / "current" / "VERSION").read_text())
            self.assertFalse((install_dir / "codex-switch").exists())
            self.assert_trusted_source_fallback(
                lib_dir / "current",
                malicious_sentinel,
                promoted=True,
            )

    def test_local_wrapper_self_updates_release_install_before_command(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root)
            tarball = self.make_remote_wrapper_tarball(root)
            env = self.self_update_env(root, tarball)

            previous_umask = os.umask(0o077)
            try:
                result = subprocess.run(
                    [str(local_wrapper), "status", "--verbose"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
            finally:
                os.umask(previous_umask)

            self.assertIn("synced-wrapper:status --verbose", result.stdout)
            self.assertIn("codex-switch self-update: checking latest release", result.stderr)
            self.assertIn(
                "codex-switch self-update: synced implementation 0.1.1 -> 9.9.9",
                result.stderr,
            )
            self.assertEqual("9.9.9\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_local_wrapper_self_update_reports_already_up_to_date(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root, version="9.9.9")
            tarball = self.make_remote_wrapper_tarball(root, version="9.9.9")
            env = self.self_update_env(root, tarball)

            result = subprocess.run(
                [str(local_wrapper), "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("old-switcher:status", result.stdout)
            self.assertIn("codex-switch self-update: checking latest release", result.stderr)
            self.assertIn(
                "codex-switch self-update: already up to date 9.9.9",
                result.stderr,
            )
            self.assertEqual("9.9.9\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_local_wrapper_self_update_checks_every_invocation(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root, version="9.9.9")
            tarball = self.make_remote_wrapper_tarball(root, version="9.9.9")
            env = self.self_update_env(root, tarball)

            first = subprocess.run(
                [str(local_wrapper), "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            second = subprocess.run(
                [str(local_wrapper), "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            for result in (first, second):
                self.assertIn("old-switcher:status", result.stdout)
                self.assertIn("codex-switch self-update: checking latest release", result.stderr)
                self.assertIn(
                    "codex-switch self-update: already up to date 9.9.9",
                    result.stderr,
                )
            self.assertEqual("9.9.9\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_local_wrapper_skip_self_update_keeps_existing_install(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root)
            tarball = self.make_remote_wrapper_tarball(root)
            env = self.self_update_env(root, tarball)

            result = subprocess.run(
                [str(local_wrapper), "--skip-self-update", "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("old-switcher:status", result.stdout)
            self.assertNotIn("self-update", result.stderr)
            self.assertEqual("0.1.1\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_local_wrapper_split_previews_skip_self_update_and_preserve_install(
        self,
    ) -> None:
        command_forms = (
            ("split", "--dry-run"),
            ("internal", "--app-profile", "official", "--dry-run"),
        )

        for command_form in command_forms:
            with self.subTest(command_form=command_form):
                temp_dir, root = self.make_workspace()
                with temp_dir:
                    local_wrapper = self.make_installed_wrapper(root)
                    tarball = self.make_remote_wrapper_tarball(root)
                    env = self.self_update_env(root, tarball)

                    result = subprocess.run(
                        [str(local_wrapper), *command_form],
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )

                    self.assertNotIn("synced-wrapper", result.stdout)
                    self.assertNotIn("self-update", result.stderr)
                    self.assertIn("old-switcher:switch internal --dry-run", result.stdout)
                    self.assertEqual(
                        "0.1.1\n",
                        (root / "lib" / "current" / "VERSION").read_text(),
                    )

    def test_local_wrapper_split_apply_preserves_normal_self_update(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root)
            tarball = self.make_remote_wrapper_tarball(root)
            env = self.self_update_env(root, tarball)

            result = subprocess.run(
                [str(local_wrapper), "split"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("synced-wrapper:split", result.stdout)
            self.assertIn(
                "codex-switch self-update: checking latest release",
                result.stderr,
            )
            self.assertEqual(
                "9.9.9\n",
                (root / "lib" / "current" / "VERSION").read_text(),
            )

    def test_local_wrapper_split_keep_version_skips_self_update_and_retains_workflow(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root)
            tarball = self.make_remote_wrapper_tarball(root)
            env = self.self_update_env(root, tarball)
            env["CODEX_SWITCH_HOME"] = str(root / "store")

            result = subprocess.run(
                [str(local_wrapper), "split", "--keep-version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertNotIn("self-update", result.stderr)
            self.assertNotIn("synced-wrapper", output)
            self.assertIn("Update check: skipped by command option.", output)
            self.assertIn(
                "old-switcher:switch internal --dry-run --app-profile official",
                output,
            )
            self.assertIn(
                "old-switcher:switch internal --app-profile official",
                output,
            )
            self.assertIn("old-switcher:repair-plugins internal", output)
            self.assertIn("old-switcher:verify internal", output)
            self.assertIn("old-switcher:doctor", output)
            self.assertIn("old-switcher:status", output)
            self.assertEqual("0.1.1\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_local_wrapper_does_not_self_update_to_older_release(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root, version="0.1.13-dev")
            tarball = self.make_remote_wrapper_tarball(root, version="0.1.12")
            env = self.self_update_env(root, tarball)

            result = subprocess.run(
                [str(local_wrapper), "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("old-switcher:status", result.stdout)
            self.assertIn("codex-switch self-update: checking latest release", result.stderr)
            self.assertIn(
                "codex-switch self-update: already up to date 0.1.13-dev",
                result.stderr,
            )
            self.assertNotIn("synced implementation", result.stderr)
            self.assertEqual("0.1.13-dev\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_local_wrapper_self_updates_prerelease_to_formal_release(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root, version="0.1.13-dev")
            tarball = self.make_remote_wrapper_tarball(root, version="0.1.13")
            env = self.self_update_env(root, tarball)

            result = subprocess.run(
                [str(local_wrapper), "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("synced-wrapper:status", result.stdout)
            self.assertIn(
                "codex-switch self-update: synced implementation 0.1.13-dev -> 0.1.13",
                result.stderr,
            )
            self.assertEqual("0.1.13\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_source_checkout_wrapper_does_not_self_update(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            fake_switcher = root / "fake_switcher.py"
            fake_switcher.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "print('source-switcher:' + ' '.join(sys.argv[1:]))\n"
            )
            fake_switcher.chmod(0o755)
            tarball = self.make_remote_wrapper_tarball(root)
            env = self.self_update_env(root, tarball)
            env["CODEX_SWITCH_SCRIPT"] = str(fake_switcher)

            result = subprocess.run(
                [str(WRAPPER), "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("source-switcher:status", result.stdout)
            self.assertFalse((root / "lib" / "current" / "VERSION").exists())

    def test_self_update_failure_does_not_block_local_command(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root)
            env = self.self_update_env(root, root / "missing-release.tar.gz")

            result = subprocess.run(
                [str(local_wrapper), "status"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("old-switcher:status", result.stdout)
            self.assertIn("sync failed; continuing", result.stderr)
            self.assertEqual("0.1.1\n", (root / "lib" / "current" / "VERSION").read_text())

    def test_local_wrapper_self_update_falls_back_to_source_archive(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root)
            source_archive = self.make_source_archive(root)
            malicious_sentinel = root / "self-update-malicious-executed"
            env = self.self_update_env(root, root / "missing-release.tar.gz")
            env["CODEX_SWITCH_SOURCE_TARBALL_URL"] = source_archive.as_uri()
            env["CODEX_SWITCH_MALICIOUS_SENTINEL"] = str(malicious_sentinel)

            result = subprocess.run(
                [str(local_wrapper), "status", "--verbose"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("raw-source:status --verbose", result.stdout)
            self.assertIn(
                f"source-root:{(root / 'lib' / 'current').resolve()}",
                result.stdout,
            )
            self.assertEqual("9.9.9\n", (root / "lib" / "current" / "VERSION").read_text())
            self.assert_trusted_source_fallback(
                root / "lib" / "current",
                malicious_sentinel,
                promoted=True,
            )

    def test_downloaded_source_self_update_stops_after_staging_cleanup_failure(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            local_wrapper = self.make_installed_wrapper(root)
            source_archive = self.make_source_archive(root)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            failure_state = root / "rm-failure-state"
            write_fake_script(
                fake_bin / "rm",
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *"/scripts/__pycache__")\n'
                '    : > "$CODEX_SWITCH_RM_FAILURE_STATE"\n'
                "    exit 17\n"
                "    ;;\n"
                "esac\n"
                'if [ -f "$CODEX_SWITCH_RM_FAILURE_STATE" ]; then\n'
                '  case "$*" in\n'
                '    *".self-update."*) exit 18 ;;\n'
                "  esac\n"
                "fi\n"
                'exec /bin/rm "$@"\n',
            )
            env = self.self_update_env(root, root / "missing-release.tar.gz")
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["CODEX_SWITCH_SOURCE_TARBALL_URL"] = source_archive.as_uri()
            env["CODEX_SWITCH_RM_FAILURE_STATE"] = str(failure_state)

            result = subprocess.run(
                [str(local_wrapper), "status"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertEqual(0, result.returncode)
            self.assertIn("old-switcher:status", result.stdout)
            self.assertNotIn("raw-source:status", result.stdout)
            self.assertIn("sync failed; continuing", result.stderr)
            self.assertEqual(
                "0.1.1\n",
                (root / "lib" / "current" / "VERSION").read_text(),
            )

    def test_release_workflow_uploads_required_assets(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text()

        self.assertIn("contents: write", workflow)
        self.assertIn("@fission-ai/openspec@1.3.1", workflow)
        self.assertIn("scripts/package-release.sh", workflow)
        self.assertIn('"$RELEASE_AUTO" assets', workflow)
        self.assertIn('"$RELEASE_AUTO" reconcile', workflow)
        self.assertNotIn("gh release upload", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertIn("install.sh", workflow)
        self.assertIn("dist/run.sh", workflow)
        self.assertIn("dist/codex-switch.tar.gz", workflow)

    def test_package_release_includes_troubleshooting_docs(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            dist = root / "dist"
            env = os.environ.copy()
            env["CODEX_SWITCH_DIST_DIR"] = str(dist)

            subprocess.run(
                [str(Path("scripts/package-release.sh"))],
                cwd=Path(__file__).parents[1],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertTrue(
                (
                    dist
                    / "codex-switch"
                    / "docs"
                    / "troubleshooting"
                    / "internal-azure-responses-resource-stickiness.md"
                ).exists()
            )

    def test_auto_release_plan_detects_runtime_change_and_next_patch_tag(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            repo = self.init_release_repo(root)
            (repo / "scripts" / "codex-switch").write_text(
                "#!/usr/bin/env sh\necho changed\n"
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "feat: change runtime"],
                cwd=repo,
                check=True,
            )

            plan = self.release_plan(repo)

        self.assertTrue(plan["release_required"])
        self.assertEqual(plan["latest_tag"], "v0.1.3")
        self.assertEqual(plan["next_tag"], "v0.1.4")
        self.assertEqual(plan["next_version"], "0.1.4")
        self.assertIn("scripts/codex-switch", plan["release_relevant_files"])

    def test_auto_release_plan_rejects_non_ancestor_latest_tag(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            repo = self.init_release_repo(root)
            main_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(
                ["git", "checkout", "-q", "-b", "side-release", "v0.1.3"],
                cwd=repo,
                check=True,
            )
            (repo / "scripts" / "side").write_text("side\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "side release"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "tag", "v0.1.4"], cwd=repo, check=True)
            subprocess.run(
                ["git", "checkout", "-q", main_branch],
                cwd=repo,
                check=True,
            )
            (repo / "scripts" / "codex-switch").write_text(
                "#!/usr/bin/env sh\necho main\n"
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "main change"],
                cwd=repo,
                check=True,
            )

            result = self.run_release_auto(repo, "plan", "--json", check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("is not an ancestor", result.stderr)

    def test_auto_release_plan_resumes_latest_tag_with_missing_assets(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            repo = self.init_release_repo(root)
            planning = repo / ".planning"
            planning.mkdir()
            (planning / "note.md").write_text("later planning note\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "docs: planning note"],
                cwd=repo,
                check=True,
            )
            github = FakeGitHubReleaseAdapter(
                assets={
                    "install.sh": b"installer",
                    "run.sh": b"runner",
                }
            )

            plan = release_auto.build_plan(repo, "HEAD", github=github)
            tagged_commit = subprocess.run(
                ["git", "rev-list", "-n", "1", "v0.1.3"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

        self.assertEqual("reconcile", plan["release_action"])
        self.assertTrue(plan["release_required"])
        self.assertEqual("v0.1.3", plan["target_tag"])
        self.assertEqual(tagged_commit, plan["target_commit"])
        self.assertEqual(["codex-switch.tar.gz"], plan["missing_assets"])

    def test_auto_release_plan_skips_matching_complete_published_tag(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            repo = self.init_release_repo(root)
            github = FakeGitHubReleaseAdapter(
                assets={
                    "install.sh": b"installer",
                    "run.sh": b"runner",
                    "codex-switch.tar.gz": b"archive",
                }
            )

            plan = release_auto.build_plan(repo, "HEAD", github=github)

        self.assertEqual("none", plan["release_action"])
        self.assertFalse(plan["release_required"])
        self.assertEqual("v0.1.3", plan["latest_tag"])
        self.assertEqual("", plan["target_tag"])
        self.assertEqual([], plan["missing_assets"])

    def test_auto_release_plan_resumes_complete_draft_release(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            repo = self.init_release_repo(root)
            github = FakeGitHubReleaseAdapter(
                assets={
                    "install.sh": b"installer",
                    "run.sh": b"runner",
                    "codex-switch.tar.gz": b"archive",
                },
                draft=True,
            )

            plan = release_auto.build_plan(repo, "HEAD", github=github)

        self.assertEqual("reconcile", plan["release_action"])
        self.assertTrue(plan["release_required"])
        self.assertEqual("v0.1.3", plan["target_tag"])
        self.assertEqual([], plan["missing_assets"])

    def test_auto_release_prepare_rejects_tag_on_different_commit(self) -> None:
        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.validate_prepare_state(
                source_commit="a" * 40,
                remote_main_commit="a" * 40,
                candidate_commit="b" * 40,
                existing_tag_commit="c" * 40,
            )

        self.assertIn("different commit", str(caught.exception))

    def test_auto_release_prepare_rejects_remote_main_race(self) -> None:
        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.validate_prepare_state(
                source_commit="a" * 40,
                remote_main_commit="d" * 40,
                candidate_commit="b" * 40,
                existing_tag_commit=None,
            )

        self.assertIn("remote main moved", str(caught.exception))

    def test_auto_release_prepare_rejects_existing_same_commit_tag(self) -> None:
        with self.assertRaises(release_auto.ReleaseConflict) as caught:
            release_auto.validate_prepare_state(
                source_commit="a" * 40,
                remote_main_commit="a" * 40,
                candidate_commit="b" * 40,
                existing_tag_commit="b" * 40,
            )

        self.assertIn("already exists", str(caught.exception))

    def test_release_source_binding_rejects_untracked_and_ignored_files(self) -> None:
        for ignored in (False, True):
            with self.subTest(ignored=ignored):
                temp_dir = tempfile.TemporaryDirectory()
                root = Path(temp_dir.name)
                with temp_dir:
                    repo = self.init_release_repo(root)
                    (repo / "install.sh").write_text(
                        "#!/usr/bin/env sh\nexit 0\n"
                    )
                    (repo / "install.sh").chmod(0o755)
                    if ignored:
                        (repo / ".gitignore").write_text("scripts/generated.py\n")
                    subprocess.run(
                        ["git", "add", "install.sh", ".gitignore"]
                        if ignored
                        else ["git", "add", "install.sh"],
                        cwd=repo,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "commit", "-q", "-m", "bind release source"],
                        cwd=repo,
                        check=True,
                    )
                    (repo / "scripts" / "generated.py").write_text("VALUE = 1\n")

                    with self.assertRaises(release_auto.ReleaseConflict) as caught:
                        release_auto.assert_release_source_matches_commit(
                            repo,
                            release_auto.resolve_commit(repo, "HEAD"),
                            required_files=("VERSION",),
                            required_directories=("scripts",),
                        )

                self.assertIn(
                    "Release source file set differs from commit tree",
                    str(caught.exception),
                )
                self.assertIn("scripts/generated.py", str(caught.exception))

    def test_release_reconciliation_rejects_existing_asset_checksum_mismatch(
        self,
    ) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            assets = self.release_assets(root)
            github = FakeGitHubReleaseAdapter(
                assets={"install.sh": b"different installer"}
            )

            with self.assertRaises(release_auto.ReleaseConflict) as caught:
                release_auto.reconcile_release_assets(
                    tag="v1.2.3",
                    release_commit="a" * 40,
                    tag_commit="a" * 40,
                    assets=assets,
                    github=github,
                )

        self.assertIn("checksum mismatch", str(caught.exception))
        self.assertEqual([], github.upload_attempts)

    def test_release_reconciliation_publish_failure_reruns_same_tag(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            assets = self.release_assets(root)
            github = FakeGitHubReleaseAdapter(
                exists=False,
                fail_publish_once=True,
            )

            with self.assertRaises(release_auto.ReleaseError):
                release_auto.reconcile_release_assets(
                    tag="v1.2.3",
                    release_commit="a" * 40,
                    tag_commit="a" * 40,
                    assets=assets,
                    github=github,
                )

            receipt = release_auto.reconcile_release_assets(
                tag="v1.2.3",
                release_commit="a" * 40,
                tag_commit="a" * 40,
                assets=assets,
                github=github,
            )

        self.assertEqual(["v1.2.3"], github.create_calls)
        self.assertEqual(
            Counter(
                {
                    "install.sh": 1,
                    "run.sh": 1,
                    "codex-switch.tar.gz": 1,
                }
            ),
            Counter(github.upload_attempts),
        )
        self.assertEqual(["v1.2.3", "v1.2.3"], github.publish_calls)
        self.assertFalse(github.draft)
        self.assertEqual(
            {"install.sh", "run.sh", "codex-switch.tar.gz"},
            set(github.assets),
        )
        self.assertEqual("published", receipt["outcome"])
        self.assertEqual([], receipt["uploaded_assets"])

    def test_release_reconciliation_detects_post_publish_asset_corruption(
        self,
    ) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            assets = self.release_assets(root)
            github = FakeGitHubReleaseAdapter(
                exists=False,
                corrupt_after_publish="codex-switch.tar.gz",
            )

            with self.assertRaises(release_auto.ReleaseConflict) as caught:
                release_auto.reconcile_release_assets(
                    tag="v1.2.3",
                    release_commit="a" * 40,
                    tag_commit="a" * 40,
                    assets=assets,
                    github=github,
                )

        self.assertIn("Published release asset checksum mismatch", str(caught.exception))

    def test_github_release_creation_requires_existing_tag_and_starts_draft(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(release_auto, "_run", return_value=completed) as run:
            release_auto.GitHubCliAdapter("owner/repo").create_release("v1.2.3")

        command = run.call_args.args[0]
        self.assertIn("--verify-tag", command)
        self.assertIn("--draft", command)

    def test_auto_release_plan_skips_planning_only_changes(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            repo = self.init_release_repo(root)
            planning = repo / ".planning" / "verification"
            planning.mkdir(parents=True)
            (planning / "note.md").write_text("verified\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "docs: record verification"],
                cwd=repo,
                check=True,
            )

            plan = self.release_plan(repo)

        self.assertFalse(plan["release_required"])
        self.assertEqual(plan["latest_tag"], "v0.1.3")
        self.assertEqual(plan["next_tag"], "")
        self.assertEqual(plan["release_relevant_files"], [])

    def test_auto_release_bump_updates_version_for_tag(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        with temp_dir:
            repo = self.init_release_repo(root)

            self.run_release_auto(repo, "bump", "--tag", "v0.1.4")

            self.assertEqual((repo / "VERSION").read_text(), "0.1.4\n")

    def test_auto_release_workflow_creates_tag_and_release_assets(self) -> None:
        workflow = AUTO_RELEASE_WORKFLOW.read_text()

        self.assertIn("branches:", workflow)
        self.assertIn("- main", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn('"$RELEASE_AUTO" plan', workflow)
        self.assertIn("release_required", workflow)
        self.assertIn('"$RELEASE_AUTO" bump --tag', workflow)
        self.assertIn('git tag "$NEXT_TAG"', workflow)
        self.assertIn("push --atomic origin", workflow)
        self.assertIn("scripts/package-release.sh", workflow)
        self.assertIn('"$RELEASE_AUTO" reconcile', workflow)
        self.assertIn("scripts/test_codex_update_release.py", workflow)
        self.assertIn(
            'if [ -f scripts/test_codex_update_release.py ]; then',
            workflow,
        )
        self.assertIn(
            "steps.plan.outputs.prepare_required == 'true'",
            workflow,
        )
        self.assertNotIn(
            'if [ "$RELEASE_ACTION" = "prepare" ]; then',
            workflow,
        )
        self.assertIn(
            "Historical release source has no "
            "scripts/test_codex_update_release.py",
            workflow,
        )
        self.assertIn('$CODEX_SWITCH_DIST_DIR/run.sh', workflow)
        self.assertIn('$CODEX_SWITCH_DIST_DIR/codex-switch.tar.gz', workflow)
        self.assertLess(
            workflow.index("scripts/package-release.sh"),
            workflow.index('git tag "$NEXT_TAG"'),
        )
        self.assertLess(
            workflow.index("scripts/package-release.sh"),
            workflow.index("push --atomic origin"),
        )
        self.assertLess(
            workflow.index('"$RELEASE_AUTO" prepare'),
            workflow.index("push --atomic origin"),
        )
        self.assertLess(
            workflow.index('"$RELEASE_AUTO" assets'),
            workflow.index('git tag "$NEXT_TAG"'),
        )
        self.assertIn(
            '--force-with-lease="refs/heads/main:$SOURCE_COMMIT"',
            workflow,
        )
        self.assertIn(
            '--force-with-lease="refs/tags/$NEXT_TAG:"',
            workflow,
        )

    def test_manual_release_workflow_packages_before_reconciliation(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text()

        self.assertIn("$RUNNER_TEMP/codex-switch-release-tools", workflow)
        self.assertIn("ref: main", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn('"$RELEASE_AUTO" assets', workflow)
        self.assertIn('"$RELEASE_AUTO" reconcile', workflow)
        self.assertIn("scripts/test_codex_update_release.py", workflow)
        self.assertIn(
            'if [ -f scripts/test_codex_update_release.py ]; then',
            workflow,
        )
        self.assertIn(
            "Historical release source has no "
            "scripts/test_codex_update_release.py",
            workflow,
        )
        self.assertLess(
            workflow.index("scripts/package-release.sh"),
            workflow.index('"$RELEASE_AUTO" reconcile'),
        )
        self.assertNotIn("gh release upload", workflow)
        self.assertNotIn("--clobber", workflow)

    def test_init_defaults_official_codex_bin_to_app_cli_not_path_codex(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)

            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            official = self.read_manifest(root, "openai-official")
            internal = self.read_manifest(root, "internal")
            self.assertEqual(official["codex_bin"], str(official_codex))
            self.assertEqual(official["app_cli_path"], str(official_codex))
            self.assertEqual(internal["codex_bin"], str(internal_codex))
            self.assertEqual(
                internal["app_cli_path"],
                str(root / "store" / "bin" / "codex-internal-app"),
            )

    def test_init_capture_internal_resolves_path_symlink_backend(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            path_codex = root / "path" / "codex"
            backend = root / "internal-bin" / "codex"
            self.assertTrue(path_codex.is_symlink())

            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            internal = self.read_manifest(root, "internal")
            self.assertEqual(internal["codex_bin"], str(backend.resolve()))
            self.assertNotEqual(internal["codex_bin"], str(path_codex))

    def test_internal_switch_migrates_legacy_symlink_backend_manifest(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            path_codex = root / "path" / "codex"
            backend = root / "internal-bin" / "codex"
            write_fake_capability_codex(backend)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = (
                root / "store" / "profiles" / "internal" / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["codex_bin"] = str(path_codex)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

            switched = self.run_switcher(
                root,
                "switch",
                "internal",
                "--skip-launchctl",
                env=env,
                check=False,
            )
            self.assertEqual(
                switched.returncode,
                0,
                switched.stdout + switched.stderr,
            )

            migrated = json.loads(manifest_path.read_text())
            self.assertEqual(migrated["codex_bin"], str(backend.resolve()))
            receipt_path = (
                root / "store" / "bin" / "codex-internal-app.capabilities.json"
            )
            self.assertTrue(receipt_path.is_file())
            self.assertFalse(receipt_path.is_symlink())
            shim_text = (root / "store" / "bin" / "codex").read_text()
            self.assertIn("exec-internal-shell", shim_text)
            self.assertIn(
                "codex_switch_runtime_binding.py",
                shim_text,
            )
            self.assertIn(
                str(backend.resolve()),
                (root / "store" / "bin" / "codex-internal-app").read_text(),
            )

    def test_switch_updates_shim_and_app_cli_to_target_profile(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")
            shim = root / "store" / "bin" / "codex"
            self.assertIn(f'exec "{official_codex}" "$@"', shim.read_text())
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual("openai-official", active["profile"])
            self.assertEqual("openai-official", active["cli_profile"])
            self.assertEqual("openai-official", active["app_profile"])
            self.assertEqual(active["app_cli_path"], str(official_codex))
            agent = plistlib.loads((root / "agent.plist").read_bytes())
            self.assertEqual(agent["ProgramArguments"][-1], str(official_codex))
            dirty_env = dict(env)
            dirty_env["CODEX_CLI_PATH"] = "/tmp/not-the-isolated-app-cli"
            self.run_switcher(root, "doctor", env=dirty_env)

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_shim = shim.read_text()
            self.assertIn("exec-internal-shell", internal_shim)
            self.assertIn(
                "codex_switch_runtime_binding.py",
                internal_shim,
            )
            active = json.loads((root / "store" / "active.json").read_text())
            internal_app = root / "store" / "bin" / "codex-internal-app"
            self.assertEqual("internal", active["profile"])
            self.assertEqual("internal", active["cli_profile"])
            self.assertEqual("internal", active["app_profile"])
            self.assertEqual(active["app_cli_path"], str(internal_app))
            agent = plistlib.loads((root / "agent.plist").read_bytes())
            self.assertEqual(agent["ProgramArguments"][-1], str(internal_app))

    def test_split_switch_preview_names_cli_and_app_profiles(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            preview_output = self.run_verified_split_switch(
                root,
                official_codex,
                dry_run=True,
            )
            self.assertIn("CLI profile: internal", preview_output)
            self.assertIn("App profile: openai-official", preview_output)
            self.assertIn(f"CLI binary: {internal_codex}", preview_output)
            self.assertIn(f"App binary: {official_codex}", preview_output)

    def test_split_switch_resolves_verified_official_binding_for_legacy_manifest(
        self,
    ) -> None:
        manifests = {
            "internal": {"name": "internal"},
            "openai-official": {
                "name": "openai-official",
                "codex_bin": "/tmp/legacy-official",
                "app_cli_path": "/tmp/legacy-official",
            },
        }
        store = SimpleNamespace(load_manifest=lambda name: manifests[name])
        verified_cli = Path("/verified/ChatGPT.app/Contents/Resources/codex")
        receipt = SimpleNamespace(preview_lines=("switch preview",))

        with (
            mock.patch.object(
                switching_module,
                "manifest_uses_canonical_binding",
                return_value=False,
            ),
            mock.patch.object(
                switching_module,
                "resolve_store_runtime_binding",
                return_value=SimpleNamespace(desktop_cli=verified_cli),
            ) as resolve_binding,
            mock.patch(
                "codex_switch_transaction.execute_transaction",
                return_value=receipt,
            ) as execute,
            redirect_stdout(io.StringIO()),
        ):
            switching_module._switch_profile_unlocked(
                store,
                "internal",
                True,
                False,
                "snapshot",
                None,
                False,
                False,
                True,
                "official",
            )

        self.assertEqual(1, resolve_binding.call_count)
        self.assertEqual("openai-official", resolve_binding.call_args.args[1])
        request = execute.call_args.args[1]
        self.assertEqual(
            str(verified_cli),
            request.options["canonical_app_cli_path"],
        )

    def test_internal_app_selection_rejects_cli_only_generation_before_transaction(
        self,
    ) -> None:
        backend = Path("/tmp/codex-switch-cli-only-backend")
        manifest = {
            "name": "internal",
            "codex_bin": str(backend),
            "internal_cli_generation": {
                "schema_version": 1,
                "scope": "cli-only",
                "backend_sha256": "a" * 64,
                "backend_version": "2.0.0",
            },
            "internal_app_readiness": "unverified",
        }
        store = SimpleNamespace(load_manifest=lambda _name: manifest)

        with (
            mock.patch.object(
                switching_module,
                "resolve_store_runtime_binding",
                return_value=SimpleNamespace(
                    desktop_cli=Path("/tmp/codex-internal-app")
                ),
            ),
            mock.patch(
                "codex_switch_transaction.execute_transaction"
            ) as execute,
            self.assertRaisesRegex(
                SwitchError,
                "internal.app_readiness.unverified",
            ),
        ):
            switching_module._switch_profile_unlocked(
                store,
                "internal",
                True,
                False,
                "snapshot",
                None,
                False,
                False,
                True,
                None,
            )

        execute.assert_not_called()

    def test_split_switch_uses_internal_cli_and_official_app_atomically(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_verified_split_switch(
                root,
                official_codex,
                dry_run=False,
            )

            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual("internal", active["profile"])
            self.assertEqual("internal", active["cli_profile"])
            self.assertEqual("openai-official", active["app_profile"])
            self.assertEqual(str(official_codex), active["app_cli_path"])
            self.assertEqual(
                root / "store" / "homes" / "internal",
                Path(active["codex_home"]),
            )
            shim = (root / "store" / "bin" / "codex").read_text()
            self.assertIn("exec-internal-shell", shim)
            self.assertIn(str(internal_codex), shim)
            agent = plistlib.loads((root / "agent.plist").read_bytes())
            self.assertEqual(str(official_codex), agent["ProgramArguments"][-1])

    def test_unsupported_split_request_preserves_committed_switch_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _internal_codex, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--skip-launchctl",
                env=env,
            )
            protected_paths = (
                root / "store" / "active.json",
                root / "store" / "bin" / "codex",
                root / "agent.plist",
            )
            before = {path: path.read_bytes() for path in protected_paths}

            rejected = self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--app-profile",
                "internal",
                "--skip-launchctl",
                env=env,
                check=False,
            )

            self.assertNotEqual(0, rejected.returncode)
            self.assertIn(
                "selection.unsupported",
                rejected.stdout + rejected.stderr,
            )
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in protected_paths},
            )

    def test_status_reports_shell_codex_alignment(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl", env=env)

            mismatch = self.run_switcher(root, "status", env=env)
            mismatch_output = mismatch.stdout + mismatch.stderr
            self.assertIn("PATH codex alignment: mismatch", mismatch_output)
            self.assertIn('eval "$(codex-switch shim-env)"', mismatch_output)

            shim_dir = root / "store" / "bin"
            aligned_env = dict(env)
            aligned_env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
            aligned = self.run_switcher(root, "status", env=aligned_env)
            self.assertIn("PATH codex alignment: ok", aligned.stdout + aligned.stderr)

            shim_env = self.run_switcher(root, "shim-env")
            self.assertIn(f'export PATH="{shim_dir}:$PATH"', shim_env.stdout)
            self.assertIn("hash -r", shim_env.stdout)

    def test_parity_diagnostics_share_unhealthy_core_code(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
            )
            store.ensure()
            report = self.make_parity_diagnostic_report(
                root,
                findings=(
                    ParityFinding(
                        category="protocol",
                        code="parity.protocol.core_incompatible",
                        severity="error",
                        message="core protocol is incompatible",
                    ),
                ),
            )

            for command in ("status", "doctor", "verify"):
                with self.subTest(command=command):
                    code, output, collect_count = (
                        self.run_parity_diagnostic_command(
                            command,
                            store,
                            report,
                        )
                    )
                    self.assertEqual(1, collect_count)
                    self.assertEqual(
                        0 if command == "status" else 1,
                        code,
                        output,
                    )
                    self.assertIn("Parity health: unhealthy", output)
                    self.assertIn(
                        "Parity finding: "
                        "parity.protocol.core_incompatible",
                        output,
                    )

    def test_parity_diagnostics_share_optional_queue_order(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
            )
            store.ensure()
            report = self.make_parity_diagnostic_report(
                root,
                findings=(
                    ParityFinding(
                        category="protocol",
                        code="parity.protocol.optional_missing",
                        severity="warning",
                        message="optional protocol method is absent",
                    ),
                    ParityFinding(
                        category="feature",
                        code="parity.feature.optional_missing",
                        severity="warning",
                        message="optional feature is absent",
                    ),
                ),
                queue=(
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

            for command in ("status", "doctor", "verify"):
                with self.subTest(command=command):
                    code, output, collect_count = (
                        self.run_parity_diagnostic_command(
                            command,
                            store,
                            report,
                        )
                    )
                    self.assertEqual(1, collect_count)
                    self.assertEqual(0, code, output)
                    self.assertIn("Parity health: healthy", output)
                    self.assertIn(
                        "Parity finding: "
                        "parity.feature.optional_missing",
                        output,
                    )
                    self.assertIn(
                        "Parity finding: "
                        "parity.protocol.optional_missing",
                        output,
                    )
                    feature_queue = (
                        "Parity sync: feature skill_search "
                        "(parity.feature.optional_missing)"
                    )
                    protocol_queue = (
                        "Parity sync: protocol client_request:app/read "
                        "(parity.protocol.optional_missing)"
                    )
                    self.assertIn(feature_queue, output)
                    self.assertIn(protocol_queue, output)
                    self.assertLess(
                        output.index(feature_queue),
                        output.index(protocol_queue),
                        output,
                    )

    def test_split_diagnostics_make_internal_app_parity_not_applicable(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
            )
            store.ensure()
            internal_profile = store.profile_dir("internal")
            internal_profile.mkdir(parents=True)
            store.manifest_path("internal").write_text("{}\n")
            official_profile = store.profile_dir("openai-official")
            official_profile.mkdir(parents=True)
            store.manifest_path("openai-official").write_text("{}\n")
            store.active_path.write_text(
                json.dumps(
                    {
                        "profile": "internal",
                        "cli_profile": "internal",
                        "app_profile": "openai-official",
                    }
                )
                + "\n"
            )
            report = self.make_parity_diagnostic_report(
                root,
                findings=(),
            )
            expected = (
                "Internal App parity: not applicable "
                "(App profile: openai-official)"
            )

            status_output = io.StringIO()
            with (
                mock.patch.object(status_module, "make_store", return_value=store),
                mock.patch.object(
                    status_module,
                    "print_active_profile_status",
                    return_value=SimpleNamespace(
                        cli_profile="internal",
                        app_profile="openai-official",
                    ),
                ),
                mock.patch.object(status_module, "print_shell_codex_status"),
                mock.patch.object(status_module, "print_app_codex_status"),
                mock.patch.object(
                    status_module,
                    "selection_uses_shared_configuration",
                    return_value=False,
                ),
                mock.patch.object(
                    status_module,
                    "collect_parity_report",
                    return_value=report,
                ) as status_collect,
                redirect_stdout(status_output),
            ):
                status_module.cmd_status(SimpleNamespace())
            self.assertEqual(0, status_collect.call_count)
            self.assertIn(expected, status_output.getvalue())

            doctor_output = io.StringIO()
            parity_binding = SimpleNamespace(profile="internal")
            with (
                mock.patch.object(doctor_module, "make_store", return_value=store),
                mock.patch.object(
                    doctor_module,
                    "active_runtime_binding_for_observation",
                    return_value=None,
                ),
                mock.patch.object(
                    doctor_module,
                    "collect_store_runtime_observation",
                    return_value=None,
                ),
                mock.patch.object(
                    doctor_module,
                    "active_shared_configuration_report",
                    return_value=None,
                ),
                mock.patch.object(
                    doctor_module,
                    "collect_doctor_problems",
                    return_value=[],
                ),
                mock.patch.object(
                    doctor_module,
                    "manifest_uses_canonical_binding",
                    return_value=True,
                ),
                mock.patch.object(
                    doctor_module,
                    "resolve_store_runtime_binding",
                    return_value=parity_binding,
                ),
                mock.patch.object(
                    doctor_module,
                    "collect_parity_report",
                    return_value=report,
                ) as doctor_collect,
                redirect_stdout(doctor_output),
            ):
                doctor_module.cmd_doctor(SimpleNamespace())
            self.assertEqual(0, doctor_collect.call_count)
            self.assertIn(expected, doctor_output.getvalue())
            self.assertIn("Doctor passed", doctor_output.getvalue())

            verify_output = io.StringIO()
            verify_args = SimpleNamespace(
                name="internal",
                repair="none",
                app_server_smoke=False,
                runtime_smoke=False,
                exec_smoke=None,
                responses_tool_smoke=False,
                report=False,
            )
            with (
                mock.patch.object(verify_module, "make_store", return_value=store),
                mock.patch.object(
                    verify_module,
                    "selection_uses_shared_configuration",
                    return_value=False,
                ),
                mock.patch.object(
                    verify_module,
                    "manifest_uses_canonical_binding",
                    return_value=False,
                ),
                mock.patch.object(
                    verify_module,
                    "collect_store_runtime_observation",
                    return_value=None,
                ),
                mock.patch.object(
                    verify_module,
                    "collect_active_state_problems",
                    return_value=[],
                ),
                mock.patch.object(
                    verify_module,
                    "collect_runtime_config_problems",
                    return_value=[],
                ),
                mock.patch.object(
                    verify_module,
                    "collect_parity_report",
                    return_value=report,
                ) as verify_collect,
                redirect_stdout(verify_output),
            ):
                verify_module.cmd_verify(verify_args)
            self.assertEqual(0, verify_collect.call_count)
            self.assertIn(expected, verify_output.getvalue())
            self.assertIn(
                "Verification passed for internal",
                verify_output.getvalue(),
            )

    def test_read_only_parity_diagnostics_preserve_owned_artifacts(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
            )
            store.ensure()
            profile = store.profile_dir("internal")
            parity_dir = profile / "parity"
            parity_dir.mkdir(parents=True)
            owned_paths = (
                store.manifest_path("internal"),
                parity_dir / "receipt.json",
                parity_dir / "model-catalog.json",
                profile / "config.toml",
                store.bin_dir / "codex-internal-app",
            )
            for index, path in enumerate(owned_paths):
                path.write_bytes(f"artifact-{index}\n".encode())
                path.chmod(0o755 if path == owned_paths[-1] else 0o600)

            def snapshot() -> tuple[
                tuple[str, int, int, int, int, int, int, bytes],
                ...,
            ]:
                rows = []
                for path in owned_paths:
                    info = path.lstat()
                    rows.append(
                        (
                            str(path),
                            info.st_mode,
                            info.st_dev,
                            info.st_ino,
                            info.st_size,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                            path.read_bytes(),
                        )
                    )
                return tuple(rows)

            report = self.make_parity_diagnostic_report(
                root,
                findings=(
                    ParityFinding(
                        category="receipt",
                        code="parity.receipt.missing",
                        severity="error",
                        message="receipt is missing",
                    ),
                ),
            )
            before = snapshot()

            for command in ("status", "doctor", "verify"):
                with self.subTest(command=command):
                    self.run_parity_diagnostic_command(
                        command,
                        store,
                        report,
                    )
                    self.assertEqual(before, snapshot())

    def test_switch_installs_shell_bootstrap_for_cli_alignment(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            shell_profile = root / "home" / ".zshrc"
            shell_profile.parent.mkdir()
            shell_profile.write_text("# user shell config\nexport OTHER=1\n")
            switch_env = dict(env)
            switch_env["CODEX_SWITCH_SHELL_PROFILE"] = str(shell_profile)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=switch_env,
            )

            result = self.run_switcher(
                root,
                "switch",
                "internal",
                "--skip-launchctl",
                env=switch_env,
            )

            output = result.stdout + result.stderr
            shim_dir = root / "store" / "bin"
            content = shell_profile.read_text()
            self.assertIn("Shell CLI bootstrap:", output)
            self.assertIn("# user shell config", content)
            self.assertIn("export OTHER=1", content)
            self.assertIn("# >>> codex-switch shell cli >>>", content)
            self.assertIn(f'export PATH="{shim_dir}:$PATH"', content)
            self.assertIn("hash -r", content)
            self.assertIn("# <<< codex-switch shell cli <<<", content)
            active = json.loads((root / "store" / "active.json").read_text())
            backup = json.loads((root / "store" / "backups" / active["backup_id"] / "backup.json").read_text())
            self.assertIn(str(shell_profile), [entry["path"] for entry in backup["entries"]])

    def test_switch_replaces_existing_shell_bootstrap_without_duplication(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            shell_profile = root / "home" / ".zshrc"
            shell_profile.parent.mkdir()
            shell_profile.write_text(
                "before\n"
                "# >>> codex-switch shell cli >>>\n"
                'export PATH="/old/codex-switch/bin:$PATH"\n'
                "# <<< codex-switch shell cli <<<\n"
                "after\n"
            )
            switch_env = dict(env)
            switch_env["CODEX_SWITCH_SHELL_PROFILE"] = str(shell_profile)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=switch_env,
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl", env=switch_env)
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl", env=switch_env)

            content = shell_profile.read_text()
            self.assertEqual(1, content.count("# >>> codex-switch shell cli >>>"))
            self.assertEqual(1, content.count("# <<< codex-switch shell cli <<<"))
            self.assertNotIn("/old/codex-switch/bin", content)
            self.assertIn(f'export PATH="{root / "store" / "bin"}:$PATH"', content)
            self.assertIn("before", content)
            self.assertIn("after", content)

    def test_switch_can_skip_shell_bootstrap(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            shell_profile = root / "home" / ".zshrc"
            switch_env = dict(env)
            switch_env["CODEX_SWITCH_SHELL_PROFILE"] = str(shell_profile)
            switch_env["CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP"] = "1"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=switch_env,
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl", env=switch_env)

            self.assertFalse(shell_profile.exists())

    def test_internal_switch_uses_managed_home_and_backup_plan(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                "[features]\n"
                "memory = true\n"
                "\n"
                "[mcp_servers.shared]\n"
                'command = "shared-mcp"\n'
            )
            (live_home / "auth.json").write_text('{"official":"auth"}\n')
            (live_home / "sessions").mkdir()
            (live_home / "history.jsonl").write_text("official history\n")
            (live_home / "rules").mkdir()
            (live_home / "rules" / "tool.json").write_text("{}\n")

            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            internal_profile_config = root / "store" / "profiles" / "internal" / "config.toml"
            internal_profile_config.write_text(
                'model = "internal-model"\n'
                'model_provider = "internal-provider"\n'
                "\n"
                "[model_providers.internal-provider]\n"
                'name = "Internal"\n'
            )

            dry_run = self.run_switcher(root, "switch", "internal", "--dry-run")
            dry_output = dry_run.stdout + dry_run.stderr
            self.assertIn("Backup plan:", dry_output)
            self.assertIn("Mutation plan:", dry_output)
            self.assertIn(str(root / "store" / "homes" / "internal"), dry_output)

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_home = root / "store" / "homes" / "internal"
            internal_config = (internal_home / "config.toml").read_text()
            self.assertIn("[features]", internal_config)
            self.assertIn("[mcp_servers.shared]", internal_config)
            self.assertIn('model = "internal-model"', internal_config)
            self.assertFalse((internal_home / "auth.json").exists())
            self.assertFalse((internal_home / "sessions").exists())
            self.assertFalse((internal_home / "history.jsonl").exists())
            self.assertTrue((live_home / "auth.json").exists())

            shim = root / "store" / "bin" / "codex"
            shim_text = shim.read_text()
            self.assertIn(str(internal_home), shim_text)
            self.assertIn("exec-internal-shell", shim_text)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "internal")
            self.assertEqual(active["home_mode"], "managed")
            self.assertEqual(active["codex_home"], str(internal_home))
            self.assertTrue(active.get("backup_id"))
            backup_manifest = (
                root / "store" / "backups" / active["backup_id"] / "backup.json"
            )
            self.assertTrue(backup_manifest.exists())
            backup = json.loads(backup_manifest.read_text())
            self.assertEqual(backup["operation"], "switch")
            self.assertEqual(backup["to_profile"], "internal")

    def test_official_switch_syncs_shared_state_back_without_internal_runtime(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            (live_home / "auth.json").write_text('{"official":"auth"}\n')
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_home = root / "store" / "homes" / "internal"
            (internal_home / "config.toml").write_text(
                'notify = ["turn-ended"]\n'
                'model = "internal-runtime-model"\n'
                "\n"
                "[features]\n"
                "codex_hooks = true\n"
                "\n"
                "[mcp_servers.internal_shared]\n"
                'command = "internal-mcp"\n'
            )
            (internal_home / "auth.json").write_text('{"internal":"auth"}\n')
            (internal_home / "history.jsonl").write_text("internal history\n")

            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--skip-launchctl",
                "--skip-app-cli",
                "--skip-shim",
            )

            official_config = (live_home / "config.toml").read_text()
            self.assertIn('notify = ["turn-ended"]', official_config)
            self.assertIn("[features]", official_config)
            self.assertIn("codex_hooks = true", official_config)
            self.assertIn("[mcp_servers.internal_shared]", official_config)
            self.assertNotIn("internal-runtime-model", official_config)
            self.assertEqual('{"official":"auth"}\n', (live_home / "auth.json").read_text())
            self.assertFalse((live_home / "history.jsonl").exists())
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "openai-official")
            self.assertEqual(active["home_mode"], "official")
            self.assertEqual(active["codex_home"], str(live_home))

    def test_official_switch_does_not_create_self_referential_rules_symlink(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            rules = live_home / "rules"
            rules.mkdir()
            (rules / "workflow.md").write_text("keep rules\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_rules = root / "store" / "homes" / "internal" / "rules"
            self.assertTrue(internal_rules.is_symlink())
            self.assertEqual(os.readlink(internal_rules), str(rules))

            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--skip-launchctl",
                "--skip-app-cli",
                "--skip-shim",
            )

            self.assertFalse(rules.is_symlink())
            self.assertEqual("keep rules\n", (rules / "workflow.md").read_text())

    def test_shared_support_sync_removes_target_home_symlink_instead_of_copying_loop(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            source_home = root / "source"
            target_home = root / "target"
            source_home.mkdir()
            target_home.mkdir()
            (target_home / "prompts").mkdir()
            (target_home / "prompts" / "state.json").write_text("{}\n")
            (source_home / "prompts").symlink_to(target_home / "prompts")
            (source_home / "rules").symlink_to(target_home / "rules")
            (target_home / "rules").symlink_to(target_home / "rules")

            mutated = sync_shared_support(source_home, target_home, prefer_link=False)

            self.assertIn(target_home / "prompts", mutated)
            self.assertFalse((target_home / "prompts").is_symlink())
            self.assertEqual("{}\n", (target_home / "prompts" / "state.json").read_text())
            self.assertIn(target_home / "rules", mutated)
            self.assertFalse((target_home / "rules").exists())
            self.assertFalse((target_home / "rules").is_symlink())

    def test_shared_support_sync_does_not_propagate_source_self_symlink(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            source_home = root / "source"
            target_home = root / "target"
            source_home.mkdir()
            target_home.mkdir()
            (source_home / "prompts").symlink_to(source_home / "prompts")
            (source_home / "skills").symlink_to(source_home / "skills")
            (target_home / "skills").symlink_to(target_home / "skills")

            mutated = sync_shared_support(source_home, target_home, prefer_link=False)

            self.assertNotIn(target_home / "prompts", mutated)
            self.assertFalse((target_home / "prompts").exists())
            self.assertFalse((target_home / "prompts").is_symlink())
            self.assertIn(target_home / "skills", mutated)
            self.assertFalse((target_home / "skills").exists())
            self.assertFalse((target_home / "skills").is_symlink())

    def test_shared_support_directory_copy_skips_nested_target_home_symlinks(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            source_home = root / "source"
            target_home = root / "target"
            source_home.mkdir()
            target_home.mkdir()
            source_tool = source_home / "rules"
            source_tool.mkdir()
            (source_tool / "settings.json").write_text("{}\n")
            (source_tool / "nested-loop").symlink_to(
                target_home / "rules" / "nested-loop"
            )

            sync_shared_support(source_home, target_home, prefer_link=False)

            target_tool = target_home / "rules"
            self.assertTrue(target_tool.is_dir())
            self.assertEqual("{}\n", (target_tool / "settings.json").read_text())
            self.assertFalse((target_tool / "nested-loop").exists())
            self.assertFalse((target_tool / "nested-loop").is_symlink())

    def test_desktop_global_settings_state_sync_merges_safe_settings_only(self) -> None:
        from codex_switch_home_sync import merge_desktop_global_state_settings

        source = desktop_global_state_payload(
            bounds_width=1440,
            hotkey="cmd+shift+5",
            auto_context=True,
            prompt_history_label="source",
        )
        target = desktop_global_state_payload(
            bounds_width=1024,
            hotkey="cmd+shift+4",
            auto_context=False,
            prompt_history_label="target",
        )
        target["target-only-runtime"] = "keep"
        target_atom = target["electron-persisted-atom-state"]
        self.assertIsInstance(target_atom, dict)
        target_atom["target-only-ui-setting"] = "keep"

        merged = merge_desktop_global_state_settings(source, target)

        self.assertEqual(
            1440,
            merged["electron-main-window-bounds"]["width"],
        )
        self.assertEqual("cmd+shift+5", merged["appshotHotkey"])
        self.assertEqual(
            True,
            merged["electron-persisted-atom-state"][
                "composer-auto-context-enabled"
            ],
        )
        self.assertEqual(
            {"entries": ["target-prompt"]},
            merged["electron-persisted-atom-state"]["prompt-history"],
        )
        self.assertNotIn(
            "source-thread",
            merged["electron-persisted-atom-state"][
                "heartbeat-thread-permissions-by-id"
            ],
        )
        self.assertEqual(
            "target-remote-host",
            merged["selected-remote-host-id"],
        )
        self.assertEqual(
            [{"thread": "target-queued"}],
            merged["queued-follow-ups"],
        )
        self.assertEqual("keep", merged["target-only-runtime"])
        self.assertEqual(
            "keep",
            merged["electron-persisted-atom-state"]["target-only-ui-setting"],
        )

    def test_switch_syncs_desktop_global_settings_state_between_independent_homes(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            (live_home / ".codex-global-state.json").write_text(
                json.dumps(
                    desktop_global_state_payload(
                        bounds_width=1400,
                        hotkey="cmd+shift+5",
                        auto_context=True,
                        prompt_history_label="official",
                    )
                )
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_switcher(
                root,
                "switch",
                "internal",
                "--skip-launchctl",
                "--skip-app-cli",
                "--skip-shim",
            )

            internal_home = root / "store" / "homes" / "internal"
            internal_state = json.loads(
                (internal_home / ".codex-global-state.json").read_text()
            )
            self.assertEqual(
                1400,
                internal_state["electron-main-window-bounds"]["width"],
            )
            self.assertEqual("cmd+shift+5", internal_state["appshotHotkey"])
            self.assertNotIn(
                "prompt-history",
                internal_state.get("electron-persisted-atom-state", {}),
            )
            self.assertNotIn("queued-follow-ups", internal_state)
            self.assertNotIn("selected-remote-host-id", internal_state)
            self.assertFalse((internal_home / ".credentials.json").exists())

            (internal_home / ".codex-global-state.json").write_text(
                json.dumps(
                    desktop_global_state_payload(
                        bounds_width=1680,
                        hotkey="cmd+shift+6",
                        auto_context=False,
                        prompt_history_label="internal",
                    )
                )
            )
            (live_home / ".codex-global-state.json").write_text(
                json.dumps(
                    desktop_global_state_payload(
                        bounds_width=1200,
                        hotkey="cmd+shift+4",
                        auto_context=True,
                        prompt_history_label="official-target",
                    )
                )
            )

            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--skip-launchctl",
                "--skip-app-cli",
                "--skip-shim",
            )

            official_state = json.loads(
                (live_home / ".codex-global-state.json").read_text()
            )
            self.assertEqual(
                1680,
                official_state["electron-main-window-bounds"]["width"],
            )
            self.assertEqual("cmd+shift+6", official_state["appshotHotkey"])
            self.assertEqual(
                {"entries": ["official-target-prompt"]},
                official_state["electron-persisted-atom-state"]["prompt-history"],
            )
            self.assertEqual(
                [{"thread": "official-target-queued"}],
                official_state["queued-follow-ups"],
            )
            self.assertEqual(
                "official-target-remote-host",
                official_state["selected-remote-host-id"],
            )

    def test_internal_switch_syncs_rules_support_and_ignores_unknown_pets(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            pets = live_home / "pets"
            pets.mkdir()
            (pets / "settings.json").write_text('{"enabled":true}\n')
            rules = live_home / "rules"
            rules.mkdir()
            (rules / "settings.json").write_text('{"enabled":true}\n')
            plugins = live_home / "plugins"
            plugins.mkdir()
            (plugins / "cache-marker").write_text("do not sync\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_switcher(
                root,
                "switch",
                "internal",
                "--skip-launchctl",
                "--skip-app-cli",
                "--skip-shim",
            )

            internal_home = root / "store" / "homes" / "internal"
            self.assertFalse((internal_home / "pets").exists())
            self.assertTrue((internal_home / "rules").is_symlink())
            self.assertEqual(
                '{"enabled":true}\n',
                (internal_home / "rules" / "settings.json").read_text(),
            )
            self.assertFalse((internal_home / "plugins" / "cache-marker").exists())

    def test_official_switch_excludes_bulky_support_state_from_sync_plan(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            for name in (
                "agent-kb",
                "plugins",
                "computer-use",
                "cache",
                "model-catalogs",
                "sqlite",
            ):
                directory = live_home / name
                directory.mkdir()
                (directory / "payload.txt").write_text(f"{name}\n")
            for name in (
                ".credentials.json",
                ".codex-global-state.json",
                "models_cache.json",
                "version.json",
            ):
                (live_home / name).write_text(f"{name}\n")
            (live_home / "rules").mkdir()
            (live_home / "rules" / "tool.json").write_text("{}\n")

            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            dry_run = self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--internal-codex-home",
                str(live_home),
                "--dry-run",
            )
            dry_output = dry_run.stdout + dry_run.stderr
            self.assertIn(
                str(root / "store" / "homes" / "openai-official" / "rules"),
                dry_output,
            )
            for name in (
                "agent-kb",
                "plugins",
                "computer-use",
                "cache",
                "model-catalogs",
                "sqlite",
                ".credentials.json",
                "models_cache.json",
                "version.json",
            ):
                self.assertNotIn(
                    str(root / "store" / "homes" / "openai-official" / name),
                    dry_output,
                )
            self.assertNotIn(
                str(
                    root
                    / "store"
                    / "homes"
                    / "openai-official"
                    / ".codex-global-state.json"
                ),
                dry_output,
            )

            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--internal-codex-home",
                str(live_home),
                "--skip-launchctl",
                "--skip-app-cli",
                "--skip-shim",
            )

            official_home = root / "store" / "homes" / "openai-official"
            self.assertTrue((official_home / "rules" / "tool.json").exists())
            for name in (
                "agent-kb",
                "plugins",
                "computer-use",
                "cache",
                "model-catalogs",
                "sqlite",
                ".credentials.json",
                ".codex-global-state.json",
                "models_cache.json",
                "version.json",
            ):
                self.assertFalse((official_home / name).exists(), name)
            active = json.loads((root / "store" / "active.json").read_text())
            backup = json.loads(
                (
                    root / "store" / "backups" / active["backup_id"] / "backup.json"
                ).read_text()
            )
            backup_paths = {entry["path"] for entry in backup["entries"]}
            for name in (
                "agent-kb",
                "plugins",
                "computer-use",
                "cache",
                "model-catalogs",
                "sqlite",
                ".credentials.json",
                "models_cache.json",
                "version.json",
            ):
                self.assertNotIn(str(official_home / name), backup_paths)
            self.assertNotIn(
                str(official_home / ".codex-global-state.json"),
                backup_paths,
            )

    def test_internal_switch_prefers_last_runtime_config_and_refreshes_canonical(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            canonical = root / "store" / "profiles" / "internal" / "config.toml"
            canonical.write_text('model = "canonical-internal"\n')
            internal_home = root / "store" / "homes" / "internal"
            internal_home.mkdir(parents=True)
            (internal_home / "config.toml").write_text(
                'model = "runtime-internal"\n'
                'model_provider = "runtime-provider"\n'
                "\n"
                "[model_providers.runtime-provider]\n"
                'name = "Runtime Provider"\n'
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            runtime_config = (internal_home / "config.toml").read_text()
            self.assertIn("# codex-switch: managed runtime config for profile internal", runtime_config)
            self.assertIn("# codex-switch: shared settings are merged from", runtime_config)
            self.assertIn("\n# codex-switch: profile-specific settings\n", runtime_config)
            self.assertIn("\n# codex-switch: shared settings\n", runtime_config)
            self.assertIn('model = "runtime-internal"', runtime_config)
            self.assertIn('model_provider = "runtime-provider"', runtime_config)
            self.assertIn("[model_providers.runtime-provider]", runtime_config)
            self.assertIn("[features]", runtime_config)
            canonical_config = canonical.read_text()
            self.assertIn("# codex-switch: canonical fallback config for profile internal", canonical_config)
            self.assertIn('model = "runtime-internal"', canonical_config)
            self.assertIn('model_provider = "runtime-provider"', canonical_config)
            self.assertNotIn("[features]", canonical_config)

    def test_internal_switch_falls_back_to_canonical_when_last_runtime_config_is_invalid(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            canonical = root / "store" / "profiles" / "internal" / "config.toml"
            canonical.write_text('model = "canonical-internal"\n')
            internal_home = root / "store" / "homes" / "internal"
            internal_home.mkdir(parents=True)
            (internal_home / "config.toml").write_text("model = [\n")

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            runtime_config = (internal_home / "config.toml").read_text()
            self.assertIn('model = "canonical-internal"', runtime_config)
            self.assertIn("[features]", runtime_config)
            self.assertIn("# codex-switch: profile-specific settings are preserved from fallback", runtime_config)

    def test_internal_switch_falls_back_when_runtime_reasoning_effort_is_unsupported(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            catalog = root / "azure-models.json"
            catalog.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.5-2026-04-24",
                                "default_reasoning_level": "xhigh",
                                "supported_reasoning_levels": [
                                    {"effort": "low"},
                                    {"effort": "medium"},
                                    {"effort": "high"},
                                    {"effort": "xhigh"},
                                ],
                            }
                        ]
                    }
                )
                + "\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            canonical = root / "store" / "profiles" / "internal" / "config.toml"
            canonical.write_text(
                'model = "gpt-5.5-2026-04-24"\n'
                f'model_catalog_json = "{catalog}"\n'
                'model_reasoning_effort = "xhigh"\n'
            )
            internal_home = root / "store" / "homes" / "internal"
            internal_home.mkdir(parents=True)
            (internal_home / "config.toml").write_text(
                'model = "gpt-5.5-2026-04-24"\n'
                f'model_catalog_json = "{catalog}"\n'
                'model_reasoning_effort = "max"\n'
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            runtime_config = (internal_home / "config.toml").read_text()
            canonical_config = canonical.read_text()
            self.assertIn('model_reasoning_effort = "xhigh"', runtime_config)
            self.assertIn('model_reasoning_effort = "xhigh"', canonical_config)
            self.assertNotIn('model_reasoning_effort = "max"', runtime_config)
            self.assertNotIn('model_reasoning_effort = "max"', canonical_config)
            self.assertIn("# codex-switch: profile-specific settings are preserved from fallback", runtime_config)

    def test_desktop_app_proxy_masks_versioned_model_alias_without_max_effort(self) -> None:
        actual_model = "gpt-5.5-2026-04-24"
        desktop_model = "gpt-5.5"
        message = {
            "id": 2,
            "result": {
                "data": [
                    {
                        "id": actual_model,
                        "model": actual_model,
                        "displayName": "Azure / GPT-5.5 2026-04-24",
                        "hidden": False,
                        "isDefault": True,
                        "defaultReasoningEffort": "xhigh",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "low effort"},
                            {"reasoningEffort": "medium", "description": "medium effort"},
                            {"reasoningEffort": "high", "description": "high effort"},
                            {"reasoningEffort": "xhigh", "description": "xhigh effort"},
                        ],
                    }
                ]
            },
        }

        masked = mask_backend_message_for_desktop(
            message,
            method="model/list",
            actual_model=actual_model,
            desktop_model=desktop_model,
        )

        [model] = masked["result"]["data"]
        self.assertEqual(model["id"], desktop_model)
        self.assertEqual(model["model"], desktop_model)
        self.assertEqual(model["defaultReasoningEffort"], "xhigh")
        self.assertEqual(
            [effort["reasoningEffort"] for effort in model["supportedReasoningEfforts"]],
            ["low", "medium", "high", "xhigh"],
        )

    def test_desktop_app_proxy_masks_thread_model_fields_for_reasoning_lookup(self) -> None:
        actual_model = "gpt-5.5-2026-04-24"
        desktop_model = "gpt-5.5"
        message = {
            "id": 4,
            "result": {
                "conversation": {
                    "model": actual_model,
                    "latestModel": actual_model,
                    "previousTurnModel": actual_model,
                    "settings": {
                        "model": actual_model,
                        "reasoning_effort": "xhigh",
                    },
                },
                "writes": [
                    {"key": "model", "value": actual_model},
                ],
            },
        }

        masked = mask_backend_message_for_desktop(
            message,
            method="thread/load",
            actual_model=actual_model,
            desktop_model=desktop_model,
        )

        conversation = masked["result"]["conversation"]
        self.assertEqual(conversation["model"], desktop_model)
        self.assertEqual(conversation["latestModel"], desktop_model)
        self.assertEqual(conversation["previousTurnModel"], desktop_model)
        self.assertEqual(conversation["settings"]["model"], desktop_model)
        self.assertEqual(masked["result"]["writes"][0]["value"], actual_model)
        self.assertEqual(message["result"]["conversation"]["latestModel"], actual_model)

    def test_desktop_app_proxy_translates_desktop_model_alias_for_backend(self) -> None:
        actual_model = "gpt-5.5-2026-04-24"
        desktop_model = "gpt-5.5"
        message = {
            "id": 9,
            "method": "thread/start",
            "params": {
                "model": desktop_model,
                "threadSettings": {
                    "model": desktop_model,
                    "reasoning_effort": "xhigh",
                },
                "writes": [
                    {"key": "model", "value": desktop_model},
                    {"key": "model_reasoning_effort", "value": "xhigh"},
                ],
            },
        }

        translated = translate_desktop_message_for_backend(
            message,
            actual_model=actual_model,
            desktop_model=desktop_model,
        )

        self.assertEqual(translated["params"]["model"], actual_model)
        self.assertEqual(translated["params"]["threadSettings"]["model"], desktop_model)
        self.assertEqual(translated["params"]["writes"][0]["value"], desktop_model)
        self.assertEqual(message["params"]["model"], desktop_model)

    def test_desktop_app_proxy_removes_resume_history_item_ids(self) -> None:
        message = {
            "id": 10,
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
                        "type": "message",
                        "id": "019f8dfe-5fb3-7443-9889-6d89991bd9e8",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hook"}],
                    }
                ],
            },
        }

        translated = translate_desktop_message_for_backend(
            message,
            actual_model="gpt-5.5-2026-04-24",
            desktop_model="gpt-5.5",
        )

        self.assertEqual(1, len(translated["params"]["history"]))
        self.assertNotIn("id", translated["params"]["history"][0])
        self.assertEqual("message", translated["params"]["history"][0]["type"])
        self.assertEqual(2, len(message["params"]["history"]))
        self.assertEqual(
            message["params"]["history"][1]["id"],
            "019f8dfe-5fb3-7443-9889-6d89991bd9e8",
        )

    def test_desktop_app_proxy_preserves_unchanged_client_jsonl_line(self) -> None:
        adapter = ProtocolAdapter(
            actual_model="gpt-5.5-2026-04-24",
            desktop_model="gpt-5.5",
            capabilities=BackendCapabilities(True, True, None),
        )
        tracker = PendingRequestTracker()
        line = (
            ' { "id": 21, "method": "custom/tool", '
            '"params": { "model": "gpt-5.5" } }\n'
        )

        output, message = adapt_client_json_line(line, adapter, tracker)

        self.assertEqual(line, output)
        self.assertEqual("custom/tool", message["method"])

        crlf_line = line.replace("\n", "\r\n").encode()
        crlf_output, crlf_message = adapt_client_json_line(
            crlf_line,
            adapter,
            tracker,
        )
        self.assertEqual(crlf_line, crlf_output)
        self.assertEqual("custom/tool", crlf_message["method"])

    def test_desktop_app_proxy_jsonl_tracker_is_direction_aware(self) -> None:
        adapter = ProtocolAdapter(
            actual_model="gpt-5.5-2026-04-24",
            desktop_model="gpt-5.5",
            capabilities=BackendCapabilities(True, True, None),
        )
        tracker = PendingRequestTracker()
        request = (
            '{"id":"same","method":"thread/load",'
            '"params":{"metadata":{"model":"gpt-5.5"}}}\n'
        )
        server_request = (
            '{"id":"same","method":"server/request",'
            '"params":{"model":"gpt-5.5-2026-04-24"}}\n'
        )
        response = (
            '{"id":"same","result":{"conversation":'
            '{"model":"gpt-5.5-2026-04-24"},'
            '"writes":[{"key":"model","value":"gpt-5.5-2026-04-24"}]}}\n'
        )

        client_output, _ = adapt_client_json_line(request, adapter, tracker)
        server_output, _, server_method = adapt_backend_json_line(
            server_request,
            adapter,
            tracker,
        )
        response_output, _, response_method = adapt_backend_json_line(
            response,
            adapter,
            tracker,
        )

        self.assertEqual(request, client_output)
        self.assertEqual(server_request, server_output)
        self.assertIsNone(server_method)
        self.assertEqual("thread/load", response_method)
        parsed = json.loads(response_output)
        self.assertEqual(
            "gpt-5.5",
            parsed["result"]["conversation"]["model"],
        )
        self.assertEqual(
            "gpt-5.5-2026-04-24",
            parsed["result"]["writes"][0]["value"],
        )

    def test_desktop_app_proxy_flattens_namespace_dynamic_tools_for_older_backend(self) -> None:
        actual_model = "gpt-5.5-2026-04-24"
        desktop_model = "gpt-5.5"
        message = {
            "id": 10,
            "method": "thread/start",
            "params": {
                "model": desktop_model,
                "dynamicTools": [
                    {
                        "type": "namespace",
                        "name": "tool_search",
                        "description": "Search available tools.",
                        "tools": [
                            {
                                "type": "function",
                                "name": "search",
                                "description": "Search tool catalog.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                },
                                "deferLoading": True,
                            }
                        ],
                    },
                    {
                        "type": "function",
                        "name": "read_file",
                        "description": "Read a file.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                ],
            },
        }

        translated = translate_desktop_message_for_backend(
            message,
            actual_model=actual_model,
            desktop_model=desktop_model,
        )

        self.assertEqual(
            translated["params"]["dynamicTools"],
            [
                {
                    "namespace": "tool_search",
                    "type": "function",
                    "name": "search",
                    "description": "Search tool catalog.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                    "deferLoading": True,
                },
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read a file.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            ],
        )
        self.assertEqual(translated["params"]["model"], actual_model)
        self.assertEqual(message["params"]["dynamicTools"][0]["type"], "namespace")

    def test_desktop_app_proxy_preserves_canonical_dynamic_tools_for_namespace_backend(self) -> None:
        actual_model = "gpt-5.5-2026-04-24"
        desktop_model = "gpt-5.5"
        dynamic_tools = [
            {
                "type": "namespace",
                "name": "tool_search",
                "description": "Search available tools.",
                "tools": [
                    {
                        "type": "function",
                        "name": "search",
                        "description": "Search tool catalog.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                        "deferLoading": True,
                    }
                ],
            },
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        ]
        message = {
            "id": 12,
            "method": "thread/start",
            "params": {
                "model": desktop_model,
                "dynamicTools": dynamic_tools,
            },
        }

        translated = translate_desktop_message_for_backend(
            message,
            actual_model=actual_model,
            desktop_model=desktop_model,
            supports_canonical_dynamic_tools=True,
        )

        self.assertEqual(translated["params"]["dynamicTools"], dynamic_tools)
        self.assertEqual(translated["params"]["model"], actual_model)
        self.assertEqual(message["params"]["dynamicTools"][0]["type"], "namespace")

    def test_desktop_app_proxy_detects_namespace_dynamic_tool_support_from_backend_version(self) -> None:
        self.assertFalse(codex_version_supports_canonical_dynamic_tools("codex-cli 0.140.0"))
        self.assertTrue(codex_version_supports_canonical_dynamic_tools("codex-cli 0.141.0"))
        self.assertTrue(
            codex_version_supports_canonical_dynamic_tools("codex-cli 0.142.0-alpha.6")
        )
        self.assertFalse(codex_version_supports_canonical_dynamic_tools("not a version"))
        self.assertEqual(
            protocol_capabilities_for_version("codex-cli 0.140.0"),
            BackendCapabilities(False, False, None),
        )
        self.assertEqual(
            protocol_capabilities_for_version("codex-cli 0.144.6"),
            BackendCapabilities(True, None, None),
        )
        self.assertEqual(
            protocol_capabilities_for_version("not a version"),
            BackendCapabilities(None, None, None),
        )

    def test_desktop_app_proxy_filters_unsupported_plugin_marketplace_kind(self) -> None:
        actual_model = "gpt-5.5-2026-04-24"
        desktop_model = "gpt-5.5"
        message = {
            "id": 11,
            "method": "plugin/list",
            "params": {
                "marketplaceKinds": [
                    "local",
                    "created-by-me-remote",
                    "shared-with-me",
                ],
                "cwds": ["/Users/cY/dev/codex-switch"],
            },
        }

        translated = translate_desktop_message_for_backend(
            message,
            actual_model=actual_model,
            desktop_model=desktop_model,
        )

        self.assertEqual(
            translated["params"]["marketplaceKinds"],
            ["local", "shared-with-me"],
        )
        self.assertEqual(
            message["params"]["marketplaceKinds"],
            ["local", "created-by-me-remote", "shared-with-me"],
        )

    def test_missing_shared_config_defaults_preserve_new_desktop_value(self) -> None:
        defaults = (
            'model = "gpt-5.5-2026-04-24"\n'
            'model_provider = "azure"\n'
            "\n"
            "[model_providers.azure]\n"
            'name = "Azure"\n'
            "\n"
            "[desktop]\n"
            'appearanceTheme = "dark"\n'
            'followUpQueueMode = "off"\n'
            "\n"
            "[desktop.appearanceDarkChromeTheme]\n"
            'background = "#101010"\n'
            "\n"
            "[memories]\n"
            "enabled = true\n"
            "\n"
            "[apps.connector_test]\n"
            'command = "connector-test"\n'
            "\n"
            "[marketplaces.cy-codex-skills]\n"
            'source_type = "github"\n'
            'source = "cy/codex-skills"\n'
            "\n"
            '[plugins."agent-kb@cy-codex-skills"]\n'
            "enabled = true\n"
            "\n"
            "[[skills.config]]\n"
            'path = "/Users/me/.codex/skills/agent-kb/SKILL.md"\n'
            "\n"
            '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]\n'
            "enabled = true\n"
        )
        updated = (
            "[desktop]\n"
            'followUpQueueMode = "queue"\n'
            "\n"
            '[plugins."github@openai-curated"]\n'
            "enabled = true\n"
        )

        merged = merge_missing_shared_config_defaults(updated, defaults)

        self.assertIn('followUpQueueMode = "queue"', merged)
        self.assertIn('appearanceTheme = "dark"', merged)
        self.assertNotIn('followUpQueueMode = "off"', merged)
        self.assertIn("[desktop.appearanceDarkChromeTheme]", merged)
        self.assertIn("[memories]", merged)
        self.assertIn("[apps.connector_test]", merged)
        self.assertIn("[marketplaces.cy-codex-skills]", merged)
        self.assertIn('[plugins."agent-kb@cy-codex-skills"]', merged)
        self.assertIn('[plugins."github@openai-curated"]', merged)
        self.assertIn("[[skills.config]]", merged)
        self.assertIn(
            '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]',
            merged,
        )
        self.assertNotIn('model_provider = "azure"', merged)
        self.assertNotIn("[model_providers.azure]", merged)

    def test_canonical_refresh_does_not_resurrect_removed_profile_settings(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            runtime_config = root / "runtime.toml"
            canonical_config = root / "canonical.toml"
            runtime_config.write_text('model = "runtime-model"\n')
            canonical_config.write_text(
                'cli_auth_credentials_store = "file"\n'
                'model = "old-model"\n'
                'model_provider = "old-provider"\n'
                'personality = "pragmatic"\n'
                "\n"
                "[model_providers.old-provider]\n"
                'name = "Old Provider"\n'
            )

            refresh_profile_canonical_config(
                "openai-official",
                runtime_config,
                canonical_config,
            )

            canonical_text = canonical_config.read_text()
            self.assertIn('cli_auth_credentials_store = "file"', canonical_text)
            self.assertIn('model = "runtime-model"', canonical_text)
            self.assertNotIn("old-provider", canonical_text)
            self.assertNotIn('personality = "pragmatic"', canonical_text)

    def test_official_switch_preserves_last_official_runtime_profile_settings(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'model = "official-runtime"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_home = root / "store" / "homes" / "internal"
            (internal_home / "config.toml").write_text(
                (internal_home / "config.toml").read_text()
                + "\n[mcp_servers.from-internal]\n"
                + 'command = "internal-mcp"\n'
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            official_config = (live_home / "config.toml").read_text()
            self.assertIn('model = "official-runtime"', official_config)
            self.assertIn("[mcp_servers.from-internal]", official_config)
            self.assertIn("# codex-switch: managed runtime config for profile openai-official", official_config)
            self.assertIn("\n# codex-switch: profile-specific settings\n", official_config)
            self.assertIn("\n# codex-switch: shared settings\n", official_config)
            canonical_config = (
                root / "store" / "profiles" / "openai-official" / "config.toml"
            ).read_text()
            self.assertIn("# codex-switch: canonical fallback config for profile openai-official", canonical_config)
            self.assertIn('model = "official-runtime"', canonical_config)
            self.assertNotIn("[mcp_servers.from-internal]", canonical_config)

    def test_official_switch_repairs_contaminated_managed_runtime_profile_seed(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            (live_home / "auth.json").write_text('{"official":"auth"}\n')
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Internal Azure"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_home = root / "store" / "homes" / "internal"
            (internal_home / "config.toml").write_text(
                (internal_home / "config.toml").read_text()
                + "\n[mcp_servers.from-internal]\n"
                + 'command = "internal-mcp"\n'
            )
            contaminated = (
                "# codex-switch: managed runtime config for profile openai-official\n"
                "# codex-switch: shared settings are merged from "
                f"{internal_home / 'config.toml'}\n"
                "# codex-switch: profile-specific settings are preserved from last runtime config\n"
                "\n"
                "# codex-switch: profile-specific settings\n"
                'model = "internal-model"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Internal Azure"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            (live_home / "config.toml").write_text(contaminated)
            (root / "store" / "profiles" / "openai-official" / "config.toml").write_text(
                "# codex-switch: canonical fallback config for profile openai-official\n"
                "\n"
                'cli_auth_credentials_store = "file"\n'
                'model = "internal-model"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Internal Azure"\n'
            )
            (live_home / "openai-official.config.toml").write_text(
                'cli_auth_credentials_store = "file"\n'
                "\n"
                '[plugins."figma@openai-curated"]\n'
                "enabled = true\n"
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            official_config = (live_home / "config.toml").read_text()
            self.assertIn("# codex-switch: managed runtime config for profile openai-official", official_config)
            self.assertIn("[mcp_servers.from-internal]", official_config)
            self.assertNotIn('[plugins."figma@openai-curated"]', official_config)
            self.assertNotIn('model = "internal-model"', official_config)
            self.assertNotIn('model_provider = "azure"', official_config)
            self.assertNotIn("[model_providers.azure]", official_config)
            canonical_config = (
                root / "store" / "profiles" / "openai-official" / "config.toml"
            ).read_text()
            self.assertIn('cli_auth_credentials_store = "file"', canonical_config)
            self.assertNotIn('model = "internal-model"', canonical_config)
            self.assertNotIn("[plugins.", canonical_config)
            home_profile_layer = (live_home / "openai-official.config.toml").read_text()
            self.assertNotIn('model = "internal-model"', home_profile_layer)
            self.assertNotIn("[plugins.", home_profile_layer)

    def test_official_switch_ignores_unannotated_internal_runtime_seed(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'model = "official-model"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Internal Azure"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_home = root / "store" / "homes" / "internal"
            (internal_home / "config.toml").write_text(
                (internal_home / "config.toml").read_text()
                + "\n[mcp_servers.from-internal]\n"
                + 'command = "internal-mcp"\n'
            )
            (live_home / "config.toml").write_text(
                'model = "internal-model"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Internal Azure"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            (live_home / "openai-official.config.toml").write_text(
                'model = "official-model"\n'
                "\n"
                '[plugins."figma@openai-curated"]\n'
                "enabled = true\n"
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            official_config = (live_home / "config.toml").read_text()
            self.assertIn('model = "official-model"', official_config)
            self.assertIn("[mcp_servers.from-internal]", official_config)
            self.assertNotIn('[plugins."figma@openai-curated"]', official_config)
            self.assertNotIn('model = "internal-model"', official_config)
            self.assertNotIn('model_provider = "azure"', official_config)
            self.assertNotIn("[model_providers.azure]", official_config)
            canonical_config = (
                root / "store" / "profiles" / "openai-official" / "config.toml"
            ).read_text()
            self.assertIn('model = "official-model"', canonical_config)
            self.assertNotIn('model = "internal-model"', canonical_config)
            self.assertNotIn("[model_providers.azure]", canonical_config)

    def test_official_switch_ignores_unannotated_provider_runtime_when_explicit_layer_is_clean(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'model = "official-model"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Internal Azure"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            (live_home / "config.toml").write_text(
                'model = "workspace-provider-model"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Workspace Azure"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            (live_home / "openai-official.config.toml").write_text(
                'model = "official-model"\n'
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            official_config = (live_home / "config.toml").read_text()
            self.assertIn('model = "official-model"', official_config)
            self.assertNotIn('model = "workspace-provider-model"', official_config)
            self.assertNotIn('model_provider = "azure"', official_config)
            self.assertNotIn("[model_providers.azure]", official_config)
            profile_layer = (live_home / "openai-official.config.toml").read_text()
            self.assertIn('model = "official-model"', profile_layer)
            self.assertNotIn("workspace-provider-model", profile_layer)
            self.assertNotIn("model_provider", profile_layer)

    def test_official_switch_keeps_managed_runtime_model_without_provider(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_home = root / "store" / "homes" / "internal"
            (live_home / "config.toml").write_text(
                "# codex-switch: managed runtime config for profile openai-official\n"
                "# codex-switch: shared settings are merged from "
                f"{internal_home / 'config.toml'}\n"
                "# codex-switch: profile-specific settings are preserved from last runtime config\n"
                "\n"
                "# codex-switch: profile-specific settings\n"
                'model = "official-runtime"\n'
                "\n"
                "# codex-switch: shared settings\n"
                "[features]\n"
                "memory = true\n"
            )
            (live_home / "openai-official.config.toml").write_text(
                'cli_auth_credentials_store = "file"\n'
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            official_config = (live_home / "config.toml").read_text()
            self.assertIn('model = "official-runtime"', official_config)
            canonical_config = (
                root / "store" / "profiles" / "openai-official" / "config.toml"
            ).read_text()
            self.assertIn('model = "official-runtime"', canonical_config)

    def test_internal_switch_uses_legacy_profile_layer_support_not_usage_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            (live_home / "internal.config.toml").write_text(
                'model = "internal-model"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."dev-flow@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/legacy-skill/SKILL.md"\n'
                "enabled = false\n"
                "\n"
                '[hooks.state."dev-flow@cy-codex-skills:hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:test"\n'
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_config = (root / "store" / "homes" / "internal" / "config.toml").read_text()
            self.assertIn('model = "internal-model"', internal_config)
            self.assertIn("[marketplaces.cy-codex-skills]", internal_config)
            self.assertNotIn('[plugins."dev-flow@cy-codex-skills"]', internal_config)
            self.assertNotIn("[[skills.config]]", internal_config)
            self.assertIn(
                '[hooks.state."dev-flow@cy-codex-skills:hooks.json:stop:0:0"]',
                internal_config,
            )
            canonical_config = (
                root / "store" / "profiles" / "internal" / "config.toml"
            ).read_text()
            self.assertIn('model = "internal-model"', canonical_config)
            self.assertNotIn("[marketplaces.cy-codex-skills]", canonical_config)
            self.assertNotIn('[plugins."dev-flow@cy-codex-skills"]', canonical_config)

    def test_internal_home_config_uses_parity_projection_without_mutating_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            official_home = root / "homes" / "openai-official"
            internal_home = root / "homes" / "internal"
            profile_config = root / "profiles" / "internal" / "config.toml"
            shared_config = official_home / "config.toml"
            target_runtime_config = internal_home / "config.toml"
            overlay_path = (
                profile_config.parent / "parity" / "model-catalog.json"
            )
            profile_config.parent.mkdir(parents=True)
            official_home.mkdir(parents=True)
            internal_home.mkdir(parents=True)
            profile_source = (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "azure"\n'
                'cli_auth_credentials_store = "file"\n'
                'model_reasoning_effort = "high"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Azure"\n'
                'wire_api = "responses"\n'
            ).encode()
            shared_source = (
                "[features]\n"
                "memory = true\n"
                "\n"
                "[agents]\n"
                "max_threads = 6\n"
                "max_depth = 3\n"
                'role = "explorer"\n'
                "\n"
                "[tui]\n"
                'theme = "dark"\n'
            ).encode()
            projected_profile = (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "azure"\n'
                'cli_auth_credentials_store = "file"\n'
                'model_reasoning_effort = "high"\n'
                f"model_catalog_json = {json.dumps(str(overlay_path))}\n"
                "\n"
                "[model_providers.azure]\n"
                'name = "Azure"\n'
                'wire_api = "responses"\n'
                "\n"
                "[features]\n"
                "multi_agent_v2 = true\n"
            ).encode()
            projected_shared = (
                "[features]\n"
                "memory = true\n"
                "\n"
                "[agents]\n"
                "max_depth = 3\n"
                'role = "explorer"\n'
                "\n"
                "[tui]\n"
                'theme = "dark"\n'
            ).encode()
            profile_config.write_bytes(profile_source)
            shared_config.write_bytes(shared_source)
            projection = self.parity_config_projection(
                profile_config=profile_config,
                profile_source=profile_source,
                projected_profile=projected_profile,
                shared_config=shared_config,
                shared_source=shared_source,
                projected_shared=projected_shared,
                overlay_path=overlay_path,
            )
            parser = self.tomllib_parser_for_config_test()

            with (
                mock.patch("codex_switch_config_document.tomllib", parser),
                mock.patch("codex_switch_toml_validate.tomllib", parser),
            ):
                rendered = self.build_internal_home_config_with_projection(
                    official_home,
                    "internal",
                    target_runtime_config,
                    profile_config,
                    projection,
                )
                from codex_switch_config_document import ConfigDocument

                document = ConfigDocument.parse(
                    rendered,
                    "projected internal managed-home config",
                )

            self.assertEqual(
                str(overlay_path),
                document.data.get("model_catalog_json"),
            )
            self.assertIs(
                document.data.get("features", {}).get("multi_agent_v2"),
                True,
            )
            self.assertIs(document.data["features"]["memory"], True)
            self.assertNotIn("max_threads", document.data["agents"])
            self.assertEqual(3, document.data["agents"]["max_depth"])
            self.assertEqual("explorer", document.data["agents"]["role"])
            self.assertEqual("dark", document.data["tui"]["theme"])
            self.assertEqual("gpt-5.6-sol", document.data["model"])
            self.assertEqual("azure", document.data["model_provider"])
            self.assertEqual("high", document.data["model_reasoning_effort"])
            self.assertEqual(
                "responses",
                document.data["model_providers"]["azure"]["wire_api"],
            )
            self.assertEqual(profile_source, profile_config.read_bytes())
            self.assertEqual(shared_source, shared_config.read_bytes())
            self.assertFalse(target_runtime_config.exists())

    def test_materialized_internal_home_projection_is_staged_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            official_home = root / "homes" / "openai-official"
            internal_home = root / "homes" / "internal"
            profile_config = root / "profiles" / "internal" / "config.toml"
            shared_config = official_home / "config.toml"
            target_runtime_config = internal_home / "config.toml"
            overlay_path = (
                profile_config.parent / "parity" / "model-catalog.json"
            )
            profile_config.parent.mkdir(parents=True)
            official_home.mkdir(parents=True)
            internal_home.mkdir(parents=True)
            profile_source = (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "azure"\n'
            ).encode()
            shared_source = (
                "[features]\n"
                "memory = true\n"
                "\n"
                "[agents]\n"
                "max_threads = 6\n"
                "max_depth = 3\n"
            ).encode()
            projected_profile = (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "azure"\n'
                f"model_catalog_json = {json.dumps(str(overlay_path))}\n"
                "\n"
                "[features]\n"
                "multi_agent_v2 = true\n"
            ).encode()
            projected_shared = (
                "[features]\n"
                "memory = true\n"
                "\n"
                "[agents]\n"
                "max_depth = 3\n"
            ).encode()
            stale_runtime = (
                "# codex-switch: managed runtime config for profile internal\n"
                'model = "stale-model"\n'
                "\n"
                "[features]\n"
                "memory = false\n"
                "\n"
                "[agents]\n"
                "max_threads = 12\n"
                "max_depth = 1\n"
            ).encode()
            profile_config.write_bytes(profile_source)
            shared_config.write_bytes(shared_source)
            target_runtime_config.write_bytes(stale_runtime)
            projection = self.parity_config_projection(
                profile_config=profile_config,
                profile_source=profile_source,
                projected_profile=projected_profile,
                shared_config=shared_config,
                shared_source=shared_source,
                projected_shared=projected_shared,
                overlay_path=overlay_path,
            )
            parser = self.tomllib_parser_for_config_test()

            with (
                mock.patch("codex_switch_config_document.tomllib", parser),
                mock.patch("codex_switch_toml_validate.tomllib", parser),
            ):
                rendered = self.build_internal_home_config_with_projection(
                    official_home,
                    "internal",
                    target_runtime_config,
                    profile_config,
                    projection,
                )
                from codex_switch_config_document import ConfigDocument

                document = ConfigDocument.parse(
                    rendered,
                    "staged internal managed-home config",
                )

            self.assertEqual("gpt-5.6-sol", document.data["model"])
            self.assertEqual(
                str(overlay_path),
                document.data.get("model_catalog_json"),
            )
            self.assertIs(
                document.data.get("features", {}).get("multi_agent_v2"),
                True,
            )
            self.assertIs(document.data["features"]["memory"], True)
            self.assertNotIn("max_threads", document.data["agents"])
            self.assertEqual(3, document.data["agents"]["max_depth"])
            self.assertEqual(stale_runtime, target_runtime_config.read_bytes())
            self.assertEqual(profile_source, profile_config.read_bytes())
            self.assertEqual(shared_source, shared_config.read_bytes())

    def test_internal_switch_recovers_target_support_without_restoring_usage_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'model = "internal-model"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."agent-kb@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/agent-kb/SKILL.md"\n'
                "enabled = true\n"
                "\n"
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:agent-kb"\n'
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_home = root / "store" / "homes" / "internal"
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            (live_home / "config.toml").write_text(
                'model = "gpt-5.5"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_config = (internal_home / "config.toml").read_text()
            self.assertIn("[features]", internal_config)
            self.assertIn("[marketplaces.cy-codex-skills]", internal_config)
            self.assertNotIn('[plugins."agent-kb@cy-codex-skills"]', internal_config)
            self.assertNotIn("[[skills.config]]", internal_config)
            self.assertIn(
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]',
                internal_config,
            )
            canonical_config = (
                root / "store" / "profiles" / "internal" / "config.toml"
            ).read_text()
            self.assertNotIn("[marketplaces.cy-codex-skills]", canonical_config)
            self.assertNotIn('[plugins."agent-kb@cy-codex-skills"]', canonical_config)

    def test_internal_switch_recovers_snapshot_support_without_restoring_usage_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'model = "internal-model"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."lark-feishu-ops@cy-codex-skills"]\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_home = root / "store" / "homes" / "internal"
            home_snapshot = internal_home / "internal.plugin-support.config.toml"
            store_snapshot = (
                root
                / "store"
                / "profiles"
                / "internal"
                / "internal.plugin-support.config.toml"
            )
            for snapshot in (home_snapshot, store_snapshot):
                snapshot_text = snapshot.read_text()
                self.assertIn("[marketplaces.cy-codex-skills]", snapshot_text)
                self.assertIn('[plugins."lark-feishu-ops@cy-codex-skills"]', snapshot_text)

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")
            (live_home / "config.toml").write_text('model = "gpt-5.5"\n')
            (internal_home / "config.toml").write_text(
                "# codex-switch: managed runtime config for profile internal\n"
                "\n"
                'model = "internal-model"\n'
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_config = (internal_home / "config.toml").read_text()
            self.assertIn("[marketplaces.cy-codex-skills]", internal_config)
            self.assertNotIn('[plugins."lark-feishu-ops@cy-codex-skills"]', internal_config)
            canonical_config = (
                root / "store" / "profiles" / "internal" / "config.toml"
            ).read_text()
            self.assertNotIn("[marketplaces.cy-codex-skills]", canonical_config)
            self.assertNotIn('[plugins."lark-feishu-ops@cy-codex-skills"]', canonical_config)

    def test_internal_switch_does_not_restore_usage_from_source_profile_snapshot(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'model = "gpt-5.5"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            source_snapshot = (
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."agent-kb@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                '[plugins."lark-feishu-ops@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/agent-kb/SKILL.md"\n'
                "enabled = true\n"
            )
            (live_home / "openai-official.plugin-support.config.toml").write_text(
                source_snapshot
            )
            (
                root
                / "store"
                / "profiles"
                / "openai-official"
                / "openai-official.plugin-support.config.toml"
            ).write_text(source_snapshot)

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_config = (
                root / "store" / "homes" / "internal" / "config.toml"
            ).read_text()
            self.assertIn("[features]", internal_config)
            self.assertIn("[marketplaces.cy-codex-skills]", internal_config)
            self.assertNotIn('[plugins."agent-kb@cy-codex-skills"]', internal_config)
            self.assertNotIn('[plugins."lark-feishu-ops@cy-codex-skills"]', internal_config)
            self.assertNotIn("[[skills.config]]", internal_config)

    def test_switch_propagates_plugin_and_skill_removal_both_directions(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_config_path = live_home / "config.toml"
            live_config_path.write_text(
                'model = "official-model"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."agent-kb@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/agent-kb/SKILL.md"\n'
                "enabled = true\n"
                "\n"
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:agent-kb"\n'
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_config_path = (
                root / "store" / "homes" / "internal" / "config.toml"
            )
            self.assertIn(
                '[plugins."agent-kb@cy-codex-skills"]',
                internal_config_path.read_text(),
            )

            internal_config_path.write_text(
                'model = "internal-model"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:agent-kb"\n'
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            official_config = live_config_path.read_text()
            self.assertIn("[marketplaces.cy-codex-skills]", official_config)
            self.assertIn(
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]',
                official_config,
            )
            self.assertNotIn('[plugins."agent-kb@cy-codex-skills"]', official_config)
            self.assertNotIn("[[skills.config]]", official_config)

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_config = internal_config_path.read_text()
            self.assertIn("[marketplaces.cy-codex-skills]", internal_config)
            self.assertIn(
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]',
                internal_config,
            )
            self.assertNotIn('[plugins."agent-kb@cy-codex-skills"]', internal_config)
            self.assertNotIn("[[skills.config]]", internal_config)

    def test_switch_preserves_disabled_skill_once_by_path(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_home.joinpath("config.toml").write_text(
                'model = "official-model"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/shared-skill/SKILL.md"\n'
                "enabled = false\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "profiles" / "internal" / "config.toml").write_text(
                'model = "internal-model"\n'
            )
            internal_home = root / "store" / "homes" / "internal"
            internal_home.mkdir(parents=True, exist_ok=True)
            stale_usage = (
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/shared-skill/SKILL.md"\n'
                "enabled = true\n"
            )
            (internal_home / "config.toml").write_text(
                'model = "internal-model"\n\n' + stale_usage
            )
            (internal_home / "internal.plugin-support.config.toml").write_text(
                stale_usage
            )

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            internal_config = (internal_home / "config.toml").read_text()
            self.assertEqual(
                1,
                internal_config.count('path = "/tmp/shared-skill/SKILL.md"'),
            )
            self.assertIn("enabled = false", internal_config)
            self.assertNotIn("enabled = true", internal_config)

    def test_official_switch_does_not_narrow_existing_non_usage_shared_settings(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_config_path = live_home / "config.toml"
            live_config_path.write_text(
                'model = "official-runtime"\n'
                "\n"
                "[desktop]\n"
                "preventSleepWhileRunning = true\n"
                'appearanceTheme = "dark"\n'
                'followUpQueueMode = "off"\n'
                "\n"
                "[memories]\n"
                "enabled = true\n"
                "\n"
                "[apps.connector_test]\n"
                'command = "connector-test"\n'
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/official-skill/SKILL.md"\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            internal_home = root / "store" / "homes" / "internal"
            (internal_home / "config.toml").write_text(
                "# codex-switch: managed runtime config for profile internal\n"
                "\n"
                'model = "internal-runtime"\n'
                "\n"
                "[desktop]\n"
                'followUpQueueMode = "queue"\n'
            )
            live_config_path.write_text(
                'model = "official-runtime"\n'
                "\n"
                "[desktop]\n"
                "preventSleepWhileRunning = true\n"
                'appearanceTheme = "dark"\n'
                'followUpQueueMode = "off"\n'
                "\n"
                "[memories]\n"
                "enabled = true\n"
                "\n"
                "[apps.connector_test]\n"
                'command = "connector-test"\n'
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/official-skill/SKILL.md"\n'
                "enabled = true\n"
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            live_config = live_config_path.read_text()
            self.assertIn('model = "official-runtime"', live_config)
            self.assertIn("preventSleepWhileRunning = true", live_config)
            self.assertIn('appearanceTheme = "dark"', live_config)
            self.assertIn('followUpQueueMode = "queue"', live_config)
            self.assertIn("[memories]", live_config)
            self.assertIn("[apps.connector_test]", live_config)
            self.assertNotIn("[[skills.config]]", live_config)
            self.assertNotIn('model = "internal-runtime"', live_config)

    def test_plugin_support_snapshot_refresh_does_not_restore_removed_usage_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            runtime_config = root / "runtime.toml"
            runtime_config.write_text(
                "[desktop]\n"
                'followUpQueueMode = "queue"\n'
            )
            home_snapshot = root / "home.plugin-support.config.toml"
            profile_snapshot = root / "profile.plugin-support.config.toml"
            home_snapshot.write_text(
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."agent-kb@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/agent-kb/SKILL.md"\n'
                "enabled = true\n"
                "\n"
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:agent-kb"\n'
            )

            refresh_profile_plugin_support_snapshot(
                "internal",
                runtime_config,
                [home_snapshot, profile_snapshot],
            )

            for snapshot_path in (home_snapshot, profile_snapshot):
                snapshot = snapshot_path.read_text()
                self.assertIn("[marketplaces.cy-codex-skills]", snapshot)
                self.assertIn(
                    '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]',
                    snapshot,
                )
                self.assertNotIn('[plugins."agent-kb@cy-codex-skills"]', snapshot)
                self.assertNotIn("[[skills.config]]", snapshot)

    def test_doctor_reports_missing_active_profile_enabled_plugin_cache(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                "[marketplaces.local-test]\n"
                'source_type = "local"\n'
                f'source = "{root / "marketplace"}"\n'
                "\n"
                '[plugins."missing-plugin@local-test"]\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            result = self.run_switcher(root, "doctor", check=False)

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn(
                "active profile openai-official: enabled plugin is not installed in "
                "CODEX_HOME: missing-plugin@local-test",
                output,
            )
            self.assertIn("codex-switch repair-plugins openai-official", output)

    def test_verify_reports_official_provider_contamination(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_home_config = live_home / "config.toml"
            live_home_config.write_text('model = "gpt-5.5"\n')
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")
            live_home_config.write_text(
                'model = "gpt-5.5"\n'
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'base_url = "https://internal.example"\n'
            )

            result = self.run_switcher(root, "verify", "openai-official", check=False)

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Verification found issues:", output)
            self.assertIn(
                "openai-official runtime config contains model_provider",
                output,
            )

    def test_verify_safe_repair_refreshes_missing_plugin_support_snapshot(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_home_config = live_home / "config.toml"
            live_home_config.write_text(
                "[marketplaces.local-test]\n"
                'source_type = "local"\n'
                f'source = "{root / "marketplace"}"\n'
                "\n"
                '[plugins."installed-plugin@local-test"]\n'
                "enabled = false\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")
            home_snapshot = live_home / "openai-official.plugin-support.config.toml"
            profile_snapshot = (
                root
                / "store"
                / "profiles"
                / "openai-official"
                / "openai-official.plugin-support.config.toml"
            )
            if home_snapshot.exists():
                home_snapshot.unlink()
            if profile_snapshot.exists():
                profile_snapshot.unlink()

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                "--repair=safe",
            )

            self.assertIn("Verification passed for openai-official", result.stdout)
            self.assertIn("Refreshed plugin support snapshot", result.stdout)
            self.assertIn("[marketplaces.local-test]", home_snapshot.read_text())
            self.assertIn("[marketplaces.local-test]", profile_snapshot.read_text())

    def test_verify_reports_missing_plugin_support_snapshot_without_repair(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_home_config = live_home / "config.toml"
            live_home_config.write_text(
                "[marketplaces.local-test]\n"
                'source_type = "local"\n'
                f'source = "{root / "marketplace"}"\n'
                "\n"
                '[plugins."installed-plugin@local-test"]\n'
                "enabled = false\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")
            home_snapshot = live_home / "openai-official.plugin-support.config.toml"
            profile_snapshot = (
                root
                / "store"
                / "profiles"
                / "openai-official"
                / "openai-official.plugin-support.config.toml"
            )
            if home_snapshot.exists():
                home_snapshot.unlink()
            if profile_snapshot.exists():
                profile_snapshot.unlink()

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("plugin support snapshot is missing", output)
            self.assertIn(str(home_snapshot), output)
            self.assertIn(str(profile_snapshot), output)

    def test_verify_runtime_smoke_runs_profile_codex_with_target_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_runtime_smoke_codex(official_codex)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                "--runtime-smoke",
            )

            self.assertIn("Runtime smoke: passed", result.stdout)
            log_lines = (root / "live" / "runtime-smoke.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{root / 'live'}|--version",
                    f"{root / 'live'}|plugin list --json",
                ],
                log_lines,
            )

    def test_verify_app_server_smoke_accepts_plugin_auth_before_parity_gate(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(internal_codex)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            verify_log = root / "verify-app-server-smoke.log"
            verify_env = dict(env)
            verify_env["CODEX_SWITCH_TEST_APP_SERVER_LOG"] = str(verify_log)
            result = self.run_switcher(
                root,
                "verify",
                "internal",
                "--app-server-smoke",
                env=verify_env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("App-server smoke: passed", output)
            self.assertIn("parity.receipt.missing", output)
            self.assertNotIn("app-server smoke failed", output)
            log_lines = verify_log.read_text().splitlines()
            self.assertTrue(
                any(
                    line.endswith("|app-server --analytics-default-enabled")
                    for line in log_lines
                )
            )
            self.assertTrue(any('"method":"initialize"' in line for line in log_lines))
            self.assertTrue(any('"method":"plugin/list"' in line for line in log_lines))

    def test_verify_app_server_smoke_reports_early_241_exit(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                exit_241_after_plugin_list=True,
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            result = self.run_switcher(
                root,
                "verify",
                "internal",
                "--app-server-smoke",
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("app-server smoke failed", output)
            self.assertIn("exit 241", output)
            self.assertIn("plugin/list featured plugin fetch failed", output)

    def test_verify_responses_tool_smoke_runs_profile_codex_with_target_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_responses_tool_smoke_codex(official_codex)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                "--responses-tool-smoke",
            )

            self.assertIn("Responses tool smoke: passed", result.stdout)
            log_lines = (root / "live" / "responses-tool-smoke.log").read_text().splitlines()
            self.assertEqual(1, len(log_lines))
            home, args = log_lines[0].split("|", 1)
            self.assertEqual(str(root / "live"), home)
            self.assertIn("exec --json --ephemeral --ignore-rules", args)
            self.assertIn("-c approval_policy=\"never\"", args)
            self.assertIn("-s read-only", args)
            self.assertIn(f"-C {Path.cwd()}", args)
            self.assertIn("codex_switch_responses_tool_smoke", args)

    def test_verify_responses_tool_smoke_reports_azure_resource_mismatch(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_responses_tool_smoke_codex(
                official_codex,
                fail_resource_mismatch=True,
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                "--responses-tool-smoke",
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("internal Responses resource-stickiness failure", output)
            self.assertIn("globalttswedencentral010 -> globalttswedencentral053", output)
            self.assertIn("deployment-gpt-5.5-2026-04-24-platform-global", output)
            self.assertIn("741c1f3e-fad4-48be-abe0-d0c2e99b3506", output)
            self.assertIn("202607031120158DCF6A7C87F2A6AF4908", output)
            self.assertNotIn("should-not-leak", output)

    def test_verify_responses_tool_smoke_reports_missing_reasoning_item(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_responses_tool_smoke_codex(
                official_codex,
                fail_reasoning_item_not_found=True,
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                "--responses-tool-smoke",
                "--report",
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("internal Responses reasoning continuity failure", output)
            self.assertIn("rs_08926f6eb84342d1006a61d1f955e081938793643ece5c1c56", output)
            self.assertNotIn("should-not-leak", output)
            [report_path] = sorted(
                (root / "store" / "verification").glob("*-openai-official.json")
            )
            report = json.loads(report_path.read_text())
            self.assertEqual(
                "responses_reasoning_item_unavailable",
                report["smoke_diagnostics"][0]["kind"],
            )
            self.assertEqual(
                ["rs_08926f6eb84342d1006a61d1f955e081938793643ece5c1c56"],
                report["smoke_diagnostics"][0]["item_ids"],
            )
            self.assertNotIn("should-not-leak", json.dumps(report))

    def test_verify_report_includes_sanitized_responses_tool_smoke_diagnostics(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_responses_tool_smoke_codex(
                official_codex,
                fail_resource_mismatch=True,
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                "--responses-tool-smoke",
                "--report",
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            reports = sorted((root / "store" / "verification").glob("*-openai-official.json"))
            self.assertEqual(1, len(reports))
            report = json.loads(reports[0].read_text())
            self.assertFalse(report["ok"])
            self.assertTrue(report["responses_tool_smoke"])
            self.assertEqual(
                [
                    {
                        "kind": "azure_responses_resource_mismatch",
                        "message": (
                            "Responses context follow-up must stay on the same "
                            "Azure OpenAI resource"
                        ),
                        "accounts": [
                            "globalttswedencentral010",
                            "globalttswedencentral053",
                        ],
                        "deployments": [
                            "deployment-gpt-5.5-2026-04-24-platform-global",
                        ],
                        "model_request_ids": [
                            "b1ce23f9-e838-47c5-a705-afa2564e4409",
                            "741c1f3e-fad4-48be-abe0-d0c2e99b3506",
                        ],
                        "tt_log_ids": [
                            "20260703112009D6B58AAAD12F032ED7AB",
                            "202607031120158DCF6A7C87F2A6AF4908",
                        ],
                    }
                ],
                report["smoke_diagnostics"],
            )
            self.assertNotIn("should-not-leak", json.dumps(report))

    def test_verify_sanitizes_exec_smoke_output_prompt_and_report(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_secret_smoke_codex(official_codex)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--skip-launchctl",
            )

            result = self.run_switcher(
                root,
                "verify",
                "openai-official",
                "--exec-smoke",
                "prompt-secret",
                "--report",
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            [report_path] = sorted(
                (root / "store" / "verification").glob(
                    "*-openai-official.json"
                )
            )
            report_text = report_path.read_text()
            for secret in (
                "prompt-secret",
                "auth-secret",
                "standalone-secret",
                "api-secret",
                "cookie-secret",
                "signed-secret",
                "exception-secret",
            ):
                self.assertNotIn(secret, output)
                self.assertNotIn(secret, report_text)
            self.assertIn("safe-route-123", output)
            self.assertIn("safe-route-123", report_text)

    def test_one_key_switch_forwards_responses_tool_smoke_to_verify(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_responses_tool_smoke_codex(official_codex)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            result = self.run_wrapper(
                root,
                "official",
                "--skip-login",
                "--skip-update-check",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                "--responses-tool-smoke",
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertIn("Responses tool smoke: passed", output)
            self.assertIn("Verify: passed", output)
            self.assertTrue((root / "live" / "responses-tool-smoke.log").exists())

    def test_doctor_accepts_active_profile_enabled_plugin_cache(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                "[marketplaces.local-test]\n"
                'source_type = "local"\n'
                f'source = "{root / "marketplace"}"\n'
                "\n"
                '[plugins."installed-plugin@local-test"]\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")
            installed = (
                root
                / "live"
                / "plugins"
                / "cache"
                / "local-test"
                / "installed-plugin"
                / "1.0.0"
            )
            installed.mkdir(parents=True)
            marker = installed / ".codex-plugin" / "plugin.json"
            marker.parent.mkdir()
            marker.write_text(
                json.dumps(
                    {"name": "installed-plugin", "version": "1.0.0"}
                )
                + "\n"
            )

            result = self.run_switcher(root, "doctor")

            self.assertEqual(0, result.returncode)
            self.assertIn("Doctor passed", result.stdout)

    def test_doctor_accepts_revision_named_enabled_plugin_cache(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            test_python = None
            if sys.version_info < (3, 11):
                test_python = shutil.which("python3.12") or shutil.which("python3.11")
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                "[marketplaces.openai-curated]\n"
                'source_type = "local"\n'
                f'source = "{root / "marketplace"}"\n'
                "\n"
                '[plugins."figma@openai-curated"]\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=test_python,
            )
            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--skip-launchctl",
                python_executable=test_python,
            )
            installed = (
                live_home
                / "plugins"
                / "cache"
                / "openai-curated"
                / "figma"
                / "11c74d6b"
            )
            write_plugin_source(
                installed,
                name="figma",
                version="2.0.13",
                payload="curated snapshot\n",
            )

            result = self.run_switcher(
                root,
                "doctor",
                python_executable=test_python,
            )

            self.assertEqual(0, result.returncode)
            self.assertIn("Doctor passed", result.stdout)

    def test_repair_plugins_installs_missing_profile_plugins(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, env, _ = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog={
                    "installed": [],
                    "available": [
                        {
                            "pluginId": selector,
                            "name": "missing-plugin",
                            "marketplaceName": "local-test",
                            "version": "1.0.0",
                        }
                    ],
                },
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl", env=env)

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)
            doctor = self.run_switcher(root, "doctor", check=False)

            self.assertIn(f"Installing plugin: {selector}", repair.stdout)
            self.assertNotEqual(0, doctor.returncode)
            self.assertIn(
                "parity.receipt.missing",
                doctor.stdout + doctor.stderr,
            )

    def test_plugin_catalog_result_distinguishes_command_and_schema_states(
        self,
    ) -> None:
        selector = "available-plugin@local-test"
        complete = json.dumps(
            {
                "installed": [],
                "available": [
                    {
                        "pluginId": selector,
                        "name": "available-plugin",
                        "marketplaceName": "local-test",
                        "version": "1.0.0",
                    }
                ],
            }
        )
        cases = (
            (
                "verified-empty",
                '{"installed":[],"available":[]}',
                "",
                0,
                "verified",
                frozenset(),
            ),
            (
                "verified-complete",
                complete,
                "",
                0,
                "verified",
                frozenset({selector}),
            ),
            (
                "command-failed",
                complete,
                "network failed",
                17,
                "command_failed",
                frozenset(),
            ),
            ("empty-output", "", "", 0, "empty_output", frozenset()),
            (
                "invalid-json",
                '{"installed":[',
                "",
                0,
                "invalid_json",
                frozenset(),
            ),
            (
                "stderr-warning",
                complete,
                "catalog warning",
                0,
                "stderr_output",
                frozenset(),
            ),
            (
                "unsupported-schema",
                '{"unexpected":[]}',
                "",
                0,
                "unsupported_schema",
                frozenset(),
            ),
        )
        for (
            label,
            stdout,
            stderr,
            returncode,
            expected_status,
            expected_selectors,
        ) in cases:
            with self.subTest(label=label):
                result = available_plugin_catalog(
                    stdout,
                    stderr=stderr,
                    returncode=returncode,
                )
                self.assertEqual(expected_status, result.status)
                self.assertEqual(expected_selectors, frozenset(result.entries))
                self.assertEqual(stderr, result.stderr)
                self.assertEqual(returncode, result.returncode)
                self.assertEqual(
                    expected_status == "verified",
                    result.verified,
                )

    def test_repair_plugins_unverified_catalog_performs_no_plugin_or_config_writes(
        self,
    ) -> None:
        selector = "missing-plugin@local-test"
        cases = (
            (
                "command_failed",
                '{"installed":[],"available":[]}\n',
                "network failed\n",
                17,
            ),
            ("stderr_output", '{"installed":[],"available":[]}\n', "warning\n", 0),
            ("empty_output", "", "", 0),
            ("invalid_json", '{"installed":[', "", 0),
            ("unsupported_schema", '{"unexpected":[]}\n', "", 0),
        )
        for expected_status, catalog_stdout, catalog_stderr, returncode in cases:
            with self.subTest(expected_status=expected_status):
                temp_dir, root = self.make_workspace()
                with temp_dir:
                    internal_codex, env, home = (
                        self.prepare_internal_plugin_profile(
                            root,
                            config_text=(
                                f'[plugins."{selector}"]\n'
                                "enabled = true\n"
                            ),
                            catalog={"installed": [], "available": []},
                        )
                    )
                    write_fake_plugin_refresh_codex(
                        internal_codex,
                        catalog={"installed": [], "available": []},
                        catalog_stdout=catalog_stdout,
                        catalog_stderr=catalog_stderr,
                        catalog_returncode=returncode,
                    )
                    config_before = {
                        path: path.read_bytes()
                        for path in root.rglob("*.toml")
                    }

                    repair = self.run_wrapper(
                        root,
                        "repair-plugins",
                        "internal",
                        "--disable-unavailable",
                        env=env,
                        check=False,
                    )

                    output = repair.stdout + repair.stderr
                    self.assertNotEqual(0, repair.returncode)
                    self.assertIn(
                        f"plugin catalog is unverified ({expected_status})",
                        output,
                    )
                    calls = (home / "codex-calls.log").read_text()
                    self.assertNotIn(f"plugin add {selector}", calls)
                    self.assertEqual(
                        config_before,
                        {
                            path: path.read_bytes()
                            for path in root.rglob("*.toml")
                        },
                    )

    def test_partial_plugin_caches_are_not_materialized(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            home = root / "home"
            requirement = plugin_requirement("partial-plugin@local-test")
            self.assertIsNotNone(requirement)
            assert requirement is not None
            cache_root = (
                home
                / "plugins"
                / "cache"
                / requirement.marketplace
                / requirement.plugin
            )
            partial_builders = (
                lambda: (cache_root / ".tmp").write_text("partial\n"),
                lambda: (cache_root / ".DS_Store").write_text("partial\n"),
                lambda: (
                    cache_root / "1.0.0" / "payload.txt"
                ).write_text("partial\n"),
                lambda: (
                    cache_root / "1.0.0" / ".codex-plugin"
                ).mkdir(parents=True),
            )
            for index, build_partial in enumerate(partial_builders):
                with self.subTest(index=index):
                    if cache_root.exists():
                        shutil.rmtree(cache_root)
                    cache_root.mkdir(parents=True)
                    if index == 2:
                        (cache_root / "1.0.0").mkdir()
                    build_partial()
                    self.assertFalse(
                        plugin_is_installed(home, requirement),
                        cache_root,
                    )

            valid = cache_root / "1.0.0" / ".codex-plugin" / "plugin.json"
            if cache_root.exists():
                shutil.rmtree(cache_root)
            valid.parent.mkdir(parents=True)
            valid.write_text(
                json.dumps(
                    {"name": requirement.plugin, "version": "1.0.0"}
                )
                + "\n"
            )
            self.assertTrue(plugin_is_installed(home, requirement))

    def test_repair_plugins_dry_run_returns_conditional_plan(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, _, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog={"installed": [], "available": []},
            )
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
                internal_codex_home=home,
            )

            plan = repair_profile_plugins(store, "internal", dry_run=True)

            self.assertFalse(plan.catalog_verified)
            self.assertEqual(
                (selector,),
                tuple(
                    requirement.selector
                    for requirement in plan.conditional_install
                ),
            )
            self.assertEqual((), plan.install)
            self.assertEqual((), plan.disable)
            self.assertEqual((), plan.refresh)

    def test_disable_unavailable_rolls_back_prior_config_writes(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, _, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog={"installed": [], "available": []},
            )
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
                internal_codex_home=home,
            )
            requirement = plugin_requirement(selector)
            self.assertIsNotNone(requirement)
            assert requirement is not None
            config_paths = profile_plugin_config_paths(store, "internal", home)
            self.assertGreaterEqual(len(config_paths), 2)
            before = {path: path.read_bytes() for path in config_paths}
            writes = 0

            def fail_second_write(
                path: Path,
                data: bytes,
                mode: int | None = None,
            ) -> None:
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("injected config write failure")
                path.write_bytes(data)
                if mode is not None:
                    path.chmod(mode)

            with mock.patch(
                "codex_switch_plugins.atomic_write",
                side_effect=fail_second_write,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "injected config write failure",
                ):
                    disable_unavailable_plugin_requirements(
                        store,
                        "internal",
                        home,
                        [requirement],
                    )

            self.assertEqual(
                before,
                {path: path.read_bytes() for path in config_paths},
            )

    def test_disable_unavailable_validates_all_configs_before_writing(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, _, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog={"installed": [], "available": []},
            )
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
                internal_codex_home=home,
            )
            requirement = plugin_requirement(selector)
            self.assertIsNotNone(requirement)
            assert requirement is not None
            config_paths = profile_plugin_config_paths(store, "internal", home)
            self.assertGreaterEqual(len(config_paths), 2)
            invalid_path = config_paths[-1]
            invalid_path.write_text(
                f'[plugins."{selector}"]\n'
                "enabled = [\n"
            )
            before = {path: path.read_bytes() for path in config_paths}

            with self.assertRaises(SwitchError):
                disable_unavailable_plugin_requirements(
                    store,
                    "internal",
                    home,
                    [requirement],
                )

            self.assertEqual(
                before,
                {path: path.read_bytes() for path in config_paths},
            )

    def test_plugin_config_apply_rejects_plan_drift_before_writing(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, _, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog={"installed": [], "available": []},
            )
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
                internal_codex_home=home,
            )
            requirement = plugin_requirement(selector)
            self.assertIsNotNone(requirement)
            assert requirement is not None
            updates = build_plugin_config_updates(
                store,
                "internal",
                home,
                [requirement],
            )
            self.assertGreaterEqual(len(updates), 2)
            updates[-1].path.write_bytes(updates[-1].before + b"# changed\n")

            with mock.patch(
                "codex_switch_plugins.atomic_write",
            ) as writer:
                with self.assertRaisesRegex(
                    SwitchError,
                    "plugin config changed after planning",
                ):
                    apply_plugin_config_updates(updates)

            writer.assert_not_called()

    def test_repair_plugins_refreshes_available_catalog_before_installing_missing_profile_plugins(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog={
                    "installed": [],
                    "available": [
                        {
                            "pluginId": selector,
                            "name": "missing-plugin",
                            "marketplaceName": "local-test",
                            "version": "1.0.0",
                        }
                    ],
                },
            )

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            calls = (home / "codex-calls.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{home}|--version",
                    f"{home}|plugin marketplace upgrade --json",
                    f"{home}|plugin list --available --json",
                    f"{home}|plugin add {selector}",
                ],
                calls,
            )
            self.assertTrue((home / ".tmp" / "plugins" / "plugins" / "catalog-only").is_dir())
            self.assertTrue(
                (
                    home
                    / "plugins"
                    / "cache"
                    / "local-test"
                    / "missing-plugin"
                    / "1.0.0"
                    / ".codex-plugin"
                ).exists()
            )
            self.assertIn("Refreshing plugin marketplaces for internal", repair.stdout)
            self.assertIn("Priming available plugin catalog for internal", repair.stdout)
            self.assertIn(f"Installing plugin: {selector}", repair.stdout)

    def test_repair_plugins_refreshes_available_catalog_when_enabled_plugins_are_installed(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "installed-plugin@local-test"
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog={
                    "installed": [
                        {
                            "pluginId": selector,
                            "name": "installed-plugin",
                            "marketplaceName": "local-test",
                            "version": "1.0.0",
                        }
                    ],
                    "available": [],
                },
            )
            installed = (
                home
                / "plugins"
                / "cache"
                / "local-test"
                / "installed-plugin"
                / "1.0.0"
            )
            installed.mkdir(parents=True)
            marker = installed / ".codex-plugin" / "plugin.json"
            marker.parent.mkdir()
            marker.write_text(
                json.dumps(
                    {"name": "installed-plugin", "version": "1.0.0"}
                )
                + "\n"
            )

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            calls = (home / "codex-calls.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{home}|--version",
                    f"{home}|plugin marketplace upgrade --json",
                    f"{home}|plugin list --available --json",
                ],
                calls,
            )
            self.assertTrue((home / ".tmp" / "plugins" / "plugins" / "catalog-only").is_dir())
            self.assertIn("No missing enabled plugins for internal", repair.stdout)

    def test_repair_plugins_refreshes_stale_local_plugin_cache(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "installed-plugin@local-test"
            source = root / "marketplace" / "plugins" / "installed-plugin"
            write_plugin_source(
                source,
                name="installed-plugin",
                version="1.0.0",
                payload="source-current\n",
            )
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "installed-plugin",
                        "marketplaceName": "local-test",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                        "source": {"source": "local", "path": str(source)},
                    }
                ],
                "available": [],
            }
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog=catalog,
                sources={selector: source},
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "local-test"
                / "installed-plugin"
                / "1.0.0"
            )
            write_plugin_source(
                cache,
                name="installed-plugin",
                version="1.0.0",
                payload="cache-stale\n",
            )

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            calls = (home / "codex-calls.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{home}|--version",
                    f"{home}|plugin marketplace upgrade --json",
                    f"{home}|plugin list --available --json",
                    f"{home}|plugin add {selector}",
                ],
                calls,
            )
            self.assertEqual("source-current\n", (cache / "payload.txt").read_text())
            self.assertIn(f"Refreshing stale plugin cache: {selector}", repair.stdout)

    def test_repair_plugins_keeps_current_cache_with_runtime_residue(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "installed-plugin@local-test"
            source = root / "marketplace" / "plugins" / "installed-plugin"
            write_plugin_source(
                source,
                name="installed-plugin",
                version="1.0.0",
                payload="same\n",
            )
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "installed-plugin",
                        "marketplaceName": "local-test",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                        "source": {"source": "local", "path": str(source)},
                    }
                ],
                "available": [],
            }
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog=catalog,
                sources={selector: source},
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "local-test"
                / "installed-plugin"
                / "1.0.0"
            )
            shutil.copytree(source, cache, symlinks=True)
            (cache / ".DS_Store").write_text("runtime residue\n")
            pycache = cache / "__pycache__"
            pycache.mkdir()
            (pycache / "module.cpython-312.pyc").write_bytes(b"bytecode")
            pytest_cache = cache / ".pytest_cache"
            pytest_cache.mkdir()
            (pytest_cache / "README.md").write_text("runtime residue\n")
            git_dir = cache / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("runtime residue\n")

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            calls = (home / "codex-calls.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{home}|--version",
                    f"{home}|plugin marketplace upgrade --json",
                    f"{home}|plugin list --available --json",
                ],
                calls,
            )
            self.assertIn(f"Plugin cache current: {selector}", repair.stdout)

    def test_repair_plugins_keeps_current_revision_named_cache(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "figma@openai-curated"
            source = root / "marketplace" / "plugins" / "figma"
            write_plugin_source(
                source,
                name="figma",
                version="2.0.13",
                payload="curated snapshot\n",
            )
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "figma",
                        "marketplaceName": "openai-curated",
                        "version": "11c74d6b",
                        "installed": True,
                        "enabled": True,
                        "source": {"source": "local", "path": str(source)},
                    }
                ],
                "available": [],
            }
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog=catalog,
                sources={selector: source},
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "openai-curated"
                / "figma"
                / "11c74d6b"
            )
            shutil.copytree(source, cache, symlinks=True)

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            calls = (home / "codex-calls.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{home}|--version",
                    f"{home}|plugin marketplace upgrade --json",
                    f"{home}|plugin list --available --json",
                ],
                calls,
            )
            self.assertIn(f"Plugin cache current: {selector}", repair.stdout)

    def test_repair_plugins_skips_revision_cache_manifest_version_mismatch(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "figma@openai-curated"
            source = root / "marketplace" / "plugins" / "figma"
            write_plugin_source(
                source,
                name="figma",
                version="2.0.13",
                payload="new curated snapshot\n",
            )
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "figma",
                        "marketplaceName": "openai-curated",
                        "version": "11c74d6b",
                        "installed": True,
                        "enabled": True,
                        "source": {"source": "local", "path": str(source)},
                    }
                ],
                "available": [],
            }
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog=catalog,
                sources={selector: source},
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "openai-curated"
                / "figma"
                / "11c74d6b"
            )
            write_plugin_source(
                cache,
                name="figma",
                version="2.0.12",
                payload="old curated snapshot\n",
            )

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            self.assertIn(
                f"Skipping stale-cache check for {selector}: "
                "source and cache manifest versions differ",
                repair.stdout,
            )
            self.assertNotIn(
                f"plugin add {selector}",
                (home / "codex-calls.log").read_text(),
            )

    def test_plugin_tree_manifest_propagates_walk_errors(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            tree = root / "plugin-tree"
            tree.mkdir()

            def fail_walk(*args, **kwargs):
                onerror = kwargs.get("onerror")
                self.assertIsNotNone(onerror)
                onerror(PermissionError("denied"))
                return []

            with mock.patch(
                "codex_switch_plugins.os.walk",
                side_effect=fail_walk,
            ):
                with self.assertRaises(PermissionError):
                    plugin_tree_manifest(tree)

    def test_repair_plugins_skips_uninspectable_installed_source(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "installed-plugin@remote-test"
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "installed-plugin",
                        "marketplaceName": "remote-test",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                        "source": {
                            "source": "git",
                            "path": "git@example.com:plugins/installed-plugin.git",
                        },
                    }
                ],
                "available": [],
            }
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog=catalog,
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "remote-test"
                / "installed-plugin"
                / "1.0.0"
            )
            write_plugin_source(
                cache,
                name="installed-plugin",
                version="1.0.0",
                payload="installed\n",
            )

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            self.assertIn(
                f"Skipping stale-cache check for {selector}: "
                "catalog source is not inspectable",
                repair.stdout,
            )
            self.assertNotIn(f"plugin add {selector}", (home / "codex-calls.log").read_text())

    def test_repair_plugins_skips_catalog_source_manifest_version_mismatch(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "installed-plugin@local-test"
            source = root / "marketplace" / "plugins" / "installed-plugin"
            write_plugin_source(
                source,
                name="installed-plugin",
                version="2.0.0",
                payload="newer-source\n",
            )
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "installed-plugin",
                        "marketplaceName": "local-test",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                        "source": {"source": "local", "path": str(source)},
                    }
                ],
                "available": [],
            }
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog=catalog,
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "local-test"
                / "installed-plugin"
                / "1.0.0"
            )
            write_plugin_source(
                cache,
                name="installed-plugin",
                version="1.0.0",
                payload="installed\n",
            )

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            self.assertIn(
                f"Skipping stale-cache check for {selector}: "
                "source manifest does not match catalog name/version",
                repair.stdout,
            )
            self.assertNotIn(f"plugin add {selector}", (home / "codex-calls.log").read_text())

    def test_repair_plugins_uses_canonical_binding_cli_and_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            canonical_cli = root / "canonical-chatgpt-codex"
            canonical_home = root / "canonical-official-home"
            canonical_home.mkdir()
            (canonical_home / "config.toml").write_text("")
            write_fake_plugin_refresh_codex(
                canonical_cli,
                catalog={"installed": [], "available": []},
            )
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
            )
            binding = SimpleNamespace(
                backend_cli=canonical_cli,
                codex_home=canonical_home,
                desktop_cli=canonical_cli,
                requires_proxy=False,
            )

            with mock.patch(
                "codex_switch_plugins.resolve_store_runtime_binding",
                return_value=binding,
            ) as resolver:
                repair_profile_plugins(store, "openai-official")

            resolver.assert_called_once_with(store, "openai-official")
            self.assertEqual(
                [
                    f"{canonical_home}|--version",
                    f"{canonical_home}|plugin marketplace upgrade --json",
                    f"{canonical_home}|plugin list --available --json",
                ],
                (canonical_home / "codex-calls.log").read_text().splitlines(),
            )

    def test_repair_plugins_blocks_stale_refresh_for_running_target_app_server(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "installed-plugin@local-test"
            source = root / "marketplace" / "plugins" / "installed-plugin"
            write_plugin_source(
                source,
                name="installed-plugin",
                version="1.0.0",
                payload="source-current\n",
            )
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "installed-plugin",
                        "marketplaceName": "local-test",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                        "source": {"source": "local", "path": str(source)},
                    }
                ],
                "available": [],
            }
            internal_cli, _, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog=catalog,
                sources={selector: source},
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "local-test"
                / "installed-plugin"
                / "1.0.0"
            )
            write_plugin_source(
                cache,
                name="installed-plugin",
                version="1.0.0",
                payload="cache-stale\n",
            )
            store = Store(
                root / "store",
                root / "live",
                root / "agent.plist",
                internal_codex_home=home,
            )
            desktop_cli = root / "store" / "bin" / "codex-internal-app"
            binding = SimpleNamespace(
                backend_cli=internal_cli,
                codex_home=home,
                desktop_cli=desktop_cli,
                requires_proxy=True,
            )
            observation = RuntimeObservation(
                processes=(
                    RunningCodexProcess(
                        pid=4242,
                        kind="app-server",
                        command_path=str(internal_cli),
                        app_cli_env="",
                        parent_command="",
                    ),
                )
            )

            with mock.patch(
                "codex_switch_plugins.resolve_store_runtime_binding",
                return_value=binding,
            ), mock.patch(
                "codex_switch_plugins.collect_store_runtime_observation",
                return_value=observation,
            ):
                with self.assertRaisesRegex(
                    SwitchError,
                    "target profile internal app-server is running",
                ):
                    repair_profile_plugins(store, "internal")

            self.assertNotIn(f"plugin add {selector}", (home / "codex-calls.log").read_text())

    def test_wrapper_one_key_refreshes_stale_plugins_before_parity_gate(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "installed-plugin@local-test"
            source = root / "marketplace" / "plugins" / "installed-plugin"
            write_plugin_source(
                source,
                name="installed-plugin",
                version="1.0.0",
                payload="source-current\n",
            )
            catalog = {
                "installed": [
                    {
                        "pluginId": selector,
                        "name": "installed-plugin",
                        "marketplaceName": "local-test",
                        "version": "1.0.0",
                        "installed": True,
                        "enabled": True,
                        "source": {"source": "local", "path": str(source)},
                    }
                ],
                "available": [],
            }
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=f'[plugins."{selector}"]\nenabled = true\n',
                catalog=catalog,
                sources={selector: source},
            )
            cache = (
                home
                / "plugins"
                / "cache"
                / "local-test"
                / "installed-plugin"
                / "1.0.0"
            )
            write_plugin_source(
                cache,
                name="installed-plugin",
                version="1.0.0",
                payload="cache-stale\n",
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-update-check",
                "--skip-launchctl",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertIn(f"Refreshing stale plugin cache: {selector}", output)
            self.assertLess(
                output.index(f"Refreshing stale plugin cache: {selector}"),
                output.index("== Verification =="),
            )
            self.assertIn("Parity health: unhealthy", output)
            self.assertIn("parity.receipt.missing", output)
            self.assertIn("Doctor: not run", output)
            self.assertIn("Outcome: ACTION REQUIRED", output)

    def test_repair_plugins_skips_unavailable_enabled_plugins_after_catalog_refresh(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog={
                    "installed": [],
                    "available": [
                        {
                            "pluginId": "other-plugin@local-test",
                            "name": "other-plugin",
                            "marketplaceName": "local-test",
                            "version": "1.0.0",
                        }
                    ],
                },
            )

            repair = self.run_wrapper(root, "repair-plugins", "internal", env=env)

            calls = (home / "codex-calls.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{home}|--version",
                    f"{home}|plugin marketplace upgrade --json",
                    f"{home}|plugin list --available --json",
                ],
                calls,
            )
            self.assertIn(
                f"Skipping unavailable enabled plugin: {selector}",
                repair.stdout,
            )

    def test_repair_plugins_disable_unavailable_stale_enabled_plugins(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            config_text = (
                "[marketplaces.local-test]\n"
                'source_type = "local"\n'
                f'source = "{root / "marketplace"}"\n'
                "\n"
                f'[plugins."{selector}"]\n'
                "enabled = true\n"
            )
            _, env, home = self.prepare_internal_plugin_profile(
                root,
                config_text=config_text,
                catalog={
                    "installed": [],
                    "available": [
                        {
                            "pluginId": "other-plugin@local-test",
                            "name": "other-plugin",
                            "marketplaceName": "local-test",
                            "version": "1.0.0",
                        }
                    ],
                },
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl", env=env)
            live_home = root / "live"
            profile_layer = home / "internal.config.toml"
            profile_snapshot = home / "internal.plugin-support.config.toml"
            canonical_profile = root / "store" / "profiles" / "internal" / "config.toml"
            for config_path in (
                live_home / "config.toml",
                profile_layer,
                profile_snapshot,
                canonical_profile,
            ):
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(config_text)

            repair = self.run_wrapper(
                root,
                "repair-plugins",
                "internal",
                "--disable-unavailable",
                env=env,
            )
            doctor = self.run_switcher(root, "doctor", check=False)

            self.assertIn(
                f"Disabling unavailable enabled plugin: {selector}",
                repair.stdout,
            )
            calls = (home / "codex-calls.log").read_text().splitlines()
            self.assertEqual(
                [
                    f"{home}|--version",
                    f"{home}|plugin marketplace upgrade --json",
                    f"{home}|plugin list --available --json",
                ],
                calls,
            )
            for config_path in (
                live_home / "config.toml",
                profile_layer,
                profile_snapshot,
                canonical_profile,
            ):
                config_text = config_path.read_text()
                self.assertIn(f'[plugins."{selector}"]', config_text)
                self.assertIn("enabled = false", config_text)
                self.assertNotIn("enabled = true", config_text)
            self.assertNotEqual(0, doctor.returncode)
            self.assertIn(
                "parity.receipt.missing",
                doctor.stdout + doctor.stderr,
            )

    def test_repair_plugins_dry_run_does_not_claim_unverified_plugin_add(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, env, _ = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog={"installed": [], "available": []},
            )

            repair = self.run_wrapper(
                root,
                "repair-plugins",
                "internal",
                "--dry-run",
                env=env,
            )

            self.assertIn("Dry run: plugin catalog is not inspected", repair.stdout)
            self.assertIn(
                f"Dry run: would install if available: {selector}",
                repair.stdout,
            )
            self.assertNotIn(f"plugin add {selector}", repair.stdout)

    def test_repair_plugins_does_not_refresh_project_workflow_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, env, _ = self.prepare_internal_plugin_profile(
                root,
                config_text="",
                catalog={"installed": [], "available": []},
            )
            project = root / "project"
            project.mkdir()
            generated = project / "AGENTS.md.generated"
            generated.write_text("project-generated-guidance\n")
            state = project / ".planning" / "devflow" / "STATE.md"
            state.parent.mkdir(parents=True)
            state.write_text("project-state\n")
            spec = project / "openspec" / "changes" / "keep" / "spec.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("project-spec\n")
            skill_source = root / "project-skill-source"
            skill_source.mkdir()
            skill_link = project / ".agents" / "skills" / "dev-flow"
            skill_link.parent.mkdir(parents=True)
            skill_link.symlink_to(skill_source, target_is_directory=True)

            def snapshot() -> list[tuple[str, str, bytes | str]]:
                entries: list[tuple[str, str, bytes | str]] = []
                for path in sorted(project.rglob("*")):
                    relative = path.relative_to(project).as_posix()
                    if path.is_symlink():
                        entries.append((relative, "symlink", os.readlink(path)))
                    elif path.is_file():
                        entries.append((relative, "file", path.read_bytes()))
                    else:
                        entries.append((relative, "dir", ""))
                return entries

            before = snapshot()
            self.run_wrapper(
                root,
                "repair-plugins",
                "internal",
                env=env,
                cwd=project,
            )

            self.assertEqual(before, snapshot())

    def test_wrapper_one_key_help_is_pure_help(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            result = self.run_wrapper(root, "internal", "--help")

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode)
            self.assertIn("split                    Use internal CLI", output)
            self.assertIn("Switch options for internal/split/official:", output)
            self.assertIn("--keep-version", output)
            self.assertNotIn("Dry-run plan", output)
            self.assertFalse((root / "store").exists())

    def test_wrapper_one_key_repairs_plugins_before_parity_gate(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, env, _ = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog={
                    "installed": [],
                    "available": [
                        {
                            "pluginId": selector,
                            "name": "missing-plugin",
                            "marketplaceName": "local-test",
                            "version": "1.0.0",
                        }
                    ],
                },
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-update-check",
                "--skip-launchctl",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertIn("Plugin repair", output)
            self.assertIn(f"Installing plugin: {selector}", output)
            self.assertIn("Parity health: unhealthy", output)
            self.assertIn("parity.receipt.missing", output)
            self.assertIn("Doctor: not run", output)
            self.assertIn("Outcome: ACTION REQUIRED", output)

    def test_wrapper_one_key_stops_at_missing_parity_before_doctor(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, env, _ = self.prepare_internal_plugin_profile(
                root,
                config_text="",
                catalog={"installed": [], "available": []},
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-update-check",
                "--skip-launchctl",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertIn("== Verification ==", output)
            self.assertIn("Parity health: unhealthy", output)
            self.assertIn("parity.receipt.missing", output)
            self.assertNotIn("== Doctor ==", output)
            self.assertIn("Verify: failed (exit 1)", output)
            self.assertIn("Doctor: not run", output)
            self.assertIn("Outcome: ACTION REQUIRED", output)

    def test_wrapper_one_key_unavailable_plugin_reaches_doctor_without_repair_failure(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            selector = "missing-plugin@local-test"
            _, env, _ = self.prepare_internal_plugin_profile(
                root,
                config_text=(
                    "[marketplaces.local-test]\n"
                    'source_type = "local"\n'
                    f'source = "{root / "marketplace"}"\n'
                    "\n"
                    f'[plugins."{selector}"]\n'
                    "enabled = true\n"
                ),
                catalog={"installed": [], "available": []},
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-update-check",
                "--skip-verify",
                "--skip-launchctl",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn(f"Skipping unavailable enabled plugin: {selector}", output)
            self.assertIn("Doctor found issues:", output)
            self.assertIn("Doctor: failed", output)
            self.assertNotIn("Failed step: plugin repair", output)

    def test_wrapper_one_key_can_skip_plugin_repair(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            (root / "live" / "config.toml").write_text(
                "[marketplaces.local-test]\n"
                'source_type = "local"\n'
                f'source = "{root / "marketplace"}"\n'
                "\n"
                '[plugins."missing-plugin@local-test"]\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            result = self.run_wrapper(
                root,
                "official",
                "--skip-login",
                "--skip-update-check",
                "--skip-plugin-repair",
                "--skip-launchctl",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Plugin repair: skipped by command option.", output)
            self.assertIn("Doctor found issues:", output)
            self.assertIn("codex-switch repair-plugins openai-official", output)

    def test_internal_switch_can_adopt_live_home_and_move_official_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'model = "legacy-internal-runtime"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            (live_home / "sessions").mkdir()
            (live_home / "history.jsonl").write_text("legacy internal history\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_switcher(
                root,
                "switch",
                "internal",
                "--internal-codex-home",
                str(live_home),
                "--skip-launchctl",
            )

            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "internal")
            self.assertEqual(active["codex_home"], str(live_home))
            self.assertEqual(active["home_mode"], "adopted")
            self.assertEqual("legacy internal history\n", (live_home / "history.jsonl").read_text())
            internal_manifest = self.read_manifest(root, "internal")
            official_manifest = self.read_manifest(root, "openai-official")
            official_home = root / "store" / "homes" / "openai-official"
            self.assertEqual(internal_manifest["codex_home"], str(live_home))
            self.assertEqual(internal_manifest["home_mode"], "adopted")
            self.assertEqual(official_manifest["codex_home"], str(official_home))
            self.assertEqual(official_manifest["home_mode"], "managed")

            shim = root / "store" / "bin" / "codex"
            shim_text = shim.read_text()
            self.assertIn(str(live_home), shim_text)
            self.assertIn("exec-internal-shell", shim_text)

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "openai-official")
            self.assertEqual(active["codex_home"], str(official_home))
            self.assertEqual(active["home_mode"], "managed")
            self.assertTrue((official_home / "config.toml").exists())
            self.assertEqual("legacy internal history\n", (live_home / "history.jsonl").read_text())
            self.assertFalse((official_home / "history.jsonl").exists())

    def test_switch_rejects_explicit_identical_independent_homes(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--store-dir",
                    str(root / "store"),
                    "--official-codex-home",
                    str(live_home),
                    "--internal-codex-home",
                    str(live_home),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "switch",
                    "internal",
                    "--skip-launchctl",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to use the same Codex home", result.stderr)
            self.assertFalse((root / "store" / "active.json").exists())

    def test_wrapper_forwards_internal_codex_home_option(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            fake_switcher = root / "fake_switcher.py"
            fake_switcher.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "print('ARGS:' + ' '.join(sys.argv[1:]))\n"
            )
            fake_switcher.chmod(0o755)
            adopted_home = root / "live"
            env = os.environ.copy()
            env["CODEX_SWITCH_SCRIPT"] = str(fake_switcher)
            env["CODEX_SWITCH_SKIP_SELF_UPDATE"] = "1"

            result = subprocess.run(
                [
                    str(WRAPPER),
                    "--store-dir",
                    str(root / "store"),
                    "--live-codex-home",
                    str(root / "live"),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "internal",
                    "--internal-codex-home",
                    str(adopted_home),
                    "--dry-run",
                    "--skip-update-check",
                    "--skip-login",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn(f"--internal-codex-home {adopted_home}", result.stdout)

    def test_interactive_home_prompt_prioritizes_target_profile_and_recommended_option(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            prompt_env = dict(env)
            prompt_env["CODEX_SWITCH_FORCE_HOME_PROMPT"] = "1"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--store-dir",
                    str(root / "store"),
                    "--live-codex-home",
                    str(root / "live"),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "switch",
                    "openai-official",
                    "--skip-launchctl",
                    "--skip-app-cli",
                    "--skip-shim",
                ],
                input="\n\n\n",
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=prompt_env,
            )

            output = result.stdout
            official_prompt = output.index("Select Codex home for openai-official:")
            internal_prompt = output.index("Select Codex home for internal:")
            self.assertLess(official_prompt, internal_prompt)
            self.assertIn(f"  1. {root / 'live'} (Recommended)", output)
            self.assertIn(
                f"  1. {root / 'store' / 'homes' / 'internal'} (Recommended)",
                output,
            )

    def test_interactive_prompt_prefers_semantic_default_for_unconfirmed_internal_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            managed_internal_home = root / "store" / "homes" / "internal"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            internal_manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            internal_manifest = json.loads(internal_manifest_path.read_text())
            internal_manifest["codex_home"] = str(live_home)
            internal_manifest["home_mode"] = "adopted"
            internal_manifest.pop("home_selection_confirmed", None)
            internal_manifest_path.write_text(json.dumps(internal_manifest))

            prompt_env = dict(env)
            prompt_env["CODEX_SWITCH_FORCE_HOME_PROMPT"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--store-dir",
                    str(root / "store"),
                    "--live-codex-home",
                    str(live_home),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "switch",
                    "internal",
                    "--skip-launchctl",
                    "--skip-app-cli",
                    "--skip-shim",
                ],
                input="\n\n\n",
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=prompt_env,
            )

            output = result.stdout
            self.assertIn("Select Codex home for internal:", output)
            self.assertIn(f"  1. {managed_internal_home} (Recommended)", output)
            self.assertIn(str(live_home), output)
            confirmed_manifest = self.read_manifest(root, "internal")
            self.assertEqual(confirmed_manifest["codex_home"], str(managed_internal_home))
            self.assertTrue(confirmed_manifest["home_selection_confirmed"])

    def test_interactive_prompt_prefers_official_home_for_unconfirmed_official_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            managed_official_home = root / "store" / "homes" / "openai-official"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            official_manifest_path = (
                root / "store" / "profiles" / "openai-official" / "manifest.json"
            )
            official_manifest = json.loads(official_manifest_path.read_text())
            official_manifest["codex_home"] = str(managed_official_home)
            official_manifest["home_mode"] = "managed"
            official_manifest.pop("home_selection_confirmed", None)
            official_manifest_path.write_text(json.dumps(official_manifest))

            prompt_env = dict(env)
            prompt_env["CODEX_SWITCH_FORCE_HOME_PROMPT"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--store-dir",
                    str(root / "store"),
                    "--live-codex-home",
                    str(live_home),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "switch",
                    "openai-official",
                    "--skip-launchctl",
                    "--skip-app-cli",
                    "--skip-shim",
                ],
                input="\n\n",
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=prompt_env,
            )

            output = result.stdout
            self.assertIn("Select Codex home for openai-official:", output)
            self.assertIn(f"  1. {live_home} (Recommended)", output)
            self.assertIn(str(managed_official_home), output)
            confirmed_manifest = self.read_manifest(root, "openai-official")
            self.assertEqual(confirmed_manifest["codex_home"], str(live_home))
            self.assertTrue(confirmed_manifest["home_selection_confirmed"])

    def test_interactive_profile_change_prompts_target_away_from_active_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "store" / "active.json").write_text(
                json.dumps(
                    {
                        "profile": "openai-official",
                        "codex_home": str(live_home),
                    }
                )
            )
            internal_manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            internal_manifest = json.loads(internal_manifest_path.read_text())
            internal_manifest["codex_home"] = str(live_home)
            internal_manifest["home_mode"] = "adopted"
            internal_manifest["home_selection_confirmed"] = True
            internal_manifest_path.write_text(json.dumps(internal_manifest))

            prompt_env = dict(env)
            prompt_env["CODEX_SWITCH_FORCE_HOME_PROMPT"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--store-dir",
                    str(root / "store"),
                    "--official-codex-home",
                    str(live_home),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "switch",
                    "internal",
                    "--skip-launchctl",
                    "--skip-app-cli",
                    "--skip-shim",
                ],
                input="\n",
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=prompt_env,
            )

            output = result.stdout
            internal_home = root / "store" / "homes" / "internal"
            self.assertIn(
                f"openai-official currently uses {live_home}; "
                "choose a different Codex home for internal.",
                output,
            )
            self.assertIn("Select Codex home for internal:", output)
            self.assertIn(f"  1. {internal_home} (Recommended)", output)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "internal")
            self.assertEqual(active["codex_home"], str(internal_home))
            confirmed_manifest = self.read_manifest(root, "internal")
            self.assertEqual(confirmed_manifest["codex_home"], str(internal_home))
            self.assertTrue(confirmed_manifest["home_selection_confirmed"])

    def test_interactive_same_home_collision_prompts_for_other_profile_home(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            prompt_env = dict(env)
            prompt_env["CODEX_SWITCH_FORCE_HOME_PROMPT"] = "1"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--store-dir",
                    str(root / "store"),
                    "--official-codex-home",
                    str(live_home),
                    "--internal-codex-home",
                    str(live_home),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "switch",
                    "internal",
                    "--skip-launchctl",
                    "--skip-app-cli",
                    "--skip-shim",
                ],
                input="\n",
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=prompt_env,
            )

            output = result.stdout
            official_home = root / "store" / "homes" / "openai-official"
            self.assertIn("already uses", output)
            self.assertIn("Select Codex home for openai-official:", output)
            self.assertIn(f"  1. {official_home} (Recommended)", output)
            official_manifest = self.read_manifest(root, "openai-official")
            self.assertEqual(official_manifest["codex_home"], str(official_home))
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "internal")
            self.assertEqual(active["codex_home"], str(live_home))

    def test_restore_backup_dry_run_and_apply(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            active = json.loads((root / "store" / "active.json").read_text())
            backup_id = active["backup_id"]
            shim = root / "store" / "bin" / "codex"
            self.assertTrue(shim.exists())

            dry_run = self.run_switcher(root, "restore", backup_id, "--dry-run")
            self.assertIn("Dry run: restore backup", dry_run.stdout)
            self.assertTrue(shim.exists())

            self.run_switcher(root, "restore", backup_id, "--apply")

            self.assertFalse(shim.exists())
            self.assertFalse((root / "store" / "active.json").exists())

    def test_backup_failure_aborts_before_mutation(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            original_config = "[features]\nmemory = true\n"
            (live_home / "config.toml").write_text(original_config)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            shutil.rmtree(root / "store" / "backups")
            (root / "store" / "backups").write_text("not a directory\n")

            result = self.run_switcher(
                root,
                "switch",
                "internal",
                "--skip-launchctl",
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(original_config, (live_home / "config.toml").read_text())
            self.assertFalse((root / "store" / "homes" / "internal").exists())
            self.assertFalse((root / "store" / "bin" / "codex").exists())
            self.assertFalse((root / "agent.plist").exists())

    def test_switch_writes_profile_v2_config_and_removes_non_file_auth(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            (root / "live" / "auth.json").write_text("{}\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            (root / "live" / "internal.config.toml").write_text('model = "old-internal"\n')

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            live_config = (root / "live" / "config.toml").read_text()
            self.assertIn('profile = "internal"', live_config)
            internal_config = (root / "store" / "homes" / "internal" / "config.toml").read_text()
            self.assertNotIn('profile = "internal"', internal_config)
            self.assertNotIn("[profiles.internal]", internal_config)
            self.assertTrue((root / "live" / "internal.config.toml").exists())
            self.assertTrue((root / "live" / "auth.json").exists())
            active = json.loads((root / "store" / "active.json").read_text())
            backup_dir = Path(active["backup_dir"])
            backup = json.loads((backup_dir / "backup.json").read_text())
            self.assertEqual(backup["to_profile"], "internal")

    def test_switch_preserves_live_shared_preferences(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            (root / "live" / "config.toml").write_text(
                'model = "gpt-5.5-2026-04-24"\n'
                'model_provider = "azure"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
                "\n"
                "[tui]\n"
                'theme = "catppuccin-latte"\n'
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_switcher(root, "switch", "openai-official", "--skip-launchctl")

            live_config = (root / "live" / "config.toml").read_text()
            self.assertIn('model = "gpt-5.5-2026-04-24"', live_config)
            self.assertIn('model_provider = "azure"', live_config)
            self.assertNotIn('cli_auth_credentials_store = "file"', live_config)
            self.assertIn("[features]", live_config)
            self.assertIn("memory = true", live_config)
            self.assertIn("[tui]", live_config)
            self.assertIn('theme = "catppuccin-latte"', live_config)
            profile_config = (
                root / "store" / "profiles" / "openai-official" / "config.toml"
            ).read_text()
            self.assertIn('cli_auth_credentials_store = "file"', profile_config)

    def test_profile_v2_config_flattens_legacy_profile_table(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            (root / "live" / "config.toml").write_text(
                'theme = "dark"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            profile_config = root / "profile.toml"
            profile_config.write_text(
                'profile = "internal"\n'
                'background_terminal_max_timeout = 10\n'
                "\n"
                "[profiles.internal]\n"
                'model = "gpt-5.5-2026-04-24"\n'
                'model_provider = "azure"\n'
                'model_catalog_json = "/Users/me/.codex/model-catalogs/azure-models.json"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "Azure"\n'
                "\n"
                "[model_providers.azure.query_params]\n"
                'api-version = "2025-03-01-preview"\n'
            )

            config = build_profile_v2_config_text("internal", profile_config)
            base = build_base_config_text(root / "live" / "config.toml")

            self.assertNotIn('profile = "internal"', config)
            self.assertNotIn("[profiles.internal]", config)
            self.assertIn('model = "gpt-5.5-2026-04-24"', config)
            self.assertIn('model_provider = "azure"', config)
            self.assertIn(
                'model_catalog_json = "/Users/me/.codex/model-catalogs/azure-models.json"',
                config,
            )
            self.assertIn("[model_providers.azure]", config)
            self.assertIn('name = "Azure"', config)
            self.assertIn("[model_providers.azure.query_params]", config)
            self.assertIn('api-version = "2025-03-01-preview"', config)
            self.assertIn('theme = "dark"', base)
            self.assertIn("[features]", base)
            self.assertIn("memory = true", base)

    def test_profile_v2_config_replaces_target_model_provider_table(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            (root / "live" / "config.toml").write_text(
                "[model_providers.azure]\n"
                'name = "Old Azure"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            profile_config = root / "profile.toml"
            profile_config.write_text(
                "[profiles.internal]\n"
                'model_provider = "azure"\n'
                "\n"
                "[model_providers.azure]\n"
                'name = "New Azure"\n'
            )

            config = build_profile_v2_config_text("internal", profile_config)

            self.assertIn('model_provider = "azure"', config)
            self.assertIn('name = "New Azure"', config)
            self.assertNotIn('name = "Old Azure"', config)

    def test_base_config_removes_profile_keys_and_profile_layer_keeps_target_keys(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            (root / "live" / "config.toml").write_text(
                'model = "gpt-5.5-2026-04-24"\n'
                'model_provider = "azure"\n'
                'model_catalog_json = "/Users/me/.codex/model-catalogs/azure-models.json"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
                "\n"
                "[tui]\n"
                'theme = "catppuccin-latte"\n'
            )
            profile_config = root / "profile.toml"
            profile_config.write_text(
                'profile = "openai-official"\n'
                'cli_auth_credentials_store = "file"\n'
                "\n"
                "[profiles.openai-official]\n"
            )

            config = build_base_config_text(root / "live" / "config.toml")
            profile_layer = build_profile_v2_config_text("openai-official", profile_config)

            self.assertNotIn('profile = "openai-official"', config)
            self.assertNotIn("[profiles.openai-official]", config)
            self.assertNotIn('model = "gpt-5.5-2026-04-24"', config)
            self.assertNotIn('model_provider = "azure"', config)
            self.assertNotIn("model_catalog_json", config)
            self.assertNotIn("model_catalog_json", profile_layer)
            self.assertIn('cli_auth_credentials_store = "file"', profile_layer)
            self.assertIn("[features]", config)
            self.assertIn("memory = true", config)
            self.assertIn("[tui]", config)
            self.assertIn('theme = "catppuccin-latte"', config)

    def test_snapshot_switch_preserves_live_plugin_and_skill_config(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            (root / "live" / "config.toml").write_text(
                'model = "gpt-5.5-2026-04-24"\n'
                "\n"
                "[marketplaces.local-personal-plugins]\n"
                'source_type = "local"\n'
                f'source = "{root / "personal-marketplace"}"\n'
                "\n"
                '[plugins."dev-flow@local-personal-plugins"]\n'
                "enabled = true\n"
                "\n"
                "[[skills.config]]\n"
                f'path = "{root / "skills" / "dev-flow" / "SKILL.md"}"\n'
                "\n"
                '[hooks.state."dev-flow@local-personal-plugins:hooks.json:stop:0:0"]\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            self.run_switcher(
                root,
                "switch",
                "openai-official",
                "--config-mode",
                "snapshot",
                "--skip-launchctl",
            )

            live_config = (root / "live" / "config.toml").read_text()
            self.assertIn("[marketplaces.local-personal-plugins]", live_config)
            self.assertIn('[plugins."dev-flow@local-personal-plugins"]', live_config)
            self.assertIn("[[skills.config]]", live_config)
            self.assertIn(
                '[hooks.state."dev-flow@local-personal-plugins:hooks.json:stop:0:0"]',
                live_config,
            )
            self.assertNotIn('cli_auth_credentials_store = "file"', live_config)
            self.assertNotIn('model = "gpt-5.5-2026-04-24"', live_config)
            profile_config = (root / "live" / "openai-official.config.toml").read_text()
            self.assertIn('cli_auth_credentials_store = "file"', profile_config)

    def test_internal_switch_refreshes_desktop_wrapper_with_shared_config(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            (root / "live" / "config.toml").write_text(
                'model = "gpt-5.5-2026-04-24"\n'
                'model_provider = "azure"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."agent-kb@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]\n'
                'trusted_hash = "sha256:test"\n'
            )
            (root / "live" / "auth.json").write_text('{"official":"auth"}\n')
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(root / "store" / "bin" / "codex-internal-app")
            manifest_path.write_text(json.dumps(manifest))
            (root / "store" / "bin").mkdir(parents=True, exist_ok=True)
            (root / "store" / "bin" / "codex-internal-app").write_text(
                "#!/usr/bin/env sh\n"
                "SWITCH_SCRIPTS=/old/missing/path\n"
                "exit 99\n"
            )
            (root / "store" / "bin" / "codex-internal-app").chmod(0o755)

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            wrapper_text = app_wrapper.read_text()
            self.assertIn(str(Path(__file__).parent), wrapper_text)
            self.assertNotIn("/old/missing/path", wrapper_text)
            self.assertIn("codex_switch_app_proxy.py", wrapper_text)
            self.assertIn("codex_switch_home_sync.py", wrapper_text)
            self.assertIn("prepare-launch", wrapper_text)
            self.assertIn('"$PYTHON_BIN" -B', wrapper_text)
            self.assertNotIn('find "$APP_CODEX_HOME"', wrapper_text)
            self.assertNotIn('find "$LIVE_CODEX_HOME"', wrapper_text)
            self.assertNotIn("is_runtime_state_name()", wrapper_text)
            self.assertNotIn("<<'PY'", wrapper_text)
            self.assertNotIn('if [ "${1:-}" = "app-server" ]; then', wrapper_text)
            self.assertNotIn('&& [ "${2:-}" = "--stdio" ]', wrapper_text)

            result = subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self.assertEqual(1, result.stdout.count("internal-codex"))
            child_receipt = root / "proxy-child.json"
            proxy_env = dict(env)
            proxy_env["CODEX_SWITCH_PROXY_CHILD_RECEIPT"] = str(child_receipt)
            app_server_result = subprocess.run(
                [
                    str(app_wrapper),
                    "-c",
                    "features.code_mode_host=true",
                    "app-server",
                    "--analytics-default-enabled",
                ],
                input="",
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=proxy_env,
            )
            self.assertTrue(
                child_receipt.exists(),
                app_server_result.stdout + app_server_result.stderr,
            )
            receipt = json.loads(child_receipt.read_text())
            self.assertEqual(
                receipt["args"],
                [
                    "-c",
                    "features.code_mode_host=true",
                    "app-server",
                    "--analytics-default-enabled",
                ],
            )
            pythonpath_backend = root / "internal-bin" / "codex"
            write_fake_script(
                pythonpath_backend,
                "#!/usr/bin/env sh\n"
                "printf 'pythonpath:%s\\n' \"${PYTHONPATH-unset}\"\n",
            )
            pythonpath_env = dict(env)
            pythonpath_env["PYTHONPATH"] = "/original/pythonpath"
            pythonpath_result = subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=pythonpath_env,
            )
            self.assertEqual(
                "pythonpath:/original/pythonpath\n",
                pythonpath_result.stdout,
            )
            app_home_config = (
                root / "store" / "homes" / "internal" / "config.toml"
            ).read_text()
            self.assertIn("[marketplaces.cy-codex-skills]", app_home_config)
            self.assertIn('[plugins."agent-kb@cy-codex-skills"]', app_home_config)
            self.assertIn(
                '[hooks.state."agent-kb@cy-codex-skills:hooks.json:stop:0:0"]',
                app_home_config,
            )
            self.assertIn('model = "gpt-5.5-2026-04-24"', app_home_config)
            self.assertIn("# codex-switch: managed runtime config for profile internal", app_home_config)
            self.assertIn("\n# codex-switch: profile-specific settings\n", app_home_config)
            self.assertIn("\n# codex-switch: shared settings\n", app_home_config)
            self.assertFalse(
                (root / "store" / "homes" / "internal" / "auth.json").exists()
            )

    def test_internal_desktop_wrapper_matches_switch_for_forbidden_symlinks(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            (live_home / "sessions").mkdir()
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            manifest["app_cli_path"] = str(app_wrapper)
            manifest_path.write_text(json.dumps(manifest))
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            app_home = root / "store" / "homes" / "internal"
            other_profile_home = root / "store" / "homes" / "other-profile"
            other_profile_home.mkdir(parents=True)
            (other_profile_home / "cache").mkdir()
            (app_home / "target-state").write_text("target\n")
            (live_home / "history.jsonl").write_text("live\n")

            links = {
                "sessions": Path("../../../../live/sessions"),
                "cache": other_profile_home / "cache",
                "plugins": root / "missing-plugin-cache",
                "history.jsonl": live_home / "history.jsonl",
                "models_cache.json": app_home / "target-state",
                "state_5.sqlite": app_home / "state_5.sqlite",
            }

            def install_links() -> None:
                for name, target in links.items():
                    path = app_home / name
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        shutil.rmtree(path)
                    path.symlink_to(target)

            install_links()
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            switch_removed = {
                name for name in links if not (app_home / name).is_symlink()
            }

            install_links()
            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            launcher_removed = {
                name for name in links if not (app_home / name).is_symlink()
            }

            self.assertEqual(set(links), switch_removed)
            self.assertEqual(switch_removed, launcher_removed)

    def test_internal_desktop_wrapper_and_switch_skip_unsafe_shareable_symlinks(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text("[features]\nmemory = true\n")
            external = root / "external-agents.md"
            external.write_text("# External safe support\n")
            (live_home / "live-share").mkdir()
            relative_target = root / "relative-share"
            relative_target.mkdir()
            (live_home / "prompts").symlink_to(Path("../relative-share"))
            (live_home / "rules").symlink_to(root / "missing-share")
            (live_home / "skills").symlink_to(live_home / "live-share")
            (live_home / "AGENTS.md").symlink_to(external)

            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            manifest["app_cli_path"] = str(app_wrapper)
            manifest_path.write_text(json.dumps(manifest))
            app_home = root / "store" / "homes" / "internal"
            app_home.mkdir(parents=True, exist_ok=True)

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            unsafe_names = {
                "prompts",
                "rules",
                "skills",
            }
            switch_state = {
                name: (app_home / name).is_symlink() for name in unsafe_names
            }
            self.assertEqual(
                {name: False for name in unsafe_names},
                switch_state,
            )
            self.assertTrue((app_home / "AGENTS.md").is_symlink())
            self.assertEqual(str(external), os.readlink(app_home / "AGENTS.md"))

            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            launcher_state = {
                name: (app_home / name).is_symlink() for name in unsafe_names
            }
            self.assertEqual(switch_state, launcher_state)
            self.assertTrue((app_home / "AGENTS.md").is_symlink())

    def test_internal_desktop_wrapper_preflights_config_before_home_mutation(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_config = live_home / "config.toml"
            live_config.write_text("[features]\nmemory = true\n")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            manifest["app_cli_path"] = str(app_wrapper)
            manifest_path.write_text(json.dumps(manifest))
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            app_home = root / "store" / "homes" / "internal"
            stale = app_home / "sessions"
            if stale.exists() or stale.is_symlink():
                if stale.is_symlink() or stale.is_file():
                    stale.unlink()
                else:
                    shutil.rmtree(stale)
            stale.symlink_to(live_home / "sessions")
            auth = app_home / "auth.json"
            auth.write_text('{"must":"remain"}\n')
            app_config = app_home / "config.toml"
            before_config = app_config.read_bytes()
            snapshot_paths = (
                app_home / "internal.plugin-support.config.toml",
                root
                / "store"
                / "profiles"
                / "internal"
                / "internal.plugin-support.config.toml",
            )
            before_snapshots = {
                path: path.read_bytes() for path in snapshot_paths if path.exists()
            }
            live_config.write_text('value = "unterminated\n')

            result = subprocess.run(
                [str(app_wrapper), "--version"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Invalid TOML", result.stderr)
            self.assertTrue(stale.is_symlink())
            self.assertEqual('{"must":"remain"}\n', auth.read_text())
            self.assertEqual(before_config, app_config.read_bytes())
            self.assertEqual(
                before_snapshots,
                {
                    path: path.read_bytes()
                    for path in snapshot_paths
                    if path.exists()
                },
            )

            live_config.write_text("[features]\nmemory = true\n")
            profile_config = (
                root / "store" / "profiles" / "internal" / "config.toml"
            )
            profile_config.write_text('value = "unterminated\n')
            result = subprocess.run(
                [str(app_wrapper), "--version"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Invalid TOML", result.stderr)
            self.assertTrue(stale.is_symlink())
            self.assertEqual('{"must":"remain"}\n', auth.read_text())
            self.assertEqual(before_config, app_config.read_bytes())
            self.assertEqual(
                before_snapshots,
                {
                    path: path.read_bytes()
                    for path in snapshot_paths
                    if path.exists()
                },
            )

    def test_internal_desktop_wrapper_persists_app_home_plugin_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            (root / "live" / "config.toml").write_text(
                "[marketplaces.openai-bundled]\n"
                'source_type = "local"\n'
                f'source = "{root / "openai-bundled"}"\n'
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(root / "store" / "bin" / "codex-internal-app")
            manifest_path.write_text(json.dumps(manifest))

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            app_home_config_path = root / "store" / "homes" / "internal" / "config.toml"
            app_home_config_path.write_text(
                'notify = ["turn-ended"]\n'
                + "\n"
                + app_home_config_path.read_text()
                + "\n"
                + "[features]\n"
                + "codex_hooks = true\n"
                + "\n"
                + "[mcp_servers.local-test]\n"
                + 'command = "local-mcp"\n'
                + "\n"
                + '[plugins."computer-use@openai-bundled"]\n'
                + "enabled = true\n"
                + "\n"
                + '[hooks.state."computer-use@openai-bundled:hooks.json:stop:0:0"]\n'
                + 'trusted_hash = "sha256:computer-use"\n'
            )

            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            live_config = (root / "live" / "config.toml").read_text()
            app_home_config = app_home_config_path.read_text()
            for config_text in (live_config, app_home_config):
                self.assertIn('notify = ["turn-ended"]', config_text)
                self.assertIn("[features]", config_text)
                self.assertIn("codex_hooks = true", config_text)
                self.assertIn("[mcp_servers.local-test]", config_text)
                self.assertIn('[plugins."computer-use@openai-bundled"]', config_text)
                self.assertIn(
                    '[hooks.state."computer-use@openai-bundled:hooks.json:stop:0:0"]',
                    config_text,
                )
            self.assertNotIn('model = "gpt-5.5-2026-04-24"', live_config)
            for snapshot_path in (
                root
                / "store"
                / "homes"
                / "internal"
                / "internal.plugin-support.config.toml",
                root
                / "store"
                / "profiles"
                / "internal"
                / "internal.plugin-support.config.toml",
            ):
                snapshot = snapshot_path.read_text()
                self.assertIn("[marketplaces.openai-bundled]", snapshot)
                self.assertIn('[plugins."computer-use@openai-bundled"]', snapshot)
                self.assertIn(
                    '[hooks.state."computer-use@openai-bundled:hooks.json:stop:0:0"]',
                    snapshot,
                )

    def test_internal_desktop_wrapper_uses_internal_plugin_skill_state_without_narrowing_support(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_config_path = live_home / "config.toml"
            live_config_path.write_text(
                'model = "official-runtime"\n'
                'personality = "friendly"\n'
                "\n"
                "[desktop]\n"
                "preventSleepWhileRunning = true\n"
                'appearanceTheme = "dark"\n'
                'followUpQueueMode = "off"\n'
                "\n"
                "[desktop.appearanceDarkChromeTheme]\n"
                'background = "#101010"\n'
                "\n"
                "[memories]\n"
                "enabled = true\n"
                "\n"
                "[apps.connector_test]\n"
                'command = "connector-test"\n'
                "\n"
                "[marketplaces.cy-codex-skills]\n"
                'source_type = "local"\n'
                f'source = "{root / "cy-codex-skills"}"\n'
                "\n"
                '[plugins."agent-kb@cy-codex-skills"]\n'
                "enabled = true\n"
                "\n"
                "[[skills.config]]\n"
                'path = "/tmp/agent-kb/SKILL.md"\n'
                "enabled = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(root / "store" / "bin" / "codex-internal-app")
            manifest_path.write_text(json.dumps(manifest))
            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            app_home_config_path = root / "store" / "homes" / "internal" / "config.toml"
            app_home_config_path.write_text(
                "# codex-switch: managed runtime config for profile internal\n"
                "\n"
                'model = "internal-runtime"\n'
                "\n"
                "[desktop]\n"
                'followUpQueueMode = "queue"\n'
                "\n"
                '[plugins."github@openai-curated"]\n'
                "enabled = true\n"
            )

            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            live_config = live_config_path.read_text()
            self.assertIn('model = "official-runtime"', live_config)
            self.assertIn('personality = "friendly"', live_config)
            self.assertIn("preventSleepWhileRunning = true", live_config)
            self.assertIn('appearanceTheme = "dark"', live_config)
            self.assertIn('followUpQueueMode = "queue"', live_config)
            self.assertIn("[desktop.appearanceDarkChromeTheme]", live_config)
            self.assertIn("[memories]", live_config)
            self.assertIn("[apps.connector_test]", live_config)
            self.assertIn("[marketplaces.cy-codex-skills]", live_config)
            self.assertNotIn('[plugins."agent-kb@cy-codex-skills"]', live_config)
            self.assertNotIn("[[skills.config]]", live_config)
            self.assertIn('[plugins."github@openai-curated"]', live_config)
            self.assertNotIn('model = "internal-runtime"', live_config)
            for snapshot_path in (
                app_home_config_path.parent
                / "internal.plugin-support.config.toml",
                root
                / "store"
                / "profiles"
                / "internal"
                / "internal.plugin-support.config.toml",
            ):
                snapshot = snapshot_path.read_text()
                self.assertIn("[marketplaces.cy-codex-skills]", snapshot)
                self.assertNotIn(
                    '[plugins."agent-kb@cy-codex-skills"]',
                    snapshot,
                )
                self.assertNotIn("[[skills.config]]", snapshot)
                self.assertIn('[plugins."github@openai-curated"]', snapshot)

    def test_internal_desktop_wrapper_preserves_official_personality(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_home_config = live_home / "config.toml"
            live_home_config.write_text(
                'model = "official-runtime"\n'
                'personality = "friendly"\n'
                "\n"
                "[features]\n"
                "memory = true\n"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(root / "store" / "bin" / "codex-internal-app")
            manifest_path.write_text(json.dumps(manifest))

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            app_home_config_path = root / "store" / "homes" / "internal" / "config.toml"
            app_home_config_path.write_text(
                app_home_config_path.read_text()
                + "\n"
                + "[mcp_servers.local-test]\n"
                + 'command = "local-mcp"\n'
            )

            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            live_config = live_home_config.read_text()
            self.assertIn('model = "official-runtime"', live_config)
            self.assertIn('personality = "friendly"', live_config)
            self.assertIn("[mcp_servers.local-test]", live_config)

    def test_internal_desktop_wrapper_syncs_desktop_global_settings_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_home_config = live_home / "config.toml"
            live_home_config.write_text("[features]\nmemory = true\n")
            (live_home / ".codex-global-state.json").write_text(
                json.dumps(
                    desktop_global_state_payload(
                        bounds_width=1300,
                        hotkey="cmd+shift+4",
                        auto_context=True,
                        prompt_history_label="official-before-switch",
                    )
                )
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(root / "store" / "bin" / "codex-internal-app")
            manifest_path.write_text(json.dumps(manifest))

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")

            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            app_home = root / "store" / "homes" / "internal"
            (live_home / ".codex-global-state.json").write_text(
                json.dumps(
                    desktop_global_state_payload(
                        bounds_width=1550,
                        hotkey="cmd+shift+5",
                        auto_context=False,
                        prompt_history_label="official-after-switch",
                    )
                )
            )
            (app_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "queued-follow-ups": [
                            {"thread": "internal-target-queued"}
                        ],
                        "selected-remote-host-id": "internal-target-remote-host",
                        "electron-persisted-atom-state": {
                            "prompt-history": {
                                "entries": ["internal-target-prompt"]
                            },
                            "heartbeat-thread-permissions-by-id": {
                                "internal-target-thread": "full"
                            },
                            "composer-prompt-drafts-v1": {
                                "internal-target-thread": "draft"
                            },
                            "unread-thread-ids-by-host-v1": {
                                "internal-target-host": [
                                    "internal-target-thread"
                                ]
                            },
                            "remote-thread-summaries:remote": {
                                "title": "internal-target-summary"
                            },
                        },
                    }
                )
            )

            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            app_state = json.loads((app_home / ".codex-global-state.json").read_text())
            self.assertEqual(1550, app_state["electron-main-window-bounds"]["width"])
            self.assertEqual("cmd+shift+5", app_state["appshotHotkey"])
            self.assertEqual(
                False,
                app_state["electron-persisted-atom-state"][
                    "composer-auto-context-enabled"
                ],
            )
            self.assertEqual(
                {"entries": ["internal-target-prompt"]},
                app_state["electron-persisted-atom-state"]["prompt-history"],
            )
            self.assertEqual(
                [{"thread": "internal-target-queued"}],
                app_state["queued-follow-ups"],
            )
            self.assertEqual(
                "internal-target-remote-host",
                app_state["selected-remote-host-id"],
            )
            self.assertFalse((app_home / ".credentials.json").exists())

    def test_internal_desktop_wrapper_syncs_rules_and_ignores_unknown_pets(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            live_home_config = live_home / "config.toml"
            live_home_config.write_text("[features]\nmemory = true\n")
            pets = live_home / "pets"
            pets.mkdir()
            (pets / "settings.json").write_text('{"enabled":true}\n')
            rules = live_home / "rules"
            rules.mkdir()
            (rules / "settings.json").write_text('{"enabled":true}\n')
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(root / "store" / "bin" / "codex-internal-app")
            manifest_path.write_text(json.dumps(manifest))

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            app_wrapper = root / "store" / "bin" / "codex-internal-app"
            app_home = root / "store" / "homes" / "internal"
            if (app_home / "rules").exists() or (app_home / "rules").is_symlink():
                if (app_home / "rules").is_symlink():
                    (app_home / "rules").unlink()
                else:
                    shutil.rmtree(app_home / "rules")
            (rules / "settings.json").write_text('{"enabled":false}\n')

            subprocess.run(
                [str(app_wrapper), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertTrue((app_home / "rules").is_symlink())
            self.assertEqual(
                '{"enabled":false}\n',
                (app_home / "rules" / "settings.json").read_text(),
            )
            self.assertFalse((app_home / "pets").exists())

    def test_internal_desktop_wrapper_isolates_response_runtime_state(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            live_home = root / "live"
            (live_home / "config.toml").write_text(
                'profile = "internal"\n'
                "\n"
                "[profiles.internal]\n"
                "\n"
                "[plugins.local]\n"
                "enabled = true\n"
            )
            for dirname in (
                "sessions",
                "archived_sessions",
                "browser",
                "log",
                "tmp",
                ".tmp",
                "process_manager",
                "node_repl",
                "shell_snapshots",
                "ambient-suggestions",
                "agent-kb",
                "cache",
                "computer-use",
                "model-catalogs",
                "plugins",
                "sqlite",
            ):
                (live_home / dirname).mkdir()
            for filename in (
                "history.jsonl",
                "session_index.jsonl",
                "state_5.sqlite",
                "state_5.sqlite-shm",
                "state_5.sqlite-wal",
                "state_5.sqlite.corrupt.20260522-173044",
                "state_5.sqlite-shm.corrupt.20260522-173044",
                "state_5.sqlite-wal.corrupt.20260522-173044",
                ".credentials.json",
                ".codex-global-state.json",
                "models_cache.json",
                "version.json",
            ):
                (live_home / filename).write_text(f"{filename}\n")

            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "internal" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["app_cli_path"] = str(root / "store" / "bin" / "codex-internal-app")
            manifest_path.write_text(json.dumps(manifest))

            self.run_switcher(root, "switch", "internal", "--skip-launchctl")
            app_home = root / "store" / "homes" / "internal"
            app_home.mkdir(parents=True, exist_ok=True)
            for stale_name in ("sessions", "state_5.sqlite"):
                (app_home / stale_name).symlink_to(
                    live_home / stale_name,
                    target_is_directory=(live_home / stale_name).is_dir(),
                )

            result = subprocess.run(
                [str(root / "store" / "bin" / "codex-internal-app"), "--version"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            self.assertIn("internal-codex", result.stdout)
            excluded_names = (
                "sessions",
                "archived_sessions",
                "browser",
                "log",
                "tmp",
                ".tmp",
                "process_manager",
                "node_repl",
                "shell_snapshots",
                "ambient-suggestions",
                "agent-kb",
                "cache",
                "computer-use",
                "model-catalogs",
                "plugins",
                "sqlite",
                "history.jsonl",
                "session_index.jsonl",
                "state_5.sqlite",
                "state_5.sqlite-shm",
                "state_5.sqlite-wal",
                "state_5.sqlite.corrupt.20260522-173044",
                "state_5.sqlite-shm.corrupt.20260522-173044",
                "state_5.sqlite-wal.corrupt.20260522-173044",
                ".credentials.json",
                ".codex-global-state.json",
                "models_cache.json",
                "version.json",
            )
            for name in excluded_names:
                self.assertFalse(
                    (app_home / name).is_symlink(),
                    f"{name} must not be shared from live CODEX_HOME",
                )
            self.assertFalse((app_home / "auth.json").exists())

    def test_wrapper_one_key_official_checks_update_before_switch(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            result = self.run_wrapper(
                root,
                "official",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertIn("Checking Codex CLI update for openai-official", output)
            self.assertIn(f"Official profile codex: {official_codex}", output)
            self.assertIn("Official login: missing", output)
            self.assertTrue(
                (root / "store" / "profiles" / "openai-official" / "auth.json").exists()
            )
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "openai-official")

    def test_wrapper_one_key_official_can_skip_auto_login(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            result = self.run_wrapper(
                root,
                "official",
                "--skip-login",
                "--skip-update-check",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertNotIn("Official login: missing", output)
            self.assertFalse(
                (root / "store" / "profiles" / "openai-official" / "auth.json").exists()
            )
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "openai-official")

    def test_wrapper_one_key_can_skip_update_check(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            result = self.run_wrapper(
                root,
                "official",
                "--skip-launchctl",
                "--skip-update-check",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertNotIn("Checking Codex CLI update", output)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "openai-official")

    def test_wrapper_profile_dry_run_allows_empty_switch_args_on_bash_32(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--dry-run",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("Outcome: DRY RUN OK", output)
            self.assertNotIn("unbound variable", output)

    def test_wrapper_split_shortcut_routes_supported_pairing(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            active_path = root / "store" / "active.json"
            active_before = active_path.read_bytes() if active_path.exists() else None

            result = self.run_wrapper(
                root,
                "split",
                "--dry-run",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn("Outcome: DRY RUN OK", output)
            self.assertIn("CLI profile: internal", output)
            self.assertIn("App profile: openai-official", output)
            self.assertEqual(
                active_before,
                active_path.read_bytes() if active_path.exists() else None,
            )

    def test_packaged_wrapper_split_keep_version_preview_uses_no_checkout_code(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            receipt = build_release_bundle(
                Path(__file__).parents[1],
                root / "release",
            )
            packaged_wrapper = receipt.package_dir / "scripts" / "codex-switch"
            packaged_env = dict(env)
            packaged_env.pop("PYTHONPATH", None)
            packaged_env.pop("CODEX_SWITCH_SCRIPT", None)
            packaged_env["CODEX_SWITCH_SCRIPT"] = packaged_env[
                "CODEX_SWITCH_TEST_DESKTOP_DRIVER"
            ]
            packaged_env["CODEX_SWITCH_TEST_REAL_SWITCHER"] = str(
                receipt.package_dir / "scripts" / "codex_profile_switch.py"
            )
            packaged_env["CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP"] = "1"

            result = subprocess.run(
                [
                    str(packaged_wrapper),
                    "--store-dir",
                    str(root / "store"),
                    "--live-codex-home",
                    str(root / "live"),
                    "--launch-agent-path",
                    str(root / "agent.plist"),
                    "split",
                    "--keep-version",
                    "--dry-run",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=packaged_env,
                cwd=root,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn("Outcome: DRY RUN OK", output)
            self.assertIn("CLI profile: internal", output)
            self.assertIn("App profile: openai-official", output)
            self.assertEqual(
                hashlib.sha256(WRAPPER.read_bytes()).hexdigest(),
                hashlib.sha256(packaged_wrapper.read_bytes()).hexdigest(),
            )

    def test_wrapper_keep_version_is_rejected_outside_split(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            result = self.run_wrapper(
                root,
                "internal",
                "--keep-version",
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(2, result.returncode, output)
            self.assertIn(
                "--keep-version is only supported with codex-switch split.",
                output,
            )
            self.assertNotIn("Dry-run plan", output)
            self.assertFalse((root / "store").exists())

    def test_wrapper_split_rejects_app_profile_override(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            result = self.run_wrapper(
                root,
                "split",
                "--app-profile",
                "internal",
                "--dry-run",
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(2, result.returncode, output)
            self.assertIn(
                "codex-switch split already selects the official App; "
                "do not pass --app-profile.",
                output,
            )
            self.assertNotIn("Dry-run plan", output)
            self.assertFalse((root / "store").exists())

    def test_wrapper_one_key_split_reports_both_profiles_after_apply(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _internal_codex, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            switch_args_log = root / "split-switch-args.jsonl"
            fake_switcher = root / "split-switcher.py"
            fake_switcher.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "store = Path(args[args.index('--store-dir') + 1])\n"
                "with Path(os.environ['CODEX_SWITCH_SPLIT_ARGS_LOG']).open('a') as log:\n"
                "    log.write(json.dumps(args) + '\\n')\n"
                "if '--dry-run' in args:\n"
                "    print('CLI profile: internal')\n"
                "    print('App profile: openai-official')\n"
                "    print('App action: preserve')\n"
                "    raise SystemExit(0)\n"
                "store.mkdir(parents=True, exist_ok=True)\n"
                "active = {\n"
                "    'profile': 'internal',\n"
                "    'cli_profile': 'internal',\n"
                "    'app_profile': 'openai-official',\n"
                "    'codex_home': str(store / 'homes' / 'internal'),\n"
                f"    'app_cli_path': {str(official_codex)!r},\n"
                "}\n"
                "(store / 'active.json').write_text(json.dumps(active))\n"
                "print('App action: preserve')\n"
                "print('Switched to profile internal')\n"
            )
            fake_switcher.chmod(0o755)
            env["CODEX_SWITCH_SCRIPT"] = str(fake_switcher)
            env["CODEX_SWITCH_SPLIT_ARGS_LOG"] = str(switch_args_log)

            result = self.run_wrapper(
                root,
                "internal",
                "--app-profile",
                "official",
                "--skip-update-check",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                "--skip-login",
                "--skip-launchctl",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn("Update check: skipped by command option.", output)
            self.assertIn("Plugin repair: skipped by command option.", output)
            final_result = output.rsplit("== Final result ==", 1)[-1]
            self.assertIn("Profile: internal", final_result)
            self.assertIn("CLI profile: internal", final_result)
            self.assertIn("App profile: openai-official", final_result)
            self.assertNotIn("Restart ChatGPT", final_result)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual("internal", active["cli_profile"])
            self.assertEqual("openai-official", active["app_profile"])
            forwarded = [json.loads(line) for line in switch_args_log.read_text().splitlines()]
            self.assertEqual(3, len(forwarded))
            for args in forwarded[:2]:
                self.assertEqual("official", args[args.index("--app-profile") + 1])
            self.assertIn("sync-shared", forwarded[2])
            self.assertNotIn("--app-profile", forwarded[2])
            self.assertIn("== Shared configuration ==", output)

    def test_wrapper_apply_progress_is_visible_before_switcher_exits(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _internal_codex, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            release = root / "release-progress-switcher"
            fake_switcher = root / "progress-switcher.py"
            fake_switcher.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys, time\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "store = Path(args[args.index('--store-dir') + 1])\n"
                f"release = Path({str(release)!r})\n"
                "print('CLI profile: internal')\n"
                "print('App profile: openai-official')\n"
                "print('App action: preserve')\n"
                "if '--dry-run' in args:\n"
                "    raise SystemExit(0)\n"
                "print('Applying shared support [1/2]')\n"
                "deadline = time.monotonic() + 5.0\n"
                "while not release.exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.01)\n"
                "if not release.exists():\n"
                "    raise SystemExit(43)\n"
                "store.mkdir(parents=True, exist_ok=True)\n"
                "active = {\n"
                "    'profile': 'internal',\n"
                "    'cli_profile': 'internal',\n"
                "    'app_profile': 'openai-official',\n"
                "    'codex_home': str(store / 'homes' / 'internal'),\n"
                f"    'app_cli_path': {str(official_codex)!r},\n"
                "}\n"
                "(store / 'active.json').write_text(json.dumps(active))\n"
                "print('Applying shared support [2/2]')\n"
            )
            fake_switcher.chmod(0o755)
            env["CODEX_SWITCH_SCRIPT"] = str(fake_switcher)
            clean_env = dict(env)
            clean_env.pop("CODEX_CLI_PATH", None)
            clean_env.pop("CODEX_SWITCH_HOME", None)
            clean_env["CODEX_SWITCH_SKIP_SHELL_BOOTSTRAP"] = "1"
            command = [
                str(WRAPPER),
                "--store-dir",
                str(root / "store"),
                "--live-codex-home",
                str(root / "live"),
                "--launch-agent-path",
                str(root / "agent.plist"),
                "internal",
                "--app-profile",
                "official",
                "--skip-update-check",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                "--skip-login",
                "--skip-launchctl",
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=clean_env,
                cwd=root,
            )
            self.assertIsNotNone(process.stdout)
            progress = b"Applying shared support [1/2]"
            observed = bytearray()
            deadline = time.monotonic() + 2.0
            try:
                while progress not in observed and time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    ready, _, _ = select.select(
                        [process.stdout],
                        [],
                        [],
                        remaining,
                    )
                    if not ready:
                        break
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    observed.extend(chunk)
                was_visible_while_running = (
                    progress in observed and process.poll() is None
                )
            finally:
                release.write_text("continue\n")
                try:
                    remaining_output, _ = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    remaining_output, _ = process.communicate()
            complete_output = bytes(observed) + remaining_output
            self.assertTrue(
                was_visible_while_running,
                complete_output.decode(errors="replace"),
            )
            self.assertEqual(
                0,
                process.returncode,
                complete_output.decode(errors="replace"),
            )

    def test_wrapper_split_rebind_retains_app_restart_guidance(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _internal_codex, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            fake_switcher = root / "split-rebind-switcher.py"
            fake_switcher.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "store = Path(args[args.index('--store-dir') + 1])\n"
                "print('CLI profile: internal')\n"
                "print('App profile: openai-official')\n"
                "print('App action: rebind')\n"
                "if '--dry-run' in args:\n"
                "    raise SystemExit(0)\n"
                "store.mkdir(parents=True, exist_ok=True)\n"
                "active = {\n"
                "    'profile': 'internal',\n"
                "    'cli_profile': 'internal',\n"
                "    'app_profile': 'openai-official',\n"
                "    'codex_home': str(store / 'homes' / 'internal'),\n"
                f"    'app_cli_path': {str(official_codex)!r},\n"
                "}\n"
                "(store / 'active.json').write_text(json.dumps(active))\n"
                "print('Switched to profile internal')\n"
            )
            fake_switcher.chmod(0o755)
            env["CODEX_SWITCH_SCRIPT"] = str(fake_switcher)

            result = self.run_wrapper(
                root,
                "internal",
                "--app-profile",
                "official",
                "--skip-update-check",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                "--skip-login",
                "--skip-launchctl",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            final_result = output.rsplit("== Final result ==", 1)[-1]
            self.assertIn(
                "Restart ChatGPT if it was already running.",
                final_result,
            )

    def test_wrapper_prints_final_action_required_when_doctor_fails(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
            )
            manifest_path = root / "store" / "profiles" / "openai-official" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            doctor_mismatch_app_cli = root / "doctor-mismatch-app-cli"
            write_fake_codex(doctor_mismatch_app_cli, "doctor-mismatch-app-cli")
            manifest["app_cli_path"] = str(doctor_mismatch_app_cli)
            manifest_path.write_text(json.dumps(manifest))
            (root / "agent.plist").write_bytes(
                plistlib.dumps(
                    {
                        "Label": "test",
                        "ProgramArguments": [
                            "/bin/launchctl",
                            "setenv",
                            "CODEX_CLI_PATH",
                            str(official_codex),
                        ],
                        "RunAtLoad": True,
                    }
                )
            )

            result = self.run_wrapper(
                root,
                "official",
                "--skip-login",
                "--skip-update-check",
                "--skip-shim",
                "--skip-app-cli",
                "--skip-plugin-repair",
                "--skip-verify",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("== Final result ==", output)
            self.assertIn("Outcome: ACTION REQUIRED", output)
            self.assertIn("Switch: succeeded", output)
            self.assertIn("Doctor: failed", output)
            self.assertIn("Next steps:", output)
            self.assertIn("Re-run doctor: codex-switch doctor", output)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "openai-official")

    def test_wrapper_internal_update_check_failure_does_not_block_switch(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = "http://127.0.0.1:1/latest"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertIn("Checking Codex CLI update for internal", output)
            self.assertIn(f"Internal profile codex: {internal_codex}", output)
            self.assertIn(
                "Update check did not complete; continuing with switch.",
                output,
            )
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "internal")

    def test_wrapper_split_still_runs_internal_update_check(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = "http://127.0.0.1:1/latest"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--app-profile",
                "official",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertIn("Checking Codex CLI update for internal", output)
            self.assertIn(f"Internal profile codex: {internal_codex}", output)
            self.assertIn(
                "Update check did not complete; continuing with switch.",
                output,
            )
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual("internal", active["cli_profile"])
            self.assertEqual("openai-official", active["app_profile"])

    def test_wrapper_split_shortcut_still_runs_internal_update_check(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = "http://127.0.0.1:1/latest"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "split",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                env=env,
            )

            output = result.stdout + result.stderr
            self.assertIn("Checking Codex CLI update for internal", output)
            self.assertIn(f"Internal profile codex: {internal_codex}", output)
            self.assertIn(
                "Update check did not complete; continuing with switch.",
                output,
            )
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual("internal", active["cli_profile"])
            self.assertEqual("openai-official", active["app_profile"])

    def test_wrapper_split_keep_version_skips_internal_update_check(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = "http://127.0.0.1:1/latest"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "split",
                "--keep-version",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn("Update check: skipped by command option.", output)
            self.assertNotIn("Checking Codex CLI update for internal", output)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual("internal", active["cli_profile"])
            self.assertEqual("openai-official", active["app_profile"])

    def test_wrapper_split_auto_update_uses_cli_only_promotion_and_runtime_smoke(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            updated_codex = root / "updated-codex"
            write_fake_app_server_smoke_codex(
                updated_codex,
                version="codex-cli 1.1.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            verify_args = root / "verify-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=updated_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            env["CODEX_SWITCH_TEST_VERIFY_ARGS_LOG"] = str(verify_args)
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "split",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn(
                "Auto-update: running staged CLI-only internal promotion",
                output,
            )
            self.assert_update_scope(root, env, "cli-only")
            verification_argv = shlex.split(verify_args.read_text())
            self.assertIn("--runtime-smoke", verification_argv)
            self.assertNotIn("--app-server-smoke", verification_argv)
            self.assertIn("Runtime smoke: passed", output)
            self.assertNotIn("App-server smoke: passed", output)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(str(official_codex), active["app_cli_path"])
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="1.1.0",
            )

    def test_wrapper_direct_update_internal_retains_full_promotion(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            updated_codex = root / "updated-codex"
            write_fake_app_server_smoke_codex(
                updated_codex,
                version="codex-cli 1.1.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=updated_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "1.1.0",
                env=env,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assert_update_scope(root, env, "full")
            self.assertIn(
                "update-internal: capability and parity receipts verified.",
                output,
            )

    def test_internal_check_compares_with_official_stable_without_helper(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 0.144.6",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            helper_called = root / "helper-called"
            curl_args = root / "curl-args"
            write_fake_release_redirects(
                fake_tools / "curl",
                {
                    "internal.example/latest": (
                        "https://github.com/SDGLBL/codex/releases/tag/"
                        "internal-rust-v0.144.6"
                    ),
                    "official.example/latest": (
                        "https://github.com/openai/codex/releases/tag/"
                        "rust-v0.145.0"
                    ),
                },
                args_log=curl_args,
            )
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"touch {helper_called}\n"
                "exit 0\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = (
                "https://internal.example/latest"
            )
            env["CODEX_SWITCH_OFFICIAL_LATEST_URL"] = (
                "https://official.example/latest"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )
            store_before = filesystem_snapshot(root / "store")

            result = self.run_wrapper(
                root,
                "check-update",
                "internal",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn("Official stable comparison (internal)", output)
            self.assertIn("Current internal CLI: 0.144.6", output)
            self.assertIn(
                "Latest openai/codex stable: 0.145.0 (rust-v0.145.0)",
                output,
            )
            self.assertIn("Status: behind upstream stable", output)
            self.assertIn(
                "internal updates remain governed by the internal release source",
                output,
            )
            self.assertFalse(helper_called.exists())
            self.assertEqual(store_before, filesystem_snapshot(root / "store"))
            curl_calls = curl_args.read_text().splitlines()
            self.assertTrue(
                any(
                    "--connect-timeout 3 --max-time 20 "
                    "https://internal.example/latest" in call
                    for call in curl_calls
                ),
                curl_calls,
            )
            self.assertTrue(
                any(
                    "--connect-timeout 3 --max-time 8 "
                    "https://official.example/latest" in call
                    for call in curl_calls
                ),
                curl_calls,
            )

    def test_official_check_compares_bundled_prerelease_with_stable(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                official_codex,
                version="codex-cli 0.146.0-alpha.3",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            curl_args = root / "curl-args"
            write_fake_release_redirects(
                fake_tools / "curl",
                {
                    "official.example/latest": (
                        "https://github.com/openai/codex/releases/tag/"
                        "rust-v0.145.0"
                    ),
                },
                args_log=curl_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_OFFICIAL_LATEST_URL"] = (
                "https://official.example/latest"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )
            store_before = filesystem_snapshot(root / "store")

            result = self.run_wrapper(
                root,
                "check-update",
                "official",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn(
                "Update: managed by ChatGPT.app; codex-switch does not modify "
                "the app bundle.",
                output,
            )
            self.assertIn("Official stable comparison (openai-official)", output)
            self.assertIn("Current official CLI: 0.146.0-alpha.3", output)
            self.assertIn("Status: ahead of upstream stable", output)
            self.assertIn("Current channel: prerelease or vendor build", output)
            self.assertEqual(store_before, filesystem_snapshot(root / "store"))
            self.assertTrue(
                any(
                    "--connect-timeout 3 --max-time 8 "
                    "https://official.example/latest" in call
                    for call in curl_args.read_text().splitlines()
                )
            )

    def test_official_unparseable_version_is_nonblocking(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                official_codex,
                version="codex-cli unknown",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            write_fake_release_redirects(
                fake_tools / "curl",
                {
                    "official.example/latest": (
                        "https://github.com/openai/codex/releases/tag/"
                        "rust-v0.145.0"
                    ),
                },
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_OFFICIAL_LATEST_URL"] = (
                "https://official.example/latest"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )
            store_before = filesystem_snapshot(root / "store")

            result = self.run_wrapper(
                root,
                "check-update",
                "official",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn(
                "Official stable comparison: unavailable "
                "(selected profile CLI version is not parseable)",
                output,
            )
            self.assertEqual(store_before, filesystem_snapshot(root / "store"))

    def test_internal_switch_compares_after_successful_auto_update(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 0.144.6",
            )
            successor = root / "successor-codex"
            write_fake_app_server_smoke_codex(
                successor,
                version="codex-cli 0.145.1",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_release_redirects(
                fake_tools / "curl",
                {
                    "internal.example/latest": (
                        "https://github.com/SDGLBL/codex/releases/tag/"
                        "internal-rust-v0.145.1"
                    ),
                    "official.example/latest": (
                        "https://github.com/openai/codex/releases/tag/"
                        "rust-v0.145.0"
                    ),
                },
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=successor,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = (
                "https://internal.example/latest"
            )
            env["CODEX_SWITCH_OFFICIAL_LATEST_URL"] = (
                "https://official.example/latest"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="0.145.1",
            )
            self.assertIn("Official stable comparison (internal)", output)
            self.assertLess(
                output.index("Auto-update: installed 0.145.1"),
                output.index("Official stable comparison (internal)"),
            )
            self.assertIn("Current internal CLI: 0.145.1", output)
            self.assertIn("Status: ahead of upstream stable", output)

    def test_official_stable_lookup_failure_is_nonblocking(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 0.144.6",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            write_fake_release_redirects(
                fake_tools / "curl",
                {
                    "internal.example/latest": (
                        "https://github.com/SDGLBL/codex/releases/tag/"
                        "internal-rust-v0.144.6"
                    ),
                    "official.example/latest": None,
                },
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = (
                "https://internal.example/latest"
            )
            env["CODEX_SWITCH_OFFICIAL_LATEST_URL"] = (
                "https://official.example/latest"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn(
                "Official stable comparison: unavailable (release lookup failed)",
                output,
            )
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual("internal", active["profile"])

    def test_prerelease_tag_is_not_used_as_stable_baseline(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 0.144.6",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            write_fake_release_redirects(
                fake_tools / "curl",
                {
                    "internal.example/latest": (
                        "https://github.com/SDGLBL/codex/releases/tag/"
                        "internal-rust-v0.144.6"
                    ),
                    "official.example/latest": (
                        "https://github.com/openai/codex/releases/tag/"
                        "rust-v0.146.0-alpha.6"
                    ),
                },
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_INTERNAL_LATEST_URL"] = (
                "https://internal.example/latest"
            )
            env["CODEX_SWITCH_OFFICIAL_LATEST_URL"] = (
                "https://official.example/latest"
            )
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "check-update",
                "internal",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn(
                "Official stable comparison: unavailable "
                "(latest upstream tag is not a stable rust-v semantic version)",
                output,
            )
            self.assertNotIn("Latest openai/codex stable: 0.146.0-alpha.6", output)

    def test_skip_update_check_skips_official_release_lookup(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            curl_called = root / "curl-called"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                f"touch {curl_called}\n"
                "exit 22\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-update-check",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-verify",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assertIn("Update check: skipped by command option.", output)
            self.assertNotIn("Official stable comparison", output)
            self.assertFalse(curl_called.exists())

    def test_internal_update_check_skips_blocked_latest_on_fallback(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 0.142.4",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v0.142.5\n"
                "EOF\n",
            )
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"printf '%s\\n' \"$*\" > {update_args}\n"
                "exit 0\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(root, "check-update", "internal", env=env)

            output = result.stdout + result.stderr
            self.assertIn("Latest release tag: internal-rust-v0.142.5", output)
            self.assertIn("blocked", output)
            self.assertIn("Update: skipped", output)
            self.assertIn("0.142.4", output)
            self.assertFalse(update_args.exists())

    def test_one_key_internal_auto_update_pins_blocked_current_to_fallback(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 0.142.5",
            )
            fallback_codex = root / "fallback-codex"
            write_fake_app_server_smoke_codex(
                fallback_codex,
                version="codex-cli 0.142.4",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v0.142.5\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=fallback_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertIn("Latest release tag: internal-rust-v0.142.5", output)
            self.assertIn("blocked", output)
            self.assertIn(
                "Auto-update: running staged parity-safe internal promotion",
                output,
            )
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="0.142.4",
            )

    def test_one_key_internal_auto_update_resumes_for_successor_latest(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 0.142.4",
            )
            successor_codex = root / "successor-codex"
            write_fake_app_server_smoke_codex(
                successor_codex,
                version="codex-cli 0.142.6",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v0.142.6\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=successor_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                allow_switch_script=True,
            )

            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="0.142.6",
            )

    def test_update_internal_command_pins_blocked_latest_without_explicit_version(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 0.142.5",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v0.142.5\n"
                "EOF\n",
            )
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"printf '%s\\n' \"$*\" > {update_args}\n"
                "exit 0\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(root, "update-internal", "--dry-run", env=env)

            output = result.stdout + result.stderr
            self.assertIn(
                "Current internal release status: blocked by codex-switch policy",
                output,
            )
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="0.142.4",
                expected_flags=("--dry-run",),
            )

    def test_update_internal_command_keeps_healthy_newer_current(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 1.2.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"printf '%s\\n' \"$*\" > {update_args}\n"
                "exit 0\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(root, "update-internal", env=env)

            output = result.stdout + result.stderr
            self.assertIn(
                "healthy current 1.2.0 is newer than reported latest 1.1.0",
                output,
            )
            self.assertIn("update-internal: no automatic update required", output)
            self.assertFalse(update_args.exists())

    def test_update_internal_binds_helper_and_postcondition_to_profile_binary(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            custom_dir = root / "custom-internal-bin"
            custom_dir.mkdir()
            custom_codex = custom_dir / "codex"
            write_fake_app_server_smoke_codex(
                custom_codex,
                version="codex-cli 1.0.0",
            )
            updated_codex = root / "custom-updated-codex"
            write_fake_app_server_smoke_codex(
                updated_codex,
                version="codex-cli 1.1.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=updated_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )
            manifest_path = (
                root / "store" / "profiles" / "internal" / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["codex_bin"] = str(custom_codex)
            manifest_path.write_text(json.dumps(manifest))

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "1.1.0",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=custom_codex,
                version="1.1.0",
            )
            self.assertIn(
                "codex-cli 1.1.0",
                subprocess.run(
                    [str(custom_codex), "--version"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                ).stdout,
            )
            self.assertIn("App-server smoke: passed", output)

    def test_update_internal_expands_store_before_manifest_binding(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=internal_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )
            home = root / "home"
            home.mkdir()
            store = home / ".codex-switch"
            shutil.move(root / "store", store)
            env["HOME"] = str(home)
            env["CODEX_SWITCH_SKIP_SELF_UPDATE"] = "1"
            env["CODEX_SWITCH_PYTHON"] = (
                shutil.which("python3.12")
                or shutil.which("python3.11")
                or sys.executable
            )

            for raw_store in ("~/.codex-switch", "$HOME/.codex-switch"):
                with self.subTest(raw_store=raw_store):
                    update_args.unlink(missing_ok=True)
                    env["CODEX_SWITCH_HOME"] = raw_store
                    result = subprocess.run(
                        [
                            str(WRAPPER),
                            "update-internal",
                            "--version",
                            "1.1.0",
                            "--dry-run",
                        ],
                        check=False,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )

                    output = result.stdout + result.stderr
                    self.assertEqual(0, result.returncode, output)
                    self.assert_staged_update_helper_args(
                        update_args,
                        bound_bin=internal_codex,
                        version="1.1.0",
                        expected_flags=("--dry-run",),
                    )

    def test_update_internal_rejects_option_as_version_before_helper(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, _, env = self.prepare_profiles(root)
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            helper_called = root / "helper-called"
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"touch {helper_called}\n"
                "exit 0\n",
            )
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "--dry-run",
                env=env,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn(
                "--version requires a non-option value",
                result.stdout + result.stderr,
            )
            self.assertFalse(helper_called.exists())

    def test_update_internal_rejects_option_as_install_dir_before_helper(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, _, env = self.prepare_profiles(root)
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            helper_called = root / "helper-called"
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"touch {helper_called}\n"
                "exit 0\n",
            )
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "1.1.0",
                "--install-dir",
                "--dry-run",
                env=env,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn(
                "--install-dir requires a non-option value",
                result.stdout + result.stderr,
            )
            self.assertFalse(helper_called.exists())

    def test_update_internal_rejects_dry_run_as_helper_option_value(
        self,
    ) -> None:
        value_options = (
            "--internal-bin",
            "--official-bin",
            "--installer-url",
            "--latest-url",
            "--model",
            "--azure-base-url",
            "--codex-install-base-url",
        )
        for option in value_options:
            with self.subTest(option=option):
                temp_dir, root = self.make_workspace()
                with temp_dir:
                    _, _, env = self.prepare_profiles(root)
                    fake_tools = root / "fake-tools"
                    fake_tools.mkdir()
                    helper_called = root / "helper-called"
                    write_fake_script(
                        fake_tools / "codex-env-setup",
                        "#!/usr/bin/env sh\n"
                        f"touch {helper_called}\n"
                        "exit 0\n",
                    )
                    env["CODEX_SWITCH_ENV_SETUP"] = str(
                        fake_tools / "codex-env-setup"
                    )

                    result = self.run_wrapper(
                        root,
                        "update-internal",
                        "--version",
                        "1.1.0",
                        option,
                        "--dry-run",
                        env=env,
                        check=False,
                    )

                    self.assertEqual(2, result.returncode)
                    self.assertIn(
                        f"{option} requires a non-option value",
                        result.stdout + result.stderr,
                    )
                    self.assertFalse(helper_called.exists())

    def test_update_internal_normalizes_equals_options(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            updated_codex = root / "equals-updated-codex"
            write_fake_app_server_smoke_codex(
                updated_codex,
                version="codex-cli 1.1.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=updated_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version=1.1.0",
                f"--install-dir={internal_codex.parent}",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(0, result.returncode, output)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="1.1.0",
            )
            self.assertIn("App-server smoke: passed", output)

    def test_update_internal_rejects_noncanonical_profile_binary_before_helper(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            custom_dir = root / "custom-internal-bin"
            custom_dir.mkdir()
            custom_codex = custom_dir / "codex-internal"
            write_fake_app_server_smoke_codex(
                custom_codex,
                version="codex-cli 1.0.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            helper_called = root / "helper-called"
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"touch {helper_called}\n"
                "exit 0\n",
            )
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )
            manifest_path = (
                root / "store" / "profiles" / "internal" / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["codex_bin"] = str(custom_codex)
            manifest_path.write_text(json.dumps(manifest))

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "1.1.0",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn(
                "profile codex_bin basename must be codex",
                result.stdout + result.stderr,
            )
            self.assertFalse(helper_called.exists())

    def test_update_internal_rejects_invalid_existing_profile_manifest(
        self,
    ) -> None:
        manifest_values = (
            "{not-json",
            json.dumps({"name": "internal"}),
        )
        for manifest_text in manifest_values:
            with self.subTest(manifest_text=manifest_text):
                temp_dir, root = self.make_workspace()
                with temp_dir:
                    _, _, env = self.prepare_profiles(root)
                    manifest_path = (
                        root
                        / "store"
                        / "profiles"
                        / "internal"
                        / "manifest.json"
                    )
                    manifest_path.parent.mkdir(parents=True)
                    manifest_path.write_text(manifest_text)
                    fake_tools = root / "fake-tools"
                    fake_tools.mkdir()
                    helper_called = root / "helper-called"
                    write_fake_script(
                        fake_tools / "codex-env-setup",
                        "#!/usr/bin/env sh\n"
                        f"touch {helper_called}\n"
                        "exit 0\n",
                    )
                    env["CODEX_SWITCH_ENV_SETUP"] = str(
                        fake_tools / "codex-env-setup"
                    )

                    result = self.run_wrapper(
                        root,
                        "update-internal",
                        "--version",
                        "1.1.0",
                        env=env,
                        check=False,
                    )

                    self.assertEqual(2, result.returncode)
                    self.assertIn(
                        "internal profile manifest is invalid",
                        result.stdout + result.stderr,
                    )
                    self.assertFalse(helper_called.exists())

    def test_one_key_internal_rejects_noncanonical_profile_binary_before_helper(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            custom_dir = root / "custom-internal-bin"
            custom_dir.mkdir()
            custom_codex = custom_dir / "codex-internal"
            write_fake_app_server_smoke_codex(
                custom_codex,
                version="codex-cli 1.0.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            helper_called = root / "helper-called"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"touch {helper_called}\n"
                "exit 0\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )
            manifest_path = (
                root / "store" / "profiles" / "internal" / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["codex_bin"] = str(custom_codex)
            manifest_path.write_text(json.dumps(manifest))

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn(
                "profile codex_bin basename must be codex",
                result.stdout + result.stderr,
            )
            self.assertFalse(helper_called.exists())
            self.assertFalse((root / "store" / "active.json").exists())

    def test_update_internal_rejects_failed_current_version_probe(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_script(
                internal_codex,
                "#!/usr/bin/env sh\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                "  echo codex-cli 1.2.0\n"
                "  exit 9\n"
                "fi\n"
                "exit 0\n",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"printf '%s\\n' \"$*\" > {update_args}\n"
                "exit 0\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "update-internal",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(9, result.returncode)
            self.assertIn("current version probe failed (exit 9)", output)
            self.assertFalse(update_args.exists())

    def test_one_key_internal_blocks_failed_current_version_probe(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_script(
                internal_codex,
                "#!/usr/bin/env sh\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                "  echo codex-cli 1.2.0\n"
                "  exit 9\n"
                "fi\n"
                "exit 0\n",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            helper_called = root / "helper-called"
            write_fake_script(
                fake_tools / "codex-env-setup",
                "#!/usr/bin/env sh\n"
                f"touch {helper_called}\n"
                "exit 0\n",
            )
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(9, result.returncode)
            self.assertIn("current version probe failed (exit 9)", output)
            self.assertIn("Failed step: update check", output)
            self.assertFalse(helper_called.exists())
            self.assertFalse((root / "store" / "active.json").exists())

    def test_update_internal_explicit_version_rejects_wrong_after_version(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=internal_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "1.1.0",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("expected 1.1.0", output)
            self.assertIn("observed 1.0.0", output)
            self.assertFalse(
                (
                    root
                    / "store"
                    / "homes"
                    / "internal"
                    / "app-server-smoke.log"
                ).exists()
            )

    def test_update_internal_rejects_nonzero_version_probe(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            failed_probe_codex = root / "failed-probe-codex"
            write_fake_script(
                failed_probe_codex,
                "#!/usr/bin/env sh\n"
                "if [ \"${1:-}\" = \"--version\" ]; then\n"
                "  echo codex-cli 1.1.0\n"
                "  exit 9\n"
                "fi\n"
                "exit 0\n",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=failed_probe_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "1.1.0",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("version probe failed (exit 9)", output)
            self.assertFalse(
                (
                    root
                    / "store"
                    / "homes"
                    / "internal"
                    / "app-server-smoke.log"
                ).exists()
            )

    def test_update_internal_requires_compatibility_smoke_success(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            failed_candidate = root / "failed-direct-update-codex"
            write_fake_app_server_smoke_codex(
                failed_candidate,
                version="codex-cli 1.1.0",
                exit_241_after_plugin_list=True,
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=failed_candidate,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            bound_before = internal_codex.read_bytes()
            profiles_before = filesystem_snapshot(root / "store/profiles")
            result = self.run_wrapper(
                root,
                "update-internal",
                "--version",
                "1.1.0",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assertIn("compatibility smoke failed", output)
            self.assertIn("exit 241", output)
            self.assertEqual(bound_before, internal_codex.read_bytes())
            self.assertEqual(profiles_before, filesystem_snapshot(root / "store/profiles"))
            self.assertNotIn("Restart required", output)

    def test_internal_update_runs_compatibility_when_switch_apply_fails(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            updated_codex = root / "switch-failure-updated-codex"
            write_fake_app_server_smoke_codex(
                updated_codex,
                version="codex-cli 1.1.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=updated_codex,
                args_log=update_args,
            )
            python_shim = fake_tools / "python-shim"
            write_fake_script(
                python_shim,
                f"#!{sys.executable}\n"
                "import os\n"
                "from pathlib import Path\n"
                "import subprocess\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "script_index = 1 if args[:1] == ['-B'] else 0\n"
                "if len(args) > script_index "
                "and Path(args[script_index]).name in {'codex_profile_switch.py', 'desktop-inventory-driver.py'} "
                "and 'switch' in args and '--dry-run' not in args:\n"
                "    raise SystemExit(43)\n"
                "raise SystemExit(subprocess.call(["
                "os.environ['CODEX_SWITCH_TEST_REAL_PYTHON'], *args]))\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            env["CODEX_SWITCH_PYTHON"] = str(python_shim)
            env["CODEX_SWITCH_TEST_REAL_PYTHON"] = (
                shutil.which("python3.12")
                or shutil.which("python3.11")
                or sys.executable
            )
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=env["CODEX_SWITCH_TEST_REAL_PYTHON"],
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(43, result.returncode)
            self.assertIn("App-server smoke: passed", output)
            self.assert_update_smoke_log(env)
            record = self.assert_update_scope(root, env, "full")
            self.assertEqual("1.1.0", record["actual_version"])

    def test_internal_update_adapter_preserves_full_semver_tokens(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            _, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 10.20.30-rc-alpha.1+build-7",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v10.20.30-rc-alpha.2+build-8\n"
                "EOF\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(root, "check-update", "internal", env=env)

            output = result.stdout + result.stderr
            self.assertIn(
                "Current: codex-cli 10.20.30-rc-alpha.1+build-7",
                output,
            )
            self.assertIn(
                "ordered upgrade to 10.20.30-rc-alpha.2+build-8",
                output,
            )

    def test_blocked_current_repairs_when_latest_lookup_fails(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 0.142.5",
            )
            fallback_codex = root / "offline-fallback-codex"
            write_fake_app_server_smoke_codex(
                fallback_codex,
                version="codex-cli 0.142.4",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "exit 22\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=fallback_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertIn("Latest release: <unavailable", output)
            self.assertIn("blocked-current fallback", output)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="0.142.4",
            )
            self.assertIn("App-server smoke: passed", output)

    def test_internal_update_runs_compatibility_after_plugin_repair_failure(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 1.0.0",
            )
            updated_codex = root / "plugin-failure-updated-codex"
            write_fake_app_server_smoke_codex(
                updated_codex,
                version="codex-cli 1.1.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=updated_codex,
                args_log=update_args,
            )
            python_shim = fake_tools / "python-shim"
            write_fake_script(
                python_shim,
                f"#!{sys.executable}\n"
                "import os\n"
                "from pathlib import Path\n"
                "import subprocess\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "script_index = 1 if args[:1] == ['-B'] else 0\n"
                "if len(args) > script_index "
                "and Path(args[script_index]).name in {'codex_profile_switch.py', 'desktop-inventory-driver.py'} "
                "and 'repair-plugins' in args:\n"
                "    raise SystemExit(41)\n"
                "raise SystemExit(subprocess.call(["
                "os.environ['CODEX_SWITCH_TEST_REAL_PYTHON'], *args]))\n",
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            env["CODEX_SWITCH_PYTHON"] = str(python_shim)
            env["CODEX_SWITCH_TEST_REAL_PYTHON"] = (
                shutil.which("python3.12")
                or shutil.which("python3.11")
                or sys.executable
            )
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=env["CODEX_SWITCH_TEST_REAL_PYTHON"],
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertEqual(41, result.returncode)
            self.assertIn("App-server smoke: passed", output)
            self.assert_update_smoke_log(env)
            record = self.assert_update_scope(root, env, "full")
            self.assertEqual("1.1.0", record["actual_version"])

    def test_one_key_internal_auto_update_runs_app_server_smoke(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                root / "internal-bin" / "codex",
                version="codex-cli 1.0.0",
            )
            updated_codex = root / "updated-codex"
            write_fake_app_server_smoke_codex(
                updated_codex,
                version="codex-cli 9.9.9",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v9.9.9\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=updated_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            check_result = self.run_wrapper(root, "check-update", "internal", env=env)
            check_output = check_result.stdout + check_result.stderr
            self.assertIn("Update: available", check_output)
            self.assertFalse(update_args.exists())

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertIn("Auto-update: internal Codex update detected.", output)
            self.assertIn(
                "Auto-update: running staged parity-safe internal promotion",
                output,
            )
            self.assertIn("App-server smoke: passed", output)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="9.9.9",
            )
            self.assert_update_smoke_log(env)
            active = json.loads((root / "store" / "active.json").read_text())
            self.assertEqual(active["profile"], "internal")

    def test_set_bin_internal_unhealthy_parity_evidence_promotes_nothing(
        self,
    ) -> None:
        cases = (
            ("failed", "parity.preparation.failed", True),
            ("unknown", "parity.probe.unknown", False),
            ("core", "parity.protocol.core_incompatible", False),
            (
                "unclassified",
                "parity.feature.unclassified_drift",
                False,
            ),
        )
        for name, finding_code, raises in cases:
            with self.subTest(case=name):
                temp_dir, root = self.make_workspace()
                with temp_dir:
                    (
                        _store,
                        source_catalog,
                        args,
                        observed_paths,
                    ) = self.internal_rebind_fixture(root)
                    before = self.snapshot_rebind_paths(observed_paths)
                    finding = SimpleNamespace(
                        code=finding_code,
                        severity="error",
                    )
                    unhealthy_bundle = SimpleNamespace(
                        healthy=False,
                        findings=(finding,),
                        synchronization_queue=(),
                        receipt=SimpleNamespace(
                            healthy=False,
                            findings=(finding,),
                        ),
                        receipt_payload=b"",
                        overlay=SimpleNamespace(overlay_payload=b""),
                        config_projection=SimpleNamespace(
                            healthy=False,
                            findings=(finding,),
                            payloads=(),
                            changed_paths=(),
                            max_threads_source=None,
                        ),
                        manifest_metadata={},
                    )
                    prepare_effect: object = (
                        SwitchError(
                            "Parity preparation failed before promotion"
                        )
                        if raises
                        else unhealthy_bundle
                    )
                    rebind_python = (
                        sys.executable
                        if sys.version_info >= (3, 11)
                        else (
                            shutil.which("python3.12")
                            or shutil.which("python3.11")
                        )
                    )
                    self.assertIsNotNone(rebind_python)
                    bundle_commit = mock.Mock()
                    error: SwitchError | None = None
                    with mock.patch.dict(
                        os.environ,
                        {"CODEX_SWITCH_PYTHON": str(rebind_python)},
                    ), mock.patch.object(
                        bindings_module,
                        "prepare_parity_bundle",
                        side_effect=(
                            prepare_effect
                            if isinstance(prepare_effect, BaseException)
                            else None
                        ),
                        return_value=(
                            None
                            if isinstance(prepare_effect, BaseException)
                            else prepare_effect
                        ),
                        create=True,
                    ), mock.patch.object(
                        bindings_module,
                        "commit_runtime_binding_bundle",
                        bundle_commit,
                        create=True,
                    ), redirect_stdout(io.StringIO()):
                        try:
                            cmd_set_bin(args)
                        except SwitchError as exc:
                            error = exc
                    after = self.snapshot_rebind_paths(observed_paths)
                    changed = [
                        str(path)
                        for path in observed_paths
                        if before[path] != after[path]
                    ]
                    self.assertEqual(
                        {
                            "raised_switch_error": error is not None,
                            "changed_paths": changed,
                            "bundle_commit_calls": (
                                bundle_commit.call_count
                            ),
                            "source_bytes": source_catalog.read_bytes(),
                            "source_mode": (
                                source_catalog.stat().st_mode & 0o777
                            ),
                        },
                        {
                            "raised_switch_error": True,
                            "changed_paths": [],
                            "bundle_commit_calls": 0,
                            "source_bytes": before[source_catalog][1],
                            "source_mode": before[source_catalog][2],
                        },
                    )

    def test_full_internal_rebind_drops_cli_only_metadata_before_parity(
        self,
    ) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            store, _source_catalog, args, _observed_paths = (
                self.internal_rebind_fixture(root)
            )
            manifest_path = store.manifest_path("internal")
            manifest = json.loads(manifest_path.read_text())
            manifest["internal_cli_generation"] = {
                "schema_version": 1,
                "scope": "cli-only",
                "backend_sha256": "a" * 64,
                "backend_version": "1.0.0",
            }
            manifest["internal_app_readiness"] = "unverified"
            manifest_path.write_text(json.dumps(manifest) + "\n")
            observed: list[dict[str, object]] = []

            def stop_after_candidate(candidate: object, **_kwargs: object) -> object:
                observed.append(dict(candidate.internal_manifest))
                raise SwitchError("captured full rebind candidate")

            with (
                mock.patch.object(
                    bindings_module,
                    "prepare_capability_receipt_artifact",
                    return_value=SimpleNamespace(
                        receipt=SimpleNamespace(schema_sha256="b" * 64),
                        payload=b"{}\n",
                        payload_sha256="c" * 64,
                    ),
                ),
                mock.patch.object(
                    bindings_module,
                    "prepare_parity_bundle",
                    side_effect=stop_after_candidate,
                ),
                self.assertRaisesRegex(
                    SwitchError,
                    "captured full rebind candidate",
                ),
            ):
                cmd_set_bin(args)

            self.assertEqual(1, len(observed))
            self.assertNotIn("internal_cli_generation", observed[0])
            self.assertNotIn("internal_app_readiness", observed[0])

    def test_one_key_internal_auto_update_propagates_helper_exit_17(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=None,
                args_log=update_args,
                exit_status=17,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(17, result.returncode)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="1.1.0",
            )
            self.assertIn("Auto-update: failed", output)
            self.assertNotIn("Auto-update: completed", output)
            self.assertFalse(
                (
                    root
                    / "store"
                    / "homes"
                    / "internal"
                    / "app-server-smoke.log"
                ).exists()
            )

    def test_one_key_internal_auto_update_rejects_wrong_after_version(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=internal_codex,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="1.1.0",
            )
            self.assertIn("expected 1.1.0", output)
            self.assertIn("observed 1.0.0", output)
            self.assertNotIn("Auto-update: completed", output)
            self.assertFalse(
                (
                    root
                    / "store"
                    / "homes"
                    / "internal"
                    / "app-server-smoke.log"
                ).exists()
            )

    def test_blocked_current_repair_failure_is_not_reported_successful(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 0.142.5",
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v0.142.5\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=None,
                args_log=update_args,
                exit_status=23,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(23, result.returncode)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="0.142.4",
            )
            self.assertIn("Auto-update: failed", output)
            self.assertNotIn("Auto-update: completed", output)

    def test_internal_auto_update_requires_compatibility_smoke_success(self) -> None:
        temp_dir, root = self.make_workspace()
        with temp_dir:
            internal_codex, official_codex, env = self.prepare_profiles(root)
            write_fake_app_server_smoke_codex(
                internal_codex,
                version="codex-cli 1.0.0",
            )
            failed_candidate = root / "failed-candidate-codex"
            write_fake_app_server_smoke_codex(
                failed_candidate,
                version="codex-cli 1.1.0",
                exit_241_after_plugin_list=True,
            )
            fake_tools = root / "fake-tools"
            fake_tools.mkdir()
            update_args = root / "update-args.txt"
            write_fake_script(
                fake_tools / "curl",
                "#!/usr/bin/env sh\n"
                "cat <<'EOF'\n"
                "HTTP/2 302\n"
                "location: https://github.com/SDGLBL/codex/releases/tag/internal-rust-v1.1.0\n"
                "EOF\n",
            )
            write_fake_staged_update_helper(
                fake_tools / "codex-env-setup",
                candidate_source=failed_candidate,
                args_log=update_args,
            )
            env["PATH"] = f"{fake_tools}{os.pathsep}{env.get('PATH', '')}"
            env["CODEX_SWITCH_ENV_SETUP"] = str(fake_tools / "codex-env-setup")
            self.enable_staged_internal_update_fixture(root, env)
            self.run_switcher(
                root,
                "init",
                "--app-cli-path",
                str(official_codex),
                "--capture-current",
                "internal",
                env=env,
                python_executable=(
                    shutil.which("python3.12")
                    or shutil.which("python3.11")
                    or sys.executable
                ),
            )

            bound_before = internal_codex.read_bytes()
            profiles_before = filesystem_snapshot(root / "store/profiles")
            result = self.run_wrapper(
                root,
                "internal",
                "--skip-launchctl",
                "--skip-plugin-repair",
                "--skip-doctor",
                "--no-status",
                env=env,
                check=False,
                allow_switch_script=True,
            )

            output = result.stdout + result.stderr
            self.assertNotEqual(0, result.returncode)
            self.assert_staged_update_helper_args(
                update_args,
                bound_bin=internal_codex,
                version="1.1.0",
            )
            self.assertIn("compatibility smoke failed", output)
            self.assertIn("exit 241", output)
            self.assertEqual(bound_before, internal_codex.read_bytes())
            self.assertEqual(profiles_before, filesystem_snapshot(root / "store/profiles"))
            self.assertNotIn("Restart required", output)
            self.assertNotIn("Auto-update: completed", output)

    def test_parse_running_processes_ignores_headers_and_bad_lines(self) -> None:
        output = """
          PID ARGS
        123 /Applications/Codex.app/Contents/MacOS/Codex
        nope
        456 /Users/cY/.local/bin/codex app-server --analytics-default-enabled
        """

        self.assertEqual(
            parse_ps_processes(output),
            [
                (123, "/Applications/Codex.app/Contents/MacOS/Codex"),
                (456, "/Users/cY/.local/bin/codex app-server --analytics-default-enabled"),
            ],
        )

    def test_parse_env_app_cli_path_only_extracts_safe_value(self) -> None:
        output = (
            "PID COMMAND SECRET_TOKEN=should-not-print "
            "CODEX_CLI_PATH=/Users/cY/.local/bin/codex OTHER=value"
        )

        self.assertEqual(parse_env_app_cli_path(output), "/Users/cY/.local/bin/codex")

    def test_app_server_command_path_ignores_payload_mentions(self) -> None:
        args = (
            "/Users/cY/.codex/computer-use/Codex Computer Use.app/Contents/"
            "SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient "
            "turn-ended "
            '{"last-assistant-message":"Host --> AppServer[\\"codex app-server --stdio\\"]"}'
        )

        self.assertEqual(app_server_command_path(args), "")

    def test_app_server_command_path_accepts_codex_executables(self) -> None:
        self.assertEqual(
            app_server_command_path(
                "/Users/cY/.codex-switch/bin/codex-internal-app "
                "app-server --analytics-default-enabled"
            ),
            "/Users/cY/.codex-switch/bin/codex-internal-app",
        )
        self.assertEqual(
            app_server_command_path(
                "/Users/cY/.vscode/extensions/openai.chatgpt/bin/macos-aarch64/codex "
                "app-server --analytics-default-enabled"
            ),
            "/Users/cY/.vscode/extensions/openai.chatgpt/bin/macos-aarch64/codex",
        )

    def test_running_desktop_problem_reports_stale_app_server(self) -> None:
        store = Store(
            root=Path("/tmp/store"),
            live_codex_home=Path("/tmp/live"),
            launch_agent_path=Path("/tmp/agent.plist"),
            launch_agent_label="test",
        )
        observations = [
            RunningCodexProcess(
                pid=42,
                kind="app-server",
                command_path="/Applications/Codex.app/Contents/Resources/codex",
                app_cli_env="/Applications/Codex.app/Contents/Resources/codex",
            )
        ]

        problems = running_desktop_problems(
            store,
            active_profile="internal",
            expected_app_cli="/Users/cY/.local/bin/codex",
            observations=observations,
            enforce_default_context=False,
        )

        self.assertEqual(len(problems), 1)
        self.assertIn("running Codex app-server pid 42 uses", problems[0])

    def test_running_desktop_problem_accepts_internal_proxy_child_app_server(self) -> None:
        store = Store(
            root=Path("/tmp/store"),
            live_codex_home=Path("/tmp/live"),
            launch_agent_path=Path("/tmp/agent.plist"),
            launch_agent_label="test",
        )
        expected_app_cli = "/Users/cY/.codex-switch/bin/codex-internal-app"
        observations = [
            SimpleNamespace(
                pid=42,
                kind="app-server",
                command_path="/Users/cY/.local/bin/codex",
                app_cli_env=expected_app_cli,
                parent_command=(
                    "/usr/bin/python3 /Users/cY/.local/share/codex-switch/current/"
                    "scripts/codex_switch_app_proxy.py /Users/cY/.local/bin/codex "
                    "/Users/cY/.codex-switch/homes/internal/config.toml "
                    "app-server --analytics-default-enabled"
                ),
            )
        ]

        problems = running_desktop_problems(
            store,
            active_profile="internal",
            expected_app_cli=expected_app_cli,
            observations=observations,
            enforce_default_context=False,
        )

        self.assertEqual([], problems)


if __name__ == "__main__":
    unittest.main()
