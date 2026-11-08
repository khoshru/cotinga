from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone

from accounts.models import User
from tracker.credentials import encrypt_key
from tracker.models import Meal, MealEstimate, OpenRouterSettings, SavedPortion
from tracker.portions import MAX_SAVED_PORTIONS, serialize_portion

from .test_ai_replacement import MODEL, ROW

pytestmark = pytest.mark.django_db
FOOD = {"name": "Eggs", "quantity": "2", "unit": "piece", "note": "medium, boiled"}


@pytest.fixture(autouse=True)
def journal(client: Client, owner: User) -> None:
    client.force_login(owner)
    OpenRouterSettings.objects.create(
        user=owner,
        model=MODEL.id,
        encrypted_api_key=encrypt_key("fictional-portion-key"),
    )


def save(client: Client, **changes: Any) -> dict[str, Any]:
    response = client.post("/portions/", {**FOOD, **changes})
    assert response.status_code == 200
    return dict(response.json()["portion"])


def test_saved_food_reuses_normalized_name_and_explicitly_updates_default(
    client: Client, owner: User
) -> None:
    first = save(client)
    assert first["quantity"] == "2"
    second = save(
        client, name="  ＥＧＧＳ   ", quantity="0.50", unit="cup", note="cooked"
    )
    assert second["id"] == first["id"]
    portion = SavedPortion.objects.get(user=owner)
    assert portion.quantity == Decimal("0.50") and portion.unit == "cup"
    assert portion.normalized_name == "eggs"
    assert str(portion) == "ＥＧＧＳ"
    assert serialize_portion(portion)["quantity"] == "0.5"
    assert not Meal.objects.exists() and not MealEstimate.objects.exists()


@pytest.mark.parametrize("unit", SavedPortion.Unit.values)
def test_canonical_units_and_fractional_amounts(client: Client, unit: str) -> None:
    food = save(client, unit=unit, quantity="1.25")
    assert food["unit"] == unit and food["quantity"] == "1.25"


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": "0"},
        {"quantity": "-2"},
        {"quantity": "NaN"},
        {"quantity": "Infinity"},
        {"quantity": "0.001"},
        {"quantity": "100001"},
        {"name": " "},
        {"name": "x" * 101},
        {"unit": "unknown"},
        {"note": "x" * 141},
    ],
)
def test_invalid_portions_do_not_get_saved(
    client: Client, changes: dict[str, str]
) -> None:
    response = client.post("/portions/", {**FOOD, **changes})
    assert response.status_code == 400 and response.json()["errors"]
    assert not SavedPortion.objects.exists()


def test_saved_portions_are_private_and_mutations_require_csrf(
    client: Client, owner: User
) -> None:
    private = save(client)
    other = User.objects.create_user("other@example.com", "different-password")
    client.force_login(other)
    response = client.post(private["url"], {"action": "archive"})
    assert response.status_code == 404
    other_food = save(client)
    assert other_food["id"] != private["id"]
    assert SavedPortion.objects.filter(user=owner, is_active=True).exists()
    secured = Client(enforce_csrf_checks=True)
    secured.force_login(owner)
    assert secured.post("/portions/", FOOD).status_code == 403
    assert secured.post(private["url"], {"action": "use"}).status_code == 403
    assert client.get("/portions/").status_code == 405
    assert client.get(private["url"]).status_code == 405
    client.logout()
    assert client.post("/portions/", FOOD).status_code == 302
    assert client.post(private["url"], {"action": "use"}).status_code == 302


