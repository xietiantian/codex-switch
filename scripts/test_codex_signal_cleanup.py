"""Signal lifecycle regressions at native probe entry points.

Each test owns one new temporary root, its private HOME/cwd, and every process
started by its harness. PID records are written by those processes only. The
test always kills/reaps its recorded group before releasing that root, including
on a RED failure. No installed runtime, user configuration, or network is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _backend_payload(root: Path, *, complete_handshake: bool = False, complete_core: bool = False) -> bytes:
    core = ""
    if complete_core:
        core = (
            "if '--analytics-default-enabled' in sys.argv:\n"
            "    for raw in sys.stdin:\n"
            "        message = json.loads(raw)\n"
            "        if 'id' in message:\n"
            "            result = {'thread': {'id': 'parent'}} if message['method'] == 'thread/start' else {'data': []}\n"
            "            print(json.dumps({'id': message['id'], 'result': result}), flush=True)\n"
            "    sys.exit(0)\n"
        )
    handshake = ""
    if complete_handshake:
        handshake = (
            "    def cleanup_started(_sig, _frame):\n"
            f"        Path({str(root / 'cleanup-started')!r}).write_text('ready')\n"
            "    signal.signal(signal.SIGTERM, cleanup_started)\n"
            "    for raw in sys.stdin:\n"
            "        message = json.loads(raw)\n"
            "        if 'id' in message:\n"
            "            print(json.dumps({'id': message['id'], 'result': {}}), flush=True)\n"
        )
    return (
        f"#!{sys.executable}\n"
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        + core +
        "for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):\n"
        "    signal.signal(sig, signal.SIG_IGN)\n"
        "child = os.fork()\n"
        "if child:\n"
        f"    Path({str(root / 'pids.json')!r}).write_text(json.dumps([os.getpid(), child]))\n"
        + handshake +
        "while True:\n"
        "    time.sleep(60)\n"
    ).encode()


def _run_harness(seam: str, root: Path) -> None:
    def cancel(sig: int, _frame: object) -> None:
        raise SystemExit(128 + sig)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancel)
    backend = root / "backend"
    backend.write_bytes(_backend_payload(root, complete_handshake=seam == "successful-cleanup"))
    backend.chmod(0o755)
    try:
        if seam in {"parity", "typed-parity"}:
            from test_codex_parity import ParityProbeTests
            from codex_switch_parity import run_parity_probes
            fixture = ParityProbeTests()
            inputs = fixture.inputs(fixture.probe_seams(), root,
                                    backend_payload=_backend_payload(root, complete_core=seam == "typed-parity"))
            run_parity_probes(inputs=inputs, timeout_seconds=30, max_output_bytes=4096)
        elif seam == "features":
            from codex_switch_parity import collect_feature_inventory
            effective_home = root / "effective"
            effective_home.mkdir(mode=0o700)
            collect_feature_inventory(side="internal", cli_path=backend,
                                      isolated_home=root / "home", effective_home=effective_home,
                                      timeout_seconds=30)
        elif seam == "bounded":
            from codex_switch_verify import run_bounded_process
            run_bounded_process([str(backend)], kind="signal-fixture", cwd=root / "home",
                                timeout_seconds=30)
        elif seam in {"handshake", "successful-cleanup"}:
            from codex_switch_verify import run_app_server_smoke
            run_app_server_smoke(str(backend), root / "home", isolate_home=True,
                                 response_timeout_seconds=30, settle_seconds=0)
        elif seam == "schema":
            from codex_switch_protocol_adapter import generate_app_server_schema
            generate_app_server_schema(backend, timeout_seconds=30)
        elif seam == "config-write":
            from codex_switch_protocol_adapter import probe_config_write_capability
            probe_config_write_capability(backend, timeout_seconds=30)
        elif seam in {"model-export", "version"}:
            import codex_switch_parity as parity
            from test_codex_model_catalog_routing import ModelCatalogRoutingTests
            from test_codex_parity import RecordingFeatureRunner, feature_result, ParityPreparationTests
            fixture = ModelCatalogRoutingTests()
            fixture.setUp()
            if seam == "model-export":
                fixture.candidate.internal_binding.backend_cli.write_bytes(_backend_payload(root))
                fixture.internal_cache.unlink()
                fixture.prepare(timeouts=parity.ParityTimeouts(command_seconds=30, probe_seconds=30))
            else:
                fixture.candidate.official_binding.backend_cli.write_bytes(_backend_payload(root))
                # Only unrelated metadata/protocol fixtures are injected. The
                # requested version/export subprocess is always the real runner.
                def probe(request):
                    output = (ParityPreparationTests.CORE_SUCCESS if request.name == "core_protocol"
                              else ParityPreparationTests.TYPED_SUCCESS)
                    return parity.ParityProbeCommandResult(returncode=0, stdout=output, stderr="")
                parity.prepare_parity_bundle(
                    fixture.candidate, work_root=fixture.work,
                    timeouts=parity.ParityTimeouts(command_seconds=30, probe_seconds=30),
                    _schema_loader=lambda _path, _timeout: fixture.schema,
                    _feature_runner=RecordingFeatureRunner([
                        feature_result("multi_agent_v2  stable  true\n") for _ in range(4)]),
                    _probe_runner=probe,
                )
        else:
            raise AssertionError(f"unknown seam: {seam}")
    finally:
        # A reaped leader is no longer waitable by its parent. Do not hide a
        # missing production wait by letting the test harness reap it silently.
        pids = json.loads((root / "pids.json").read_text())
        try:
            os.waitpid(pids[0], os.WNOHANG)
        except ChildProcessError:
            reaped = True
        else:
            reaped = False
        (root / "finished.json").write_text(json.dumps({
            "leader_reaped": reaped,
            "group_gone": not _group_exists(pids[0]),
            "handlers_restored": all(signal.getsignal(sig) is cancel for sig in (
                signal.SIGTERM, signal.SIGINT, signal.SIGHUP)),
        }))


class NativeSignalCleanupTests(unittest.TestCase):
    def assert_cancelled_probe_is_reaped(self, seam: str, *, repeated: bool = False) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-signal-test-") as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir(mode=0o700)
            environment = {
                "PATH": os.defpath, "HOME": str(home), "CODEX_HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(root),
            }
            harness = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--harness", seam, str(root)],
                env=environment, cwd=home, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
            pids: list[int] = []
            try:
                deadline = time.monotonic() + 10
                while not (root / "pids.json").exists() and time.monotonic() < deadline:
                    if harness.poll() is not None:
                        self.fail(f"probe exited before readiness: {harness.communicate()!r}")
                    time.sleep(0.01)
                self.assertTrue((root / "pids.json").exists(), "probe did not start")
                pids = json.loads((root / "pids.json").read_text())
                if seam == "successful-cleanup":
                    while not (root / "cleanup-started").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((root / "cleanup-started").exists(), "successful probe did not start cleanup")
                started = time.monotonic()
                harness.send_signal(signal.SIGTERM)
                if repeated:
                    for _ in range(10):
                        time.sleep(0.025)
                        if harness.poll() is not None:
                            break
                        harness.send_signal(signal.SIGINT)
                stdout, stderr = harness.communicate(timeout=5)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(harness.returncode, 128 + signal.SIGTERM,
                                 (stdout, stderr))
                self.assertFalse(_group_exists(pids[0]), "probe process group survived cancellation")
                self.assertFalse(any(_pid_exists(pid) for pid in pids),
                                 "probe descendant survived cancellation outside its leader's group")
                self.assertEqual(json.loads((root / "finished.json").read_text()),
                                 {"leader_reaped": True, "group_gone": True, "handlers_restored": True})
            finally:
                for pid in pids[:1]:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                # Also cover a broken runner that inherited the harness group
                # instead of creating its promised private process session.
                try:
                    os.killpg(harness.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for pid in pids:
                    try:
                        if os.getpgid(pid) in {pids[0], harness.pid}:
                            os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if harness.poll() is None:
                    harness.kill()
                harness.communicate(timeout=5)

    def test_parity_cancellation_reaps_the_entire_process_group(self) -> None:
        self.assert_cancelled_probe_is_reaped("parity")

    def test_repeated_signals_preserve_original_cancellation_and_reap_group(self) -> None:
        self.assert_cancelled_probe_is_reaped("parity", repeated=True)

    def test_feature_collection_cancellation_reaps_the_process_group(self) -> None:
        self.assert_cancelled_probe_is_reaped("features", repeated=True)

    def test_bounded_command_cancellation_reaps_the_process_group(self) -> None:
        self.assert_cancelled_probe_is_reaped("bounded", repeated=True)

    def test_handshake_cancellation_reaps_despite_repeated_signals(self) -> None:
        self.assert_cancelled_probe_is_reaped("handshake", repeated=True)

    def test_schema_cancellation_reaps_despite_repeated_signals(self) -> None:
        self.assert_cancelled_probe_is_reaped("schema", repeated=True)

    def test_config_write_cancellation_reaps_despite_repeated_signals(self) -> None:
        self.assert_cancelled_probe_is_reaped("config-write", repeated=True)

    def test_model_export_cancellation_reaps_despite_repeated_signals(self) -> None:
        self.assert_cancelled_probe_is_reaped("model-export", repeated=True)

    def test_version_cancellation_reaps_the_process_group(self) -> None:
        self.assert_cancelled_probe_is_reaped("version", repeated=True)

    def test_typed_parity_cancellation_reaps_the_process_group(self) -> None:
        self.assert_cancelled_probe_is_reaped("typed-parity", repeated=True)

    def test_first_signal_during_successful_cleanup_is_deferred_not_lost(self) -> None:
        self.assert_cancelled_probe_is_reaped("successful-cleanup", repeated=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--harness":
        _run_harness(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
