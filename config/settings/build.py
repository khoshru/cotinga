"""Collect static files without runtime secrets or a database connection."""

from .base import *  # noqa: F403

SECRET_KEY = "django-insecure-build-only"  # nosec B105
DEBUG = False
