import json
from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import translation

from accounts.models import User
from tracker import openrouter
from tracker.credentials import encrypt_key
from tracker.models import (
    DEFAULT_AI_MODEL,
    DailyTarget,
    MealEstimate,
    OpenRouterSettings,
    TargetEstimate,
)

MODEL = openrouter.ModelInfo(
    DEFAULT_AI_MODEL,
    "Google: Gemini 4 Argon",
    Decimal("0.000002"),
    Decimal("0.00001"),
    reasoning=True,
)
TARGETS = {
    "calories_min": 1700,
    "calories_max": 1850,
    "protein_min": 110,
    "protein_max": 140,
    "carbs_min": 160,
    "carbs_max": 210,
    "fat_min": 50,
    "fat_max": 65,
}
RESULT = {
    "targets": TARGETS,
    "explanation": "A modest starting range based on your activity.",
    "assumptions": ["Activity is estimated."],
}
PROFILE = {
    "age": 32,
    "height": 180,
    "weight": "85.00",
    "sex": "male",
    "activity": "light",
    "goal": "lose",
    "notes": "Three walks per week.",
}


@pytest.fixture(autouse=True)
def reset_language() -> Iterator[None]:
    translation.deactivate_all()
    yield
    translation.deactivate_all()


@pytest.fixture
def connected(client: Client, owner: User) -> Client:
    OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key("fictional-test-key")
    )
    client.force_login(owner)
    return client


@pytest.mark.django_db
def test_farsi_ai_response_instructions_and_reasoning() -> None:
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "can_suggest": True,
                            **RESULT,
                            "explanation_fa": "بازه‌ای متناسب با فعالیت شما.",
                            "assumptions_fa": ["فعالیت تخمینی است."],
                        }
                    )
                },
            }
        ]
    }
    with (
        translation.override("fa"),
        patch("tracker.openrouter._request", return_value=response) as call,
    ):
        result = openrouter.estimate_targets("fictional-key", MODEL, PROFILE)
        assert result["explanation"] == RESULT["explanation"]
        assert result["explanation_fa"] == "بازه‌ای متناسب با فعالیت شما."
    payload = call.call_args.kwargs["payload"]
    assert "Persian (Farsi)" in payload["messages"][0]["content"]
    assert payload["reasoning"] == {"effort": "low", "exclude": True}
    assert payload["response_format"]["json_schema"]["strict"]
    assert json.loads(payload["messages"][1]["content"]) == PROFILE


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    [
        {"calories_min": 700},
        {"calories_max": 6000},
        {"protein_min": True},
        {"fat_max": -5},
        {"carbs_max": 10},
    ],
)
def test_invalid_target_ranges_are_rejected(change: dict[str, int]) -> None:
    with pytest.raises(openrouter.OpenRouterError, match="unusable"):
        openrouter.validate_targets(
            {"can_suggest": True, **RESULT, "targets": {**TARGETS, **change}}
        )


def test_target_refusal_and_unfinished_response() -> None:
    with pytest.raises(openrouter.OpenRouterError, match="Cannot suggest"):
        openrouter.validate_targets(
            {
                "can_suggest": False,
                "targets": None,
                "explanation": "Cannot suggest a medical plan.",
                "assumptions": [],
            }
        )
    with (
        patch(
            "tracker.openrouter._request",
            return_value={"choices": [{"finish_reason": "length"}]},
        ),
        pytest.raises(openrouter.OpenRouterError, match="could not finish"),
    ):
        openrouter.estimate_targets("fictional-key", MODEL, PROFILE)


@pytest.mark.django_db
def test_suggestion_changes_no_targets_until_reviewed(
    connected: Client, owner: User
) -> None:
    current = DailyTarget.objects.create(user=owner)
    with (
        patch("tracker.ai_views.openrouter.find_model", return_value=MODEL),
        patch(
            "tracker.ai_views.openrouter.estimate_targets", return_value=RESULT
        ) as estimate,
    ):
        response = connected.post("/ai/targets/", PROFILE)
    assert response.status_code == 302
    batch = TargetEstimate.objects.get(user=owner)
    assert batch.status == MealEstimate.Status.READY
    assert batch.result == RESULT
    estimate.assert_called_once()
    current.refresh_from_db()
    assert current.calories_min == 1750
    assert connected.get(response.headers["Location"]).status_code == 200
    edited = {**TARGETS, "calories_min": 1650}
    assert connected.post(response.headers["Location"], edited).status_code == 302
    current.refresh_from_db()
    assert current.calories_min == 1650
    batch.refresh_from_db()
    assert batch.status == MealEstimate.Status.SAVED
    connected.post(
        response.headers["Location"],
        {**TARGETS, "calories_min": 1900, "calories_max": 2000},
    )
    current.refresh_from_db()
    assert current.calories_min == 1650


@pytest.mark.django_db
def test_review_is_private_and_validated(connected: Client, owner: User) -> None:
    other = User.objects.create_user("other@example.test", "a-fictional-password")
    foreign = TargetEstimate.objects.create(
        user=other,
        model=DEFAULT_AI_MODEL,
        result=RESULT,
        status=MealEstimate.Status.READY,
    )
    assert connected.get(f"/ai/targets/{foreign.pk}/").status_code == 404
    owned = TargetEstimate.objects.create(
        user=owner,
        model=DEFAULT_AI_MODEL,
        result=RESULT,
        status=MealEstimate.Status.READY,
    )
    response = connected.post(
        f"/ai/targets/{owned.pk}/", {**TARGETS, "calories_min": 2200}
    )
    assert response.status_code == 200
    assert not DailyTarget.objects.filter(user=owner).exists()
    owned.refresh_from_db()
    assert owned.status == MealEstimate.Status.READY


@pytest.mark.django_db
def test_target_failure_and_shared_rate_limit(connected: Client, owner: User) -> None:
    with (
        patch("tracker.ai_views.openrouter.find_model", return_value=MODEL),
        patch(
            "tracker.ai_views.openrouter.estimate_targets",
            side_effect=openrouter.OpenRouterError("Provider unavailable"),
        ) as estimate,
    ):
        assert connected.post("/ai/targets/", PROFILE).status_code == 200
        assert (
            TargetEstimate.objects.get(user=owner).status == MealEstimate.Status.FAILED
        )
        assert connected.post("/ai/targets/", PROFILE).status_code == 200
        assert estimate.call_count == 1
    assert not DailyTarget.objects.filter(user=owner).exists()
    assert TargetEstimate.objects.get(user=owner).result == {}


@pytest.mark.django_db
def test_target_profile_validation_and_missing_connection(
    client: Client, owner: User
) -> None:
    assert client.get("/ai/targets/").status_code == 302
    client.force_login(owner)
    assert client.get("/ai/targets/").headers["Location"] == "/ai/settings/"
    OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key("fictional-key")
    )
    with patch("tracker.ai_views.openrouter.estimate_targets") as estimate:
        assert client.post("/ai/targets/", {**PROFILE, "age": 15}).status_code == 200
        estimate.assert_not_called()
