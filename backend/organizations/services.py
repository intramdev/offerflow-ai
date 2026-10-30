"""Membership, invitation, and tenant-authorization operations.

Tenant identity always comes from authenticated membership on the server. A
request header, URL value, object id, or queue payload alone never authorizes
access: every operation below rechecks live membership before acting.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from organizations.models import Invitation, Membership, Organization, OrganizationRole

SESSION_ACTIVE_ORG_KEY = "offerflow_active_org_id"


def get_active_membership(user, session) -> Membership | None:
    """Return the user's membership in the session's active organization."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    org_id = (session or {}).get(SESSION_ACTIVE_ORG_KEY)
    if not org_id:
        return None
    try:
        return Membership.objects.select_related("organization").get(
            user=user, organization_id=org_id
        )
    except (Membership.DoesNotExist, ValueError, ValidationError):
        return None


def require_active_membership(user, session) -> Membership:
    """Return the active membership or refuse without an active organization."""
    membership = get_active_membership(user, session)
    if membership is None:
        raise PermissionDenied("No active organization selected.")
    return membership


def set_active_organization(session, user, organization_id: int) -> Membership:
    """Select the session's active organization after a membership check."""
    try:
        membership = Membership.objects.select_related("organization").get(
            user=user, organization_id=organization_id
        )
    except (Membership.DoesNotExist, ValueError, ValidationError):
        raise PermissionDenied("Not a member of that organization.") from None
    session[SESSION_ACTIVE_ORG_KEY] = membership.organization_id
    return membership


def authorize_organization_access(user, organization_id: int) -> Organization:
    """Return the organization if the user holds live membership in it."""
    try:
        membership = Membership.objects.select_related("organization").get(
            user=user, organization_id=organization_id
        )
    except (Membership.DoesNotExist, ValueError, ValidationError):
        raise PermissionDenied("Not a member of that organization.") from None
    return membership.organization


def _require_manager(user, organization: Organization) -> Membership:
    try:
        membership = Membership.objects.get(user=user, organization=organization)
    except Membership.DoesNotExist:
        raise PermissionDenied("Not a member of that organization.") from None
    if not membership.can_manage_members:
        raise PermissionDenied("Managing members requires Owner or Admin.")
    return membership


def invite_member(
    *,
    organization: Organization,
    email: str,
    role: str,
    invited_by,
    expires_in_days: int = 7,
) -> Invitation:
    """Create a pending invitation after checking the inviter's authority."""
    manager = _require_manager(invited_by, organization)
    if role not in OrganizationRole.values:
        raise ValidationError(f"Unknown role: {role!r}.")
    if role == OrganizationRole.OWNER and manager.role != OrganizationRole.OWNER:
        raise PermissionDenied("Only owners may invite owners.")
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        raise ValidationError("An invitation email is required.")
    return Invitation.objects.create(
        organization=organization,
        email=normalized_email,
        role=role,
        created_by=invited_by,
        expires_at=timezone.now() + timedelta(days=expires_in_days),
    )


def accept_invitation(*, token: str, user) -> Membership:
    """Convert a live invitation into membership for its matching user."""
    try:
        invitation = Invitation.objects.select_related("organization").get(
            token=token, accepted_at__isnull=True
        )
    except Invitation.DoesNotExist:
        raise ValidationError("Unknown or already accepted invitation.") from None
    if invitation.is_expired:
        raise ValidationError("That invitation has expired.")
    user_email = (getattr(user, "email", "") or "").strip().lower()
    if user_email != invitation.email.lower():
        raise PermissionDenied("That invitation belongs to another email address.")
    membership, _created = Membership.objects.get_or_create(
        user=user,
        organization=invitation.organization,
        defaults={"role": invitation.role},
    )
    # An existing membership keeps its role: accepting never silently
    # upgrades or downgrades live access.
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at"])
    return membership


def revoke_membership(*, organization: Organization, target_user, revoked_by) -> None:
    """Remove a member after checking authority and last-owner protection."""
    _require_manager(revoked_by, organization)
    try:
        membership = Membership.objects.get(
            user=target_user, organization=organization
        )
    except Membership.DoesNotExist:
        raise ValidationError("That user is not a member.") from None
    if membership.role == OrganizationRole.OWNER:
        owners = Membership.objects.filter(
            organization=organization, role=OrganizationRole.OWNER
        ).count()
        if owners <= 1:
            raise ValidationError("An organization must keep at least one owner.")
    membership.delete()


OPERATING_ROLES = frozenset(
    {OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.OPERATOR}
)


def require_organization_role(user, organization_id: int, allowed=OPERATING_ROLES):
    """Return the membership if the user may change pipeline data.

    Viewers keep read access; remap and delete need an operating role.
    """
    try:
        membership = Membership.objects.get(
            user=user, organization_id=organization_id
        )
    except (Membership.DoesNotExist, ValueError, ValidationError):
        raise PermissionDenied("Not a member of that organization.") from None
    if membership.role not in allowed:
        raise PermissionDenied("That action requires Operator, Admin, or Owner.")
    return membership
