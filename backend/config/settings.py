"""Django settings for the OfferFlow AI backend.

Local development configuration (Package A). Every secret comes from the
environment; nothing secret is committed. Staging and production use a
separate configuration built for Package F.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _env_required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill in every value."
        )
    return value


SECRET_KEY = _env_required("OFFERFLOW_SECRET_KEY")

# Debug is strictly opt-in and only for local development. Staging and
# production must run with OFFERFLOW_DEBUG unset or "false".
DEBUG = (_env("OFFERFLOW_DEBUG", "false") or "").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in (_env("OFFERFLOW_ALLOWED_HOSTS", "localhost,127.0.0.1") or "").split(",")
    if host.strip()
]
# The internal Compose hostname is always allowed: the frontend calls Django
# as http://backend:8000 (middleware auth check, API rewrites), so that Host
# header must validate even when .env predates it. Unresolvable outside the
# container network, so it cannot be abused externally.
if "backend" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("backend")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # Project utilities (provider selection, storage, health). No models yet;
    # tenant-owned apps arrive with Packages B and C.
    "common",
    "organizations",
    "pipeline",
]

MIDDLEWARE = [
    # First: normalize proxy-stripped API slashes before routing sees them.
    "common.middleware.PreserveApiSlashMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "organizations.middleware.ActiveOrganizationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

_DB_ENGINE = _env("OFFERFLOW_DB_ENGINE", "postgresql")
if _DB_ENGINE == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _env("OFFERFLOW_DB_NAME", "offerflow"),
            "USER": _env("OFFERFLOW_DB_USER", "offerflow"),
            "PASSWORD": _env_required("OFFERFLOW_DB_PASSWORD"),
            "HOST": _env("OFFERFLOW_DB_HOST", "db"),
            "PORT": _env("OFFERFLOW_DB_PORT", "5432"),
        }
    }
elif _DB_ENGINE == "sqlite":
    # Dependency-free local unit-test runs only. Never for application runs.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _env("OFFERFLOW_DB_NAME", ":memory:"),
        }
    }
else:
    raise ImproperlyConfigured(f"Unsupported OFFERFLOW_DB_ENGINE: {_DB_ENGINE!r}")

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Secure API default: every future DRF view requires authentication unless it
# explicitly opts out. Session auth keeps credentials out of browser storage.
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
}

# Cookies are secured whenever debug mode is off.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
# Browser POSTs (frontend origin, direct backend use) send an Origin header
# that Django must explicitly trust, otherwise every login 403s.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in (
        _env(
            "OFFERFLOW_CSRF_TRUSTED_ORIGINS",
            "http://localhost:4344,http://127.0.0.1:4344,"
            "http://localhost:8384,http://127.0.0.1:8384",
        )
        or ""
    ).split(",")
    if origin.strip()
]

# Celery. The broker carries job identifiers only; PostgreSQL holds
# authoritative job state and checkpoints (no result backend for the MVP).
CELERY_BROKER_URL = _env("OFFERFLOW_BROKER_URL", "amqp://guest:guest@broker:5672//")
CELERY_TASK_ACKS_LATE = True  # tasks must be idempotent (see Package C)
# Remote control declares transient non-exclusive pidbox queues, which
# RabbitMQ >= 4.3 rejects by default (541 INTERNAL_ERROR crash-loop). The
# single local worker needs no remote control, mingle, or gossip.
CELERY_WORKER_ENABLE_REMOTE_CONTROL = False
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_SOFT_TIME_LIMIT = 540
CELERY_TASK_TIME_LIMIT = 600
CELERY_TIMEZONE = "UTC"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

APP_VERSION = _env("OFFERFLOW_APP_VERSION", "0.2.0-beta")
OFFERFLOW_FILE_ROOT = _env("OFFERFLOW_FILE_ROOT", "/var/offerflow/files")

# Spreadsheet intake limits (overridable per environment).
OFFERFLOW_UPLOAD_MAX_BYTES = int(_env("OFFERFLOW_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)) or 0)
OFFERFLOW_MAX_UNCOMPRESSED_BYTES = int(
    _env("OFFERFLOW_MAX_UNCOMPRESSED_BYTES", str(64 * 1024 * 1024)) or 0
)
OFFERFLOW_MAX_ROWS = int(_env("OFFERFLOW_MAX_ROWS", "2000") or 0)
OFFERFLOW_MAX_COLS = int(_env("OFFERFLOW_MAX_COLS", "100") or 0)
OFFERFLOW_HEADER_SCAN_ROWS = int(_env("OFFERFLOW_HEADER_SCAN_ROWS", "20") or 0)
