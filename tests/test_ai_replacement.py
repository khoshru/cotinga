from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone, translation

from accounts.models import User
from tracker import openrouter
from tracker.credentials import encrypt_key
from tracker.meal_imports import submission_key
from tracker.models import Meal, MealEstimate, OpenRouterSettings

pytestmark = pytest.mark.django_db
DAY = date(2026, 10, 10)
MODEL = openrouter.ModelInfo("google/test", "Test", Decimal(0), Decimal(0))
ROW: dict[str, Any] = {
    "kind": "breakfast",
    "time": "08:00",
    "description": "Eggs and toast",
    "portion": "2 eggs, 1 slice toast",
    "calories": 325,
    "protein": 20,
    "carbs": 20,
    "fat": 18,
}


@pytest.fixture(autouse=True)
def setup(client: Client, owner: User, monkeypatch: pytest.MonkeyPatch) -> None:
    client.force_login(owner)
    OpenRouterSettings.objects.create(
        user=owner,
        model=MODEL.id,
        encrypted_api_key=encrypt_key("sk-or-fictional-replacement-key"),
    )
    monkeypatch.setattr(openrouter, "models", lambda: [MODEL])


def result() -> dict[str, Any]:
    return {"meals": [ROW.copy()], "assumptions": []}


def draft(owner: User, value: dict[str, Any] | None = None) -> MealEstimate:
    return MealEstimate.objects.create(
        user=owner, date=DAY, model=MODEL.id, status="ready", result=value or result()
    )


def review(client: Client, batch: MealEstimate) -> tuple[str, dict[str, Any]]:
    path = f"/ai/review/{batch.pk}/"
    response = client.get(path)
    assert response.status_code == 200
    data = {
        "journal_state": response.context["save_form"]["journal_state"].value(),
        "meals-TOTAL_FORMS": str(len(batch.result["meals"])),
        "meals-INITIAL_FORMS": str(len(batch.result["meals"])),
    }
    for index, row in enumerate(batch.result["meals"]):
        data.update({f"meals-{index}-{key}": value for key, value in row.items()})
    return path, data


def test_back_to_estimate_reuses_the_result_without_another_charge(
    client: Client, owner: User
) -> None:
    request = {
        "submission_id": str(uuid4()),
        "date": DAY.isoformat(),
        "prompt": "Breakfast: eggs and toast",
    }
    with patch("tracker.openrouter.estimate_day", return_value=result()) as provider:
        first = client.post("/ai/", request)
        assert first.status_code == 302
        second = client.post("/ai/", request)
        assert second.status_code == 302 and second.url == first.url  # type: ignore[attr-defined]
        batch = MealEstimate.objects.get()
        path, data = review(client, batch)
        assert client.post(path, data).status_code == 302
        assert client.post("/ai/", request).status_code == 302
    provider.assert_called_once()
    assert MealEstimate.objects.count() == 1
    assert Meal.objects.count() == 1 and Meal.objects.get().date == DAY


@pytest.mark.parametrize(
    "change", [{"date": "2026-10-09"}, {"prompt": "Breakfast: milk"}]
)
def test_changing_food_or_past_date_is_a_separate_estimate(
    client: Client, change: dict[str, str]
) -> None:
    request = {
        "submission_id": str(uuid4()),
        "date": DAY.isoformat(),
        "prompt": "Breakfast: eggs",
    }
    with patch("tracker.openrouter.estimate_day", return_value=result()) as provider:
        assert client.post("/ai/", request).status_code == 302
        MealEstimate.objects.update(created_at=timezone.now() - timedelta(seconds=30))
        assert client.post("/ai/", {**request, **change}).status_code == 302
    assert provider.call_count == 2 and MealEstimate.objects.count() == 2
    assert not Meal.objects.exists()


@pytest.mark.parametrize("stale", [False, True])
def test_replayed_processing_request_is_reused_or_recovers_after_worker_failure(
    client: Client, owner: User, stale: bool
) -> None:
    identifier = uuid4()
    data = {"submission_id": identifier, "date": DAY, "prompt": "Breakfast eggs"}
    batch = MealEstimate.objects.create(
        user=owner, date=DAY, model=MODEL.id, submission_key=submission_key(data)
    )
    if stale:
        MealEstimate.objects.filter(pk=batch.pk).update(
            created_at=timezone.now() - timedelta(minutes=3)
        )
    with patch("tracker.openrouter.estimate_day", return_value=result()) as provider:
        response = client.post("/ai/", {**data, "date": DAY.isoformat()})
    if stale:
        assert response.status_code == 302 and provider.call_count == 1
        batch.refresh_from_db()
        assert batch.status == "failed" and batch.submission_key == ""
    else:
        assert response.status_code == 200 and b"already processing" in response.content
        provider.assert_not_called()
        batch.refresh_from_db()
        assert batch.status == "processing"


