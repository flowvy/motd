import unittest

from flowvy_motd.dashboard import app_status, render_dashboard
from flowvy_motd.inventory import Component


class DashboardTests(unittest.TestCase):
    def setUp(self):
        web = Component('unit:web.service', 'Web', state='running')
        update = Component('unit:web-update.service', 'Update', state='failed', detail='exit-code')
        self.components = {component.id: component for component in (web, update)}
        self.app = {'name': 'Web app', 'members': list(self.components), 'primary': web.id,
                    'check_url': 'http://127.0.0.1:8080/health'}

    def test_confirmed_main_function_and_failed_helper(self):
        tone, status, details = app_status(self.app, self.components, ('ok', 'HTTP 200'))
        self.assertEqual((tone, status), ('warn', 'Available with warnings'))
        self.assertIn('Update', ' '.join(details))

    def test_stopped_helper_is_not_an_issue_when_waiting(self):
        self.components['unit:web-update.service'] = Component(
            'unit:web-update.service', 'Update', state='waiting', schedule='scheduled')
        tone, status, _ = app_status(self.app, self.components, ('ok', 'HTTP 200'))
        self.assertEqual((tone, status), ('ok', 'Available'))

    def test_primary_failure_reports_failure_not_a_healthy_process(self):
        tone, status, details = app_status(self.app, self.components, ('failed', 'HTTP 503'))
        self.assertEqual((tone, status), ('bad', 'Unavailable'))
        self.assertIn('HTTP 503', details)

    def test_unverified_groups_summarize_observed_issues(self):
        tone, status, _ = app_status({**self.app, 'primary': '', 'check_url': ''}, self.components, None)
        self.assertEqual((tone, status), ('warn', '1 issue in 2 components'))

    def test_small_terminal_is_plain_and_does_not_wrap(self):
        settings = {'provider': 'Example', 'role': '', 'location': '', 'services': [],
                    'containers': [], 'applications': [], 'compact': False}
        text = render_dashboard(settings, {'host': 'edge', 'disk': (3, 10)}, {}, {}, {},
                                width=40, plain=True)
        self.assertIn('Disk', text)
        self.assertNotIn('Disk /', text)
        self.assertNotIn('\x1b[', text)
        self.assertTrue(all(len(line) <= 40 for line in text.splitlines()))
