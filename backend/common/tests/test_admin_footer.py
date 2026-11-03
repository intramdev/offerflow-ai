"""The Django admin renders the same legal footer as the frontend shell."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

LEGAL_LINES = [
    "© 2026 IN.TRA.M s.r.o. · Developed by Gian Paolo Cipicchia",
    "Built for the Meta Global AI Developer Hackathon · AGPL-3.0-only",
    "Independent entry: Meta is not affiliated with this app and accepts "
    "no responsibility for it.",
]

LEGAL_LINKS = [
    ("/privacy", "Privacy policy"),
    ("/terms", "Terms of service"),
    ("/source", "Source &amp; licenses"),
]


@override_settings(FRONTEND_BASE_URL="http://frontend.test:4344")
class AdminFooterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        get_user_model().objects.create_superuser(
            "admin", "admin@example.com", "pass"
        )

    def assertLegalFooter(self, response):
        self.assertEqual(response.status_code, 200)
        for line in LEGAL_LINES:
            self.assertContains(response, line)
        for path, label in LEGAL_LINKS:
            self.assertContains(
                response,
                f'href="http://frontend.test:4344{path}">{label}</a>',
            )

    def test_login_page_has_legal_footer(self):
        self.assertLegalFooter(self.client.get("/admin/login/"))

    def test_dashboard_has_legal_footer(self):
        self.client.force_login(get_user_model().objects.get(username="admin"))
        self.assertLegalFooter(self.client.get("/admin/"))

    def test_changelist_and_change_form_have_legal_footer(self):
        self.client.force_login(get_user_model().objects.get(username="admin"))
        self.assertLegalFooter(self.client.get("/admin/auth/user/"))
        user = get_user_model().objects.get(username="admin")
        self.assertLegalFooter(self.client.get(f"/admin/auth/user/{user.pk}/change/"))
