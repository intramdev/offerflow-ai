"""Next.js dev config keeps loopback origins interactive.

Without allowedDevOrigins, Next blocks dev resources (client bundles, HMR)
on 127.0.0.1: pages SSR-render but never hydrate, so forms submit natively
and nothing works, with zero errors. Localhost is allowed by default.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO_ROOT = Path(settings.BASE_DIR).resolve().parent


class FrontendConfigTests(SimpleTestCase):
    def test_allowed_dev_origins_covers_loopback(self):
        text = (REPO_ROOT / "frontend" / "next.config.ts").read_text()
        match = re.search(r"allowedDevOrigins:\s*\[(.*?)\]", text, re.DOTALL)
        self.assertIsNotNone(match, "allowedDevOrigins missing from next.config.ts")
        origins = match.group(1)
        self.assertIn("127.0.0.1", origins)
        self.assertIn("localhost", origins)

    def test_trailing_slash_preserved_for_api_proxy(self):
        text = (REPO_ROOT / "frontend" / "next.config.ts").read_text()
        self.assertRegex(text, r"skipTrailingSlashRedirect:\s*true")
