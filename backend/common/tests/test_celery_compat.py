"""RabbitMQ >= 4.3 refuses transient non-exclusive queues (541).

Celery's remote control, mingle, and gossip declare exactly those (pidbox
and event mailboxes), crash-looping the worker. The single local worker
needs none of them, so they stay disabled here and in Compose.
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO_ROOT = Path(settings.BASE_DIR).resolve().parent


class CeleryBrokerCompatTests(SimpleTestCase):
    def test_remote_control_disabled(self):
        self.assertFalse(settings.CELERY_WORKER_ENABLE_REMOTE_CONTROL)

    def test_compose_worker_skips_mingle_and_gossip(self):
        base = (REPO_ROOT / "compose.yaml").read_text()
        base_cmd = [
            line for line in base.splitlines() if "celery -A config worker" in line
        ]
        self.assertEqual(len(base_cmd), 1)
        self.assertIn("--without-mingle", base_cmd[0])
        self.assertIn("--without-gossip", base_cmd[0])
        overlay = (REPO_ROOT / "compose.integration.yaml").read_text()
        for line in overlay.splitlines():
            if "celery -A config worker" in line:
                self.assertIn("--without-mingle", line)
                self.assertIn("--without-gossip", line)
