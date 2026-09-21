"""Opt-in real CLI verification using only an isolated loopback provider."""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_switch_parity import ParityProbeInputs, run_parity_probes


BACKEND = os.environ.get("CODEX_SWITCH_TEST_BACKEND")
FIXTURES = Path(__file__).resolve().parent.parent / "evals" / "fixtures"


@unittest.skipUnless(BACKEND, "set CODEX_SWITCH_TEST_BACKEND to an explicit test CLI")
class NativeParityTests(unittest.TestCase):
    def test_real_core_and_typed_v2_with_local_provider(self) -> None:
        requests: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                if len(requests) >= 12:
                    self.send_error(500, "unexpected test conversation")
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                inputs = body.get("input", [])
                parent = any(
                    "Use the v2 collaboration tool" in json.dumps(item)
                    for item in inputs if item.get("role") == "user"
                )
                text = "parity-parent-ok" if parent else "parity-subagent-ok"
                item = {
                    "id": "msg_1", "type": "message", "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
                if parent and not any(i.get("type") == "function_call_output" for i in inputs):
                    item = {
                        "id": "fc_spawn", "type": "function_call",
                        "namespace": "collaboration", "name": "spawn_agent",
                        "call_id": "call_spawn",
                        "arguments": json.dumps({
                            "task_name": "parity_probe", "agent_type": "explorer",
                            "fork_turns": "none", "message": "Return exactly parity-subagent-ok.",
                        }),
                    }
                elif parent and not any(i.get("call_id") == "call_wait" for i in inputs):
                    item = {
                        "id": "fc_wait", "type": "function_call",
                        "namespace": "collaboration", "name": "wait_agent",
                        "call_id": "call_wait", "arguments": '{"timeout_ms":10000}',
                    }
                response_id = f"resp_{len(requests)}"
                events = [
                    {"type": "response.created", "response": {"id": response_id}},
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {
                        "id": response_id, "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }},
                ]
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    for event in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="codex-parity-native-") as tmp:
                root = Path(tmp)
                home, workspace = root / "home", root / "workspace"
                codex_home = home / ".codex"
                parity_home = codex_home / "parity"
                parity_home.mkdir(parents=True)
                workspace.mkdir()
                overlay = parity_home / "model-catalog.json"
                overlay.write_bytes((FIXTURES / "parity-probe-model.json").read_bytes())
                capability = parity_home / "capability-receipt.json"
                capability.write_text('{"schema_version":1,"multi_agent_v2":true}\n')
                config = codex_home / "config.toml"
                config.write_text(
                    'model="gpt-5.5"\nmodel_provider="probe"\n'
                    f'model_catalog_json={json.dumps(str(overlay))}\n'
                    '[model_providers.probe]\nname="Local test"\n'
                    f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
                    'wire_api="responses"\nrequires_openai_auth=false\n'
                    'request_max_retries=0\n[features]\nmulti_agent_v2=true\n'
                )
                backend = Path(BACKEND).absolute()

                def digest(path: Path) -> str:
                    return hashlib.sha256(path.read_bytes()).hexdigest()

                inputs = ParityProbeInputs(
                    backend_cli=backend, backend_sha256=digest(backend),
                    codex_home=codex_home, workspace=workspace,
                    config_path=config, config_sha256=digest(config),
                    overlay_path=overlay, overlay_sha256=digest(overlay),
                    capability_receipt_path=capability,
                    capability_receipt_sha256=digest(capability),
                )
                environment = {
                    "HOME": str(home), "CODEX_HOME": str(codex_home),
                    "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
                    "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                    "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
                }
                with patch.dict(os.environ, environment, clear=True):
                    report = run_parity_probes(inputs=inputs)
                self.assertTrue(report.healthy, report.findings)
                self.assertEqual(
                    [(result.name, result.result_code) for result in report.results],
                    [("core_protocol", "passed"), ("typed_subagent_v2", "passed")],
                )
                self.assertEqual(len(requests), 4)
                namespace = next(t for t in requests[0]["tools"] if t.get("name") == "collaboration")
                spawn = next(t for t in namespace["tools"] if t.get("name") == "spawn_agent")
                self.assertIn("task_name", spawn["parameters"]["required"])
                self.assertIn("agent_type", spawn["parameters"]["properties"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
