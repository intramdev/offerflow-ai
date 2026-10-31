"""Organization routes (mounted under api/orgs/)."""

from django.urls import path

from organizations.auth_views import CsrfView, LoginView, LogoutView, MeView
from organizations.views import (
    ActiveOrganizationView,
    InvitationAcceptView,
    InvitationCreateView,
    MemberListView,
    MembershipListView,
    OrganizationGdprDeleteView,
    OrganizationGdprExportView,
    OrganizationSettingsView,
    OrganizationUsageView,
    PreferenceView,
)

urlpatterns = [
    path("auth/csrf/", CsrfView.as_view(), name="auth-csrf"),
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path("memberships/", MembershipListView.as_view(), name="memberships"),
    path("active/", ActiveOrganizationView.as_view(), name="active-org"),
    path("invitations/", InvitationCreateView.as_view(), name="invite"),
    path("invitations/accept/", InvitationAcceptView.as_view(), name="accept-invite"),
    path(
        "<int:organization_id>/members/",
        MemberListView.as_view(),
        name="members",
    ),
    path(
        "<int:organization_id>/settings/",
        OrganizationSettingsView.as_view(),
        name="org-settings",
    ),
    path(
        "<int:organization_id>/usage/",
        OrganizationUsageView.as_view(),
        name="org-usage",
    ),
    path(
        "<int:organization_id>/gdpr/export/",
        OrganizationGdprExportView.as_view(),
        name="org-gdpr-export",
    ),
    path(
        "<int:organization_id>/gdpr/delete/",
        OrganizationGdprDeleteView.as_view(),
        name="org-gdpr-delete",
    ),
    path("preferences/me/", PreferenceView.as_view(), name="preferences"),
]
