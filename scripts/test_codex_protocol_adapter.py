"""Native subprocess isolation for the bounded preparation probes."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_switch_protocol_adapter import (
    generate_app_server_schema,
    probe_config_write_capability,
)
from test_codex_protocol_config import write_probe_backend
from codex_switch_verify import run_app_server_smoke


class PreparationProbeIsolationTests(unittest.TestCase):
    def test_installed_handshake_does_not_execute_caller_project_hooks(self):
        from test_codex_runtime_binding import RuntimeBindingTests
        with tempfile.TemporaryDirectory(prefix="codex-handshake-isolation-") as temporary:
            root = Path(temporary).resolve()
            caller = root / "caller"
            hook_home = caller / ".codex"
            hook_home.mkdir(parents=True)
            marker = root / "caller-hook-ran"
            (hook_home / "hooks.json").write_text(json.dumps({"marker": str(marker)}))
            isolated = root / "isolated"
            isolated.mkdir()
            (isolated / "config.toml").write_text('model = "fixture"\n')
            backend = RuntimeBindingTests().write_rebind_backend(root / "codex")
            # The scripted native process emulates project/home hook discovery;
            # inheriting either caller location executes the sentinel hook.
            hook_loader = (
                "for candidate in (Path.cwd() / '.codex/hooks.json', "
                "Path(os.environ['HOME']) / '.codex/hooks.json'):\n"
                "    if candidate.exists():\n"
                "        Path(json.loads(candidate.read_text())['marker']).write_text('executed')\n"
            )
            backend.write_text(backend.read_text().replace("RUNTIME_LABEL = ", hook_loader + "RUNTIME_LABEL = ", 1))
            previous_cwd = Path.cwd()
            try:
                os.chdir(caller)
                with patch.dict(os.environ, {"HOME": str(caller)}):
                    control_code, control_output = run_app_server_smoke(str(backend), isolated,
                        settle_seconds=0.01, response_timeout_seconds=2)
                    self.assertEqual(control_code, 0, control_output)
                    self.assertTrue(marker.exists(), "The fixture must detect inherited caller locations")
                    marker.unlink()
                    code, output = run_app_server_smoke(str(backend), isolated,
                        isolate_home=True, settle_seconds=0.01, response_timeout_seconds=2)
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(code, 0, output)
            self.assertFalse(marker.exists())

    def test_schema_and_config_write_ignore_caller_home_and_working_directory(self):
        with tempfile.TemporaryDirectory(prefix="codex-probe-isolation-") as temporary:
            root = Path(temporary).resolve()
            caller = root / "caller"
            caller.mkdir()
            caller_config = caller / ".codex"
            caller_config.mkdir()
            (caller_config / "config.toml").write_text('notify = ["caller-hook"]\n')
            backend = write_probe_backend(root / "codex")
            report = root / "observations.jsonl"
            body = backend.read_text()
            observation = (
                f"with Path({str(report)!r}).open('a') as observation:\n"
                "    observation.write(json.dumps({'home': os.environ.get('HOME'), "
                "'codex_home': os.environ.get('CODEX_HOME'), "
                "'xdg_config_home': os.environ.get('XDG_CONFIG_HOME'), "
                "'cwd': os.getcwd()}) + '\\n')\n"
            )
            backend.write_text(body.replace("MODE = ", observation + "MODE = ", 1))
            previous_cwd = Path.cwd()
            try:
                os.chdir(caller)
                with patch.dict(os.environ, {
                    "HOME": str(caller), "CODEX_HOME": str(caller_config),
                    "XDG_CONFIG_HOME": str(caller / ".config"),
                }):
                    generate_app_server_schema(backend, timeout_seconds=2)
                    self.assertTrue(probe_config_write_capability(backend, timeout_seconds=2))
            finally:
                os.chdir(previous_cwd)
            observations = [json.loads(line) for line in report.read_text().splitlines()]
            self.assertEqual(len(observations), 2)
            for observed in observations:
                self.assertNotEqual(observed["home"], str(caller))
                self.assertNotEqual(observed["codex_home"], str(caller_config))
                self.assertNotEqual(observed["cwd"], str(caller))
                self.assertEqual(observed["home"], observed["codex_home"])
                self.assertEqual(observed["cwd"], observed["home"])
                self.assertEqual(Path(observed["xdg_config_home"]), Path(observed["home"]) / ".config")
            self.assertEqual((caller_config / "config.toml").read_text(), 'notify = ["caller-hook"]\n')


if __name__ == "__main__":
    unittest.main()
