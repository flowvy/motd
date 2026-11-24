import unittest

from flowvy_motd.dashboard import COLORS, RESET, app_status, render_dashboard
from flowvy_motd.inventory import Component
from flowvy_motd.snapshot import NETBIRD_CONNECTION


class DashboardTests(unittest.TestCase):
    def setUp(self):
        web = Component('unit:web.service', 'Web', state='running')
        update = Component('unit:web-update.service', 'Update', state='failed', detail='exit-code')
        self.components = {component.id: component for component in (web, update)}
        self.app = {'name': 'Web app', 'members': list(self.components), 'primary': web.id,
                    'check_url': 'http://127.0.0.1:8080/health'}

    def test_service_display_name_preserves_netbird_connection_status(self):
        netbird = Component('unit:netbird.service', 'netbird.service', source='systemd', state='running')
        settings = {'services': ['netbird.service'], 'service_labels': {'netbird.service': 'Mesh VPN'}}
        text = render_dashboard(settings, {}, {}, {netbird.id: netbird},
                                {NETBIRD_CONNECTION: ('disconnected', '')}, color=False)
        self.assertTrue(any('Mesh VPN' in line and 'Disconnected' in line for line in text.splitlines()))
        self.assertNotIn('netbird.service', text)

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

    def test_group_displays_the_selected_service_state_as_its_status(self):
        self.components['unit:web-update.service'] = Component(
            'unit:web-update.service', 'Update', state='waiting', schedule='scheduled')
        tone, status, _ = app_status(self.app, self.components, None)
        self.assertEqual((tone, status), ('ok', 'Running'))

        self.components['unit:web.service'] = Component('unit:web.service', 'Web', state='failed', detail='exit-code')
        tone, status, details = app_status(self.app, self.components, None)
        self.assertEqual((tone, status), ('bad', 'Failed'))
        self.assertIn('Web · exit-code', details)

    def test_unverified_groups_summarize_observed_issues(self):
        tone, status, _ = app_status({**self.app, 'primary': '', 'check_url': ''}, self.components, None)
        self.assertEqual((tone, status), ('warn', '1 issue in 2 components'))

    def test_resource_bars_have_equal_side_padding_and_aligned_values(self):
        gib = 1024 ** 3
        metrics = {'memory': (gib, 4 * gib), 'swap': (0, 2 * gib), 'disk': (2 * gib, 4 * gib)}
        text = render_dashboard({}, metrics, {}, {}, {}, width=80, color=False)
        rows = dict(line.split('│', 1) for line in text.splitlines() if '│' in line)
        self.assertEqual({label.strip(): value for label, value in rows.items()}, {
            'RAM': '  −−−·········  1.0 GiB / 4.0 GiB',
            'Swap': '  ············  0 MiB / 2.0 GiB',
            'Disk': '  −−−−−−······  2.0 GiB / 4.0 GiB',
        })

    def test_colored_resource_bar_uses_bold_ticks_for_usage(self):
        gib = 1024 ** 3
        text = render_dashboard({}, {'memory': (gib, 4 * gib)}, {}, {}, {})
        ram = next(line for line in text.splitlines() if 'RAM' in line)
        self.assertEqual(
            ram.split('│', 1)[1],
            f'  {COLORS["accent"]}−−−{RESET}{COLORS["muted"]}−−−−−−−−−{RESET}  1.0 GiB / 4.0 GiB',
        )

    def test_disabled_swap_preserves_the_resource_value_column(self):
        gib = 1024 ** 3
        text = render_dashboard({}, {'memory': (gib, 4 * gib), 'swap': (0, 0)}, {}, {}, {}, color=False)
        ram, swap = [line for line in text.splitlines() if line.startswith(('RAM', 'Swap'))]
        self.assertIn('Disabled', swap)
        self.assertEqual(swap.index('Disabled'), ram.index('1.0 GiB'))

    def test_small_terminal_is_plain_and_does_not_wrap(self):
        settings = {'provider': 'Example', 'role': '', 'location': '', 'services': [],
                    'containers': [], 'applications': [], 'compact': False}
        text = render_dashboard(settings, {'host': 'edge', 'disk': (3, 10)}, {}, {}, {},
                                width=40, plain=True)
        self.assertIn('Disk', text)
        self.assertNotIn('Disk /', text)
        self.assertNotIn('\x1b[', text)
        self.assertTrue(all(len(line) <= 40 for line in text.splitlines()))
