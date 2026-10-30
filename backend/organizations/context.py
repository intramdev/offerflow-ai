"""Database tenant context for row-level security.

Row policies compare tenant columns against these settings. Context is
session-scoped with an explicit reset because requests run in autocommit, so
there is no ambient transaction to scope the settings to. The middleware sets
context for the active membership and always resets it afterwards; workers
must do the same around tenant work. No context means no tenant rows.
"""

from __future__ import annotations

from django.db import connection

ORG_CONTEXT_KEY = "app.current_org_id"
USER_CONTEXT_KEY = "app.current_user_id"


def set_tenant_context(organization_id: int, user_id: int) -> None:
    """Publish the active tenant and user for row-policy evaluation."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, %s, false)", [ORG_CONTEXT_KEY, str(organization_id)])
        cursor.execute("SELECT set_config(%s, %s, false)", [USER_CONTEXT_KEY, str(user_id)])


def reset_tenant_context() -> None:
    """Clear tenant context so pooled or reused connections leak nothing."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config(%s, '', false)", [ORG_CONTEXT_KEY])
        cursor.execute("SELECT set_config(%s, '', false)", [USER_CONTEXT_KEY])
