"""Tests for session authentication endpoints."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="u", email="u@example.test", password="secret-pw"
        )
        self.client = APIClient()

    def csrf_token(self, client=None):
        client = client or self.client
        return client.get("/api/orgs/auth/csrf/").cookies["csrftoken"].value

    def login(self, client, username, password):
        return client.post(
            "/api/orgs/auth/login/",
            {"username": username, "password": password},
            format="json",
            HTTP_X_CSRFTOKEN=self.csrf_token(client),
        )

    def test_csrf_endpoint_sets_cookie(self):
        response = self.client.get("/api/orgs/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)

    def test_login_success_opens_session(self):
        response = self.login(self.client, "u", "secret-pw")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["username"], "u")
        self.assertIn("sessionid", response.cookies)
        me = self.client.get("/api/orgs/auth/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["email"], "u@example.test")

    def test_login_wrong_password_refused(self):
        response = self.login(self.client, "u", "wrong")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("sessionid", response.cookies)

    def test_login_inactive_refused(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = self.login(self.client, "u", "secret-pw")
        self.assertEqual(response.status_code, 400)

    def test_login_without_trailing_slash_reaches_view(self):
        response = self.client.post(
            "/api/orgs/auth/login",
            {"username": "u", "password": "wrong"},
            format="json",
            HTTP_X_CSRFTOKEN=self.csrf_token(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Invalid credentials.")

    def test_api_get_without_trailing_slash_reaches_view(self):
        response = self.client.get("/api/orgs/auth/me")
        self.assertEqual(response.status_code, 403)

    def test_me_requires_session(self):
        response = self.client.get("/api/orgs/auth/me/")
        self.assertEqual(response.status_code, 403)

    def test_logout_closes_session(self):
        login = self.login(self.client, "u", "secret-pw")
        self.assertEqual(login.status_code, 200)
        # The token rotates on login; re-read it before the next unsafe call.
        fresh_token = login.cookies["csrftoken"].value
        logout = self.client.post(
            "/api/orgs/auth/logout/", format="json", HTTP_X_CSRFTOKEN=fresh_token
        )
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(self.client.get("/api/orgs/auth/me/").status_code, 403)

    def test_login_enforces_csrf(self):
        strict = APIClient(enforce_csrf_checks=True)
        refused = strict.post(
            "/api/orgs/auth/login/",
            {"username": "u", "password": "secret-pw"},
            format="json",
        )
        self.assertEqual(refused.status_code, 403)
        accepted = self.login(strict, "u", "secret-pw")
        self.assertEqual(accepted.status_code, 200)

    def test_profile_read_and_update(self):
        self.client.force_authenticate(self.user)
        before = self.client.get("/api/orgs/auth/me/")
        self.assertEqual(
            before.data,
            {
                "id": self.user.id,
                "username": "u",
                "email": "u@example.test",
                "first_name": "",
                "last_name": "",
            },
        )
        response = self.client.patch(
            "/api/orgs/auth/me/",
            {
                "first_name": "Gina",
                "last_name": "Rossi",
                "email": "gina@example.test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["first_name"], "Gina")
        self.assertEqual(response.data["last_name"], "Rossi")
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Gina")
        bad = self.client.patch(
            "/api/orgs/auth/me/", {"email": "not-an-email"}, format="json"
        )
        self.assertEqual(bad.status_code, 400)
