import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import User
from tracker import openrouter
from tracker.credentials import decrypt_key, encrypt_key
from tracker.models import Meal, MealEstimate, OpenRouterSettings

pytestmark = pytest.mark.django_db
KEY = "sk-or-v1-testing-key-with-no-live-value"
MODEL = openrouter.ModelInfo("openai/test", "Test model", Decimal(0), Decimal(0))


@pytest.fixture(autouse=True)
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openrouter, "models", lambda: [MODEL])
    monkeypatch.setattr(openrouter, "validate_key", lambda key: None)


@pytest.fixture
def configured(client: Client, owner: User) -> OpenRouterSettings:
    client.force_login(owner)
    return OpenRouterSettings.objects.create(
        user=owner, encrypted_api_key=encrypt_key(KEY), model=MODEL.id
    )


def estimate_result() -> dict[str, Any]:
    return {
        "meals": [
            {
                "date": "2026-10-12",
                "kind": "lunch",
                "time": "13:30",
                "description": "Rice and lentils",
                "portion": "1 bowl",
                "calories": 450,
                "protein": 20,
                "carbs": 60,
                "fat": 12,
            }
        ],
        "assumptions": ["A medium bowl."],
    }


def review_data(result: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "meals-TOTAL_FORMS": str(len(result["meals"])),
        "meals-INITIAL_FORMS": str(len(result["meals"])),
        "meals-MIN_NUM_FORMS": "1",
        "meals-MAX_NUM_FORMS": "20",
    }
    for index, row in enumerate(result["meals"]):
        data.update({f"meals-{index}-{key}": value for key, value in row.items()})
    return data


@pytest.mark.parametrize(
    "path",
    [
        "/ai/",
        "/ai/settings/",
        "/ai/models/",
    ],
)
def test_new_pages_require_authentication(client: Client, path: str) -> None:
    assert client.get(path).status_code == 302


def test_settings_save_encrypted_key_never_render_plaintext_and_keep_blank_key(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    assert (
        client.post("/ai/settings/", {"api_key": KEY, "model": MODEL.id}).status_code
        == 302
    )
    preferences = OpenRouterSettings.objects.get(user=owner)
    encrypted = preferences.encrypted_api_key
    assert KEY not in encrypted
    assert decrypt_key(encrypted) == KEY
    response = client.get("/ai/settings/")
    assert (
        KEY.encode() not in response.content
        and encrypted.encode() not in response.content
    )
    assert "no-store" in response["Cache-Control"]
    assert (
        client.post("/ai/settings/", {"api_key": "", "model": MODEL.id}).status_code
        == 302
    )
    preferences.refresh_from_db()
    assert preferences.encrypted_api_key == encrypted
    assert (
        client.post(
            "/ai/settings/", {"remove_key": "on", "model": MODEL.id}
        ).status_code
        == 302
    )
    preferences.refresh_from_db()
    assert not preferences.has_key


@pytest.mark.parametrize(
    "data",
    [
        {"api_key": "bad"},
        {"api_key": KEY, "remove_key": "on"},
        {"api_key": KEY, "model": "invalid/model"},
    ],
)
def test_invalid_settings_are_not_saved(
    client: Client, owner: User, data: dict[str, str]
) -> None:
    client.force_login(owner)
    assert client.post("/ai/settings/", data).status_code == 200
    assert not OpenRouterSettings.objects.get(user=owner).has_key


def test_provider_key_errors_and_catalog_outages_are_rendered_safely(
    client: Client, configured: OpenRouterSettings
) -> None:
    with patch(
        "tracker.openrouter.validate_key",
        side_effect=openrouter.OpenRouterError("Key not accepted"),
    ):
        response = client.post("/ai/settings/", {"api_key": KEY, "model": MODEL.id})
    assert (
        b"Key not accepted" in response.content and KEY.encode() not in response.content
    )
    with patch(
        "tracker.openrouter.models",
        side_effect=openrouter.OpenRouterError("Catalog unavailable"),
    ):
        assert b"Catalog unavailable" in client.get("/ai/settings/").content
        assert client.post("/ai/settings/", {"model": MODEL.id}).status_code == 302
        assert client.get("/ai/models/").status_code == 503


def test_model_search_filters_and_returns_prices(
    client: Client, configured: OpenRouterSettings
) -> None:
    assert (
        client.get("/ai/models/", {"q": "test", "free": "1"}).json()["models"][0][
            "pricing"
        ]
        == "Free"
    )
    assert client.get("/ai/models/", {"q": "missing"}).json()["models"] == []
    assert client.get("/ai/settings/", {"q": "missing"}).status_code == 200


def test_estimation_requires_personal_key_and_model(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    response = client.get("/ai/")
    assert response.status_code == 302
    assert response.url == "/ai/settings/"  # type: ignore[attr-defined]


def test_estimation_creates_review_then_adds_the_day_once(
    client: Client, configured: OpenRouterSettings, owner: User
) -> None:
    with patch(
        "tracker.openrouter.estimate_day", return_value=estimate_result()
    ) as provider:
        response = client.post(
            "/ai/", {"date": "2026-10-12", "prompt": "Lunch at 13:30: rice and lentils"}
        )
    assert response.status_code == 302 and Meal.objects.count() == 0
    batch = MealEstimate.objects.get(user=owner)
    assert (
        batch.status == MealEstimate.Status.READY and provider.call_args.args[0] == KEY
    )
    assert client.get(response.url).status_code == 200  # type: ignore[attr-defined]
    data = review_data(batch.result)
    data["meals-0-calories"] = 500
    data["meals-0-date"] = "2099-01-01"
    path = f"/ai/review/{batch.pk}/"
    assert client.post(path, data).status_code == 302
    meal = Meal.objects.get(user=owner)
    assert meal.calories == 500 and meal.date == batch.date
    assert meal.estimated and meal.estimate_model == MODEL.id
    assert meal.time is not None and meal.time.hour == 13
    assert client.post(path, data).status_code == 302 and Meal.objects.count() == 1


def test_failed_provider_requests_do_not_add_meals(
    client: Client, configured: OpenRouterSettings
) -> None:
    with patch(
        "tracker.openrouter.estimate_day",
        side_effect=openrouter.OpenRouterError("Provider unavailable"),
    ):
        response = client.post("/ai/", {"date": "2026-10-12", "prompt": "Lunch"})
    assert response.status_code == 200 and b"Provider unavailable" in response.content
    assert (
        MealEstimate.objects.get().status == MealEstimate.Status.FAILED
        and not Meal.objects.exists()
    )
    configured.encrypted_api_key = "corrupted"
    configured.save()
    assert (
        b"saved key could not be read"
        in client.post("/ai/", {"date": "2026-10-12", "prompt": "Lunch"}).content
    )


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_meal_correction_uses_one_batch_and_only_saves_after_review(
    client: Client, configured: OpenRouterSettings, owner: User, repair_succeeds: bool
) -> None:
    valid = estimate_result()
    invalid = estimate_result()
    invalid["meals"][0]["portion"] = "A verbose portion explanation. " * 8
    responses = [invalid, valid if repair_succeeds else invalid]
    with patch(
        "tracker.openrouter._request",
        side_effect=[
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(value)}}
                ]
            }
            for value in responses
        ],
    ) as provider:
        response = client.post(
            "/ai/", {"date": "2026-10-12", "prompt": "Rice and lentils at 13:30"}
        )
    assert provider.call_count == 2
    assert MealEstimate.objects.filter(user=owner).count() == 1
    assert not Meal.objects.exists()
    batch = MealEstimate.objects.get(user=owner)
    if repair_succeeds:
        assert response.status_code == 302 and batch.status == "ready"
        data = review_data(batch.result)
        path = f"/ai/review/{batch.pk}/"
        assert client.post(path, data).status_code == 302
        assert client.post(path, data).status_code == 302
        assert Meal.objects.count() == 1
        assert Meal.objects.get().calories == valid["meals"][0]["calories"]
    else:
        assert response.status_code == 200 and batch.status == "failed"
        assert b"Rice and lentils at 13:30" in response.content
        assert b"meal description is still here" in response.content
        assert batch.result == {}


