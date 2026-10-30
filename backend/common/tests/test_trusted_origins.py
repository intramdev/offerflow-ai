"""CSRF trusted origins cover every local browser entry point.

Django rejects browser POSTs whose Origin header is untrusted, which turns
every frontend login into an invisible 403. Both loopback hosts on both
published ports must stay trusted.
"""

from django.conf import settings
from django.test import SimpleTestCase


class TrustedOriginsTests(SimpleTestCase):
    def test_local_origins_trusted(self):
        origins = set(settings.CSRF_TRUSTED_ORIGINS)
        for host in ("localhost", "127.0.0.1"):
            for port in ("4344", "8384"):
                self.assertIn(f"http://{host}:{port}", origins)
