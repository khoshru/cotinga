from datetime import timedelta

import pytest
from axes.models import AccessAttempt
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from accounts.models import User
from accounts.security import login_client_ip, login_name

PASSWORD = "correct-horse-battery-staple"


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/accounts/login/", "/admin/login/"])
@override_settings(COTINGA_TRUST_CADDY_HEADERS=True)
def test_lockout_survives_case_ip_and_agent_changes_then_expires(
    client: Client, path: str
) -> None:
    User.objects.create_superuser("owner@example.com", PASSWORD)
    for attempt in range(5):
        response = client.post(
            path,
            {
                "username": "OWNER@example.com" if attempt % 2 else "owner@EXAMPLE.COM",
                "password": "wrong",
            },
            HTTP_X_FORWARDED_FOR=f"192.0.2.{attempt + 1}",
            HTTP_USER_AGENT=f"agent-{attempt}",
        )
        assert response.status_code == (429 if attempt == 4 else 200)
    assert (
        client.post(
            path,
            {"username": "owner@example.com", "password": PASSWORD},
            HTTP_X_FORWARDED_FOR="198.51.100.1",
        ).status_code
        == 429
    )
    assert "_auth_user_id" not in client.session
    assert "Please wait 15 minutes" in response.content.decode()
    assert set(AccessAttempt.objects.values_list("username", flat=True)) == {
        "owner@example.com"
    }
    assert all(
        PASSWORD not in attempt.post_data and "password=wrong" not in attempt.post_data
        for attempt in AccessAttempt.objects.all()
    )
    AccessAttempt.objects.update(attempt_time=timezone.now() - timedelta(minutes=16))
    response = client.post(
        path,
        {"username": "owner@example.com", "password": PASSWORD},
        HTTP_X_FORWARDED_FOR="198.51.100.1",
    )
    assert response.status_code == 302
    assert "_auth_user_id" in client.session


@pytest.mark.django_db
def test_admin_and_public_login_share_the_failure_budget(client: Client) -> None:
    User.objects.create_superuser("owner@example.com", PASSWORD)
    for path in ["/admin/login/"] * 3 + ["/accounts/login/"] * 2:
        client.post(path, {"username": "owner@example.com", "password": "wrong"})
    assert (
        client.post(
            "/admin/login/", {"username": "owner@example.com", "password": PASSWORD}
        ).status_code
        == 429
    )
    assert (
        client.post(
            "/accounts/login/", {"username": "owner@example.com", "password": PASSWORD}
        ).status_code
        == 429
    )


@pytest.mark.django_db
def test_ip_limit_blocks_attempts_using_different_emails(client: Client) -> None:
    User.objects.create_user("owner@example.com", PASSWORD)
    for attempt in range(5):
        client.post(
            "/accounts/login/",
            {"username": f"nobody{attempt}@example.com", "password": "wrong"},
        )
    assert (
        client.post(
            "/accounts/login/", {"username": "owner@example.com", "password": PASSWORD}
        ).status_code
        == 429
    )


@pytest.mark.django_db
def test_success_resets_failures(client: Client) -> None:
    User.objects.create_user("owner@example.com", PASSWORD)
    for _ in range(4):
        client.post(
            "/accounts/login/", {"username": "owner@example.com", "password": "wrong"}
        )
    assert (
        client.post(
            "/accounts/login/", {"username": "owner@example.com", "password": PASSWORD}
        ).status_code
        == 302
    )
    assert AccessAttempt.objects.count() == 0


def test_forwarded_ip_is_ignored_outside_production() -> None:
    request = RequestFactory().post(
        "/accounts/login/", HTTP_X_FORWARDED_FOR="192.0.2.1"
    )
    assert login_client_ip(request) == "127.0.0.1"


@override_settings(COTINGA_TRUST_CADDY_HEADERS=True)
@pytest.mark.parametrize("value", ["", "not-an-ip", "192.0.2.1, 198.51.100.1"])
def test_invalid_forwarded_address_is_not_trusted(value: str) -> None:
    request = RequestFactory().post("/accounts/login/", HTTP_X_FORWARDED_FOR=value)
    assert login_client_ip(request) is None


def test_login_name_matches_email_normalization() -> None:
    request = RequestFactory().post(
        "/accounts/login/", {"username": " OWNER@EXAMPLE.COM "}
    )
    assert login_name(request) == "owner@example.com"
    assert login_name(request, {"email": " USER@EXAMPLE.COM "}) == "user@example.com"
    assert login_name(request, {}) == ""