def test_failed_request_can_be_retried_with_the_same_form(client: Client) -> None:
    request = {
        "submission_id": str(uuid4()),
        "date": DAY.isoformat(),
        "prompt": "Breakfast eggs",
    }
    with patch(
        "tracker.openrouter.estimate_day",
        side_effect=[openrouter.OpenRouterError("Busy"), result()],
    ) as provider:
        assert client.post("/ai/", request).status_code == 200
        MealEstimate.objects.update(created_at=timezone.now() - timedelta(seconds=30))
        assert client.post("/ai/", request).status_code == 302
    assert provider.call_count == 2
    assert list(
        MealEstimate.objects.order_by("created_at").values_list("status", flat=True)
    ) == ["failed", "ready"]


def test_replacement_requires_confirmation_and_replaces_only_that_day_and_kind(
    client: Client, owner: User
) -> None:
    old = Meal.objects.create(
        user=owner,
        date=DAY,
        **{**ROW, "calories": 519, "description_fa": "صبحانهٔ قبلی"},
    )
    duplicate = Meal.objects.create(user=owner, date=DAY, **{**ROW, "calories": 143})
    lunch = Meal.objects.create(
        user=owner, date=DAY, **{**ROW, "kind": "lunch", "calories": 372}
    )
    yesterday = Meal.objects.create(user=owner, date=DAY - timedelta(days=1), **ROW)
    other = User.objects.create_user("replacement-other@example.test")
    foreign = Meal.objects.create(user=other, date=DAY, **ROW)
    batch = draft(owner)
    path, data = review(client, batch)
    response = client.post(path, data)
    assert response.status_code == 200 and b"This field is required" in response.content
    assert Meal.objects.filter(pk__in=[old.pk, duplicate.pk]).count() == 2
    batch.refresh_from_db()
    assert batch.status == "ready"
    assert response.context["current_total"] == 1034
    assert response.context["updated_total"] == 697
    data["confirm_replacement"] = "on"
    data["meals-0-date"] = "2026-10-15"
    assert client.post(path, data).status_code == 302
    saved = Meal.objects.get(user=owner, date=DAY, kind="breakfast")
    assert saved.calories == 325 and saved.date == DAY
    assert Meal.objects.filter(pk__in=[lunch.pk, yesterday.pk, foreign.pk]).count() == 3
    batch.refresh_from_db()
    assert batch.status == "saved"
    assert {row["id"] for row in batch.result["replaced_meals"]} == {
        old.pk,
        duplicate.pk,
    }
    assert any(
        row["description_fa"] == "صبحانهٔ قبلی" for row in batch.result["replaced_meals"]
    )
    assert client.post(path, data).status_code == 302
    assert Meal.objects.filter(user=owner, date=DAY, kind="breakfast").count() == 1


def test_a_day_changed_in_another_tab_must_be_reviewed_again(
    client: Client, owner: User
) -> None:
    batch = draft(owner)
    path, data = review(client, batch)
    old = Meal.objects.create(user=owner, date=DAY, **{**ROW, "calories": 519})
    response = client.post(path, {**data, "confirm_replacement": "on"})
    assert response.status_code == 200 and b"This day changed" in response.content
    assert Meal.objects.get(pk=old.pk).calories == 519
    batch.refresh_from_db()
    assert batch.status == "ready"
    data["journal_state"] = response.context["save_form"]["journal_state"].value()
    data["confirm_replacement"] = "on"
    assert client.post(path, data).status_code == 302
    assert Meal.objects.get(user=owner, date=DAY, kind="breakfast").calories == 325


