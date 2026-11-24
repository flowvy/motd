import json
from pathlib import Path
import tempfile
import unittest

from flowvy_motd.settings import load, normalize, read_legacy, save, token, SettingsError


class SettingsTests(unittest.TestCase):
    def test_round_trip_and_reject_concurrent_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flowvy-motd' / 'config.toml'
            first = normalize({
                'provider': 'Helsinki "Edge" 🐦', 'services': ['ssh.service'],
                'containers': ['compose:netbird/dashboard'],
                'applications': [
                    {'name': 'NetBird', 'members': ['unit:ssh.service'], 'primary': 'unit:ssh.service',
                     'check_url': 'http://127.0.0.1:3030/health'},
                ],
            })
            save(path, first, token(path))
            self.assertEqual(load(path), first)
            previous = token(path)
            save(path, {**first, 'role': 'Proxy'}, previous)
            with self.assertRaises(SettingsError):
                save(path, first, previous)
            self.assertEqual(load(path)['role'], 'Proxy')

    def test_local_probe_does_not_permit_nonlocal_traffic(self):
        config = {'applications': [{'name': 'Bad', 'members': ['unit:ssh.service'],
                                    'check_url': 'http://127.0.0.1@example.com/'}]}
        with self.assertRaises(SettingsError):
            normalize(config)

    def test_legacy_lists_container_names_without_importing_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            path.write_text(json.dumps({'provider': 'Hetzner', 'services': ['ssh.service'],
                                        'containers': ['netbird-server']}))
            self.assertEqual(read_legacy(path)['containers'], ['container:netbird-server'])

    def test_preserves_unrelated_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            foreign = Path(tmp) / 'foreign'
            foreign.write_text('foreign')
            path.symlink_to(foreign)
            with self.assertRaises(SettingsError):
                save(path, normalize({}))
            self.assertEqual(foreign.read_text(), 'foreign')
            self.assertTrue(path.is_symlink())
