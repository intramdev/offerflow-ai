"""Template context shared across the backend, including admin pages."""

from django.conf import settings


def frontend_base_url(request):
    """Expose the public frontend origin for absolute legal-page links."""
    base = getattr(settings, "FRONTEND_BASE_URL", "") or ""
    return {"frontend_base_url": base.rstrip("/")}
