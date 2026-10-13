import json
from collections.abc import Iterator
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
from tracker.forms import MealForm
from tracker.models import Meal, MealEstimate, TargetEstimate

pytestmark = pytest.mark.django_db
MODEL = openrouter.ModelInfo("google/test", "Test model", Decimal(0), Decimal(0))
DAY = date(2026, 10, 14)
ROW = {
    "kind": "lunch",
    "time": "13:00",
    "description": "Rice and lentils",
    "portion": "1 medium bowl",
    "description_fa": "برنج و عدس",
    "portion_fa": "یک کاسهٔ متوسط",
    "calories": 450,
    "protein": 20,
    "carbs": 60,
    "fat": 12,
}
RESULT = {
    "meals": [ROW],
    "assumptions": ["A medium bowl."],
    "assumptions_fa": ["یک کاسهٔ متوسط در نظر گرفته شد."],
}


@pytest.fixture(autouse=True)
def reset_language() -> Iterator[None]:
    translation.deactivate_all()
    yield
    translation.deactivate_all()


def response(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(result)}}
        ]
    }


def test_farsi_generates_and_validates_both_languages() -> None:
    with (
        translation.override("fa"),
        patch("tracker.openrouter._request", return_value=response(RESULT)) as request,
    ):
        result = openrouter.estimate_day(
            "fictional-key", MODEL, "برای ناهار یک کاسه برنج و عدس خوردم", DAY
        )
    assert result["meals"][0]["portion"] == "1 medium bowl"
    assert result["meals"][0]["portion_fa"] == ROW["portion_fa"]
    assert result["assumptions"] == RESULT["assumptions"]
    assert result["assumptions_fa"] == RESULT["assumptions_fa"]
    schema = request.call_args.kwargs["payload"]["response_format"]["json_schema"][
        "schema"
    ]
    assert "portion_fa" in schema["properties"]["meals"]["items"]["required"]
    assert "assumptions_fa" in schema["required"]
    assert "portion_fa" not in openrouter.MEAL_PROPERTIES


def test_english_generates_only_english_even_for_farsi_input() -> None:
    with (
        translation.override("en"),
        patch("tracker.openrouter._request", return_value=response(RESULT)) as request,
    ):
        result = openrouter.estimate_day("fictional-key", MODEL, "برنج و عدس", DAY)
    assert "assumptions_fa" not in result
    assert "description_fa" not in result["meals"][0]
    payload = request.call_args.kwargs["payload"]
    assert "Generate English only" in payload["messages"][0]["content"]
    assert (
        "assumptions_fa"
        not in payload["response_format"]["json_schema"]["schema"]["properties"]
    )
    assert (
        "explanation_fa"
        not in openrouter._response_schema(openrouter.TARGET_SCHEMA)["properties"]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"portion_fa": ""},
        {"description_fa": None},
        {"portion": "یک کاسه"},
        {"description": "برنج و عدس"},
    ],
)
def test_untranslated_originals_and_incomplete_translations_fail(
    change: dict[str, Any],
) -> None:
    with translation.override("fa"), pytest.raises(openrouter.OpenRouterError):
        openrouter.validate_estimate({**RESULT, "meals": [{**ROW, **change}]}, DAY)


def test_notes_must_have_english_original_and_matching_farsi() -> None:
    with translation.override("fa"):
        change: dict[str, Any]
        for change in (
            {"assumptions_fa": []},
            {"assumptions_fa": [1]},
            {"assumptions": ["فرض فارسی"]},
        ):
            with pytest.raises(openrouter.OpenRouterError):
                openrouter.validate_estimate({**RESULT, **change}, DAY)


@pytest.mark.parametrize("save_language", ["en", "fa"])
def test_review_preserves_both_languages_and_dashboard_switches(
    client: Client, owner: User, save_language: str
) -> None:
    batch = MealEstimate.objects.create(
        user=owner, date=DAY, model=MODEL.id, result=RESULT, status="ready"
    )
    client.force_login(owner)
    path = f"/ai/review/{batch.pk}/"
    data = {
        "meals-TOTAL_FORMS": "1",
        "meals-INITIAL_FORMS": "1",
        "meals-MIN_NUM_FORMS": "1",
        "meals-MAX_NUM_FORMS": "20",
        **{f"meals-0-{key}": value for key, value in ROW.items()},
    }
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = save_language
    assert client.post(path, data).status_code == 302
    meal = Meal.objects.get(user=owner)
    assert meal.portion == ROW["portion"] and meal.portion_fa == ROW["portion_fa"]
    assert (
        meal.description == ROW["description"]
        and meal.description_fa == ROW["description_fa"]
    )
    for language, expected, absent in (
        ("en", ROW["description"], ROW["description_fa"]),
        ("fa", ROW["description_fa"], ROW["description"]),
    ):
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        body = client.get(f"/?date={DAY.isoformat()}").content.decode()
        assert expected in body and absent not in body
    assert Meal.objects.count() == 1


def test_english_edit_invalidates_only_the_changed_translation(owner: User) -> None:
    meal = Meal.objects.create(user=owner, date=DAY, **ROW)
    with translation.override("en"):
        form = MealForm(
            data={**ROW, "date": DAY, "description": "Rice and chicken"}, instance=meal
        )
        assert form.is_valid(), form.errors
        form.save()
    meal.refresh_from_db()
    assert meal.description_fa == "" and meal.portion_fa == ROW["portion_fa"]
    with translation.override("fa"):
        assert meal.display_description == "Rice and chicken"
        assert meal.display_portion == ROW["portion_fa"]


def test_manual_farsi_portion_cannot_replace_english_source() -> None:
    with translation.override("fa"):
        form = MealForm(data={**ROW, "date": DAY, "portion": "یک کاسه"})
        assert not form.is_valid() and "portion" in form.errors
        normalized = MealForm(data={**ROW, "date": DAY, "portion": "۲۵۰ g"})
        assert normalized.is_valid(), normalized.errors
        assert normalized.cleaned_data["portion"] == "250 g"


def test_target_review_switches_text_without_changing_saved_result(
    client: Client, owner: User
) -> None:
    targets = {
        "calories_min": 1700,
        "calories_max": 1850,
        "protein_min": 110,
        "protein_max": 140,
        "carbs_min": 160,
        "carbs_max": 210,
        "fat_min": 50,
        "fat_max": 65,
    }
    value: dict[str, Any] = {
        "can_suggest": True,
        "targets": targets,
        "explanation": "A modest starting range.",
        "explanation_fa": "بازه‌ای مناسب برای شروع.",
        "assumptions": RESULT["assumptions"],
        "assumptions_fa": RESULT["assumptions_fa"],
    }
    with translation.override("fa"):
        result = openrouter.validate_targets(value)
        assert (
            "explanation_fa"
            in openrouter._response_schema(openrouter.TARGET_SCHEMA)["required"]
        )
    batch = TargetEstimate.objects.create(
        user=owner, model=MODEL.id, result=result, status="ready"
    )
    client.force_login(owner)
    for language, expected in (
        ("en", value["explanation"]),
        ("fa", value["explanation_fa"]),
    ):
        client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
        assert expected in client.get(f"/ai/targets/{batch.pk}/").content.decode()
    batch.refresh_from_db()
    assert batch.result == result
