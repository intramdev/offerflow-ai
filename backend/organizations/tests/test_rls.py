"""Row-policy enforcement tests (PostgreSQL only; skipped elsewhere).

These prove the defense-in-depth layer: with no tenant context nothing is
visible, context scopes reads and writes to one organization, and resetting
context revokes visibility. Run under Compose against the real database.
"""

import unittest

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test import TestCase, override_settings
from django.utils import timezone

from organizations.context import reset_tenant_context, set_tenant_context
from organizations.models import Invitation, Membership, Organization

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InvitationRowPolicyTests(TestCase):
    databases = {"default"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "postgresql":
            raise unittest.SkipTest("row-policy tests require PostgreSQL")

    def setUp(self):
        self.addCleanup(reset_tenant_context)
        self.alice = User.objects.create_user(username="alice", password="x")
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        Membership.objects.create(user=self.alice, organization=self.org_a, role="owner")
        set_tenant_context(self.org_a.id, self.alice.id)
        self.invite_a = Invitation.objects.create(
            organization=self.org_a,
            email="a@example.test",
            role="viewer",
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        reset_tenant_context()
        set_tenant_context(self.org_b.id, self.alice.id)
        Invitation.objects.create(
            organization=self.org_b,
            email="b@example.test",
            role="viewer",
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )
        reset_tenant_context()

    def test_no_context_denies_all_rows(self):
        self.assertEqual(Invitation.objects.count(), 0)

    def test_context_scopes_reads_to_one_org(self):
        set_tenant_context(self.org_a.id, self.alice.id)
        rows = list(Invitation.objects.all())
        self.assertEqual([row.id for row in rows], [self.invite_a.id])

    def test_cross_context_write_refused(self):
        set_tenant_context(self.org_a.id, self.alice.id)
        with self.assertRaises(DatabaseError):
            Invitation.objects.create(
                organization=self.org_b,
                email="x@example.test",
                role="viewer",
                expires_at=timezone.now() + timezone.timedelta(days=1),
            )

    def test_reset_revokes_visibility(self):
        set_tenant_context(self.org_a.id, self.alice.id)
        self.assertEqual(Invitation.objects.count(), 1)
        reset_tenant_context()
        self.assertEqual(Invitation.objects.count(), 0)
