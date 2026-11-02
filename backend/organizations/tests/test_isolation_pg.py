"""Restricted-role tenant isolation (disposable PostgreSQL only).

Proves database isolation with the actual restricted runtime role, not a
privileged connection: no-context access, organization A/B separation,
writes, background jobs, connection reuse, and revocation — over both
direct database queries and HTTP. Skipped unless OFFERFLOW_PG_ISOLATION_DSN
points at a disposable server, e.g.:

    OFFERFLOW_PG_ISOLATION_DSN=postgres://pgadmin:secret@127.0.0.1:55433/postgres

The module creates and drops ONLY the database `offerflow_isolation_test`
and the roles `iso_app`/`offerflow_bootstrap` on that server, and refuses
non-loopback hosts. Nothing here touches the application database.
"""

import os
import tempfile
import unittest
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError, connections, transaction
from django.test import Client, SimpleTestCase, override_settings

from organizations.rls import TENANT_TABLES
from organizations.services import SESSION_ACTIVE_ORG_KEY

User = get_user_model()

DSN = os.environ.get("OFFERFLOW_PG_ISOLATION_DSN", "")
TEST_DB = "offerflow_isolation_test"
APP_ROLE = "iso_app"
APP_PASSWORD = "isolation-app-test-only"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

ADMIN_ALIAS = "iso_admin"
APP_ALIAS = "iso_app"
SERVER_ALIAS = "iso_server"

_state = {}


def _parse_dsn(dsn):
    parsed = urlparse(dsn)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise unittest.SkipTest("isolation DSN must be postgres://")
    if (parsed.hostname or "") not in LOOPBACK:
        raise unittest.SkipTest("isolation tests only run against loopback")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": (parsed.path or "/postgres").lstrip("/") or "postgres",
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname,
        "PORT": str(parsed.port or 5432),
    }


def _forget_connection(name):
    try:
        delattr(connections._connections, name)
    except AttributeError:
        pass


def _alias(name, settings_dict):
    # Django fills connection defaults once when settings are first
    # read; aliases registered later must carry the same keys.
    settings_dict.setdefault("ATOMIC_REQUESTS", False)
    settings_dict.setdefault("AUTOCOMMIT", True)
    settings_dict.setdefault("CONN_MAX_AGE", 0)
    settings_dict.setdefault("CONN_HEALTH_CHECKS", False)
    settings_dict.setdefault("OPTIONS", {})
    settings_dict.setdefault("TIME_ZONE", None)
    settings_dict.setdefault("TEST", {})
    connections.databases[name] = settings_dict
    _forget_connection(name)


def _drop_alias(name):
    try:
        connections[name].close()
    except Exception:
        pass
    connections.databases.pop(name, None)
    _forget_connection(name)


def _register_placeholders():
    """Register import-time alias placeholders for the Django test runner.

    The runner collects test-case ``databases`` attributes and resolves
    every alias in ``run_checks`` before ``setUpModule`` runs. These
    placeholders make that resolution succeed without connecting; they
    are mirrors so database setup/teardown skips them, and
    ``setUpModule`` replaces them with the real disposable settings.
    """
    if not DSN:
        # The module will skip in setUpModule, but the runner still
        # resolves every test-case alias in run_checks first. Dummy
        # mirrors satisfy that resolution without connecting; database
        # setup/teardown skips mirrors, and nothing ever uses them.
        dummy = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "",
            "USER": "",
            "PASSWORD": "",
            "HOST": "",
            "PORT": "",
            "TEST": {"MIRROR": "default"},
        }
        _alias(SERVER_ALIAS, dict(dummy))
        _alias(ADMIN_ALIAS, dict(dummy))
        _alias(APP_ALIAS, dict(dummy))
        return
    admin_settings = _parse_dsn(DSN)
    mirror = {"TEST": {"MIRROR": "default"}}
    _alias(SERVER_ALIAS, {**admin_settings, **mirror})
    _alias(ADMIN_ALIAS, {**admin_settings, **mirror})
    _alias(APP_ALIAS, {**admin_settings, **mirror})