def test_two_drafts_for_the_same_meal_cannot_silently_overwrite_one_another(
    client: Client, owner: User
) -> None:
    first, second = draft(owner), draft(owner)
    first_path, first_data = review(client, first)
    second_path, second_data = review(client, second)
    assert client.post(first_path, first_data).status_code == 302
    assert client.post(second_path, second_data).status_code == 200
    assert Meal.objects.filter(user=owner, date=DAY).count() == 1
    second.refresh_from_db()
    assert second.status == "ready"


def test_skipped_meal_types_are_not_replaced(client: Client, owner: User) -> None:
    old = Meal.objects.create(user=owner, date=DAY, **ROW)
    value = result()
    value["meals"].append({**ROW, "kind": "lunch", "calories": 372})
    batch = draft(owner, value)
    path, data = review(client, batch)
    data["meals-0-DELETE"] = "on"
    assert client.post(path, data).status_code == 302
    assert Meal.objects.get(pk=old.pk).calories == 325
    assert Meal.objects.filter(user=owner, date=DAY).count() == 2


def test_edited_review_cannot_save_two_entries_with_the_same_kind(
    client: Client, owner: User
) -> None:
    value = result()
    value["meals"].append({**ROW, "kind": "lunch"})
    batch = draft(owner, value)
    path, data = review(client, batch)
    data["meals-1-kind"] = "breakfast"
    response = client.post(path, data)
    assert (
        response.status_code == 200
        and b"Keep one entry per meal type" in response.content
    )
    assert not Meal.objects.exists()


@pytest.mark.parametrize("language", ["en", "fa"])
def test_past_day_replacement_preview_is_localized_and_scoped_to_owner(
    client: Client, owner: User, language: str
) -> None:
    old = Meal.objects.create(
        user=owner, date=DAY, **{**ROW, "description_fa": "تخم‌مرغ و نان"}
    )
    other = User.objects.create_user("private-other@example.test")
    Meal.objects.create(user=other, date=DAY, **{**ROW, "description": "Private food"})
    batch = draft(owner)
    with translation.override(language):
        response = client.get(f"/ai/review/{batch.pk}/", HTTP_ACCEPT_LANGUAGE=language)
    assert response.status_code == 200
    assert response.context["replacements"] == [old]
    assert b"Private food" not in response.content


def test_manual_create_and_edit_cannot_introduce_duplicate_meal_types(
    client: Client, owner: User
) -> None:
    Meal.objects.create(user=owner, date=DAY, **ROW)
    lunch = Meal.objects.create(user=owner, date=DAY, **{**ROW, "kind": "lunch"})
    request = {**ROW, "date": DAY.isoformat()}
    assert client.post("/meals/new/", request).status_code == 200
    assert client.post(f"/meals/{lunch.pk}/edit/", request).status_code == 200
    assert Meal.objects.filter(user=owner, date=DAY, kind="breakfast").count() == 1
    lunch.refresh_from_db()
    assert lunch.kind == "lunch"


def test_database_rejects_duplicate_submission_keys_but_accepts_legacy_rows(
    owner: User,
) -> None:
    MealEstimate.objects.create(
        user=owner, date=DAY, model=MODEL.id, submission_key="x" * 64
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MealEstimate.objects.create(
            user=owner, date=DAY, model=MODEL.id, submission_key="x" * 64
        )
    MealEstimate.objects.create(user=owner, date=DAY, model=MODEL.id)
    MealEstimate.objects.create(user=owner, date=DAY, model=MODEL.id)


def test_failed_save_rolls_back_replacements_and_keeps_draft_ready(
    client: Client, owner: User
) -> None:
    old = Meal.objects.create(user=owner, date=DAY, **ROW)
    batch = draft(owner)
    path, data = review(client, batch)
    data["confirm_replacement"] = "on"
    with (
        patch(
            "tracker.ai_views.Meal.objects.bulk_create",
            side_effect=RuntimeError("disk failure"),
        ),
        pytest.raises(RuntimeError),
    ):
        client.post(path, data)
    assert Meal.objects.filter(pk=old.pk).exists()
    batch.refresh_from_db()
    assert batch.status == "ready" and "replaced_meals" not in batch.result


def test_ai_must_combine_multiple_rows_of_one_meal_type() -> None:
    value = result()
    value["meals"].append(deepcopy(ROW))
    with pytest.raises(
        openrouter.MealOutputError, match="prepared for review"
    ) as error:
        openrouter.validate_estimate(value, DAY)
    assert error.value.issues == ("meals:one_entry_per_meal_type_required",)
