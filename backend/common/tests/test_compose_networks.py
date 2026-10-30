"""Backend keeps a public-network attachment for its published ports.

An internal-only backend service gets its published ports silently
dropped: `docker compose config` renders the 8384 binding, yet the
container inspects as `"8000/tcp":[]` and the host gets connection
refused. The frontend (public + internal) proves the shape that works,
so the backend mirrors it while the worker stays internal-only.
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

REPO_ROOT = Path(settings.BASE_DIR).resolve().parent


def _service_list(text, service, key):
    """Values of an indented `key:` list inside one compose service."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"  {service}:")
    values, in_key = [], False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2:
            break
        if indent == 4 and stripped == f"{key}:":
            in_key = True
            continue
        if in_key:
            if indent == 6 and stripped.startswith("- "):
                values.append(stripped[2:].strip('"'))
            elif indent <= 4:
                break
    return values


class ComposeNetworkTests(SimpleTestCase):
    def test_backend_keeps_public_network_for_published_ports(self):
        text = (REPO_ROOT / "compose.yaml").read_text()
        networks = _service_list(text, "backend", "networks")
        self.assertIn("public", networks)
        self.assertIn("internal", networks)

    def test_backend_keeps_host_port_mapping(self):
        text = (REPO_ROOT / "compose.yaml").read_text()
        self.assertIn(
            "127.0.0.1:8384:8000", _service_list(text, "backend", "ports")
        )

    def test_worker_stays_internal_only(self):
        text = (REPO_ROOT / "compose.yaml").read_text()
        self.assertEqual(["internal"], _service_list(text, "worker", "networks"))
