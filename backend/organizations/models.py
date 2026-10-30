"""Tenant ownership records.

A tenant is an independent organization. Users join organizations through
explicit memberships with a single role each. Invitations carry pending
memberships. Every tenant-owned record in this project points at exactly one
organization; see Package C business tables for the same discipline applied
to imports, rows, runs, reviews, and exports.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class OrganizationRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    OPERATOR = "operator", "Operator"
    VIEWER = "viewer", "Viewer"


MANAGING_ROLES = frozenset({OrganizationRole.OWNER, OrganizationRole.ADMIN})


def generate_invitation_token() -> str:
    """Return an unguessable invitation token (single use, revocable)."""
    return secrets.token_urlsafe(32)


class Organization(models.Model):
    """An independent customer organization (the tenant)."""

    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    """A user's explicit role within one organization."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="org_memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=16, choices=OrganizationRole.choices)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="unique_org_member"
            )
        ]
        ordering = ["organization__name"]

    def __str__(self) -> str:
        return f"{self.user} in {self.organization} as {self.role}"

    @property
    def can_manage_members(self) -> bool:
        """Whether this membership may invite, remove, or change members."""
        return self.role in MANAGING_ROLES


class Invitation(models.Model):
    """A pending, expirable offer of membership in one organization."""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    role = models.CharField(max_length=16, choices=OrganizationRole.choices)
    token = models.CharField(
        max_length=64, unique=True, default=generate_invitation_token
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=Q(accepted_at__isnull=True),
                name="unique_pending_invite",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Invite {self.email} to {self.organization} as {self.role}"

    @property
    def is_accepted(self) -> bool:
        return self.accepted_at is not None

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at


class DateTimeFormat(models.TextChoices):
    US = "us", "US (MM/DD/YYYY)"
    EUROPEAN = "eu", "European (DD/MM/YYYY)"


class UserPreference(models.Model):
    """Per-user display preferences. Keyed by user only: views always
    filter on request.user, so no organization row policy applies."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preferences",
    )
    datetime_format = models.CharField(
        max_length=8, choices=DateTimeFormat.choices, default=DateTimeFormat.US
    )

    def __str__(self) -> str:
        return f"Preferences for {self.user}"
