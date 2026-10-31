"""Password recovery: emailed single-use links, valid 30 minutes."""

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class PasswordResetApiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="u", password="old-secret-1", email="u@example.com"
        )
        self.client = APIClient()

    def _csrf(self):
        return self.client.get("/api/orgs/auth/csrf/").cookies[
            "csrftoken"
        ].value

    def _request(self, email):
        return self.client.post(
            "/api/orgs/auth/password/reset/",
            {"email": email},
            format="json",
            HTTP_X_CSRFTOKEN=self._csrf(),
        )

    def _confirm(self, uid, token, new_password):
        return self.client.post(
            "/api/orgs/auth/password/confirm/",
            {"uid": uid, "token": token, "new_password": new_password},
            format="json",
            HTTP_X_CSRFTOKEN=self._csrf(),
        )

    def _live_link(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        return uid, token

    def test_request_sends_link_to_known_address(self):
        response = self._request("u@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("/app/reset-password/", body)
        self.assertIn("30 minutes", body)
        self.assertEqual(mail.outbox[0].to, ["u@example.com"])

    def test_request_answers_unknown_address_identically(self):
        response = self._request("nobody@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("on its way", response.json()["detail"])

    def test_request_rejects_invalid_email(self):
        response = self._request("not-an-email")
        self.assertEqual(response.status_code, 400)

    def test_request_ignores_inactive_accounts(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = self._request("u@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_confirm_sets_new_password(self):
        uid, token = self._live_link()
        response = self._confirm(uid, token, "brand-new-secret-2")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("brand-new-secret-2"))
        self.assertFalse(self.user.check_password("old-secret-1"))

    def test_confirm_link_is_single_use(self):
        uid, token = self._live_link()
        first = self._confirm(uid, token, "brand-new-secret-2")
        self.assertEqual(first.status_code, 200)
        second = self._confirm(uid, token, "another-secret-3")
        self.assertEqual(second.status_code, 400)

    def test_confirm_rejects_tampered_token(self):
        uid, _ = self._live_link()
        response = self._confirm(uid, "bogus-token", "brand-new-secret-2")
        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid or expired", response.json()["detail"])

    def test_confirm_rejects_bad_uid(self):
        _, token = self._live_link()
        response = self._confirm("bogus", token, "brand-new-secret-2")
        self.assertEqual(response.status_code, 400)

    def test_confirm_rejects_weak_password(self):
        uid, token = self._live_link()
        response = self._confirm(uid, token, "short")
        self.assertEqual(response.status_code, 400)

    @override_settings(PASSWORD_RESET_TIMEOUT=-1)
    def test_confirm_rejects_expired_link(self):
        uid, token = self._live_link()
        response = self._confirm(uid, token, "brand-new-secret-2")
        self.assertEqual(response.status_code, 400)

    def test_z_request_is_throttled(self):
        from unittest import mock

        from django.core.cache import cache
        from rest_framework.throttling import ScopedRateThrottle

        cache.clear()
        with mock.patch.object(
            ScopedRateThrottle, "get_rate", return_value="1/min"
        ):
            first = self._request("u@example.com")
            self.assertEqual(first.status_code, 200)
            second = self._request("u@example.com")
            self.assertEqual(second.status_code, 429)