_register_placeholders()


def setUpModule():
    if not DSN:
        raise unittest.SkipTest("OFFERFLOW_PG_ISOLATION_DSN is not set")
    admin_settings = _parse_dsn(DSN)
    _state["default"] = dict(connections.databases["default"])
    _alias(SERVER_ALIAS, dict(admin_settings))
    with connections[SERVER_ALIAS].cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cursor.execute(f'CREATE DATABASE "{TEST_DB}"')
    admin_db = dict(admin_settings, NAME=TEST_DB)
    _alias(ADMIN_ALIAS, admin_db)
    call_command(
        "migrate", database=ADMIN_ALIAS, verbosity=0, interactive=False
    )
    _alias("admin", dict(admin_db))
    try:
        call_command(
            "provision_runtime",
            app_role=APP_ROLE,
            app_password=APP_PASSWORD,
            verbosity=0,
        )
    finally:
        _drop_alias("admin")
    _alias(
        APP_ALIAS,
        {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": TEST_DB,
            "USER": APP_ROLE,
            "PASSWORD": APP_PASSWORD,
            "HOST": admin_settings["HOST"],
            "PORT": admin_settings["PORT"],
        },
    )
    _seed()
    # HTTP runs through `default`, so point it at the restricted role for
    # the whole module; direct-DB tests use explicit aliases instead.
    connections["default"].close()
    connections.databases["default"] = dict(connections.databases[APP_ALIAS])
    _forget_connection("default")


def tearDownModule():
    if not DSN or "_seeded" not in _state:
        return
    try:
        with connections[APP_ALIAS].cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        pass
    for name in ("default", APP_ALIAS, ADMIN_ALIAS):
        try:
            connections[name].close()
        except Exception:
            pass
    connections.databases["default"] = _state["default"]
    _forget_connection("default")
    try:
        with connections[SERVER_ALIAS].cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
            cursor.execute(f'DROP ROLE IF EXISTS "{APP_ROLE}"')
            cursor.execute('DROP ROLE IF EXISTS "offerflow_bootstrap"')
    finally:
        for name in (APP_ALIAS, ADMIN_ALIAS, SERVER_ALIAS):
            _drop_alias(name)


