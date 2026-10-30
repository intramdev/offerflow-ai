"""Attach the session's active membership and publish tenant context.

Install after Django's session and authentication middleware. Authenticated
requests with a valid active organization get request.organization and
request.membership plus database tenant context; everything else gets no
tenant state at all. Context is always reset afterwards so reused
connections cannot leak one tenant's rows into another request.
"""

from __future__ import annotations

from organizations.context import reset_tenant_context, set_tenant_context
from organizations.services import get_active_membership


class ActiveOrganizationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.membership = None
        membership = get_active_membership(
            getattr(request, "user", None), getattr(request, "session", None)
        )
        if membership is not None:
            request.organization = membership.organization
            request.membership = membership
            set_tenant_context(membership.organization_id, request.user.id)
        try:
            return self.get_response(request)
        finally:
            reset_tenant_context()
