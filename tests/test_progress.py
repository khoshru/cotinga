import csv
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import User
from tracker.models import Meal, WeightEntry, WeightGoal
from tracker.statistics import progress_summary

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("path", ["/statistics/", "/statistics/export/", "/weight/"])
def test_progress_pages_require_authentication(client: Client, path: str) -> None:
    assert client.get(path).status_code == 302


def log_meal(user: User, day: date, calories: int = 1800) -> Meal:
    return Meal.objects.create(
        user=user,
        date=day,
        kind="lunch",
        description="Recorded meal",
        calories=calories,
        protein=60,
        carbs=120,
        fat=40,
    )


def test_statistics_use_logged_days_and_private_records(
    client: Client, owner: User
) -> None:
    end = timezone.localdate()
    start = end - timedelta(days=6)
    log_meal(owner, start, 1800)
    log_meal(owner, end, 2200)
    WeightEntry.objects.create(user=owner, date=start, weight=Decimal("85.00"))
    WeightEntry.objects.create(user=owner, date=end, weight=Decimal("84.50"))
    WeightGoal.objects.create(user=owner, weight=Decimal("80.00"))
    other = User.objects.create_user("other@example.com")
    log_meal(other, end, 9000)
    WeightEntry.objects.create(user=other, date=end, weight=Decimal("150.00"))
    client.force_login(owner)
    response = client.get("/statistics/", {"period": "7"})
    assert response.status_code == 200
    context = response.context
    assert context["logged_days"] == 2 and context["days"] == 7
    assert context["averages"]["calories"] == 2000
    assert context["in_range"] == 1 and context["in_range_percent"] == 50
    assert context["meal_count"] == 2
    assert context["latest_weight"].weight == Decimal("84.50")
    assert context["weight_change"] == Decimal("-0.50")
    assert context["goal_gap"] == Decimal("4.50")
    assert context["records"][1].calories is None
    assert b"9000" not in response.content


def test_empty_statistics_render_gaps_and_prompts(client: Client, owner: User) -> None:
    client.force_login(owner)
    response = client.get("/statistics/")
    assert response.status_code == 200
    assert response.context["averages"]["calories"] is None
    assert not response.context["weight_chart"]["points"]
    assert b"A trend starts with a weigh-in" in response.content
    assert b"Your meals make the picture" in response.content


def test_long_period_intake_averages_exclude_unlogged_days(owner: User) -> None:
    end = timezone.localdate()
    start = end - timedelta(days=89)
    log_meal(owner, start, 1800)
    log_meal(owner, start + timedelta(days=2), 2000)
    summary = progress_summary(owner, start, end)
    chart = summary["intake_chart"]
    assert chart["weekly"]
    assert chart["bars"][0]["calories"] == 1900
    assert chart["bars"][0]["logged"] == 2
    assert chart["bars"][1]["calories"] is None


def test_rolling_weight_average_uses_previous_days_without_clipping(
    owner: User,
) -> None:
    end = timezone.localdate()
    start = end - timedelta(days=1)
    WeightEntry.objects.create(
        user=owner, date=start - timedelta(days=2), weight=Decimal("90")
    )
    WeightEntry.objects.create(user=owner, date=start, weight=Decimal("80"))
    WeightEntry.objects.create(user=owner, date=end, weight=Decimal("79"))
    chart = progress_summary(owner, start, end)["weight_chart"]
    assert chart["points"][0]["average"] == 85
    assert all(0 <= point["trend_y"] <= 190 for point in chart["points"])


@pytest.mark.parametrize(
    "params",
    [
        {"period": "custom"},
        {"period": "bad"},
        {"period": "custom", "start": "2026-10-12", "end": "2026-10-11"},
        {"period": "custom", "start": "2020-01-01", "end": "2026-10-12"},
        {"period": "custom", "start": "2026-10-12", "end": "9999-12-31"},
        {"period": "custom", "start": "invalid", "end": "2026-10-12"},
    ],
)
def test_invalid_periods_show_errors_and_cannot_export(
    client: Client, owner: User, params: dict[str, str]
) -> None:
    client.force_login(owner)
    response = client.get("/statistics/", params)
    assert response.status_code == 200 and response.context["filter_form"].errors
    assert client.get("/statistics/export/", params).status_code == 400


