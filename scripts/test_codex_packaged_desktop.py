from __future__ import annotations

import plistlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_switch_runtime_binding import (
    DesktopRoots, RuntimeBindingContext, discover_desktop_hosts, resolve_runtime_binding,
)
from codex_switch_parity import OfficialReference
from test_codex_staged_update import ReleasedBindingUpdateTests, WRAPPER


class PackagedDesktopTests(unittest.TestCase):
    def test_packaged_reference_binds_actual_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / 'ChatGPT.app'
            main = bundle / 'Contents/MacOS/ChatGPT'
            wrapper = bundle / 'Contents/Resources/codex-cli/bin/codex'
            backend = bundle / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
            for path in (main, wrapper, backend):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'com.openai.codex',
            }))
            roots = DesktopRoots(bundle, root / 'Codex.app', root / 'Classic.app')
            inventory = discover_desktop_hosts(roots)
            self.assertIsNotNone(inventory.current, inventory.findings)
            self.assertEqual(inventory.current.bundled_cli, backend)
            binding = resolve_runtime_binding(RuntimeBindingContext(
                profile='openai-official', manifest={}, store_root=root / 'store',
                bin_dir=root / 'bin', profile_home=root / 'home'), inventory)
            reference = OfficialReference.from_runtime_binding(binding,
                bundle_version='26.924.22138', cli_version='0.158.0-alpha.2.1',
                binary_sha256='a' * 64, schema_sha256='b' * 64,
                feature_inventory_sha256='c' * 64)
            self.assertEqual(reference.bundled_cli, backend)


class PackagedStagedUpdateTests(ReleasedBindingUpdateTests):
    def setUp(self):
        super().setUp()
        backend = self.bundle / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
        backend.parent.mkdir(parents=True)
        self.bundled.rename(backend)
        self.bundled = backend
        self.entry = self.bundle / 'Contents/Resources/codex-cli/bin/codex'
        self.write(self.entry, '#!/bin/sh\nexec "$(dirname "$0")/../CodexCLI.app/Contents/MacOS/codex" "$@"\n')

    def test_invalid_installed_layout_fails_before_installer(self):
        self.bundled.unlink()
        marker = self.root / 'installer-ran'
        self.installer.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        result = self.command('stage', '--version', '2.0.0', '--install-dir', str(self.target.parent),
                              '--skip-source-check', '--skip-proxy', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('desktop.current.cli_invalid', result.stdout + result.stderr)
        self.assertIn(str(self.bundled), result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_packaged_entry_change_invalidates_stage(self):
        self.initialize()
        result = self.command('stage', '--current', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        staged = json.loads(result.stdout)
        self.entry.write_text(self.entry.read_text() + '# replacement\n')
        result = self.command('apply', staged['update_id'], '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['state'], 'stale', result.stdout + result.stderr)

    def test_check_official_uses_same_packaged_executable(self):
        result = subprocess.run([str(WRAPPER), '--skip-self-update', 'check-official'],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.bundled), result.stdout)
        self.assertIn('codex-cli 2.0.0', result.stdout)

    def test_present_packaged_layout_does_not_fall_back_to_legacy(self):
        self.write(self.bundle / 'Contents/Resources/codex', '#!/bin/sh\necho codex-cli 1.0.0\n')
        self.bundled.unlink()
        result = self.command('stage', '--version', '2.0.0', '--install-dir', str(self.target.parent),
                              '--skip-source-check', '--skip-proxy', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.bundled), result.stdout + result.stderr)

    def test_linked_backend_is_rejected(self):
        outside = self.root / 'outside-codex'
        self.bundled.rename(outside)
        self.bundled.symlink_to(outside)
        result = self.command('stage', '--version', '2.0.0', '--install-dir', str(self.target.parent),
                              '--skip-source-check', '--skip-proxy', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('desktop.current.cli_invalid', result.stdout + result.stderr)

    def test_nonexecutable_entry_is_rejected(self):
        self.entry.chmod(0o644)
        result = self.command('stage', '--version', '2.0.0', '--install-dir', str(self.target.parent),
                              '--skip-source-check', '--skip-proxy', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('desktop.current.cli_invalid', result.stdout + result.stderr)
        self.assertIn(str(self.entry), result.stdout + result.stderr)

    def test_linked_entry_is_rejected(self):
        outside = self.root / 'outside-entry'
        self.entry.rename(outside)
        self.entry.symlink_to(outside)
        result = self.command('stage', '--version', '2.0.0', '--install-dir', str(self.target.parent),
                              '--skip-source-check', '--skip-proxy', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('desktop.current.cli_invalid', result.stdout + result.stderr)
        self.assertIn(str(self.entry), result.stdout + result.stderr)

    def test_linked_packaged_directory_is_rejected(self):
        package = self.bundle / 'Contents/Resources/codex-cli'
        outside = self.root / 'outside-package'
        package.rename(outside)
        package.symlink_to(outside, target_is_directory=True)
        result = self.command('stage', '--version', '2.0.0', '--install-dir', str(self.target.parent),
                              '--skip-source-check', '--skip-proxy', '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('desktop.current.cli_invalid', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
