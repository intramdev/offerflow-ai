"""Password recovery over HMAC-signed, single-use links.

Tokens bind to the user, the password hash, and the last login, so a
used link dies with the password change; PASSWORD_RESET_TIMEOUT (30
minutes) bounds the window. Unknown addresses answer exactly like known
ones, so the endpoint never reveals who has an account.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


def reset_link_for_user(user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{base}/app/reset-password/{uid}/{token}/"


def send_reset_email(user) -> None:
    send_mail(
        subject="Reset your OfferFlow AI password",
        message=(
            f"Hello {user.username},\n"
            "\n"
            "Someone requested a password reset for your OfferFlow AI "
            "account. Choose a new password here "
            "(the link works once and expires in 30 minutes):\n"
            f"{reset_link_for_user(user)}\n"
            "\n"
            "If that was not you, ignore this email — "
            "your password stays unchanged.\n"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