def test_custom_period_and_csv_export_are_private(client: Client, owner: User) -> None:
    day = timezone.localdate() - timedelta(days=1)
    log_meal(owner, day)
    other = User.objects.create_user("other@example.com")
    log_meal(other, day, 9999)
    client.force_login(owner)
    params = {"period": "custom", "start": day.isoformat(), "end": day.isoformat()}
    assert client.get("/statistics/", params).context["days"] == 1
    response = client.get("/statistics/export/", params)
    rows = list(csv.reader(StringIO(response.content.decode())))
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert rows[1] == [day.isoformat(), "1800", "60", "120", "40", ""]
    assert len(rows) == 2 and b"9999" not in response.content


def test_weight_can_be_logged_daily_or_weekly_and_updated_for_same_day(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    day = timezone.localdate()
    assert client.get("/weight/").status_code == 200
    for when, value in [(day - timedelta(days=7), "85"), (day, "84.5"), (day, "84.25")]:
        assert (
            client.post(
                "/weight/",
                {"date": when.isoformat(), "weight": value, "notes": "Morning"},
            ).status_code
            == 302
        )
    assert WeightEntry.objects.filter(user=owner).count() == 2
    assert WeightEntry.objects.get(user=owner, date=day).weight == Decimal("84.25")
    assert b"Morning" in client.get("/weight/").content


@pytest.mark.parametrize(
    "data",
    [
        {"weight": "0"},
        {"weight": "501"},
        {"weight": "NaN"},
        {"weight": "85", "date": "9999-12-31"},
        {"weight": "85", "date": "invalid"},
    ],
)
def test_invalid_weights_are_not_saved(
    client: Client, owner: User, data: dict[str, str]
) -> None:
    client.force_login(owner)
    response = client.post(
        "/weight/", {"date": timezone.localdate().isoformat(), **data}
    )
    assert response.status_code == 200 and response.context["form"].errors
    assert not WeightEntry.objects.exists()


def test_weight_goals_are_optional_and_owner_scoped(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    assert client.post("/weight/goal/", {"weight": "80"}).status_code == 302
    assert WeightGoal.objects.get(user=owner).weight == Decimal("80")
    assert client.post("/weight/goal/", {"weight": "0"}).status_code == 200
    assert WeightGoal.objects.get(user=owner).weight == Decimal("80")
    assert client.post("/weight/goal/", {"weight": ""}).status_code == 302
    assert WeightGoal.objects.get(user=owner).weight is None


def test_weight_deletion_requires_post_and_ownership(
    client: Client, owner: User
) -> None:
    other = User.objects.create_user("other@example.com")
    own = WeightEntry.objects.create(user=owner, weight=80)
    theirs = WeightEntry.objects.create(user=other, weight=90)
    client.force_login(owner)
    assert client.get(f"/weight/{own.pk}/delete/").status_code == 405
    assert client.post(f"/weight/{theirs.pk}/delete/").status_code == 404
    assert client.post(f"/weight/{own.pk}/delete/").status_code == 302
    assert WeightEntry.objects.filter(pk=theirs.pk).exists()


def test_weight_note_is_escaped(client: Client, owner: User) -> None:
    client.force_login(owner)
    client.post(
        "/weight/",
        {
            "date": timezone.localdate().isoformat(),
            "weight": "80",
            "notes": "<script>alert(1)</script>",
        },
    )
    response = client.get("/weight/")
    assert (
        b"&lt;script&gt;" in response.content
        and b"<script>alert" not in response.content
    )


def test_meal_edits_keep_ownership_and_can_move_date_and_time(
    client: Client, owner: User
) -> None:
    day = timezone.localdate()
    meal = log_meal(owner, day)
    other = User.objects.create_user("other@example.com")
    theirs = log_meal(other, day)
    client.force_login(owner)
    assert client.get(f"/meals/{meal.pk}/edit/").status_code == 200
    assert client.post(f"/meals/{theirs.pk}/edit/", {}).status_code == 404
    assert client.post(f"/meals/{meal.pk}/edit/", {}).status_code == 200
    assert (
        client.post(
            f"/meals/{meal.pk}/edit/",
            {
                "date": (day - timedelta(days=1)).isoformat(),
                "time": "12:30",
                "kind": "lunch",
                "description": "Adjusted meal",
                "portion": "1 plate",
                "calories": "500",
                "protein": "20",
                "carbs": "60",
                "fat": "10",
            },
        ).status_code
        == 302
    )
    meal.refresh_from_db()
    assert meal.date == day - timedelta(days=1) and meal.calories == 500
    assert meal.time is not None and meal.time.hour == 12
