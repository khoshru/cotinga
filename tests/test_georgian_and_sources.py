import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import Client
from django.utils import translation

from accounts.models import User
from tracker import openrouter
from tracker.credentials import encrypt_key
from tracker.forms import MealForm
from tracker.models import Meal, MealEstimate, OpenRouterSettings, TargetEstimate

pytestmark = pytest.mark.django_db
DAY = date(2026, 10, 15)
MODEL = openrouter.ModelInfo("google/test", "Test", Decimal(0), Decimal(0))
ROW: dict[str, Any] = {
    "kind": "lunch",
    "time": None,
    "description": "Rice and lentils",
    "portion": "1 bowl",
    "description_ka": "ბრინჯი და ოსპი",
    "portion_ka": "1 ჯამი",
    "calories": 450,
    "protein": 20,
    "carbs": 60,
    "fat": 12,
}
RESULT: dict[str, Any] = {
    "meals": [ROW],
    "assumptions": ["One medium bowl."],
    "assumptions_ka": ["ერთი საშუალო ჯამი."],
}


def configure(client: Client, owner: User) -> None:
    client.force_login(owner)
    OpenRouterSettings.objects.create(
        user=owner,
        model=MODEL.id,
        encrypted_api_key=encrypt_key("fictional-source-test-key"),
    )


def test_first_visit_defaults_to_english_despite_browser_language(
    client: Client,
) -> None:
    for preference in ("ka", "fa", "ka,fa;q=0.9"):
        response = client.get("/accounts/login/", HTTP_ACCEPT_LANGUAGE=preference)
        assert '<html lang="en" dir="ltr">' in response.content.decode()
        assert "data-language-menu" in response.content.decode()
        assert 'value="ka"' in response.content.decode()


@pytest.mark.parametrize(
    "language,heading,direction",
    [
        ("en", "Welcome back", "ltr"),
        ("ka", "კეთილი იყოს თქვენი დაბრუნება", "ltr"),
        ("fa", "خوش برگشتید", "rtl"),
    ],
)
def test_explicit_language_choice_is_remembered(
    client: Client, language: str, heading: str, direction: str
) -> None:
    response = client.post(
        "/i18n/setlang/", {"language": language, "next": "/accounts/login/"}
    )
    assert response.status_code == 302
    body = client.get("/accounts/login/").content.decode()
    assert f'<html lang="{language}" dir="{direction}">' in body
    assert heading in body


def test_georgian_ai_generates_english_and_georgian_without_farsi() -> None:
    response = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(RESULT)}}
        ]
    }
    with (
        translation.override("ka"),
        patch("tracker.openrouter._request", return_value=response) as provider,
    ):
        result = openrouter.estimate_day(
            "fictional-key", MODEL, "ერთი ჯამი ბრინჯი და ოსპი", DAY
        )
    payload = provider.call_args.kwargs["payload"]
    properties = payload["response_format"]["json_schema"]["schema"]["properties"]
    assert "description_ka" in properties["meals"]["items"]["required"]
    assert "description_fa" not in properties["meals"]["items"]["properties"]
    assert "Georgian" in payload["messages"][0]["content"]
    assert result["meals"][0]["description"] == ROW["description"]
    assert result["meals"][0]["portion_ka"] == ROW["portion_ka"]


def test_georgian_cannot_replace_canonical_english_fields() -> None:
    value = deepcopy(RESULT)
    value["meals"][0]["portion"] = "ერთი ჯამი"
    with translation.override("ka"), pytest.raises(openrouter.OpenRouterError):
        openrouter.validate_estimate(value, DAY)


def test_hidden_translations_survive_review_but_old_translations_clear_on_english_edit(
    owner: User,
) -> None:
    with translation.override("en"):
        form = MealForm(data={**ROW, "date": DAY}, initial=ROW)
        assert form.is_valid(), form.errors
        meal = form.save(commit=False)
        meal.user = owner
        meal.save()
    assert meal.description_ka == ROW["description_ka"]
    with translation.override("en"):
        form = MealForm(
            data={**ROW, "date": DAY, "description": "Chicken and rice"}, instance=meal
        )
        assert form.is_valid(), form.errors
        form.save()
    meal.refresh_from_db()
    assert meal.description_ka == "" and meal.portion_ka == ROW["portion_ka"]


