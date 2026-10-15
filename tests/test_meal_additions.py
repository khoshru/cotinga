from collections.abc import Iterator
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone, translation

from accounts.models import User
from tracker.credentials import encrypt_key
from tracker.models import Meal, MealEstimate, OpenRouterSettings

from .test_ai_replacement import DAY, MODEL, ROW, draft, review

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def journal(
    client: Client, owner: User, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    client.force_login(owner)
    OpenRouterSettings.objects.create(
        user=owner,
        model=MODEL.id,
        encrypted_api_key=encrypt_key("fictional-addition-key"),
    )
    monkeypatch.setattr("tracker.openrouter.models", lambda: [MODEL])
    with translation.override("en"):
        yield


@pytest.mark.parametrize("kind", Meal.Kind.values)
@pytest.mark.parametrize("language", ["en", "ka", "fa"])
def test_addition_combines_one_meal_and_preserves_inputs(
    client: Client, owner: User, kind: str, language: str
) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    source = MealEstimate.objects.create(
        user=owner,
        date=DAY,
        model=MODEL.id,
        status="saved",
        original_input="  Original food\nexactly as typed.  ",
    )
    old = Meal.objects.create(
        user=owner,
        date=DAY,
        estimate=source,
        **{
            **ROW,
            "kind": kind,
            "description": "Rice",
            "description_fa": "برنج",
            "description_ka": "ბრინჯი",
            "calories": 500,
        },
    )
    old.source_estimates.add(source)
    value = {
        "meals": [
            {
                **ROW,
                "kind": kind,
                "description_fa": "تخم‌مرغ و نان",
                "description_ka": "კვერცხი და პური",
            }
        ],
        "assumptions": [],
    }
    batch = draft(owner, value)
    batch.original_input = "Extra food\nnot the original food again."
    batch.save(update_fields=["original_input"])
    path, data = review(client, batch)
    preview = client.get(path)
    assert preview.context["formset"][0]["action"].value() == "add"
    assert preview.context["updated_total"] == 825
    data["meals-0-action"] = "add"
    assert client.post(path, data).status_code == 302
    saved = Meal.objects.get(user=owner, date=DAY, kind=kind)
    assert saved.pk == old.pk and saved.calories == 825
    assert saved.description == "Rice; Eggs and toast"
    assert saved.description_fa == "برنج; تخم‌مرغ و نان"
    assert saved.description_ka == "ბრინჯი; კვერცხი და პური"
    for name in ("protein", "carbs", "fat"):
        assert getattr(saved, name) == 2 * ROW[name]
    assert set(saved.source_estimates.values_list("pk", flat=True)) == {
        source.pk,
        batch.pk,
    }
    batch.refresh_from_db()
    assert batch.result["save_actions"] == {kind: "add"}
    assert batch.result["replaced_meals"] == []
    assert batch.result["amended_meals"][0]["estimate_id"] == str(source.pk)
    response = client.get(f"/ai/source/{batch.pk}/?meal={saved.pk}")
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert source.original_input in response.content.decode()
    assert batch.original_input in response.content.decode()
    assert client.post(path, data).status_code == 302
    saved.refresh_from_db()
    assert (
        saved.calories == 825 and Meal.objects.filter(user=owner, date=DAY).count() == 1
    )


def test_mixed_add_replace_and_new_meals_are_scoped_and_totalled(
    client: Client, owner: User
) -> None:
    breakfast = Meal.objects.create(
        user=owner, date=DAY, **{**ROW, "description": "Old breakfast", "calories": 500}
    )
    lunch = Meal.objects.create(
        user=owner, date=DAY, **{**ROW, "kind": "lunch", "calories": 600}
    )
    snack = Meal.objects.create(
        user=owner, date=DAY, **{**ROW, "kind": "snack", "calories": 100}
    )
    past = Meal.objects.create(user=owner, date=DAY - timedelta(days=1), **ROW)
    other = User.objects.create_user("other-addition@example.test")
    foreign = Meal.objects.create(user=other, date=DAY, **ROW)
    batch = draft(
        owner,
        {
            "meals": [
                deepcopy(ROW),
                {**ROW, "kind": "lunch"},
                {**ROW, "kind": "dinner"},
            ],
            "assumptions": [],
        },
    )
    path, data = review(client, batch)
    data.update(
        {"meals-0-action": "add", "meals-1-action": "replace", "meals-2-action": "add"}
    )
    preview = client.post(path, data)
    assert preview.status_code == 200
    assert preview.context["updated_total"] == 1575
    assert preview.context["replacements"] == [lunch]
    assert Meal.objects.get(pk=breakfast.pk).calories == 500
    data["confirm_replacement"] = "on"
    assert client.post(path, data).status_code == 302
    assert Meal.objects.get(pk=breakfast.pk).calories == 825
    assert not Meal.objects.filter(pk=lunch.pk).exists()
    assert Meal.objects.get(user=owner, date=DAY, kind="lunch").calories == 325
    assert Meal.objects.filter(pk__in=[snack.pk, past.pk, foreign.pk]).count() == 3
    assert (
        sum(
            Meal.objects.filter(user=owner, date=DAY).values_list("calories", flat=True)
        )
        == 1575
    )


def test_additions_from_two_tabs_need_a_fresh_preview(
    client: Client, owner: User
) -> None:
    Meal.objects.create(user=owner, date=DAY, **ROW)
    first, second = draft(owner), draft(owner)
    path, data = review(client, first)
    second_path, second_data = review(client, second)
    data["meals-0-action"] = second_data["meals-0-action"] = "add"
    assert client.post(path, data).status_code == 302
    stale = client.post(second_path, second_data)
    assert stale.status_code == 200 and b"This day changed" in stale.content
    assert Meal.objects.get(user=owner, date=DAY).calories == 650
    second_data["journal_state"] = stale.context["save_form"]["journal_state"].value()
    assert client.post(second_path, second_data).status_code == 302
    assert Meal.objects.get(user=owner, date=DAY).calories == 975


@pytest.mark.parametrize(
    "field,amount",
    [("description", "x" * 5990), ("calories", 9900), ("protein", 995)],
    ids=["long-text", "calories", "protein"],
)
def test_combined_limits_show_an_error_and_leave_everything_unchanged(
    client: Client, owner: User, field: str, amount: str | int
) -> None:
    old = Meal.objects.create(user=owner, date=DAY, **{**ROW, field: amount})
    batch = draft(owner)
    path, data = review(client, batch)
    data["meals-0-action"] = "add"
    response = client.post(path, data)
    assert response.status_code == 200 and b"would exceed" in response.content
    old.refresh_from_db()
    batch.refresh_from_db()
    assert getattr(old, field) == amount and batch.status == "ready"
    assert not old.source_estimates.exists()


def test_long_descriptions_and_missing_translations_never_hide_food(
    client: Client, owner: User
) -> None:
    old = Meal.objects.create(
        user=owner,
        date=DAY,
        **{
            **ROW,
            "description": "Old food " * 40,
            "description_fa": "غذای قبلی",
            "portion": "Old portion " * 20,
        },
    )
    batch = draft(owner)
    path, data = review(client, batch)
    data["meals-0-action"] = "add"
    assert client.post(path, data).status_code == 302
    saved = Meal.objects.get(pk=old.pk)
    assert saved.description == old.description + "; " + ROW["description"]
    assert saved.portion == old.portion + "; " + ROW["portion"]
    assert saved.description_fa == "غذای قبلی; Eggs and toast"
    edit = client.get(f"/meals/{saved.pk}/edit/")
    assert edit.status_code == 200 and saved.description in edit.content.decode()


def test_failed_addition_rolls_back_links_totals_and_status(
    client: Client, owner: User
) -> None:
    old = Meal.objects.create(user=owner, date=DAY, **ROW)
    batch = draft(owner)
    path, data = review(client, batch)
    data["meals-0-action"] = "add"
    with (
        patch.object(
            type(old.source_estimates),
            "set",
            side_effect=RuntimeError("disk failure"),
        ),
        pytest.raises(RuntimeError),
    ):
        client.post(path, data)
    old.refresh_from_db()
    batch.refresh_from_db()
    assert old.calories == ROW["calories"] and old.estimate_id is None
    assert batch.status == "ready" and "amended_meals" not in batch.result


def test_legacy_duplicates_combine_without_losing_food(
    client: Client, owner: User
) -> None:
    first = Meal.objects.create(user=owner, date=DAY, **ROW)
    Meal.objects.create(user=owner, date=DAY, **{**ROW, "description": "Milk"})
    batch = draft(owner)
    path, data = review(client, batch)
    data["meals-0-action"] = "add"
    assert client.post(path, data).status_code == 302
    saved = Meal.objects.get(user=owner, date=DAY)
    assert saved.pk == first.pk and saved.calories == 3 * ROW["calories"]
    assert "Milk" in saved.description


def test_original_inputs_are_private_and_replacement_starts_a_new_source_list(
    client: Client, owner: User
) -> None:
    first = draft(owner)
    path, data = review(client, first)
    assert client.post(path, data).status_code == 302
    second = draft(owner)
    path, data = review(client, second)
    data.update({"meals-0-action": "replace", "confirm_replacement": "on"})
    assert client.post(path, data).status_code == 302
    saved = Meal.objects.get(user=owner, date=DAY)
    assert list(saved.source_estimates.all()) == [second]
    assert client.get(f"/ai/source/{second.pk}/?meal=not-a-number").status_code == 404
    assert client.get(f"/ai/source/{second.pk}/?meal={2**64}").status_code == 404
    other = User.objects.create_user("private-inputs@example.test")
    foreign = Meal.objects.create(user=other, date=DAY, **ROW)
    assert client.get(f"/ai/source/{second.pk}/?meal={foreign.pk}").status_code == 404
    client.force_login(other)
    assert client.get(f"/ai/source/{second.pk}/?meal={saved.pk}").status_code == 404


def test_add_food_shortcut_prefills_only_the_meal_label(
    client: Client, owner: User
) -> None:
    Meal.objects.create(user=owner, date=DAY, **{**ROW, "kind": "dinner"})
    body = client.get(f"/?date={DAY}").content.decode()
    assert f"date={DAY}&amp;kind=dinner" in body and "Add food" in body
    response = client.get(f"/ai/?date={DAY}&kind=dinner")
    assert response.context["form"]["prompt"].value() == "Dinner: "
    assert response.context["form"]["date"].value() == DAY


def test_original_inputs_follow_save_order_for_drafts_created_earlier(
    client: Client, owner: User
) -> None:
    earlier_draft, first_saved = draft(owner), draft(owner)
    now = timezone.now()
    MealEstimate.objects.filter(pk=earlier_draft.pk).update(
        status="saved", saved_at=now
    )
    MealEstimate.objects.filter(pk=first_saved.pk).update(
        status="saved", saved_at=now - timedelta(minutes=1)
    )
    meal = Meal.objects.create(user=owner, date=DAY, estimate=earlier_draft, **ROW)
    meal.source_estimates.set([earlier_draft, first_saved])
    response = client.get(f"/ai/source/{earlier_draft.pk}/?meal={meal.pk}")
    assert [entry.pk for entry in response.context["inputs"]] == [
        first_saved.pk,
        earlier_draft.pk,
    ]
