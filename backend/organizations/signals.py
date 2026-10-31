"""Record every successful sign-in for the activity dashboard."""

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver


@receiver(user_logged_in)
def record_login(sender, request, user, **kwargs):
    from organizations.models import UserLogin

    ip = None
    if request is not None:
        raw = request.META.get("REMOTE_ADDR", "")
        ip = raw or None
    try:
        UserLogin.objects.create(user=user, ip_address=ip)
    except Exception:
        # Logging in must never fail because activity tracking did.
        pass