def test_exact_original_input_survives_ai_review_and_manual_edit(
    client: Client, owner: User
) -> None:
    configure(client, owner)
    raw = "  ნადილი:\nერთი ჯამი ბრინჯი\n  <script>alert('test')</script>  "
    with (
        patch("tracker.openrouter.models", return_value=[MODEL]),
        patch(
            "tracker.openrouter.estimate_day", return_value=deepcopy(RESULT)
        ) as provider,
    ):
        response = client.post("/ai/", {"date": DAY.isoformat(), "prompt": raw})
    assert response.status_code == 302
    batch = MealEstimate.objects.get(user=owner)
    assert batch.original_input == raw and provider.call_args.args[2] == raw
    path = f"/ai/review/{batch.pk}/"
    data = {
        "meals-TOTAL_FORMS": "1",
        "meals-INITIAL_FORMS": "1",
        **{f"meals-0-{k}": v or "" for k, v in ROW.items()},
    }
    assert client.post(path, data).status_code == 302
    meal = Meal.objects.get(user=owner)
    assert meal.estimate_id == batch.pk
    source = f"/ai/source/{batch.pk}/"
    body = client.get(source)
    assert "no-store" in body["Cache-Control"]
    assert b"&lt;script&gt;" in body.content and b"<script>alert" not in body.content
    assert source in client.get(f"/?date={DAY}").content.decode()
    data = {**ROW, "date": DAY, "description": "Rice, lentils and salad", "time": ""}
    assert client.post(f"/meals/{meal.pk}/edit/", data).status_code == 302
    batch.refresh_from_db()
    assert batch.original_input == raw
    other = User.objects.create_user("source-other@example.test")
    client.force_login(other)
    assert client.get(source).status_code == 404


def test_failed_estimate_retains_original_and_whitespace_only_input_is_rejected(
    client: Client, owner: User
) -> None:
    configure(client, owner)
    with (
        patch("tracker.openrouter.models", return_value=[MODEL]),
        patch(
            "tracker.openrouter.estimate_day",
            side_effect=openrouter.OpenRouterError("Busy"),
        ) as provider,
    ):
        assert (
            client.post(
                "/ai/", {"date": DAY, "prompt": "  კვება\n " + "فارسی"}
            ).status_code
            == 200
        )
        assert client.post("/ai/", {"date": DAY, "prompt": " \n  "}).status_code == 200
    provider.assert_called_once()
    batch = MealEstimate.objects.get()
    assert batch.original_input == "  კვება\n فارسی" and batch.status == "failed"


def test_georgian_target_translation_and_original_profile_are_saved(
    client: Client, owner: User
) -> None:
    configure(client, owner)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ka"
    profile = {
        "age": "32",
        "height": "180",
        "weight": "85.00",
        "sex": "male",
        "activity": "light",
        "goal": "lose",
        "notes": "  ჩემი ჩანაწერი\nზუსტად ასე  ",
    }
    value: dict[str, Any] = {
        "can_suggest": True,
        "targets": {
            f"{name}_{bound}": amount
            for name, amount in (
                ("calories", 1800),
                ("protein", 120),
                ("carbs", 190),
                ("fat", 60),
            )
            for bound in ("min", "max")
        },
        "explanation": "A starting point.",
        "explanation_ka": "საწყისი წერტილი.",
        "assumptions": [],
        "assumptions_ka": [],
    }
    with translation.override("ka"):
        validated = openrouter.validate_targets(value)
    assert validated["explanation_ka"] == value["explanation_ka"]
    with (
        patch("tracker.openrouter.models", return_value=[MODEL]),
        patch("tracker.openrouter.estimate_targets", return_value=validated),
    ):
        assert client.post("/ai/targets/", profile).status_code == 302
    batch = TargetEstimate.objects.get(user=owner)
    assert batch.original_profile == profile
    body = client.get(f"/ai/targets/{batch.pk}/").content.decode()
    assert value["explanation_ka"] in body and "ჩემი ჩანაწერი" in body


@pytest.mark.parametrize(
    "path", ["/", "/targets/", "/weight/", "/statistics/", "/profile/", "/ai/targets/"]
)
def test_georgian_pages_render(client: Client, owner: User, path: str) -> None:
    configure(client, owner)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ka"
    with patch("tracker.openrouter.models", return_value=[MODEL]):
        response = client.get(path)
    assert response.status_code == 200
    assert '<html lang="ka" dir="ltr">' in response.content.decode()
    assert "დღის შეჯამება" in response.content.decode()