def test_estimation_rate_limit_prevents_repeated_billable_requests(
    client: Client, configured: OpenRouterSettings, owner: User
) -> None:
    MealEstimate.objects.create(user=owner, date=timezone.localdate(), model=MODEL.id)
    with patch("tracker.openrouter.estimate_day") as provider:
        response = client.post("/ai/", {"date": "2026-10-12", "prompt": "Lunch"})
    assert b"wait 20 seconds" in response.content
    provider.assert_not_called()
    MealEstimate.objects.bulk_create(
        [
            MealEstimate(user=owner, date=timezone.localdate(), model=MODEL.id)
            for _ in range(49)
        ]
    )
    MealEstimate.objects.update(created_at=timezone.now() - timedelta(seconds=30))
    with patch("tracker.openrouter.estimate_day") as provider:
        response = client.post("/ai/", {"date": "2026-10-12", "prompt": "Lunch"})
    assert b"50-estimate limit" in response.content
    provider.assert_not_called()


def test_estimate_and_settings_are_scoped_to_the_current_user(
    client: Client, configured: OpenRouterSettings
) -> None:
    other = User.objects.create_user("other@example.com")
    batch = MealEstimate.objects.create(
        user=other,
        date=timezone.localdate(),
        model=MODEL.id,
        status=MealEstimate.Status.READY,
        result=estimate_result(),
    )
    assert client.get(f"/ai/review/{batch.pk}/").status_code == 404
    assert (
        client.post(f"/ai/review/{batch.pk}/", review_data(batch.result)).status_code
        == 404
    )
    assert OpenRouterSettings.objects.filter(user=other).count() == 0


def test_invalid_review_keeps_draft_and_all_meals_unsaved(
    client: Client, configured: OpenRouterSettings, owner: User
) -> None:
    batch = MealEstimate.objects.create(
        user=owner,
        date=timezone.localdate(),
        model=MODEL.id,
        status=MealEstimate.Status.READY,
        result=estimate_result(),
    )
    path = f"/ai/review/{batch.pk}/"
    data = review_data(batch.result)
    data["meals-0-calories"] = -1
    assert client.post(path, data).status_code == 200
    batch.refresh_from_db()
    assert batch.status == MealEstimate.Status.READY and not Meal.objects.exists()
    data = review_data(batch.result)
    data["meals-0-DELETE"] = "on"
    assert client.post(path, data).status_code == 200 and not Meal.objects.exists()
    batch.status = MealEstimate.Status.FAILED
    batch.save()
    assert client.get(path).status_code == 302


def test_csrf_is_required_for_ai_settings(owner: User) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    assert (
        client.post("/ai/settings/", {"api_key": KEY, "model": MODEL.id}).status_code
        == 403
    )
