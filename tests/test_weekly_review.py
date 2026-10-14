from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import User
from tracker.models import (
    BodyProfile,
    DailyTarget,
    Meal,
    ProgressPlan,
    WeightEntry,
    WeightGoal,
)
from tracker.outlook import build_outlook, project_weight
from tracker.weekly_review import weekly_review

pytestmark = pytest.mark.django_db


@pytest.fixture
def weekly_plan(owner: User) -> ProgressPlan:
    BodyProfile.objects.create(
        user=owner, age=32, height=180, sex="male", activity="light"
    )
    WeightEntry.objects.create(
        user=owner, date=timezone.localdate() - timedelta(days=7), weight=85
    )
    WeightEntry.objects.create(user=owner, date=timezone.localdate(), weight="84.8")
    WeightGoal.objects.create(user=owner, weight=80)
    DailyTarget.objects.create(user=owner, calories_min=1950, calories_max=2050)
    return ProgressPlan.objects.create(
        user=owner, start_weight=85, maintenance_calories=2500, maintenance_weight=85
    )


def log_week(owner: User, calories: int) -> None:
    for offset in range(1, 8):
        Meal.objects.create(
            user=owner,
            date=timezone.localdate() - timedelta(days=offset),
            kind="dinner",
            description="A recorded day",
            calories=calories,
        )


@pytest.mark.parametrize(
    ("calories", "direction"), [(2000, "lose"), (2500, "maintain"), (3000, "gain")]
)
def test_weekly_estimate_matches_dynamic_model(
    weekly_plan: ProgressPlan, owner: User, calories: int, direction: str
) -> None:
    log_week(owner, calories)
    result = weekly_review(owner)
    assert (
        result["ready"]
        and result["logged_days"] == 7
        and result["direction"] == direction
    )
    assert result["change"] == round(project_weight(85, calories, 2500, 7) - 85, 2)
    assert result["average"] == calories and result["actual_change"] == -0.2
    assert result["low"] < result["change"] < result["high"]
    if direction == "lose":
        assert result["days"][-1]["maintenance"] < result["days"][0]["maintenance"]
    elif direction == "gain":
        assert result["days"][-1]["maintenance"] > result["days"][0]["maintenance"]


def test_missing_days_are_not_zero_and_today_is_excluded(
    weekly_plan: ProgressPlan, owner: User
) -> None:
    log_week(owner, 2000)
    Meal.objects.filter(
        user=owner, date=timezone.localdate() - timedelta(days=2)
    ).delete()
    Meal.objects.create(user=owner, kind="dinner", description="Today", calories=9000)
    result = weekly_review(owner)
    assert not result["ready"] and result["logged_days"] == 6 and "change" not in result
    assert sum(day["calories"] is None for day in result["days"]) == 1
    assert result["end"] == timezone.localdate() - timedelta(days=1)


def test_multiple_meals_are_summed_and_foreign_data_is_ignored(
    weekly_plan: ProgressPlan, owner: User
) -> None:
    log_week(owner, 1500)
    for offset in range(1, 8):
        Meal.objects.create(
            user=owner,
            date=timezone.localdate() - timedelta(days=offset),
            kind="lunch",
            description="Lunch",
            calories=500,
        )
    other = User.objects.create_user("weekly-other@example.test")
    log_week(other, 7000)
    WeightEntry.objects.create(user=other, weight=200)
    result = weekly_review(owner)
    assert result["average"] == 2000 and result["reference"] == 85
    assert all(day["calories"] == 2000 for day in result["days"])


def test_varying_intake_uses_each_day_and_does_not_change_targets(
    weekly_plan: ProgressPlan, owner: User
) -> None:
    start = timezone.localdate() - timedelta(days=7)
    predicted = 85.0
    for offset, intake in enumerate([1800, 2000, 2200, 2800, 1900, 2100, 2300]):
        Meal.objects.create(
            user=owner,
            date=start + timedelta(days=offset),
            kind="dinner",
            description="Recorded food",
            calories=intake,
        )
        energy = 2500 + 22 * (predicted - 85)
        predicted = project_weight(predicted, intake, energy, 1)
    result = weekly_review(owner)
    assert result["change"] == round(predicted - 85, 2)
    assert DailyTarget.objects.get(user=owner).calories_min == 1950


def test_profile_required_and_recent_weight_reference_is_labeled(
    weekly_plan: ProgressPlan, owner: User
) -> None:
    log_week(owner, 2000)
    WeightEntry.objects.filter(user=owner, date__lt=timezone.localdate()).delete()
    result = weekly_review(owner)
    assert (
        result["ready"]
        and not result["historical_reference"]
        and result["actual_change"] is None
    )
    assert result["reference"] == 84.8
    BodyProfile.objects.filter(user=owner).update(sex="")
    assert weekly_review(owner)["needs_profile"]


def test_unsupported_logs_and_weights_do_not_produce_predictions(
    weekly_plan: ProgressPlan, owner: User
) -> None:
    log_week(owner, 11000)
    assert weekly_review(owner)["unsupported"]
    Meal.objects.filter(user=owner).update(calories=2000)
    WeightEntry.objects.filter(user=owner).update(weight=500)
    assert weekly_review(owner)["unsupported"]


def test_outlook_maintenance_changes_by_week_and_new_weight(
    weekly_plan: ProgressPlan, owner: User
) -> None:
    before = build_outlook(owner)
    assert (
        before["rows"][0]["maintenance_values"][1]
        > before["rows"][12]["maintenance_values"][1]
        > before["rows"][26]["maintenance_values"][1]
    )
    WeightEntry.objects.filter(user=owner, date=timezone.localdate()).update(weight=82)
    after = build_outlook(owner)
    assert (
        after["maintenance"] < before["maintenance"]
        and after["maintenance_goal"] == before["maintenance_goal"]
    )


def test_editing_plan_preserves_known_maintenance_calibration(
    client: Client, weekly_plan: ProgressPlan, owner: User
) -> None:
    client.force_login(owner)
    WeightEntry.objects.filter(user=owner).update(weight=82)
    response = client.post(
        "/outlook/", {"mode": "lose", "goal_weight": 78, "maintenance_calories": 2500}
    )
    assert response.status_code == 302
    weekly_plan.refresh_from_db()
    assert weekly_plan.maintenance_weight == 85
    assert build_outlook(owner)["maintenance"] == 2434
    client.post(
        "/outlook/", {"mode": "lose", "goal_weight": 78, "maintenance_calories": 2400}
    )
    weekly_plan.refresh_from_db()
    assert weekly_plan.maintenance_weight == 82


def test_statistics_shows_conditional_estimate_and_dynamic_readout(
    client: Client, weekly_plan: ProgressPlan, owner: User
) -> None:
    log_week(owner, 2000)
    client.force_login(owner)
    response = client.get("/statistics/")
    assert response.status_code == 200 and response.context["weekly_review"]["ready"]
    assert b"If your logs include everything you ate" in response.content
    assert b"data-outlook-week-maintenance" in client.get("/outlook/").content
