"""Project configuration package (Django settings, URLs, Celery wiring)."""

from .celery import app as celery_app

__all__ = ("celery_app",)
