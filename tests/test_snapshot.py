import threading
import unittest
from unittest.mock import patch

from flowvy_motd import inventory, snapshot
from flowvy_motd.settings import normalize


class SnapshotTests(unittest.TestCase):
    def test_slow_docker_does_not_discard_completed_systemd_checks(self):
        release_docker = threading.Event()
        service_id = 'unit:ssh.service'
        container_id = 'container:web'
        service = inventory.Component(service_id, 'ssh.service', state='running')
        config = normalize({'services': ['ssh.service'], 'containers': [container_id]})

        def slow_docker(ids, deadline):
            release_docker.wait(1)
            return {}

        with patch.object(inventory, '_collect_systemd', return_value={service_id: service}), \
             patch.object(inventory, '_collect_docker', side_effect=slow_docker), \
             patch.object(snapshot, 'network_metrics', return_value={}):
            try:
                _, _, components, _ = snapshot.snapshot(config, timeout=0.1)
            finally:
                release_docker.set()

        self.assertEqual(components[service_id], service)
        self.assertEqual(components[container_id].state, 'unavailable')

    def test_login_leaves_time_for_a_slow_docker_check(self):
        container_id = 'container:web'
        container = inventory.Component(container_id, 'web', state='running')

        def docker(ids, deadline):
            if deadline - snapshot.time.monotonic() < 1.465:
                return {}
            return {container_id: container}

        with patch.object(inventory, '_collect_docker', side_effect=docker), \
             patch.object(snapshot, 'network_metrics', return_value={}):
            _, _, components, _ = snapshot.snapshot(normalize({'containers': [container_id]}))

        self.assertEqual(components[container_id], container)

    def test_cpu_usage_uses_kernel_counters_for_the_visible_cpus(self):
        def stat_sample(visible, other):
            return f'cpu0 {visible}\ncpu1 {other}\ncpu2 30 0 0 300 0 0 0 0'

        samples = iter([
            stat_sample('10 0 0 90 0 0 0 0', '0 0 0 100 0 0 0 0'),
            stat_sample('30 0 0 130 20 0 0 20', '100 0 0 100 0 0 0 0'),
        ])
        with patch.object(snapshot, 'native_metrics', return_value={'cpu': '1 logical CPU'}), \
             patch.object(snapshot.os, 'sched_getaffinity', return_value={0}), \
             patch.object(snapshot, 'read_file', side_effect=lambda _: next(samples)), \
             patch.object(snapshot, 'network_metrics', return_value={}):
            metrics, _, _, _ = snapshot.snapshot(normalize({}))

        self.assertEqual(metrics['cpu'], '1 logical CPU · 20%')

    def test_decreasing_cpu_counter_does_not_report_full_usage(self):
        with patch.object(snapshot.os, 'sched_getaffinity', return_value={0}), \
             patch.object(snapshot, 'read_file', side_effect=[
                 'cpu0 10 0 0 90 20 0 0 0', 'cpu0 30 0 0 95 5 0 0 0',
             ]), patch.object(snapshot.time, 'sleep'):
            usage = snapshot.cpu_usage(snapshot.time.monotonic() + 1)

        self.assertIsNone(usage)

    def test_netbird_can_use_the_snapshot_budget(self):
        def netbird(timeout):
            if timeout < 0.4:
                return 'unavailable', 'NetBird check timed out'
            return 'connected', '100.64.0.1'

        config = normalize({'services': ['netbird.service']})
        with patch.object(inventory, 'read_netbird', side_effect=netbird), \
             patch.object(inventory, '_collect_systemd', return_value={}), \
             patch.object(snapshot, 'network_metrics', return_value={}):
            _, _, _, probes = snapshot.snapshot(config)

        self.assertEqual(probes[snapshot.NETBIRD_CONNECTION], ('connected', '100.64.0.1'))
