"""Login tracking and the staff activity dashboard."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from organizations.models import Membership, Organization, UserLogin
from pipeline.models import UsageEvent

User = get_user_model()


class LoginTrackingTests(TestCase):
    def test_api_login_records_signin(self):
        user = User.objects.create_user(username="u", password="secret-pw")
        client = Client()
        response = client.post(
            "/api/orgs/auth/login/",
            {"username": "u", "password": "secret-pw"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        login = UserLogin.objects.get(user=user)
        self.assertIsNotNone(login.created_at)
        self.assertEqual(login.ip_address, "127.0.0.1")


class ActivityDashboardTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="boss", password="x", email="b@example.test"
        )
        self.alice = User.objects.create_user(username="alice", password="x")
        self.bob = User.objects.create_user(username="bob", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(
            user=self.alice, organization=self.org, role="owner"
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_staff_only(self):
        self.client.logout()
        response = self.client.get("/admin/activity/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_index_shows_charts(self):
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "of-activity")
        self.assertContains(response, "Tokens consumed")

    def test_ranked_by_tokens_then_logins(self):
        UsageEvent.objects.create(
            organization=self.org,
            kind="provider_call",
            quantity=500,
            user=self.alice,
        )
        UsageEvent.objects.create(
            organization=self.org, kind="provider_call", quantity=50, user=self.bob
        )
        for _ in range(3):
            UserLogin.objects.create(user=self.bob)
        UserLogin.objects.create(user=self.alice)
        by_tokens = self.client.get("/admin/activity/?sort=tokens&days=all")
        self.assertEqual(by_tokens.status_code, 200)
        rows = by_tokens.context["rows"]
        self.assertEqual([row["username"] for row in rows[:2]], ["alice", "bob"])
        self.assertEqual(rows[0]["tokens"], 500)
        by_logins = self.client.get("/admin/activity/?sort=logins&days=all")
        rows = by_logins.context["rows"]
        self.assertEqual([row["username"] for row in rows[:2]], ["bob", "alice"])
        self.assertEqual(rows[0]["logins"], 3)
