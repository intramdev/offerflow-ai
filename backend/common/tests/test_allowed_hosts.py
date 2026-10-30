"""Allowed hosts cover loopback browsers and the internal backend name.

The frontend calls Django as http://backend:8000 (middleware auth check,
API rewrites), so that Host header must validate even when .env predates
it. The name only resolves inside the container network.
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO_ROOT = Path(settings.BASE_DIR).resolve().parent


class AllowedHostsTests(SimpleTestCase):
    def test_internal_backend_host_allowed(self):
        self.assertIn("backend", settings.ALLOWED_HOSTS)

    def test_loopback_hosts_allowed(self):
        self.assertIn("127.0.0.1", settings.ALLOWED_HOSTS)
        self.assertIn("localhost", settings.ALLOWED_HOSTS)

    def test_example_env_documents_backend_host(self):
        text = (REPO_ROOT / ".env.example").read_text()
        line = next(
            line
            for line in text.splitlines()
            if line.startswith("OFFERFLOW_ALLOWED_HOSTS=")
        )
        self.assertIn("backend", line)
