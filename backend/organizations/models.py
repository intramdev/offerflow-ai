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
    default_currency = models.CharField(max_length=3, blank=True, default="USD")
    destination_currency = models.CharField(
        max_length=3, blank=True, default="USD"
    )
    quote_export = models.JSONField(default=dict)
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


class ThemeMode(models.TextChoices):
    SYSTEM = "system", "Follow system"
    LIGHT = "light", "Light"
    DARK = "dark", "Dark"


class ReadingFont(models.TextChoices):
    SYSTEM = "system", "System default"
    READABLE = "readable", "Highly legible (Verdana-based)"
    SERIF = "serif", "Serif"
    MONO = "mono", "Monospace"


class ReadingSize(models.TextChoices):
    COMPACT = "sm", "Compact"
    DEFAULT = "md", "Default"
    LARGE = "lg", "Large"
    XLARGE = "xl", "Extra large"


class ContrastMode(models.TextChoices):
    STANDARD = "standard", "Standard"
    HIGH = "high", "High contrast"


class MotionMode(models.TextChoices):
    SYSTEM = "system", "Follow system"
    REDUCE = "reduce", "Reduce motion"
    FULL = "full", "Full motion"


class UserLogin(models.Model):
    """One successful sign-in (app or admin), for activity ranking."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="offerflow_logins",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "created_at"])]


DEFAULT_AI_MODEL = "muse-spark-1.3-contributor"
DEFAULT_MONTHLY_CREDITS = 25_000_000


class AIProviderConfig(models.Model):
    """Singleton: the shared model account for every company.

    The API key is Fernet-encrypted at rest (see common.crypto) and is
    never rendered back; admin only reports whether one is stored. When
    this row is complete and enabled it takes precedence over the
    OFFERFLOW_META_* environment variables, which remain as fallback so
    local development and tests keep working without a database row.

    Fresh installs ship with the default model and a 25M-credit monthly
    default already filled in, so enabling paid features is just adding
    the API key.
    """

    enabled = models.BooleanField(default=True)
    api_key_encrypted = models.TextField(blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default=DEFAULT_AI_MODEL)
    default_monthly_credits = models.PositiveIntegerField(
        default=DEFAULT_MONTHLY_CREDITS
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI provider config"

    def __str__(self):  # pragma: no cover - display only
        return f"AI provider config (model={self.model or 'unset'})"

    @classmethod
    def get_solo(cls):
        return cls.objects.order_by("pk").first()

    @property
    def has_key(self) -> bool:
        return bool(self.api_key_encrypted)

    @property
    def is_complete(self) -> bool:
        return bool(self.api_key_encrypted and self.model.strip())

    def credentials(self):
        """Return (api_key, model), decrypting only at dispatch time."""
        from common.crypto import StoredKeyError, decrypt_key

        if not self.is_complete:
            raise StoredKeyError("AI provider config is incomplete.")
        return decrypt_key(self.api_key_encrypted), self.model.strip()


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
    theme_mode = models.CharField(
        max_length=8, choices=ThemeMode.choices, default=ThemeMode.LIGHT
    )
    reading_font = models.CharField(
        max_length=8, choices=ReadingFont.choices, default=ReadingFont.SYSTEM
    )
    reading_size = models.CharField(
        max_length=8, choices=ReadingSize.choices, default=ReadingSize.DEFAULT
    )
    contrast_mode = models.CharField(
        max_length=8, choices=ContrastMode.choices, default=ContrastMode.STANDARD
    )
    motion_mode = models.CharField(
        max_length=8, choices=MotionMode.choices, default=MotionMode.SYSTEM
    )
    assistant_banned = models.BooleanField(
        default=False,
        help_text="Super-admin ban from the paid help assistant.",
    )

    def __str__(self) -> str:
        return f"Preferences for {self.user}"
