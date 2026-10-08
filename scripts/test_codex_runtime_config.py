from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import unittest

from codex_switch_runtime_config import ensure_internal_runtime_generation, runtime_compatibility_digest
from codex_switch_constants import SwitchError


class RuntimeConfigDigestTests(unittest.TestCase):
    def test_bookkeeping_does_not_invalidate_runtime_evidence(self):
        base = b'model = "test"\n[features]\napps = false\n'
        additions = (
            '[marketplaces.sample]\nsource = "local"\n',
            '[plugins.sample]\nenabled = true\n',
            '[skills]\nconfig = []\n',
            '[tui]\nnotifications = false\n[history]\npersistence = "none"\n',
            '[hooks.state."sha256:test"]\ntrusted = true\n',
            '[projects."/tmp/project"]\ntrust_level = "trusted"\n',
        )
        for addition in additions:
            with self.subTest(addition=addition):
                self.assertEqual(runtime_compatibility_digest(base),
                                 runtime_compatibility_digest(base + addition.encode()))

    def test_runtime_and_unknown_settings_remain_bound(self):
        base = b'model = "test"\n'
        for addition in ('model_provider = "custom"\n', '[features]\napps = false\n',
                         'approval_policy = "never"\n', 'sandbox_mode = "read-only"\n',
                         '[hooks]\nenabled = false\n', '[unknown]\nvalue = true\n',
                         '[projects."/tmp/project"]\nmodel = "other"\n'):
            with self.subTest(addition=addition):
                self.assertNotEqual(runtime_compatibility_digest(base),
                                    runtime_compatibility_digest(base + addition.encode()))

    def test_formatting_and_typed_values(self):
        self.assertEqual(runtime_compatibility_digest(b'model="test"\nx=1\n'),
                         runtime_compatibility_digest(b'# comment\nx = 1\nmodel = "test"\n'))
        self.assertNotEqual(runtime_compatibility_digest(b'x=1\n'),
                            runtime_compatibility_digest(b'x="1"\n'))
        self.assertNotEqual(runtime_compatibility_digest(b'x=2026-09-29\n'),
                            runtime_compatibility_digest(b'x="2026-09-29"\n'))

    def test_malformed_toml_is_not_reusable(self):
        with self.assertRaises(ValueError):
            runtime_compatibility_digest(b'[features\n')


class RuntimeLaunchBoundaryTests(unittest.TestCase):
    def test_same_home_sync_does_not_overwrite_concurrent_config_edit(self):
        import codex_switch_home_sync as sync
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            config = home / "config.toml"
            config.write_text('model="test"\n[plugins.test]\nenabled=true\n[features]\napps=false\n')
            profile = root / "profile.toml"
            profile.write_text(config.read_text())
            edited = config.read_text().replace('apps=false', 'apps=true')
            def concurrent_edit(*_args):
                config.write_text(edited)
                return ()
            with patch.object(sync, "_plan_bidirectional_desktop_state_writes", side_effect=concurrent_edit):
                sync.sync_profile_app_home_for_launch(home, home, profile, "internal", preserve_runtime_settings=True)
            self.assertEqual(config.read_text(), edited)

    def test_refresh_rejects_unsafe_manifest_before_preparation(self):
        import codex_switch_bindings as bindings
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "profiles/internal/manifest.json"
            manifest.parent.mkdir(parents=True)
            target = root / "target"
            target.write_text('{}')
            manifest.symlink_to(target)
            with patch.object(bindings, "cmd_set_bin") as prepare:
                with self.assertRaisesRegex(SwitchError, "non-symlink"):
                    ensure_internal_runtime_generation(store_root=root, fallback_home=root)
                manifest.unlink()
                manifest.write_bytes(b' ' * (1024 * 1024))
                with self.assertRaises(SwitchError):
                    ensure_internal_runtime_generation(store_root=root, fallback_home=root)
                prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
