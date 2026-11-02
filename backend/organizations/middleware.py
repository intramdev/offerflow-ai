"""Attach the session's active membership and publish tenant context.

Install after Django's session and authentication middleware. Every
authenticated request publishes its verified user id first so membership
resolution itself passes row policy; requests with a valid active
organization additionally get request.organization, request.membership,
and full tenant context. Anonymous requests get no tenant state at all.
Context is always reset afterwards so reused connections cannot leak one
tenant's rows into another request.
"""

from __future__ import annotations

from organizations.context import (
    reset_tenant_context,
    set_tenant_context,
    set_user_context,
)
from organizations.services import get_active_membership


class ActiveOrganizationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.membership = None
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            # Publish the verified user before membership resolution so the
            # membership lookup itself passes row policy; the organization
            # key stays empty until a membership actually resolves.
            set_user_context(user.id)
        try:
            membership = get_active_membership(
                user, getattr(request, "session", None)
            )
            if membership is not None:
                request.organization = membership.organization
                request.membership = membership
                set_tenant_context(membership.organization_id, user.id)
            return self.get_response(request)
        finally:
            reset_tenant_context()
