"""Tests for the active-organization middleware (engine agnostic)."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase, override_settings

from organizations import services
from organizations.middleware import ActiveOrganizationMiddleware
from organizations.models import Membership, Organization

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="u", password="x")
        self.org = Organization.objects.create(name="Org")
        Membership.objects.create(user=self.user, organization=self.org, role="owner")

    def _request(self, user, session):
        request = self.factory.get("/")
        request.user = user
        request.session = session
        return request

    def test_attaches_membership_for_active_org(self):
        session = {services.SESSION_ACTIVE_ORG_KEY: self.org.id}
        seen = {}

        def view(request):
            seen["org"] = request.organization
            seen["membership"] = request.membership

            from django.http import HttpResponse

            return HttpResponse("ok")

        middleware = ActiveOrganizationMiddleware(view)
        middleware(self._request(self.user, session))
        self.assertEqual(seen["org"], self.org)
        self.assertEqual(seen["membership"].user, self.user)

    def test_no_tenant_state_when_anonymous_or_unselected(self):
        seen = {}

        def view(request):
            seen["org"] = request.organization
            seen["membership"] = request.membership

            from django.http import HttpResponse

            return HttpResponse("ok")

        middleware = ActiveOrganizationMiddleware(view)
        middleware(self._request(AnonymousUser(), {}))
        self.assertIsNone(seen["org"])
        self.assertIsNone(seen["membership"])
        middleware(self._request(self.user, {}))
        self.assertIsNone(seen["org"])
        self.assertIsNone(seen["membership"])
