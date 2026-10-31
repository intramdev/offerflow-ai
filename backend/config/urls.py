"""Root URL configuration (admin plus unauthenticated liveness probe)."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from common.health import health_view
from organizations.activity import activity_dashboard

admin.site.site_header = "OfferFlow AI backend"
admin.site.site_title = "OfferFlow AI backend"
admin.site.index_title = "Workspace administration"
admin.site.site_url = settings.FRONTEND_BASE_URL

urlpatterns = [
    path("admin/activity/", activity_dashboard, name="activity-dashboard"),
    path("admin/", admin.site.urls),
    path("api/health/", health_view, name="health"),
    path("api/orgs/", include("organizations.urls")),
    path("api/pipeline/", include("pipeline.urls")),
]
