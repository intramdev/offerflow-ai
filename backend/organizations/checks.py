"""Deployment checks for database tenant isolation."""

from django.core.checks import Warning


def check_runtime_role_is_restricted(app_configs, **kwargs):
    """Warn when the runtime connection bypasses row policy.

    A superuser or BYPASSRLS runtime role makes every tenant policy dead
    configuration: cross-tenant reads succeed at the database layer even
    when application filters behave. Skipped silently when PostgreSQL is
    unreachable (the database checks report that themselves).
    """
    from django.db import connections
    from django.db.utils import OperationalError, ProgrammingError

    connection = connections["default"]
    if connection.vendor != "postgresql":
        return []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            found = cursor.fetchone()
    except (OperationalError, ProgrammingError):
        return []
    if found and (found[0] or found[1]):
        return [
            Warning(
                "The runtime database role bypasses row-level security.",
                hint=(
                    "Provision a restricted runtime role with "
                    "provision_runtime and point OFFERFLOW_DB_USER at it; "
                    "keep migrations on OFFERFLOW_DB_ADMIN_USER."
                ),
                id="offerflow.W001",
            )
        ]
    return []
