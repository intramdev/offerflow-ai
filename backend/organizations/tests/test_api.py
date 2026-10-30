"""Tests for the membership HTTP surface, including refusal cases."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Invitation, Membership, Organization

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MembershipApiTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            username="alice", email="alice@example.test", password="x"
        )
        self.bob = User.objects.create_user(
            username="bob", email="bob@example.test", password="x"
        )
        self.carol = User.objects.create_user(
            username="carol", email="carol@example.test", password="x"
        )
        self.org_a = Organization.objects.create(name="Org A")
        self.org_b = Organization.objects.create(name="Org B")
        Membership.objects.create(user=self.alice, organization=self.org_a, role="owner")
        Membership.objects.create(user=self.bob, organization=self.org_b, role="viewer")
        Membership.objects.create(user=self.carol, organization=self.org_a, role="admin")
        Membership.objects.create(
            user=self.carol, organization=self.org_b, role="operator"
        )
        self.client = APIClient()

    def test_unauthenticated_refused(self):
        response = self.client.get("/api/orgs/memberships/")
        self.assertEqual(response.status_code, 403)

    def test_memberships_list_shows_own_only(self):
        self.client.force_authenticate(self.carol)
        response = self.client.get("/api/orgs/memberships/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        self.client.force_authenticate(self.alice)
        response = self.client.get("/api/orgs/memberships/")
        self.assertEqual(len(response.data), 1)

    def test_active_org_roundtrip_and_outsider_refused(self):
        self.client.force_authenticate(self.carol)
        response = self.client.get("/api/orgs/active/")
        self.assertEqual(response.data, {"organization": None})
        response = self.client.post(
            "/api/orgs/active/", {"organization_id": self.org_a.id}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["organization"], self.org_a.id)
        self.client.force_authenticate(self.bob)
        response = self.client.post(
            "/api/orgs/active/", {"organization_id": self.org_a.id}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/orgs/active/", {"organization_id": "not-an-id"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_invite_and_accept_flow(self):
        dave = User.objects.create_user(
            username="dave", email="dave@example.test", password="x"
        )
        self.client.force_authenticate(self.alice)
        response = self.client.post(
            "/api/orgs/invitations/",
            {"organization_id": self.org_a.id, "email": "dave@example.test", "role": "viewer"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        token = Invitation.objects.get(
            organization=self.org_a, email="dave@example.test"
        ).token
        self.client.force_authenticate(self.bob)
        response = self.client.post(
            "/api/orgs/invitations/accept/", {"token": token}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.client.force_authenticate(dave)
        response = self.client.post(
            "/api/orgs/invitations/accept/", {"token": token}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/api/orgs/invitations/accept/", {"token": token}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_invite_by_non_manager_refused(self):
        self.client.force_authenticate(self.bob)
        response = self.client.post(
            "/api/orgs/invitations/",
            {"organization_id": self.org_b.id, "email": "z@example.test", "role": "viewer"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_member_list_scoped_to_own_org(self):
        self.client.force_authenticate(self.alice)
        response = self.client.get(f"/api/orgs/{self.org_a.id}/members/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        response = self.client.get(f"/api/orgs/{self.org_b.id}/members/")
        self.assertEqual(response.status_code, 403)
