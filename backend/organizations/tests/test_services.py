"""Tests for membership, invitation, and authorization services."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from organizations import services
from organizations.models import Membership, Organization

User = get_user_model()

FAST_HASHERS = {"PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"]}


def make_user(username, email=None):
    return User.objects.create_user(
        username=username, email=email or f"{username}@example.test", password="x"
    )


@override_settings(**FAST_HASHERS)
class MembershipServiceTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.carol = make_user("carol")
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        Membership.objects.create(user=self.alice, organization=self.org_a, role="owner")
        Membership.objects.create(user=self.bob, organization=self.org_b, role="viewer")
        Membership.objects.create(
            user=self.carol, organization=self.org_a, role="admin"
        )
        Membership.objects.create(
            user=self.carol, organization=self.org_b, role="operator"
        )

    def test_active_org_requires_live_membership(self):
        session = {}
        membership = services.set_active_organization(session, self.carol, self.org_a.id)
        self.assertEqual(membership.organization, self.org_a)
        self.assertEqual(session[services.SESSION_ACTIVE_ORG_KEY], self.org_a.id)
        with self.assertRaises(PermissionDenied):
            services.set_active_organization({}, self.bob, self.org_a.id)

    def test_active_membership_none_without_selection(self):
        self.assertIsNone(services.get_active_membership(self.alice, {}))
        with self.assertRaises(PermissionDenied):
            services.require_active_membership(self.alice, {})

    def test_active_membership_none_after_revocation(self):
        session = {services.SESSION_ACTIVE_ORG_KEY: self.org_a.id}
        self.assertIsNotNone(services.get_active_membership(self.carol, session))
        Membership.objects.filter(
            user=self.carol, organization=self.org_a
        ).delete()
        self.assertIsNone(services.get_active_membership(self.carol, session))

    def test_authorize_allows_member_denies_outsider(self):
        org = services.authorize_organization_access(self.alice, self.org_a.id)
        self.assertEqual(org, self.org_a)
        with self.assertRaises(PermissionDenied):
            services.authorize_organization_access(self.bob, self.org_a.id)
        with self.assertRaises(PermissionDenied):
            services.authorize_organization_access(self.alice, 999999)

    def test_invite_requires_manager(self):
        with self.assertRaises(PermissionDenied):
            services.invite_member(
                organization=self.org_b,
                email="n@x.test",
                role="viewer",
                invited_by=self.bob,
            )
        with self.assertRaises(PermissionDenied):
            services.invite_member(
                organization=self.org_a,
                email="n@x.test",
                role="viewer",
                invited_by=self.bob,
            )
        invitation = services.invite_member(
            organization=self.org_a,
            email="n@x.test",
            role="viewer",
            invited_by=self.carol,
        )
        self.assertEqual(invitation.email, "n@x.test")

    def test_only_owners_invite_owners(self):
        with self.assertRaises(PermissionDenied):
            services.invite_member(
                organization=self.org_a,
                email="o@x.test",
                role="owner",
                invited_by=self.carol,
            )
        invitation = services.invite_member(
            organization=self.org_a,
            email="o@x.test",
            role="owner",
            invited_by=self.alice,
        )
        self.assertEqual(invitation.role, "owner")

    def test_duplicate_pending_invite_rejected(self):
        services.invite_member(
            organization=self.org_a,
            email="dup@x.test",
            role="viewer",
            invited_by=self.alice,
        )
        with self.assertRaises(IntegrityError):
            services.invite_member(
                organization=self.org_a,
                email="dup@x.test",
                role="viewer",
                invited_by=self.alice,
            )

    def test_accept_happy_path(self):
        dave = make_user("dave", "dave@x.test")
        invitation = services.invite_member(
            organization=self.org_a,
            email="dave@x.test",
            role="operator",
            invited_by=self.alice,
        )
        membership = services.accept_invitation(token=invitation.token, user=dave)
        self.assertEqual(membership.organization, self.org_a)
        self.assertEqual(membership.role, "operator")
        invitation.refresh_from_db()
        self.assertTrue(invitation.is_accepted)

    def test_accept_wrong_email_denied(self):
        mallory = make_user("mallory", "mallory@x.test")
        invitation = services.invite_member(
            organization=self.org_a,
            email="dave@x.test",
            role="viewer",
            invited_by=self.alice,
        )
        with self.assertRaises(PermissionDenied):
            services.accept_invitation(token=invitation.token, user=mallory)

    def test_accept_expired_or_reused_denied(self):
        dave = make_user("dave", "dave@x.test")
        invitation = services.invite_member(
            organization=self.org_a,
            email="dave@x.test",
            role="viewer",
            invited_by=self.alice,
        )
        invitation.expires_at = timezone.now() - timedelta(seconds=1)
        invitation.save(update_fields=["expires_at"])
        with self.assertRaises(ValidationError):
            services.accept_invitation(token=invitation.token, user=dave)
        with self.assertRaises(ValidationError):
            services.accept_invitation(token="no-such-token", user=dave)
        invitation.expires_at = timezone.now() + timedelta(days=1)
        invitation.save(update_fields=["expires_at"])
        services.accept_invitation(token=invitation.token, user=dave)
        with self.assertRaises(ValidationError):
            services.accept_invitation(token=invitation.token, user=dave)

    def test_accept_keeps_existing_role(self):
        services.accept_invitation(
            token=services.invite_member(
                organization=self.org_a,
                email="carol@example.test",
                role="viewer",
                invited_by=self.alice,
            ).token,
            user=self.carol,
        )
        membership = Membership.objects.get(user=self.carol, organization=self.org_a)
        self.assertEqual(membership.role, "admin")

    def test_revoke_requires_manager_and_keeps_last_owner(self):
        with self.assertRaises(PermissionDenied):
            services.revoke_membership(
                organization=self.org_a, target_user=self.carol, revoked_by=self.bob
            )
        with self.assertRaises(ValidationError):
            services.revoke_membership(
                organization=self.org_a, target_user=self.alice, revoked_by=self.alice
            )
        services.revoke_membership(
            organization=self.org_a, target_user=self.carol, revoked_by=self.alice
        )
        with self.assertRaises(PermissionDenied):
            services.authorize_organization_access(self.carol, self.org_a.id)
