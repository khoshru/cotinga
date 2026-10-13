from collections.abc import Mapping
from ipaddress import ip_address

from django.conf import settings
from django.http import HttpRequest


def login_name(
    request: HttpRequest, credentials: Mapping[str, object] | None = None
) -> str:
    values = credentials if credentials is not None else request.POST
    return str(values.get("username") or values.get("email") or "").strip().lower()


def login_client_ip(request: HttpRequest) -> str | None:
    # Production exposes Django only to Caddy, which replaces client-supplied XFF.
    header = (
        "HTTP_X_FORWARDED_FOR"
        if settings.COTINGA_TRUST_CADDY_HEADERS
        else "REMOTE_ADDR"
    )
    try:
        return str(ip_address(request.META.get(header, "").strip()))
    except ValueError:
        return None
