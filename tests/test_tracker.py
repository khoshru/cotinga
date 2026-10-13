from datetime import date

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import User
from tracker.forms import TargetForm
from tracker.models import DailyTarget, Meal
from tracker.summary import summarize

pytestmark = pytest.mark.django_db


def meal_data(day: date | None = None) -> dict[str, str | int]:
    return {
        "date": (day or timezone.localdate()).isoformat(),
        "kind": "lunch",
        "description": "Rice and lentils",
        "calories": 450,
        "protein": 20,
        "carbs": 60,
        "fat": 12,
    }


def test_dashboard_requires_login_without_creating_personal_data(
    client: Client,
) -> None:
    response = client.get("/")
    assert response.status_code == 302
    assert response.url == "/accounts/login/?next=/"  # type: ignore[attr-defined]
    assert Meal.objects.count() == DailyTarget.objects.count() == 0


def test_private_dashboard_starts_empty(client: Client, owner: User) -> None:
    client.force_login(owner)
    response = client.get("/")
    assert response.status_code == 200
    assert response.context["meals"] == []
    assert [n.eaten for n in response.context["nutrients"]] == [0, 0, 0, 0]
    assert DailyTarget.objects.get(user=owner).calories_max == 1850


def test_meal_persists_and_is_scoped_to_selected_date(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    day = date(2026, 10, 12)
    response = client.post("/meals/new/", meal_data(day))
    assert response.status_code == 302
    assert response.url == "/?date=2026-10-12"  # type: ignore[attr-defined]
    meal = Meal.objects.get(user=owner)
    assert meal.date == day
    assert meal.calories == 450
    assert str(meal) == "Lunch: Rice and lentils"
    assert client.get("/?date=2026-10-12").context["nutrients"][0].eaten == 450
    assert client.get("/?date=2026-10-11").context["nutrients"][0].eaten == 0


def test_user_cannot_see_or_delete_another_users_meal(
    client: Client, owner: User
) -> None:
    other = User.objects.create_user("other@example.com")
    meal = Meal.objects.create(user=other, **meal_data())
    client.force_login(owner)
    assert client.get("/").context["meals"] == []
    assert client.post(f"/meals/{meal.pk}/delete/").status_code == 404
    assert Meal.objects.filter(pk=meal.pk).exists()


def test_delete_requires_post_and_removes_own_meal(client: Client, owner: User) -> None:
    meal = Meal.objects.create(user=owner, **meal_data())
    client.force_login(owner)
    assert client.get(f"/meals/{meal.pk}/delete/").status_code == 405
    assert client.post(f"/meals/{meal.pk}/delete/").status_code == 302
    assert not Meal.objects.filter(pk=meal.pk).exists()


@pytest.mark.parametrize("path", ["/meals/new/", "/targets/", "/meals/1/delete/"])
def test_anonymous_write_paths_require_login(client: Client, path: str) -> None:
    response = client.post(path, meal_data())
    assert response.status_code == 302
    assert response.url.startswith("/accounts/login/?next=")  # type: ignore[attr-defined]


def test_meal_requires_csrf(owner: User) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    assert client.post("/meals/new/", meal_data()).status_code == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("calories", -1),
        ("protein", 1001),
        ("date", "broken"),
        ("description", ""),
        ("kind", "unknown"),
    ],
)
def test_invalid_meal_is_not_saved(
    client: Client, owner: User, field: str, value: str | int
) -> None:
    client.force_login(owner)
    data = meal_data()
    data[field] = value
    response = client.post("/meals/new/", data)
    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert Meal.objects.count() == 0


def test_meal_form_handles_empty_post(client: Client, owner: User) -> None:
    client.force_login(owner)
    assert client.get("/meals/new/").status_code == 200
    response = client.post("/meals/new/", {})
    assert response.context["form"].is_bound
    assert response.context["form"].errors


def test_targets_save_only_for_current_user(client: Client, owner: User) -> None:
    other = User.objects.create_user("other@example.com")
    other_target = DailyTarget.objects.create(user=other)
    client.force_login(owner)
    response = client.get("/targets/")
    form = response.context["form"]
    data = dict(form.initial)
    data.update(calories_min=1900, calories_max=2000)
    assert client.post("/targets/", data).status_code == 302
    assert DailyTarget.objects.get(user=owner).calories_min == 1900
    other_target.refresh_from_db()
    assert other_target.calories_min == 1750
    assert str(other_target) == "Targets for other@example.com"


def test_inverted_ranges_rejected_by_form_and_database(owner: User) -> None:
    target = DailyTarget.objects.create(user=owner)
    form = TargetForm(instance=target)
    data = dict(form.initial)
    data["protein_min"] = 200
    data["protein_max"] = 100
    form = TargetForm(data=data, instance=target)
    assert not form.is_valid()
    assert "protein_max" in form.errors
    with pytest.raises(IntegrityError), transaction.atomic():
        DailyTarget.objects.filter(pk=target.pk).update(
            protein_min=200, protein_max=100
        )


def test_invalid_targets_page_preserves_existing_values(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    assert client.post("/targets/", {}).status_code == 200
    assert DailyTarget.objects.get(user=owner).calories_min == 1750


@pytest.mark.parametrize(
    "eaten,expected",
    [
        (0, "1750–1850 kcal left"),
        (1800, "Within your range"),
        (2000, "150 kcal over range"),
    ],
)
def test_summary_under_within_and_above_range(eaten: int, expected: str) -> None:
    nutrient = summarize(
        {"calories": eaten, "protein": 0, "carbs": 0, "fat": 0}, DailyTarget()
    )[0]
    assert nutrient.status == expected
    assert 0 <= nutrient.fill <= 100
    assert nutrient.range_start + nutrient.range_width <= 100.01


def test_zero_macro_targets_do_not_divide_by_zero() -> None:
    target = DailyTarget(protein_min=0, protein_max=0)
    protein = summarize({"calories": 0, "protein": 0, "carbs": 0, "fat": 0}, target)[1]
    assert protein.fill == 0
    assert protein.status == "Within your range"


@pytest.mark.parametrize("value", ["invalid", "0001-01-01", "9999-12-31"])
def test_date_selection_handles_invalid_and_extreme_dates(
    client: Client, owner: User, value: str
) -> None:
    client.force_login(owner)
    assert client.get("/", {"date": value}).status_code == 200


def test_descriptions_are_escaped(client: Client, owner: User) -> None:
    client.force_login(owner)
    data = meal_data()
    data["description"] = "<script>alert('test')</script>"
    client.post("/meals/new/", data)
    response = client.get("/")
    assert b"&lt;script&gt;" in response.content
    assert b"<script>alert(" not in response.content


def test_sign_in_and_post_only_logout(client: Client, owner: User) -> None:
    assert client.get("/accounts/login/").status_code == 200
    assert (
        client.post(
            "/accounts/login/", {"username": "OWNER@EXAMPLE.COM", "password": "wrong"}
        ).status_code
        == 200
    )
    response = client.post(
        "/accounts/login/",
        {"username": "OWNER@EXAMPLE.COM", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 302
    assert client.get("/").status_code == 200
    assert client.get("/accounts/logout/").status_code == 405
    assert client.post("/accounts/logout/").status_code == 302
    assert client.get("/").status_code == 302


def test_login_does_not_redirect_to_external_site(client: Client, owner: User) -> None:
    response = client.post(
        "/accounts/login/?next=https://example.org",
        {"username": owner.email, "password": "correct-horse-battery-staple"},
    )
    assert response.url == "/"  # type: ignore[attr-defined]