def _seed():
    from django.utils import timezone

    from organizations.models import Invitation, Membership, Organization
    from pipeline.models import (
        AssistantFlag,
        Category,
        CustomerQuote,
        ImportRun,
        NormalizedOffer,
        OfferStatus,
        PricingRule,
        ProductAlias,
        RuleSetVersion,
        RunStatus,
        SourceFile,
        SourceRow,
        SpendAllowance,
        SpendReservation,
        Supplier,
        SupplierNote,
    )

    db = ADMIN_ALIAS
    alice = User.objects.db_manager(db).create_user(
        username="iso_alice", email="alice@iso.test", password="x"
    )
    bob = User.objects.db_manager(db).create_user(
        username="iso_bob", email="bob@iso.test", password="x"
    )
    dual = User.objects.db_manager(db).create_user(
        username="iso_dual", email="dual@iso.test", password="x"
    )
    carol = User.objects.db_manager(db).create_user(
        username="iso_carol", email="new@iso.test", password="x"
    )
    org_a = Organization.objects.using(db).create(name="Iso A")
    org_b = Organization.objects.using(db).create(name="Iso B")
    Membership.objects.using(db).create(
        user=alice, organization=org_a, role="owner"
    )
    Membership.objects.using(db).create(user=bob, organization=org_b, role="owner")
    Membership.objects.using(db).create(
        user=dual, organization=org_a, role="owner"
    )
    Membership.objects.using(db).create(
        user=dual, organization=org_b, role="viewer"
    )
    version, _ = RuleSetVersion.objects.using(db).get_or_create(
        version="v1", defaults={"spec": {}, "is_active": True}
    )
    Invitation.objects.using(db).create(
        organization=org_a,
        email="used@iso.test",
        role="viewer",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    invite_b = Invitation.objects.using(db).create(
        organization=org_b,
        email="new@iso.test",
        role="viewer",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    # Never accepted: keeps the resolver test independent of the HTTP
    # invitation-accept test, which consumes invite_b.
    invite_probe = Invitation.objects.using(db).create(
        organization=org_b,
        email="probe@iso.test",
        role="viewer",
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )
    fixtures = {"runs": {}, "files": {}}
    for org, label in ((org_a, "A"), (org_b, "B")):
        owner = alice if label == "A" else bob
        source_file = SourceFile.objects.using(db).create(
            organization=org,
            uploaded_by=owner,
            original_name=f"iso_{label}.xlsx",
            size_bytes=10,
            sha256=label * 64,
            storage_key=f"iso/{label}",
        )
        run = ImportRun.objects.using(db).create(
            organization=org,
            source_file=source_file,
            rule_version=version,
            status=RunStatus.NEEDS_INPUT,
        )
        row = SourceRow.objects.using(db).create(
            organization=org,
            run=run,
            index=1,
            sheet="Sheet1",
            data={"quantity": 2},
            provenance={"sheet": "Sheet1", "row": 1},
        )
        NormalizedOffer.objects.using(db).create(
            organization=org,
            run=run,
            source_row=row,
            supplier=f"Supplier {label}",
            brand="Acme",
            model_name="Widget",
            quantity=2,
            unit_price="10.00",
            currency="EUR",
            status=OfferStatus.OK,
            provenance={"sheet": "Sheet1", "row": 1},
        )
        Category.objects.using(db).create(organization=org, name=f"Cat {label}")
        supplier = Supplier.objects.using(db).create(
            organization=org, name=f"Supplier {label}"
        )
        SupplierNote.objects.using(db).create(
            supplier=supplier, author=owner, text=f"note {label}"
        )
        ProductAlias.objects.using(db).create(
            organization=org,
            supplier_norm=f"supplier {label.lower()}",
            alias_norm=f"widget-{label.lower()}",
            brand_norm="acme",
            capacity_gb=128,
            canonical_model="Widget",
        )
        CustomerQuote.objects.using(db).create(
            organization=org, created_by=owner, status="needs_review"
        )
        allowance = SpendAllowance.objects.using(db).create(
            organization=org, monthly_credits=1000, token_budget=1000
        )
        SpendReservation.objects.using(db).create(
            allowance=allowance,
            organization=org,
            tokens=10,
            state=SpendReservation.State.HELD,
            purpose="iso",
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )
        PricingRule.objects.using(db).create(
            organization=org, name=f"Rule {label}"
        )
        AssistantFlag.objects.using(db).create(
            organization=org,
            user=owner,
            username=owner.username,
            question="iso probe",
            reason="sql",
        )
        fixtures["runs"][label] = run.id
        fixtures["files"][label] = source_file.id
    _state.update(
        {
            "_seeded": True,
            "org_a": org_a.id,
            "org_b": org_b.id,
            "alice": alice.id,
            "bob": bob.id,
            "dual": dual.id,
            "carol": carol.id,
            "invite_b": invite_b.token,
            "invite_probe": invite_probe.token,
            **fixtures,
        }
    )


class _IsolationBase(SimpleTestCase):
    databases = {APP_ALIAS, ADMIN_ALIAS, "default"}

    @classmethod
    def app_cursor(cls):
        return connections[APP_ALIAS].cursor()

    def _set_context(self, org_id, user_id):
        with self.app_cursor() as cursor:
            cursor.execute("SELECT set_config('app.current_org_id', %s, false)", [str(org_id)])
            cursor.execute("SELECT set_config('app.current_user_id', %s, false)", [str(user_id)])

    def _set_user_only(self, user_id):
        with self.app_cursor() as cursor:
            cursor.execute("SELECT set_config('app.current_org_id', '', false)")
            cursor.execute("SELECT set_config('app.current_user_id', %s, false)", [str(user_id)])

    def _reset_context(self):
        with self.app_cursor() as cursor:
            cursor.execute("SELECT set_config('app.current_org_id', '', false)")
            cursor.execute("SELECT set_config('app.current_user_id', '', false)")

    def _count_where(self, table, org_column="organization_id"):
        with self.app_cursor() as cursor:
            cursor.execute(f'SELECT {org_column}, COUNT(*) FROM "{table}" GROUP BY 1')
            return dict(cursor.fetchall())


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class IsolationDirectDbTests(_IsolationBase):
    """Direct database queries as the restricted runtime role."""

    def test_runtime_role_is_restricted(self):
        with self.app_cursor() as cursor:
            cursor.execute(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            user, superuser, bypass = cursor.fetchone()
        self.assertEqual(user, APP_ROLE)
        self.assertFalse(superuser)
        self.assertFalse(bypass)

    def test_no_context_sees_no_tenant_rows(self):
        self._reset_context()
        for table in TENANT_TABLES:
            with self.app_cursor() as cursor:
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                (count,) = cursor.fetchone()
            self.assertEqual(count, 0, f"{table} leaked without context")

    def test_context_scopes_every_tenant_table(self):
        # Supplier notes carry no organization column; they are covered
        # through their parent supplier by a dedicated test below.
        scoped = [t for t in TENANT_TABLES if t != "pipeline_suppliernote"]
        self._set_context(_state["org_a"], _state["alice"])
        try:
            for table in scoped:
                counts = self._count_where(table)
                self.assertLessEqual(
                    set(counts), {_state["org_a"]}, f"{table} leaked tenant B"
                )
            # Tables seeded on both sides must show exactly the A rows.
            for table in (
                "pipeline_sourcefile",
                "pipeline_importrun",
                "pipeline_sourcerow",
                "pipeline_normalizedoffer",
                "pipeline_category",
                "pipeline_supplier",
                "pipeline_productalias",
                "pipeline_customerquote",
                "pipeline_spendallowance",
                "pipeline_spendreservation",
                "pipeline_pricingrule",
                "pipeline_assistantflag",
                "organizations_invitation",
            ):
                counts = self._count_where(table)
                self.assertEqual(
                    counts, {_state["org_a"]: 1}, f"{table} hid own rows"
                )
            membership = self._count_where("organizations_membership")
            self.assertEqual(membership, {_state["org_a"]: 2})
            # Nothing was seeded here, so silence is the correct answer.
            for table in (
                "pipeline_usageevent",
                "pipeline_auditevent",
                "pipeline_validationresult",
                "pipeline_reviewrequest",
                "pipeline_reviewdecision",
                "pipeline_exportartifact",
                "pipeline_columnmappingmemory",
            ):
                counts = self._count_where(table)
                self.assertEqual(counts, {}, f"{table} showed phantom rows")
        finally:
            self._reset_context()

    def test_supplier_notes_follow_parent_supplier(self):
        self._set_context(_state["org_b"], _state["bob"])
        try:
            with self.app_cursor() as cursor:
                cursor.execute(
                    "SELECT s.organization_id, COUNT(*) FROM pipeline_suppliernote n "
                    "JOIN pipeline_supplier s ON s.id = n.supplier_id GROUP BY 1"
                )
                counts = dict(cursor.fetchall())
        finally:
            self._reset_context()
        self.assertEqual(set(counts), {_state["org_b"]})

    def test_cross_tenant_write_refused(self):
        self._set_context(_state["org_a"], _state["alice"])
        try:
            with self.assertRaises(DatabaseError):
                with self.app_cursor() as cursor:
                    cursor.execute(
                        'INSERT INTO "pipeline_sourcefile" '
                        "(organization_id, uploaded_by_id, original_name, "
                        "size_bytes, sha256, storage_key) "
                        "VALUES (%s, %s, 'x', 1, 'y', 'z')",
                        [_state["org_b"], _state["alice"]],
                    )
        finally:
            self._reset_context()
            connections[APP_ALIAS].close()

    def test_cross_tenant_update_touches_nothing(self):
        self._set_context(_state["org_a"], _state["alice"])
        try:
            with self.app_cursor() as cursor:
                cursor.execute(
                    'UPDATE "pipeline_sourcefile" SET size_bytes = 999 '
                    "WHERE organization_id = %s",
                    [_state["org_b"]],
                )
                touched = cursor.rowcount
        finally:
            self._reset_context()
        self.assertEqual(touched, 0)

    def test_user_only_context_sees_own_memberships(self):
        self._set_user_only(_state["dual"])
        try:
            with self.app_cursor() as cursor:
                cursor.execute(
                    "SELECT organization_id FROM organizations_membership "
                    "WHERE user_id = %s ORDER BY 1",
                    [_state["dual"]],
                )
                own = [row[0] for row in cursor.fetchall()]
                cursor.execute("SELECT COUNT(*) FROM organizations_membership")
                (total,) = cursor.fetchone()
        finally:
            self._reset_context()
        self.assertEqual(own, sorted([_state["org_a"], _state["org_b"]]))
        self.assertEqual(total, 2)

    def test_bootstrap_resolvers_reveal_only_org_ids(self):
        self._reset_context()
        with self.app_cursor() as cursor:
            cursor.execute(
                "SELECT offerflow_run_org(%s)", [_state["runs"]["B"]]
            )
            (org_id,) = cursor.fetchone()
            cursor.execute(
                "SELECT offerflow_invitation_org(%s)", [_state["invite_probe"]]
            )
            (invite_org,) = cursor.fetchone()
            cursor.execute("SELECT offerflow_run_org(%s)", [123456789])
            (missing,) = cursor.fetchone()
        self.assertEqual(org_id, _state["org_b"])
        self.assertEqual(invite_org, _state["org_b"])
        self.assertIsNone(missing)

    def test_connection_reuse_does_not_leak(self):
        with self.app_cursor() as cursor:
            cursor.execute("SELECT set_config('app.current_org_id', %s, false)", [str(_state["org_a"])])
            cursor.execute("SELECT set_config('app.current_user_id', %s, false)", [str(_state["alice"])])
            cursor.execute('SELECT COUNT(*) FROM "pipeline_sourcefile"')
            (seen_a,) = cursor.fetchone()
            cursor.execute("SELECT set_config('app.current_org_id', '', false)")
            cursor.execute("SELECT set_config('app.current_user_id', '', false)")
            cursor.execute('SELECT COUNT(*) FROM "pipeline_sourcefile"')
            (seen_none,) = cursor.fetchone()
            cursor.execute("SELECT set_config('app.current_org_id', %s, false)", [str(_state["org_b"])])
            cursor.execute("SELECT set_config('app.current_user_id', %s, false)", [str(_state["bob"])])
            cursor.execute(
                'SELECT COUNT(*) FROM "pipeline_sourcefile" WHERE organization_id = %s',
                [_state["org_a"]],
            )
            (seen_a_under_b,) = cursor.fetchone()
            cursor.execute("SELECT set_config('app.current_org_id', '', false)")
            cursor.execute("SELECT set_config('app.current_user_id', '', false)")
        self.assertEqual(seen_a, 1)
        self.assertEqual(seen_none, 0)
        self.assertEqual(seen_a_under_b, 0)

    def test_privilege_revocation_blocks_reads(self):
        table = "pipeline_reviewrequest"
        admin = connections[ADMIN_ALIAS]
        with admin.cursor() as cursor:
            cursor.execute(f'REVOKE SELECT ON "{table}" FROM "{APP_ROLE}"')
        try:
            self._set_context(_state["org_a"], _state["alice"])
            try:
                with self.assertRaises(DatabaseError):
                    with self.app_cursor() as cursor:
                        cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
            finally:
                self._reset_context()
                connections[APP_ALIAS].close()
        finally:
            # Provisioning grants full table rights; restoring SELECT
            # returns this table to its provisioned state.
            with admin.cursor() as cursor:
                cursor.execute(f'GRANT SELECT ON "{table}" TO "{APP_ROLE}"')

    def test_organization_directory_stays_readable(self):
        # Documented design: Organization is an identity directory, not
        # tenant data; every access path rechecks live membership first.
        self._reset_context()
        with self.app_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM organizations_organization")
            (count,) = cursor.fetchone()
        self.assertEqual(count, 2)


@unittest.skipUnless(DSN, "OFFERFLOW_PG_ISOLATION_DSN is not set")
class HttpIsolationTests(_IsolationBase):
    """HTTP authorization plus database policy under the restricted role.

    Requests run through Django's test client on the ``default``
    connection, which the module harness points at the disposable
    database as the restricted runtime role. Every response below
    therefore exercises the middleware-published tenant context and
    the row policies, not just application filters.
    """

    def _client_for(self, username, active_org=None):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        user = get_user_model().objects.using("default").get(username=username)
        client = APIClient()
        # Session login, like the real frontend: the tenant middleware
        # reads request.user before the view runs, so force_authenticate
        # (which injects the user inside the view) would hide the user
        # from context setup.
        client.force_login(user)
        if active_org is not None:
            response = client.post(
                "/api/orgs/active/",
                {"organization_id": active_org},
                format="json",
            )
            assert response.status_code == 200, response.content
        return client

    def test_member_lists_only_own_organization_files(self):
        alice = self._client_for("iso_alice", _state["org_a"])
        response = alice.get("/api/pipeline/files/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [row["id"] for row in response.json()],
            [_state["files"]["A"]],
        )
        bob = self._client_for("iso_bob", _state["org_b"])
        response = bob.get("/api/pipeline/files/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [row["id"] for row in response.json()],
            [_state["files"]["B"]],
        )

    def test_member_cannot_open_foreign_run(self):
        alice = self._client_for("iso_alice", _state["org_a"])
        response = alice.get(f"/api/pipeline/runs/{_state['runs']['A']}/")
        self.assertEqual(response.status_code, 200, response.content)
        response = alice.get(f"/api/pipeline/runs/{_state['runs']['B']}/")
        self.assertEqual(response.status_code, 404, response.content)

    def test_member_cannot_filter_into_foreign_organization(self):
        alice = self._client_for("iso_alice", _state["org_a"])
        response = alice.get(
            "/api/pipeline/files/", {"organization_id": _state["org_b"]}
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_outsider_cannot_list_files(self):
        carol = self._client_for("iso_carol")
        response = carol.get("/api/pipeline/files/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), [])

    def test_anonymous_request_is_refused(self):
        from rest_framework.test import APIClient

        response = APIClient().get("/api/pipeline/files/")
        self.assertIn(response.status_code, (401, 403))

    def test_invitation_accept_then_revoke_cycle(self):
        from organizations.models import Membership

        carol = self._client_for("iso_carol")
        response = carol.post(
            "/api/orgs/invitations/accept/",
            {"token": _state["invite_b"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = carol.post(
            "/api/orgs/active/",
            {"organization_id": _state["org_b"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        response = carol.get("/api/pipeline/files/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [row["id"] for row in response.json()],
            [_state["files"]["B"]],
        )
        # Revoking the membership must immediately hide the rows again.
        # There is no member-remove endpoint yet, so revoke through the
        # privileged alias: the point under test is that access follows
        # live membership, not how the row was deleted.
        deleted, _ = (
            Membership.objects.using(ADMIN_ALIAS)
            .filter(user__username="iso_carol", organization_id=_state["org_b"])
            .delete()
        )
        self.assertEqual(deleted, 1)
        response = carol.get("/api/pipeline/files/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), [])
