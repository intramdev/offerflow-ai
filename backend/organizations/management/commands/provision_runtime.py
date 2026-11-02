"""Provision the restricted runtime database role and verify isolation.

Runs on the privileged `admin` connection (see OFFERFLOW_DB_ADMIN_USER):
creates the runtime role if missing, applies backend/db/runtime_roles.sql,
then verifies attributes, grants, bootstrap helpers, and row policies.

Idempotent: safe to re-run after migrations add tables.
"""

import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from organizations.rls import (
    BOOTSTRAP_FUNCTIONS,
    BOOTSTRAP_OWNER,
    TENANT_POLICIES,
)

_ROLE_RE = re.compile(r"[a-z_][a-z0-9_]*")

SQL_PATH = Path(__file__).resolve().parents[3] / "db" / "runtime_roles.sql"


class Command(BaseCommand):
    help = "Provision the restricted runtime DB role and verify RLS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--app-role",
            default="",
            help="Runtime role to provision (default: default connection user).",
        )
        parser.add_argument(
            "--schema-owner",
            default="",
            help="Role owning the tables (default: admin connection user).",
        )
        parser.add_argument(
            "--app-password",
            default="",
            help="Password for the runtime role (default: default connection password).",
        )
        parser.add_argument(
            "--verify-only",
            action="store_true",
            help="Check provisioning without changing any role.",
        )

    def handle(self, *args, **options):
        if "admin" not in connections:
            raise CommandError(
                "Set OFFERFLOW_DB_ADMIN_USER/OFFERFLOW_DB_ADMIN_PASSWORD "
                "to provision with a privileged connection."
            )
        admin = connections["admin"]
        if admin.vendor != "postgresql":
            raise CommandError("Provisioning needs the PostgreSQL engine.")
        default = connections["default"]
        app_role = options["app_role"] or default.settings_dict["USER"]
        schema_owner = options["schema_owner"] or admin.settings_dict["USER"]
        for label, value in (("app role", app_role), ("schema owner", schema_owner)):
            if not _ROLE_RE.fullmatch(value or ""):
                raise CommandError(f"Refusing unsafe {label} name: {value!r}.")
        if not options["verify_only"]:
            password = options["app_password"] or default.settings_dict.get(
                "PASSWORD", ""
            )
            self._ensure_app_role(admin, app_role, password)
            self._apply_sql(admin, app_role, schema_owner)
        self._verify(admin, app_role)
        self.stdout.write(
            self.style.SUCCESS(f"Runtime role {app_role!r} provisioned and verified.")
        )

    def _ensure_app_role(self, admin, app_role, password):
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", [app_role]
            )
            exists = cursor.fetchone() is not None
            if not exists:
                cursor.execute(
                    f"CREATE ROLE {app_role} WITH LOGIN NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                )
            else:
                cursor.execute(
                    f"ALTER ROLE {app_role} WITH LOGIN NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                )
            if password:
                cursor.execute(
                    f"ALTER ROLE {app_role} WITH PASSWORD %s", [password]
                )

    def _apply_sql(self, admin, app_role, schema_owner):
        try:
            template = SQL_PATH.read_text()
        except OSError as exc:
            raise CommandError(f"Cannot read {SQL_PATH}: {exc}.") from exc
        db_name = admin.settings_dict["NAME"]
        if not _ROLE_RE.fullmatch(db_name or ""):
            raise CommandError(f"Refusing unsafe database name: {db_name!r}.")
        script = (
            template.replace("{{APP_ROLE}}", app_role)
            .replace("{{SCHEMA_OWNER}}", schema_owner)
            .replace("{{DB_NAME}}", db_name)
        )
        for token in ("{{APP_ROLE}}", "{{SCHEMA_OWNER}}", "{{DB_NAME}}"):
            if token in script:
                raise CommandError(
                    f"Unsubstituted variable {token} left in runtime_roles.sql."
                )
        with admin.cursor() as cursor:
            cursor.execute(script)

    def _verify(self, admin, app_role):
        problems = []
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                "WHERE rolname = %s",
                [app_role],
            )
            row = cursor.fetchone()
            if row is None:
                problems.append(f"role {app_role!r} does not exist")
            elif row[0] or row[1] or not row[2]:
                problems.append(
                    f"role {app_role!r} must be LOGIN without SUPERUSER/BYPASSRLS"
                )
            for table, policy in TENANT_POLICIES.items():
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = %s",
                    [table],
                )
                flags = cursor.fetchone()
                if flags is None:
                    problems.append(f"table {table} is missing")
                    continue
                if not (flags[0] and flags[1]):
                    problems.append(
                        f"table {table} lacks enforced row security"
                    )
                cursor.execute(
                    "SELECT 1 FROM pg_policy WHERE polname = %s", [policy]
                )
                if cursor.fetchone() is None:
                    problems.append(f"policy {policy} is missing")
            cursor.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = %s",
                [BOOTSTRAP_OWNER],
            )
            if cursor.fetchone() is None:
                problems.append(f"role {BOOTSTRAP_OWNER!r} is missing")
            for signature in BOOTSTRAP_FUNCTIONS:
                name, _, _args = signature.partition("(")
                cursor.execute(
                    "SELECT r.rolname FROM pg_proc p "
                    "JOIN pg_roles r ON r.oid = p.proowner "
                    "WHERE p.proname = %s",
                    [name],
                )
                owner = cursor.fetchone()
                if owner is None:
                    problems.append(f"function {signature} is missing")
                elif owner[0] != BOOTSTRAP_OWNER:
                    problems.append(
                        f"function {signature} owned by {owner[0]!r}, "
                        f"expected {BOOTSTRAP_OWNER!r}"
                    )
                cursor.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    [app_role, signature],
                )
                if not cursor.fetchone()[0]:
                    problems.append(
                        f"role {app_role!r} cannot EXECUTE {signature}"
                    )
        if problems:
            raise CommandError(
                "Isolation verification failed:\n- " + "\n- ".join(problems)
            )
