import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must be set")
    return value


SECRET_KEY = _required_env("DJANGO_SECRET_KEY")
if SECRET_KEY.startswith("django-insecure-") or len(SECRET_KEY) < 50:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a random key of 50+ characters"
    )

DEBUG = False
ALLOWED_HOSTS = [h.strip() for h in _required_env("DJANGO_ALLOWED_HOSTS").split(",")]
if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "Set explicit DJANGO_ALLOWED_HOSTS; wildcards are unsafe"
    )
sqlite_path = Path(_required_env("DJANGO_SQLITE_PATH"))
if not sqlite_path.is_absolute() or sqlite_path.is_relative_to(BASE_DIR):  # noqa: F405
    raise ImproperlyConfigured(
        "DJANGO_SQLITE_PATH must be an absolute path outside the application directory"
    )
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(sqlite_path),
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    },
}
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# Caddy terminates HTTPS and supplies a trusted X-Forwarded-Proto header.
# Production Compose exposes Gunicorn only to Caddy's internal Docker network.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
