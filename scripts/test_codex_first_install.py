#!/usr/bin/env python3
"""First-install regressions at the public codex-switch command boundary."""

import os
import json
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


WRAPPER = Path(__file__).with_name("codex-switch")


class FirstInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-first-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.store = self.root / "store"
        self.target = self.home / ".local/bin/codex"
        self.installer = self.root / "install.sh"
        self.fake_bin = self.root / "tools"
        self.fake_bin.mkdir()
        self.env = {
            "HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
            "PATH": str(self.fake_bin) + os.pathsep + os.defpath,
            "CODEX_SWITCH_PYTHON": sys.executable,
            "CODEX_SWITCH_SKIP_SELF_UPDATE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_INSTALL_AK": "synthetic-test-key", "TEST_TARGET": str(self.target),
            "TEST_STORE": str(self.store), "TEST_INSTALLER": str(self.installer),
        }
        self.write(self.fake_bin / "curl", '#!/bin/sh\ncat "$TEST_INSTALLER"\n')
        self.write(self.fake_bin / "codesign", "#!/bin/sh\nexit 0\n")
        self.write_installer()

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)

    def write_installer(self, before="", candidate=None):
        candidate = candidate or '#!/bin/sh\nprintf "codex-cli 2.0.0\\n"\n'
        self.write(self.installer, '#!/bin/sh\nset -eu\n'
                   '[ "$CODEX_NON_INTERACTIVE" = 1 ]\n' + before + '\n'
                   'cat > "$CODEX_INSTALL_DIR/codex" <<\'BIN\'\n' + candidate +
                   'BIN\nchmod +x "$CODEX_INSTALL_DIR/codex"\n'
                   'printf private-config > "$CODEX_HOME/config.toml"\n')

    def run_install(self, *args, pinned=True):
        command = [str(WRAPPER), "--skip-self-update", "--store-dir", str(self.store),
                   "update-internal", "--install-dir", str(self.target.parent),
                   "--skip-source-check", "--skip-proxy"]
        if pinned:
            command += ["--version", "2.0.0"]
        return subprocess.run(command + list(args), env=self.env, text=True,
                              capture_output=True, timeout=30)

    def test_fresh_install_without_profile_config_or_parent_directory(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = subprocess.run([str(self.target), "--version"], env=self.env,
                             text=True, capture_output=True, timeout=10)
        self.assertEqual(run.stdout.strip(), "codex-cli 2.0.0")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((self.store / "profiles/internal").exists())
        self.assertFalse((self.home / ".codex/config.toml").exists())
        self.assertIn("First installation", result.stdout)

    def test_preparation_failure_leaves_target_absent(self):
        self.write_installer(before="exit 17")
        result = self.run_install()
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        self.assertFalse(os.path.lexists(self.target))

    def test_wrong_candidate_version_is_not_published(self):
        self.write_installer(candidate='#!/bin/sh\necho "codex-cli 1.0.0"\n')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.lexists(self.target))

    def test_target_created_by_another_install_is_preserved(self):
        self.write_installer(before='printf concurrent > "$TEST_TARGET"')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), "concurrent")

    def test_profile_created_during_install_is_preserved(self):
        self.write_installer(before='mkdir -p "$TEST_STORE/profiles/internal"\n'
                             'printf concurrent > "$TEST_STORE/profiles/internal/config.toml"')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.store / "profiles/internal/config.toml").read_text(), "concurrent")
        self.assertFalse(os.path.lexists(self.target))

    def test_existing_unregistered_command_requires_capture(self):
        self.write(self.target, "#!/bin/sh\necho previous\n")
        before = self.target.read_bytes()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertIn("capture", result.stderr)

    def test_dangling_target_link_is_not_a_fresh_install(self):
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to("missing")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.readlink(self.target), "missing")

    def test_partial_profile_is_not_a_fresh_install(self):
        config = self.store / "profiles/internal/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("existing-config")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(config.read_text(), "existing-config")
        self.assertFalse(os.path.lexists(self.target))

    def test_installed_path_failure_restores_absence(self):
        self.write_installer(candidate='#!/bin/sh\ncase "$0" in\n'
                             '*/.codex-internal-update-*/codex) echo "codex-cli 2.0.0";;\n'
                             '*) exit 23;;\nesac\n')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version verification failed", result.stderr)
        self.assertFalse(os.path.lexists(self.target))

    def test_dry_run_does_not_create_install_or_store_paths(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        result = self.run_install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("First installation", result.stdout)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), before)

    def test_automatic_version_resolves_latest_before_install(self):
        self.write(self.fake_bin / "curl", '#!/bin/sh\ncase "$*" in\n'
                   '*-I*|*-fsSLI*) printf "location: https://example.test/releases/tag/internal-rust-v2.0.0\\r\\n";;\n'
                   '*) cat "$TEST_INSTALLER";;\nesac\n')
        result = self.run_install(pinned=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unavailable_latest_does_not_run_unpinned_installer(self):
        self.write(self.fake_bin / "curl", '#!/bin/sh\nexit 56\n')
        result = self.run_install(pinned=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.parent.exists())

    def test_complete_standalone_runtime_survives_first_install(self):
        runtime = '''#!/usr/bin/env python3
import pathlib, subprocess
root = pathlib.Path(__file__).resolve().parent.parent
assert subprocess.check_output([str(root / "bin/codex-code-mode-host")]).strip() == b"host"
assert subprocess.check_output([str(root / "codex-path/rg")]).strip() == b"rg"
print("codex-cli 2.0.0")
'''
        files = {"bin/codex": runtime,
                 "bin/codex-code-mode-host": "#!/bin/sh\necho host\n",
                 "codex-path/rg": "#!/bin/sh\necho rg\n",
                 "codex-package.json": json.dumps({"version": "2.0.0"})}
        self.write(self.installer, '''#!/bin/sh
set -eu
"$CODEX_SWITCH_PYTHON" -I -B - <<'PY'
import os, pathlib
home = pathlib.Path(os.environ['CODEX_HOME'])
package = home / 'packages/standalone/releases/2.0.0-aarch64-apple-darwin'
for name, content in FILES.items():
    path = package / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755 if name != 'codex-package.json' else 0o644)
(package / 'codex').symlink_to('bin/codex')
(pathlib.Path(os.environ['CODEX_INSTALL_DIR']) / 'codex').symlink_to(package / 'bin/codex')
(home / 'config.toml').write_text('private-scratch-only')
PY
'''.replace('FILES', repr(files)))
        # Generated runtime probes need the selected Python executable on PATH.
        (self.fake_bin / "python3").symlink_to(sys.executable)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = subprocess.run([str(self.target), "--version"], env=self.env,
                             text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "codex-cli 2.0.0")
        self.assertFalse(self.target.is_symlink())
        self.assertFalse((self.home / '.codex/config.toml').exists())

    def test_profile_with_missing_bound_binary_keeps_upgrade_guard(self):
        manifest = self.store / "profiles/internal/manifest.json"
        manifest.parent.mkdir(parents=True)
        content = json.dumps({"name": "internal", "codex_bin": str(self.target)})
        manifest.write_text(content)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("First installation complete", result.stdout)
        self.assertEqual(manifest.read_text(), content)
        self.assertFalse(os.path.lexists(self.target))

    def test_blocked_latest_uses_valid_fallback(self):
        self.write(self.fake_bin / "curl", '#!/bin/sh\ncase "$*" in\n'
                   '*-I*|*-fsSLI*) printf "location: https://example.test/releases/tag/internal-rust-v2.1.0\\r\\n";;\n'
                   '*) cat "$TEST_INSTALLER";;\nesac\n')
        self.env.update(CODEX_SWITCH_INTERNAL_BLOCKED_VERSIONS="2.1.0",
                        CODEX_SWITCH_INTERNAL_FALLBACK_VERSION="2.0.0")
        result = self.run_install(pinned=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_build_metadata_does_not_bypass_blocked_latest(self):
        self.write(self.fake_bin / "curl", '#!/bin/sh\ncase "$*" in\n'
                   '*-I*|*-fsSLI*) printf "location: https://example.test/releases/tag/internal-rust-v2.1.0+build\\r\\n";;\n'
                   '*) cat "$TEST_INSTALLER";;\nesac\n')
        self.env.update(CODEX_SWITCH_INTERNAL_BLOCKED_VERSIONS="2.1.0",
                        CODEX_SWITCH_INTERNAL_FALLBACK_VERSION="2.0.0")
        result = self.run_install(pinned=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CLI 2.0.0", result.stdout)

    def test_ambiguous_release_tag_is_rejected_before_install(self):
        self.write(self.fake_bin / "curl", '#!/bin/sh\nprintf "location: https://example.test/releases/tag/internal-rust-v2.0.0_3.0.0\\r\\n"\n')
        result = self.run_install(pinned=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.parent.exists())

    def test_signal_to_public_cli_cancels_preparation(self):
        marker = self.root / "installer-started"
        self.env["TEST_MARKER"] = str(marker)
        self.write(self.installer, '#!/bin/sh\ntouch "$TEST_MARKER"\n'
                   'exec "$CODEX_SWITCH_PYTHON" -c "import time; time.sleep(30)"\n')
        command = [str(WRAPPER), "--skip-self-update", "--store-dir", str(self.store),
                   "update-internal", "--install-dir", str(self.target.parent),
                   "--skip-source-check", "--skip-proxy", "--version", "2.0.0"]
        with subprocess.Popen(command, env=self.env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as process:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "installer did not start")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertNotIn("First installation complete", stdout)
        self.assertFalse(os.path.lexists(self.target))

    def test_conflicting_internal_bin_is_rejected(self):
        result = self.run_install("--internal-bin", str(self.root / "other/codex"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.lexists(self.target))

    def test_explicit_internal_bin_without_install_dir_is_honored(self):
        target = self.root / "selected/codex"
        command = [str(WRAPPER), "--skip-self-update", "--store-dir", str(self.store),
                   "update-internal", "--internal-bin", str(target),
                   "--skip-source-check", "--skip-proxy", "--version", "2.0.0"]
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(target.is_file())
        self.assertFalse(self.target.exists())

    def inject_publication_fault(self, fault):
        # An isolated interpreter shim injects a signal after the real link
        # syscall, exercising the public command without a production test hook.
        shim = self.fake_bin / "python-signal"
        self.write(shim, '#!' + sys.executable + '\n' + '''
import os, runpy, signal, sys
for index, value in enumerate(sys.argv[1:], 1):
    if value.endswith('/codex_switch_first_install.py'):
        original = os.link
        def link(*args, **kwargs):
            original(*args, **kwargs)
            FAULT
        os.link = link
        sys.argv = sys.argv[index:]
        runpy.run_path(value, run_name='__main__')
        break
else:
    os.execv(REAL_PYTHON, [REAL_PYTHON, *sys.argv[1:]])
'''.replace('REAL_PYTHON', repr(sys.executable)).replace('FAULT', fault))
        self.env['CODEX_SWITCH_PYTHON'] = str(shim)

    def test_signal_immediately_after_publication_removes_owned_command(self):
        self.inject_publication_fault('os.kill(os.getpid(), signal.SIGTERM)')
        result = self.run_install()
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        self.assertFalse(os.path.lexists(self.target))
        self.assertNotIn('First installation complete', result.stdout)

    def test_concurrent_equal_hard_link_is_not_removed_on_eexist(self):
        self.inject_publication_fault("raise FileExistsError('concurrent target')")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.target.is_file())
        self.assertNotIn('First installation complete', result.stdout)


if __name__ == "__main__":
    unittest.main()
