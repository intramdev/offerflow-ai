"""Provider adapter selection with fail-closed defaults.

Simulated mode is the default: the fixture provider is a pure function of its
input and performs zero network I/O. The paid path requires an organization,
the explicit paid switch, a well-formed key, and a configured model id — and
even then the token budget must approve every dispatch. A missing key, an
unknown provider name, or a zero budget never silently selects a paid path,
and a paid failure never silently falls back to fixtures.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


class ProviderNotConfigured(Exception):
    """Raised when a non-simulated provider path cannot be fully approved."""


class BaseProvider:
    """Common interface for interpretation providers."""

    name = "base"
    is_simulated = True

    def interpret_table(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Interpret a table-like payload. Implemented per provider."""
        raise NotImplementedError


class FixtureProvider(BaseProvider):
    """Deterministic synthetic responses. Pure function; no network I/O."""

    name = "fixture"
    is_simulated = True

    def interpret_table(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        rows = payload.get("rows", []) if isinstance(payload, Mapping) else []
        if not isinstance(rows, list):
            rows = []
        return {
            "simulated": True,
            "label": "synthetic fixture response",
            "row_count": len(rows),
            "rows": [{"index": index, "echo": row} for index, row in enumerate(rows)],
        }


def _env_int(name: str, default: int) -> int:
    try:
        value = int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        raise ProviderNotConfigured(f"Invalid integer for {name}.") from None
    if value <= 0:
        raise ProviderNotConfigured(f"Non-positive value for {name}.")
    return value


def get_provider(organization_id: int | None = None):
    """Return the configured provider, refusing anything unapproved."""
    requested = (os.environ.get("OFFERFLOW_PROVIDER", "fixture") or "").strip().lower()
    if requested == "fixture":
        return FixtureProvider()
    if requested == "meta":
        from common.meta import BASE_URL_DEFAULT, KEY_PREFIX, MetaConfig, MetaProvider

        if organization_id is None:
            raise ProviderNotConfigured("Paid dispatch needs an organization.")
        if (os.environ.get("OFFERFLOW_ALLOW_PAID", "false") or "").lower() != "true":
            raise ProviderNotConfigured("Paid dispatch switch is off.")
        key = os.environ.get("OFFERFLOW_META_API_KEY", "")
        if not key:
            raise ProviderNotConfigured("No Meta API key configured.")
        if not key.startswith(KEY_PREFIX):
            raise ProviderNotConfigured("Meta API key format unexpected.")
        model = (os.environ.get("OFFERFLOW_META_MODEL", "") or "").strip()
        if not model:
            raise ProviderNotConfigured("No Meta model configured.")
        return MetaProvider(
            MetaConfig(
                organization_id=organization_id,
                api_key=key,
                model=model,
                base_url=(
                    os.environ.get("OFFERFLOW_META_BASE_URL", "") or BASE_URL_DEFAULT
                ),
                timeout_seconds=_env_int("OFFERFLOW_META_TIMEOUT_SECONDS", 60),
                max_input_tokens=_env_int("OFFERFLOW_META_MAX_INPUT_TOKENS", 8000),
                max_output_tokens=_env_int("OFFERFLOW_META_MAX_OUTPUT_TOKENS", 2000),
                max_in_flight=_env_int("OFFERFLOW_META_MAX_IN_FLIGHT", 1),
            )
        )
    raise ProviderNotConfigured(f"Unknown provider {requested!r}; refusing.")
