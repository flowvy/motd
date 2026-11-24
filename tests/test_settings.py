import json
from pathlib import Path
import tempfile
import unittest

from flowvy_motd.settings import load, normalize, read_legacy, save, token, SettingsError


class SettingsTests(unittest.TestCase):
    def test_service_display_names_round_trip_only_for_selected_units(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            data = {'services': ['web-worker.service', 'ssh.service'], 'service_labels': {
                'web-worker.service': '  Worker "EU" 🐦  ',
                'ssh.service': '',
                'inactive.service': 'Inactive service',
            }}
            save(path, data)
            self.assertEqual(load(path)['service_labels'], {'web-worker.service': 'Worker "EU" 🐦'})
            save(path, {'services': data['services']})
            self.assertNotIn('service_labels', load(path))
            self.assertNotIn('[service_labels]', path.read_text())

    def test_service_display_names_validate_table_keys_and_values(self):
        for labels in (['Remote access'], {'ssh.service': 17}, {'ssh.service': 'two\nlines'},
                       {'ssh.service': 'x' * 65}, {'sub/ssh.service': 'SSH'}):
            with self.subTest(labels=labels), self.assertRaises(SettingsError):
                normalize({'services': ['ssh.service'], 'service_labels': labels})

    def test_round_trip_with_hyphenated_systemd_services(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'flowvy-motd' / 'config.toml'
            data = {
                'services': ['data-refresh.service', r'\x2dcheck.service'],
                'applications': [{
                    'name': 'Worker',
                    'members': ['unit:service-worker@eu-west.service', 'unit:cert-refresh.service'],
                    'primary': 'unit:service-worker@eu-west.service',
                }],
            }
            save(path, data)
            config = load(path)
            self.assertEqual(config['services'], data['services'])
            for field, value in data['applications'][0].items():
                self.assertEqual(config['applications'][0][field], value)

    def test_systemd_service_names_reject_path_separators(self):
        with self.assertRaises(SettingsError):
            normalize({'services': ['subdir/worker.service']})
        with self.assertRaises(SettingsError):
            normalize({'applications': [{'name': 'Worker', 'members': ['unit:subdir/worker.service']}]})

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
