from io import BytesIO
import json
import socket
import threading
import unittest
from unittest.mock import patch

from flowvy_motd.location import LocationLookup, lookup_location


class LocationTests(unittest.TestCase):
    @patch('flowvy_motd.location._opener')
    def test_lookup_requests_only_city_and_country(self, opener):
        opener.open.return_value = BytesIO(json.dumps({
            'success': True, 'city': 'Helsinki', 'country': 'Finland',
            'ip': '192.0.2.123',
        }).encode())
        self.assertEqual(lookup_location(), 'Helsinki, Finland')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://ipwho.is/?fields=success,city,country')
        self.assertGreater(opener.open.call_args.kwargs['timeout'], 0)

    @patch('flowvy_motd.location._opener')
    def test_falls_back_to_country(self, opener):
        opener.open.return_value = BytesIO(b'{"success":true,"city":"","country":"Finland"}')
        self.assertEqual(lookup_location(), 'Finland')

    @patch('flowvy_motd.location._opener')
    def test_failed_and_malformed_responses_return_no_location(self, opener):
        for data in (b'{"success":false,"city":"Helsinki"}', b'<html>', b'[]',
                     b'{"success":true,"city":"bad\\u001b"}', b' ' * 9000):
            with self.subTest(data=data[:80]):
                opener.open.return_value = BytesIO(data)
                self.assertIsNone(lookup_location())
        opener.open.side_effect = socket.timeout()
        self.assertIsNone(lookup_location())

    def test_menu_deadline_does_not_wait_for_blocked_lookup(self):
        release_worker = threading.Event()
        started = threading.Event()

        def blocked_request():
            started.set()
            release_worker.wait(2)
            return 'Late result'

        with patch('flowvy_motd.location.time.monotonic', return_value=10) as clock:
            result = LocationLookup(callback=blocked_request)
            try:
                self.assertTrue(started.wait(1))
                self.assertEqual(result.poll(), ('pending', ''))
                clock.return_value = 100
                self.assertEqual(result.poll(), ('unavailable', ''))
            finally:
                release_worker.set()
