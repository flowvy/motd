"""Look up the public IP location while an interactive configuration is open."""

import http.client
import json
from queue import Empty, SimpleQueue
from threading import Thread
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

_ENDPOINT = 'https://ipwho.is/?fields=success,city,country'
_TIMEOUT = 3.0
_MAX_RESPONSE = 8192


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(_NoRedirect())


def lookup_location():
    request = Request(_ENDPOINT, headers={'Accept': 'application/json', 'User-Agent': 'Flowvy-MOTD'})
    try:
        with _opener.open(request, timeout=_TIMEOUT) as response:
            raw = response.read(_MAX_RESPONSE + 1)
        if len(raw) > _MAX_RESPONSE:
            return None
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return None

    if not isinstance(payload, dict) or payload.get('success') is not True:
        return None
    parts = []
    for field in ('city', 'country'):
        value = payload.get(field)
        if isinstance(value, str) and 0 < len(value) <= 78 and value.isprintable() and value.strip():
            parts.append(value.strip())
    return ', '.join(dict.fromkeys(parts)) or None


class LocationLookup:
    """A one-shot lookup. Polling keeps the menu responsive even during DNS delays."""

    def __init__(self, callback=lookup_location, timeout=_TIMEOUT):
        self._deadline = time.monotonic() + timeout
        self._result = SimpleQueue()
        self._state = ('pending', '')
        Thread(target=self._run, args=(callback,), name='flowvy-location', daemon=True).start()

    def _run(self, callback):
        result = callback()
        self._result.put((time.monotonic(), result))

    def poll(self):
        if self._state[0] != 'pending':
            return self._state
        try:
            finished, value = self._result.get_nowait()
        except Empty:
            if time.monotonic() >= self._deadline:
                self._state = ('unavailable', '')
        else:
            self._state = ('detected', value) if value and finished <= self._deadline else ('unavailable', '')
        return self._state
