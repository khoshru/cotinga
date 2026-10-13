import importlib
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.utils import OperationalError
from django.test import Client

from accounts.models import User


@pytest.mark.django_db
def test_health_checks_database(client: Client) -> None:
    with patch.dict("os.environ", {"COTINGA_REVISION": ""}):
        assert client.get("/health/").json() == {"status": "ok"}
    with patch("tracker.views.connection.cursor", side_effect=OperationalError):
        assert client.get("/health/").status_code == 503
    assert client.post("/health/").status_code == 405


@pytest.mark.django_db
def test_health_identifies_the_running_release(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "a" * 40
    monkeypatch.setenv("COTINGA_REVISION", revision)
    assert client.get("/health/").json() == {"status": "ok", "revision": revision}


def configure_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DJANGO_SECRET_KEY",
        "deployment-test-9284-strong-key-with-length-over-fifty-characters",
    )
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", " example.com, www.example.com ")
    monkeypatch.setenv("DJANGO_SQLITE_PATH", "/data/db.sqlite3")


def test_production_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_production(monkeypatch)
    monkeypatch.setenv("DJANGO_CSRF_TRUSTED_ORIGINS", " https://example.com, ")
    from config.settings import production

    importlib.reload(production)
    assert not production.DEBUG
    assert production.ALLOWED_HOSTS == ["example.com", "www.example.com"]
    assert production.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert production.DATABASES["default"]["NAME"] == "/data/db.sqlite3"
    assert production.CSRF_TRUSTED_ORIGINS == ["https://example.com"]
    assert production.SECURE_SSL_REDIRECT and production.SESSION_COOKIE_SECURE
    monkeypatch.delenv("DJANGO_SECRET_KEY")
    with pytest.raises(ImproperlyConfigured, match="must be set"):
        production._required_env("DJANGO_SECRET_KEY")


@pytest.mark.parametrize(
    "variable,value,message",
    [
        ("DJANGO_SECRET_KEY", "short", "50"),
        ("DJANGO_SECRET_KEY", "django-insecure-" + "x" * 60, "50"),
        ("DJANGO_ALLOWED_HOSTS", "*", "wildcards"),
        ("DJANGO_SQLITE_PATH", "db.sqlite3", "absolute path"),
        (
            "DJANGO_SQLITE_PATH",
            str(Path(__file__).resolve().parent.parent / "db.sqlite3"),
            "outside",
        ),
    ],
)
def test_production_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, message: str
) -> None:
    configure_production(monkeypatch)
    from config.settings import production

    monkeypatch.setenv(variable, value)
    with pytest.raises(ImproperlyConfigured, match=message):
        importlib.reload(production)


@pytest.mark.django_db
def test_owner_bootstrap_is_optional_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COTINGA_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("COTINGA_OWNER_PASSWORD", raising=False)
    output = StringIO()
    call_command("bootstrap_owner", stdout=output)
    assert User.objects.count() == 0
    monkeypatch.setenv("COTINGA_OWNER_EMAIL", "OWNER@EXAMPLE.COM")
    monkeypatch.setenv("COTINGA_OWNER_PASSWORD", "a-carefully-chosen-passphrase-908")
    call_command("bootstrap_owner", stdout=output)
    user = User.objects.get(email="owner@example.com")
    assert not user.is_staff
    assert user.check_password("a-carefully-chosen-passphrase-908")
    monkeypatch.setenv("COTINGA_OWNER_PASSWORD", "a-different-password-98")
    call_command("bootstrap_owner", stdout=output)
    user.refresh_from_db()
    assert user.check_password("a-carefully-chosen-passphrase-908")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "email,password",
    [
        ("owner@example.com", ""),
        ("", "secret"),
        ("owner@example.com", "12345678"),
        ("invalid", "a-carefully-chosen-passphrase-908"),
    ],
)
def test_bootstrap_rejects_missing_or_invalid_credentials(
    monkeypatch: pytest.MonkeyPatch, email: str, password: str
) -> None:
    monkeypatch.setenv("COTINGA_OWNER_EMAIL", email)
    monkeypatch.setenv("COTINGA_OWNER_PASSWORD", password)
    with pytest.raises(CommandError):
        call_command("bootstrap_owner")
    assert User.objects.count() == 0


def test_deployment_preparation_orders_migrations_and_bootstrap() -> None:
    with patch("tracker.management.commands.prepare_deployment.call_command") as invoke:
        call_command("prepare_deployment")
    assert [call.args[0] for call in invoke.call_args_list] == [
        "migrate",
        "bootstrap_owner",
    ]


def test_failed_migration_prevents_owner_bootstrap() -> None:
    with (
        patch(
            "tracker.management.commands.prepare_deployment.call_command",
            side_effect=CommandError("migration failed"),
        ) as invoke,
        pytest.raises(CommandError),
    ):
        call_command("prepare_deployment")
    assert invoke.call_count == 1
