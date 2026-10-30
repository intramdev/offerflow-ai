"""Root URL configuration (admin plus unauthenticated liveness probe)."""

from django.contrib import admin
from django.urls import include, path

from common.health import health_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health_view, name="health"),
    path("api/orgs/", include("organizations.urls")),
    path("api/pipeline/", include("pipeline.urls")),
]
