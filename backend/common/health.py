"""Liveness endpoint with simulated-mode visibility (no secrets, no PII)."""

from __future__ import annotations

from django.conf import settings
from django.db import connection
from django.http import JsonResponse

from common.provider import ProviderNotConfigured, get_provider


def health_view(request):
    """Report service health, provider mode, and database reachability."""
    try:
        provider = get_provider()
        provider_name = provider.name
        simulated = provider.is_simulated
        provider_ok = True
    except ProviderNotConfigured:
        provider_name = "unavailable"
        simulated = True
        provider_ok = False
    try:
        connection.ensure_connection()
        database = "reachable"
    except Exception:
        database = "unreachable"
    # Running-role check for the isolation switch: True means the
    # runtime role bypasses row policy (migrate to a restricted role);
    # False means restricted; None means not applicable/unreachable.
    # No role name is exposed.
    role_privileged = None
    if database == "reachable" and connection.vendor == "postgresql":
        try:
            from organizations.context import current_role_is_privileged

            role_privileged = current_role_is_privileged()
        except Exception:
            role_privileged = None
    healthy = provider_ok and database == "reachable"
    payload = {
        "status": "ok" if healthy else "degraded",
        "mode": "simulated" if simulated else "live",
        "provider": provider_name,
        "database": database,
        "runtime_role_privileged": role_privileged,
        "version": settings.APP_VERSION,
    }
    return JsonResponse(payload, status=200 if healthy else 503)
