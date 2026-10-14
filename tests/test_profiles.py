from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import User
from tracker.credentials import encrypt_key
from tracker.forms import BodyProfileForm, TargetEstimateForm
from tracker.models import BodyProfile, OpenRouterSettings, ProgressPlan, WeightEntry
from tracker.openrouter import OpenRouterError

pytestmark = pytest.mark.django_db

DETAILS: dict[str, str | int] = {
    "age": 32,
    "height": 180,
    "sex": "male",
    "activity": "light",
    "weight": "85",
    "goal": "lose",
    "notes": "Three walks a week.",
}


def profile_post(**changes: str) -> dict[str, str | int]:
    return {**{f"profile-{key}": value for key, value in DETAILS.items()}, **changes}


def test_profile_is_saved_once_and_reused_in_both_tools(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    response = client.post("/profile/", {**profile_post(), "next": "/outlook/"})
    assert response.status_code == 302 and response.headers["Location"] == "/outlook/"
    profile = BodyProfile.objects.get(user=owner)
    assert (
        profile.complete and profile.height == 180 and profile.notes == DETAILS["notes"]
    )
    assert WeightEntry.objects.get(user=owner).weight == Decimal("85")
    OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key("fictional-key")
    )
    response = client.get("/ai/targets/")
    assert not response.context["profile_needed"]
    for name in DETAILS:
        assert (
            str(response.context["form"][name].value()) == str(DETAILS[name])
            or name == "weight"
        )
    outlook = client.get("/outlook/")
    assert (
        not outlook.context["profile_needed"]
        and outlook.context["form"]["mode"].value() == "lose"
    )
    assert outlook.context["body_profile"].age == 32


def test_ai_failure_keeps_the_valid_profile_for_retry(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key("fictional-key")
    )
    with patch(
        "tracker.ai_views.openrouter.find_model",
        side_effect=OpenRouterError("Provider unavailable"),
    ):
        response = client.post("/ai/targets/", DETAILS)
    assert response.status_code == 200 and response.context["form"].errors
    assert BodyProfile.objects.get(user=owner).complete
    assert client.get("/ai/targets/").context["form"]["height"].value() == 180
    client.post("/profile/", profile_post(**{"profile-height": "175"}))
    assert BodyProfile.objects.count() == 1
    assert client.get("/ai/targets/").context["form"]["height"].value() == 175
    assert WeightEntry.objects.filter(user=owner).count() == 1


def test_missing_details_prompt_and_cannot_be_bypassed(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key("fictional-key")
    )
    for url in ("/outlook/", "/ai/targets/"):
        response = client.get(url)
        assert (
            response.context["profile_needed"]
            and b"data-profile-dialog" in response.content
        )
        assert b"unspecified" not in response.content
    WeightEntry.objects.create(user=owner, weight=85)
    assert (
        client.post(
            "/outlook/",
            {"mode": "lose", "goal_weight": 80, "maintenance_calories": 2500},
        ).status_code
        == 200
    )
    assert not ProgressPlan.objects.filter(user=owner).exists()
    response = client.post("/profile/", profile_post(**{"profile-height": ""}))
    assert response.status_code == 200 and response.context["form"].errors
    assert not BodyProfile.objects.filter(user=owner).exists()


@pytest.mark.parametrize("sex", ["unspecified", "other", ""])
def test_only_female_or_male_can_be_submitted(sex: str) -> None:
    assert not BodyProfileForm({**DETAILS, "sex": sex}).is_valid()
    assert not TargetEstimateForm({**DETAILS, "sex": sex}).is_valid()


def test_profile_database_constraint_and_privacy(client: Client, owner: User) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        BodyProfile.objects.create(user=owner, sex="other")
    other = User.objects.create_user("other-profile@example.test")
    BodyProfile.objects.create(
        user=other,
        age=77,
        height=223,
        sex="female",
        activity="moderate",
        notes="Private other user notes",
    )
    assert client.get("/profile/").status_code == 302
    client.force_login(owner)
    body = client.get("/profile/").content.decode()
    assert "Private other user notes" not in body and 'value="223"' not in body
    client.post("/profile/", profile_post())
    assert BodyProfile.objects.get(user=other).height == 223
    assert "no-store" in client.get("/profile/")["Cache-Control"]


def test_profile_redirect_is_local_and_csrf_protected(owner: User) -> None:
    client = Client()
    client.force_login(owner)
    response = client.post(
        "/profile/", {**profile_post(), "next": "https://evil.example/"}
    )
    assert response.headers["Location"] == "/profile/"
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(owner)
    assert protected.post("/profile/", profile_post()).status_code == 403


def test_changed_profile_weight_updates_today_without_losing_notes(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    entry = WeightEntry.objects.create(
        user=owner, date=timezone.localdate(), weight=85, notes="After breakfast"
    )
    client.post("/profile/", profile_post(**{"profile-weight": "84.50"}))
    entry.refresh_from_db()
    assert entry.weight == Decimal("84.50") and entry.notes == "After breakfast"