def test_one_off_use_keeps_default_and_archive_can_be_undone(client: Client) -> None:
    food = save(client, quantity="10")
    portion = SavedPortion.objects.get(pk=food["id"])
    before = portion.last_used_at
    response = client.post(food["url"], {"action": "use", "quantity": "50"})
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    portion.refresh_from_db()
    assert portion.quantity == 10 and portion.last_used_at >= before
    assert client.post(food["url"], {"action": "archive"}).status_code == 200
    portion.refresh_from_db()
    assert not portion.is_active
    assert client.post(food["url"], {"action": "use"}).status_code == 400
    assert client.post(food["url"], {"action": "restore"}).status_code == 200
    portion.refresh_from_db()
    assert SavedPortion.objects.get(pk=food["id"]).is_active
    assert portion.quantity == 10
    assert client.post(food["url"], {"action": "unknown"}).status_code == 400
    assert client.post(f"/portions/{2**64}/", {"action": "use"}).status_code == 404


def test_limit_allows_updates_but_not_new_or_restored_foods(
    client: Client, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_SAVED_PORTIONS == 100
    monkeypatch.setattr("tracker.portions.MAX_SAVED_PORTIONS", 1)
    food = save(client)
    assert client.post("/portions/", {**FOOD, "name": "Rice"}).status_code == 400
    assert save(client, quantity="3")["id"] == food["id"]
    assert client.post(food["url"], {"action": "archive"}).status_code == 200
    save(client, name="Rice")
    assert client.post(food["url"], {"action": "restore"}).status_code == 400
    assert client.post("/portions/", FOOD).status_code == 400
    rice = SavedPortion.objects.get(user=owner, name="Rice")
    client.post(f"/portions/{rice.pk}/", {"action": "archive"})
    assert save(client)["id"] == food["id"]
    assert SavedPortion.objects.filter(user=owner, is_active=True).count() == 1


@pytest.mark.parametrize("language", ["en", "fa", "ka"])
def test_builder_uses_private_defaults_and_safe_original_text(
    client: Client, owner: User, language: str
) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    raw = '</script><script>alert("food")</script>'
    food = save(client, name=raw, note="اندازه متوسط · საშუალო")
    archived = save(client, name="Hidden food")
    client.post(archived["url"], {"action": "archive"})
    other = User.objects.create_user("private@example.com", "different-password")
    SavedPortion.objects.create(user=other, name="Private food", quantity=1, unit="g")
    response = client.get("/ai/?kind=dinner")
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    data = response.context["saved_portions"]
    assert [item["id"] for item in data] == [food["id"]]
    assert data[0]["name"] == raw
    assert response.context["portion_kind"] == "dinner"
    content = response.content.decode()
    assert raw not in content and "\\u003C/script\\u003E" in content
    assert "Private food" not in content and "Hidden food" not in content
    assert 'name="quantity"' in content and 'name="unit"' in content
    assert 'src="/static/js/portions.js"' in content
    assert content.count('src="/static/js/portions.js"') == 1
    assert response.context["form"]["prompt"].value()


def test_composed_description_stays_verbatim_and_defaults_never_rewrite_it(
    client: Client, owner: User
) -> None:
    save(client, quantity="5", name="Eggs")
    original = "  breakfast: 2 pieces Eggs (medium, boiled)\n1.5 cups Milk  "
    with (
        patch("tracker.openrouter.find_model", return_value=MODEL),
        patch(
            "tracker.openrouter.estimate_day",
            return_value={"meals": [ROW], "assumptions": []},
        ) as estimate,
    ):
        response = client.post(
            "/ai/",
            {
                "prompt": original,
                "date": timezone.localdate(),
                "submission_id": uuid4(),
            },
        )
    assert response.status_code == 302
    assert estimate.call_args.args[2] == original
    batch = MealEstimate.objects.get(user=owner)
    assert batch.original_input == original
    assert SavedPortion.objects.get(user=owner).quantity == 5
    assert not Meal.objects.exists()


def test_invalid_estimate_keeps_builder_available(client: Client) -> None:
    save(client)
    response = client.post("/ai/?kind=invalid", {"prompt": " ", "date": "invalid"})
    assert response.status_code == 200 and len(response.context["saved_portions"]) == 1
    assert response.context["portion_kind"] == ""
