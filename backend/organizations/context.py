"""Database tenant context for row-level security.

Row policies compare tenant columns against these settings. Context is
session-scoped with an explicit reset because requests run in autocommit, so
there is no ambient transaction to scope the settings to. The middleware sets
user context for every authenticated request, adds the active organization's
context when membership resolves, and always resets both afterwards; workers
must do the same around tenant work. No context means no tenant rows.

Service helpers narrow the organization context to the authorized target
after each membership check, so operating on a non-active member
organization stays visible while foreign tenants stay hidden.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection

ORG_CONTEXT_KEY = "app.current_org_id"
USER_CONTEXT_KEY = "app.current_user_id"


def set_user_context(user_id: int) -> None:
    """Publish only the authenticated user for row-policy evaluation.

    The user id comes from the verified session, never from tenant data,
    so it is safe to publish before membership resolution. Membership
    policies admit a user's own rows on this key alone.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, %s, false)", [USER_CONTEXT_KEY, str(user_id)]
        )


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


@contextmanager
def tenant_scope(organization_id: int, user_id: int):
    """Publish tenant context for a block, then always clear it.

    Workers and one-off jobs use this so tenant work can never leak onto
    a reused connection, even when the block raises.
    """
    set_tenant_context(organization_id, user_id)
    try:
        yield
    finally:
        reset_tenant_context()


def is_missing_function(error: BaseException) -> bool:
    """True when a database error means the routine does not exist.

    SQLSTATE 42883 (undefined function) arrives on `sqlstate` with
    psycopg3 and on `pgcode` with psycopg2; Django wraps the driver
    error, so the whole cause/context chain is inspected.
    """
    seen: set[int] = set()
    node: BaseException | None = error
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if getattr(node, "pgcode", None) == "42883":
            return True
        if getattr(node, "sqlstate", None) == "42883":
            return True
        node = getattr(node, "__cause__", None) or getattr(
            node, "__context__", None
        )
    return False


def current_role_is_privileged() -> bool:
    """True when the connection role bypasses row policy (superuser/BYPASSRLS).

    Only the legacy single-role deployment answers True; it exists so
    databases migrated before role separation keep working until the
    operator provisions the restricted runtime role.
    """
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user"
        )
        found = cursor.fetchone()
    return bool(found and found[0])
