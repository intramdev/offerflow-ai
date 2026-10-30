"""Celery application wiring (broker transport only; state lives in Postgres)."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("offerflow")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
