import math
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone, translation

from accounts.models import User
from tracker.models import (
    BodyProfile,
    DailyTarget,
    ProgressPlan,
    WeightEntry,
    WeightGoal,
)
from tracker.outlook import (
    FEEDBACK,
    TIME_CONSTANT,
    build_outlook,
    current_weight,
    goal_days,
    maintenance_energy,
    project_weight,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def language() -> Iterator[None]:
    translation.deactivate_all()
    yield
    translation.deactivate_all()


@pytest.fixture
def plan(owner: User) -> ProgressPlan:
    BodyProfile.objects.create(
        user=owner, age=32, height=180, sex="male", activity="light"
    )
    WeightEntry.objects.create(user=owner, weight="85", date=timezone.localdate())
    WeightGoal.objects.create(user=owner, weight="80")
    DailyTarget.objects.create(user=owner, calories_min=1950, calories_max=2050)
    return ProgressPlan.objects.create(
        user=owner,
        mode="lose",
        maintenance_calories=2500,
        maintenance_weight=85,
        start_weight=85,
    )


def test_dynamic_loss_slows_and_respects_energy_balance() -> None:
    assert project_weight(85, 2500, 2500, 365) == 85
    first = 85 - project_weight(85, 2000, 2500, 7)
    second = project_weight(85, 2000, 2500, 7) - project_weight(85, 2000, 2500, 14)
    assert 0 < second < first < 500 * 7 / 7700
    assert project_weight(85, 2000, 2500, TIME_CONSTANT) == pytest.approx(
        85 - 500 / FEEDBACK * (1 - math.exp(-1))
    )
    assert project_weight(85, 2000, 2500, 1_000_000) == pytest.approx(85 - 500 / 22)
    assert project_weight(85, 3000, 2500, 70) - 85 == pytest.approx(
        85 - project_weight(85, 2000, 2500, 70)
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_inputs_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        project_weight(85, value, 2500, 7)
    with pytest.raises(ValueError):
        project_weight(85, 2000, 2500, -1)


def test_goal_inversion_plateaus_wrong_direction_and_horizon() -> None:
    days = goal_days(85, 80, 2000, 2500)
    assert days is not None and project_weight(85, 2000, 2500, days) <= 80
    assert project_weight(85, 2000, 2500, days - 1) > 80
    assert goal_days(85, 85.1, 2000, 2500) == 0
    assert goal_days(85, 80, 2500, 2500) is None
    assert goal_days(85, 80, 2800, 2500) is None
    assert goal_days(85, 80, 2400, 2500) is None
    assert goal_days(85, 65, 2000, 2500) is None
    assert goal_days(85, 84.6, 2490, 2500) is None
    assert goal_days(70, 75, 3000, 2500) is not None


def test_mifflin_estimate_and_manual_weight_adjustment(plan: ProgressPlan) -> None:
    assert maintenance_energy(plan, 83) == 2456
    plan.maintenance_calories = None
    profile = BodyProfile.objects.get(user=plan.user)
    assert maintenance_energy(plan, 85, profile) == pytest.approx(
        (9.99 * 85 + 6.25 * 180 - 4.92 * 32 + 5) * 1.375
    )
    profile.sex = "female"
    assert maintenance_energy(plan, 85, profile) == pytest.approx(
        (9.99 * 85 + 6.25 * 180 - 4.92 * 32 - 161) * 1.375
    )
    profile.age = None
    with pytest.raises(ValueError):
        maintenance_energy(plan, 85, profile)


def test_recent_average_excludes_old_future_and_other_user(owner: User) -> None:
    today = timezone.localdate()
    for days, value in [(0, 84), (-3, 86), (-7, 95), (1, 100)]:
        WeightEntry.objects.create(
            user=owner, date=today + timedelta(days=days), weight=value
        )
    other = User.objects.create_user("other@example.test")
    WeightEntry.objects.create(user=other, weight=200)
    reading = current_weight(owner)
    assert (
        reading
        and reading["value"] == 85
        and reading["readings"] == 2
        and reading["date"] == today
    )


def test_scenarios_order_weekly_rows_and_goal_maintenance(
    plan: ProgressPlan, owner: User
) -> None:
    result = build_outlook(owner, 26)
    assert result["ready"] and len(result["rows"]) == 27
    assert result["rows"][0]["values"] == [85, 85, 85]
    low, mid, high = result["rows"][12]["values"]
    assert low < mid < high < 85
    assert result["rows"][12]["low"] < mid < result["rows"][12]["high"]
    assert (
        result["scenarios"][0]["weeks"]
        < result["center"]["weeks"]
        < result["scenarios"][2]["weeks"]
    )
    assert result["maintenance_goal"] == 2390
    assert len(result["milestones"]) == 4
    assert result["progress"] == 0
    assert build_outlook(owner, 9999)["weeks"] == 26


def test_progress_rebases_but_preserves_milestones(
    plan: ProgressPlan, owner: User
) -> None:
    WeightEntry.objects.update(weight="82.5")
    result = build_outlook(owner)
    assert result["start"] == 82.5 and result["progress"] == 50
    assert [m["complete"] for m in result["milestones"]] == [True, True, False, False]
    original_days = goal_days(85, 80, 2000, 2500)
    assert original_days is not None and result["center"]["weeks"] < original_days / 7
    WeightEntry.objects.update(weight="79")
    assert build_outlook(owner)["reached"]


def test_maintenance_is_ongoing_with_a_band(plan: ProgressPlan, owner: User) -> None:
    plan.mode = "maintain"
    plan.maintenance_calories = 2000
    plan.save()
    WeightGoal.objects.update(weight=85)
    result = build_outlook(owner)
    assert result["center"]["weight"] == 85 and result["stays_in_band"]
    assert (
        result["band"] == 0.85
        and result["progress"] is None
        and not result["milestones"]
    )
    plan.maintenance_calories = 2500
    plan.save()
    result = build_outlook(owner)
    assert not result["stays_in_band"] and result["exit_week"] is not None


def test_limited_projection_has_no_absurd_maintenance_or_extended_curve(
    plan: ProgressPlan, owner: User
) -> None:
    WeightGoal.objects.update(weight=50)
    target = DailyTarget.objects.get(user=owner)
    target.calories_min = 1200
    target.calories_max = 1300
    target.save()
    result = build_outlook(owner, 104)
    assert result["limited"] and result["center"]["weeks"] is None
    assert result["maintenance_goal"] is None
    assert result["rows"][-1]["values"][0] is None
    target.calories_min = 700
    target.save()
    assert build_outlook(owner)["unsupported"]


def test_empty_outlooks_no_data_and_missing_goal(owner: User) -> None:
    assert not build_outlook(owner)["ready"]
    assert current_weight(owner) is None
    ProgressPlan.objects.create(user=owner, start_weight=85, maintenance_calories=2500)
    WeightEntry.objects.create(user=owner, weight=85, date=date.min)
    reading = current_weight(owner)
    assert reading is not None and reading["age_days"] > 14
    assert build_outlook(owner)["needs_goal"]


DATA = {
    "mode": "lose",
    "goal_weight": "80",
    "maintenance_calories": "2500",
}


def test_save_is_private_and_does_not_change_targets(
    client: Client, owner: User
) -> None:
    assert client.get("/outlook/").status_code == 302
    client.force_login(owner)
    assert client.get("/outlook/").status_code == 200
    assert client.post("/outlook/", DATA).status_code == 200
    assert not ProgressPlan.objects.exists()
    WeightEntry.objects.create(user=owner, weight=85)
    BodyProfile.objects.create(
        user=owner, age=32, height=180, sex="male", activity="light"
    )
    before = DailyTarget.objects.get(user=owner).calories_min
    assert client.post("/outlook/", DATA).status_code == 302
    saved = ProgressPlan.objects.get(user=owner)
    assert saved.start_weight == Decimal("85") and saved.maintenance_weight == Decimal(
        "85"
    )
    assert WeightGoal.objects.get(user=owner).weight == 80
    assert DailyTarget.objects.get(user=owner).calories_min == before
    other = User.objects.create_user("other@example.test")
    ProgressPlan.objects.create(user=other, start_weight=150, maintenance_calories=6000)
    body = client.get("/outlook/").content.decode()
    assert "6000 kcal" not in body
    assert client.get("/outlook/").context["center"]["intake"] == 1800
    assert "no-store" in client.get("/outlook/")["Cache-Control"]


@pytest.mark.parametrize(
    "change",
    [
        {"goal_weight": ""},
        {"goal_weight": "100"},
        {"maintenance_calories": "NaN"},
        {"maintenance_calories": "999"},
        {"maintenance_calories": "6001"},
        {"mode": "gain", "goal_weight": "70"},
    ],
)
def test_invalid_profile_is_not_saved(
    client: Client, owner: User, change: dict[str, str]
) -> None:
    client.force_login(owner)
    WeightEntry.objects.create(user=owner, weight=85)
    BodyProfile.objects.create(
        user=owner, age=32, height=180, sex="male", activity="light"
    )
    response = client.post("/outlook/", {**DATA, **change})
    assert response.status_code == 200 and response.context["form"].errors
    assert not ProgressPlan.objects.exists()


def test_maintenance_reference_stays_fixed_when_editing(
    client: Client, plan: ProgressPlan, owner: User
) -> None:
    client.force_login(owner)
    assert (
        client.post(
            "/outlook/", {**DATA, "mode": "maintain", "goal_weight": ""}
        ).status_code
        == 302
    )
    assert WeightGoal.objects.get(user=owner).weight == 85
    WeightEntry.objects.update(weight=84)
    assert (
        client.post(
            "/outlook/", {**DATA, "mode": "maintain", "goal_weight": "85"}
        ).status_code
        == 302
    )
    assert WeightGoal.objects.get(user=owner).weight == 85
    assert client.get("/outlook/").context["goal"] == 85


@pytest.mark.parametrize("language", ["en", "fa"])
def test_outlook_renders_and_numeric_svg_coordinates_are_not_localized(
    client: Client, plan: ProgressPlan, language: str
) -> None:
    client.force_login(plan.user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get("/outlook/", {"weeks": "52"})
    assert response.status_code == 200
    body = response.content.decode()
    assert (
        f'lang="{language}"' in body
        and "data-outlook-week" in body
        and "outlook-week-data" in body
    )
    assert 'x1="64"' in body and 'cx="64.0"' in body
    assert '="nan"' not in body.lower()


def test_csrf_protects_the_plan(owner: User) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    assert client.post("/outlook/", DATA).status_code == 403
