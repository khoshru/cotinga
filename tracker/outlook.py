"""A transparent, simplified dynamic energy-balance planner, not the full NIH model.

Mifflin–St Jeor: https://pubmed.ncbi.nlm.nih.gov/2305711/
Hall et al. long-term feedback and adaptation:
https://www.niddk.nih.gov/-/media/Files/BWP/Hall_Lancet_Web_Appendix.pdf

Planning assumptions: long-term intake/weight feedback 22 kcal/kg/day;
combined feeding/adaptation response 0.24; effective tissue energy 7700 kcal/kg.
These population assumptions are not fitted to the individual. The +/-10% envelope
is sensitivity analysis, not a confidence interval. Water and body composition are
not simulated. Never infer maintenance from incomplete food diary entries.
"""

import math
from datetime import date, timedelta
from typing import Any

from django.utils import timezone
from django.utils.translation import gettext

from accounts.models import User

from .models import DailyTarget, ProgressPlan, WeightEntry, WeightGoal

ACTIVITY = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "very_active": 1.725}
ENERGY_DENSITY = 7700.0
ADAPTATION = 0.24
FEEDBACK = 22.0
RESPONSE = 1 - ADAPTATION
TIME_CONSTANT = ENERGY_DENSITY / (FEEDBACK * RESPONSE)
MAX_TIMELINE_DAYS = 104 * 7
MAX_RELATIVE_CHANGE = 0.20


def current_weight(user: User) -> dict[str, Any] | None:
    latest = WeightEntry.objects.filter(
        user=user, date__lte=timezone.localdate()
    ).first()
    if latest is None:
        return None
    readings = list(
        WeightEntry.objects.filter(
            user=user,
            date__range=(
                max(date.min + timedelta(days=6), latest.date) - timedelta(days=6),
                latest.date,
            ),
        ).values_list("weight", flat=True)
    )
    return {
        "value": sum(float(weight) for weight in readings) / len(readings),
        "date": latest.date,
        "readings": len(readings),
        "age_days": (timezone.localdate() - latest.date).days,
    }


def maintenance_energy(plan: ProgressPlan, weight: float) -> float:
    if plan.maintenance_calories is not None:
        reference = float(plan.maintenance_weight or plan.start_weight)
        return float(plan.maintenance_calories) + FEEDBACK * (weight - reference)
    if plan.age is None or plan.height is None:
        raise ValueError("Body details are incomplete.")
    sex = {"male": 1.0, "female": 0.0, "unspecified": 0.5}[plan.sex]
    resting = 9.99 * weight + 6.25 * plan.height - 4.92 * plan.age + 166 * sex - 161
    return resting * ACTIVITY[plan.activity]


def project_weight(
    start: float, intake: float, maintenance: float, days: float
) -> float:
    """Closed-form solution of a first-order energy/weight feedback equation."""
    if (
        not all(math.isfinite(value) for value in (start, intake, maintenance, days))
        or days < 0
    ):
        raise ValueError("Finite inputs and nonnegative time are required.")
    return start + (intake - maintenance) / FEEDBACK * -math.expm1(
        -days / TIME_CONSTANT
    )


def goal_days(
    start: float, goal: float, intake: float, maintenance: float
) -> int | None:
    difference = goal - start
    if abs(difference) <= 0.25:
        return 0
    if abs(difference) > start * MAX_RELATIVE_CHANGE:
        return None
    equilibrium_change = (intake - maintenance) / FEEDBACK
    if not equilibrium_change or difference * equilibrium_change <= 0:
        return None
    fraction = difference / equilibrium_change
    if not 0 < fraction < 1:
        return None
    days = math.ceil(-TIME_CONSTANT * math.log1p(-fraction))
    return days if days <= MAX_TIMELINE_DAYS else None


def _timeline(days: int | None, today: date) -> dict[str, Any]:
    return {
        "days": days,
        "weeks": math.ceil(days / 7) if days is not None else None,
        "month": today + timedelta(days=days) if days is not None else None,
    }


def _chart(
    rows: list[dict[str, Any]], goal: float, band: float, weeks: int
) -> dict[str, Any]:
    values: list[float] = [
        goal - band,
        goal + band,
        *(value for row in rows for value in row["values"] if value is not None),
        *(
            value
            for row in rows
            for value in (row["low"], row["high"])
            if value is not None
        ),
    ]
    low, high = min(values), max(values)
    padding = max((high - low) * 0.12, 0.5)
    low -= padding
    high += padding

    def y(value: float) -> float:
        return round(238 - (value - low) / (high - low) * 200, 2)

    def x(week: int) -> float:
        return round(64 + week / weeks * 616, 2)

    paths = []
    for scenario in range(3):
        points = [
            (x(row["week"]), y(row["values"][scenario]))
            for row in rows
            if row["values"][scenario] is not None
        ]
        paths.append(
            " ".join(
                f"{'M' if index == 0 else 'L'} {px} {py}"
                for index, (px, py) in enumerate(points)
            )
        )
    envelope = [
        (x(row["week"]), y(row["low"]), y(row["high"]))
        for row in rows
        if row["low"] is not None and row["high"] is not None
    ]
    area = " ".join(
        [
            *(
                f"{'M' if index == 0 else 'L'} {px} {bottom}"
                for index, (px, bottom, _) in enumerate(envelope)
            ),
            *(f"L {px} {top}" for px, _, top in reversed(envelope)),
            "Z",
        ]
    )
    for row in rows:
        row["x"] = x(row["week"])
        row["ys"] = [y(value) if value is not None else None for value in row["values"]]
    return {
        "paths": paths,
        "area": area,
        "goal_y": y(goal),
        "band_y": y(goal + band),
        "band_height": round(y(goal - band) - y(goal + band), 2),
        "ticks": [
            {
                "value": round(low + (high - low) * index / 4, 1),
                "y": y(low + (high - low) * index / 4),
            }
            for index in range(5)
        ],
        "x_ticks": [
            {"week": week, "x": x(week)}
            for week in sorted({0, weeks // 4, weeks // 2, 3 * weeks // 4, weeks})
        ],
    }


def build_outlook(user: User, weeks: int = 26) -> dict[str, Any]:
    if weeks not in (12, 26, 52, 104):
        weeks = 26
    weight = current_weight(user)
    plan = ProgressPlan.objects.filter(user=user).first()
    target, _ = DailyTarget.objects.get_or_create(user=user)
    goal_record = WeightGoal.objects.filter(user=user).first()
    result: dict[str, Any] = {
        "plan": plan,
        "weight": weight,
        "target": target,
        "weeks": weeks,
        "ready": False,
    }
    if plan is None or weight is None:
        return result
    start = float(weight["value"])
    goal = (
        float(goal_record.weight)
        if goal_record and goal_record.weight is not None
        else None
    )
    if plan.mode == "maintain" and goal is None:
        goal = float(plan.start_weight)
    if goal is None:
        result["needs_goal"] = True
        return result
    maintenance = maintenance_energy(plan, start)
    if (
        not 20 <= goal <= 350
        or not 20 <= start <= 350
        or not 1000 <= maintenance <= 6000
        or not 1200 <= target.calories_min <= target.calories_max <= 5000
    ):
        result["unsupported"] = True
        return result
    uncertainty = (
        0.15
        if plan.maintenance_calories is None and plan.sex == "unspecified"
        else 0.10
    )
    intakes = [
        target.calories_min,
        (target.calories_min + target.calories_max) / 2,
        target.calories_max,
    ]
    labels = [
        gettext("Lower target"),
        gettext("Midpoint target"),
        gettext("Upper target"),
    ]
    today = timezone.localdate()
    band = max(goal * 0.01, 0.5) if plan.mode == "maintain" else 0.0
    rows: list[dict[str, Any]] = []
    limited = False
    for week in range(weeks + 1):

        def bounded(intake: float, energy: float, day: int = week * 7) -> float | None:
            nonlocal limited
            value = project_weight(start, intake, energy, day)
            if (
                abs(value - start) > start * MAX_RELATIVE_CHANGE
                or not 20 <= value <= 350
            ):
                limited = True
                return None
            return round(value, 2)

        rows.append(
            {
                "week": week,
                "date": today + timedelta(weeks=week),
                "values": [bounded(intake, maintenance) for intake in intakes],
                "low": bounded(intakes[1], maintenance * (1 + uncertainty)),
                "high": bounded(intakes[1], maintenance * (1 - uncertainty)),
            }
        )
    scenarios: list[dict[str, Any]] = []
    for index, intake in enumerate(intakes):
        days = goal_days(start, goal, intake, maintenance)
        scenarios.append(
            {
                "label": labels[index],
                "intake": round(intake),
                "weight": rows[-1]["values"][index],
                "week_one": rows[1]["values"][index],
                "change": round(rows[1]["values"][index] - start, 2)
                if rows[1]["values"][index] is not None
                else None,
                "equilibrium": round(start + (intake - maintenance) / FEEDBACK, 1),
                **_timeline(days, today),
            }
        )
    sign = -1 if plan.mode == "lose" else 1
    reached = abs(start - goal) <= 0.25 or (
        plan.mode != "maintain" and (start - goal) * sign >= 0
    )
    gap = abs(float(plan.start_weight) - goal)
    progress = (
        max(
            0,
            min(
                100,
                100
                * (float(plan.start_weight) - start)
                / (float(plan.start_weight) - goal),
            ),
        )
        if gap > 0.25 and plan.mode != "maintain"
        else None
    )
    milestones = []
    if plan.mode != "maintain" and gap > 0.25:
        for percent in (25, 50, 75, 100):
            value = (
                float(plan.start_weight)
                + (goal - float(plan.start_weight)) * percent / 100
            )
            milestones.append(
                {
                    "percent": percent,
                    "weight": round(value, 1),
                    "complete": (progress or 0) >= percent,
                    **_timeline(
                        goal_days(start, value, intakes[1], maintenance), today
                    ),
                }
            )
    sensitivity_days = [
        goal_days(start, goal, intakes[1], maintenance * (1 + direction * uncertainty))
        for direction in (-1, 1)
    ]
    valid_days = [days for days in sensitivity_days if days is not None]
    inside = [
        row["week"]
        for row in rows
        if row["values"][1] is not None and abs(row["values"][1] - goal) <= band
    ]
    exits = [
        row["week"]
        for row in rows
        if row["values"][1] is None or abs(row["values"][1] - goal) > band
    ]
    result.update(
        {
            "ready": True,
            "start": round(start, 2),
            "goal": goal,
            "maintenance": round(maintenance),
            "maintenance_goal": round(maintenance + FEEDBACK * (goal - start))
            if abs(goal - start) <= start * MAX_RELATIVE_CHANGE
            and 1000 <= maintenance + FEEDBACK * (goal - start) <= 6000
            else None,
            "uncertainty": round(uncertainty * 100),
            "rows": rows,
            "scenarios": scenarios,
            "center": scenarios[1],
            "chart": _chart(rows, goal, band, weeks),
            "limited": limited,
            "reached": reached,
            "progress": round(progress) if progress is not None else None,
            "milestones": milestones,
            "sensitivity_earliest": math.ceil(min(valid_days) / 7)
            if valid_days
            else None,
            "sensitivity_latest": math.ceil(max(valid_days) / 7)
            if len(valid_days) == 2
            else None,
            "stays_in_band": not exits,
            "exit_week": exits[0] if exits else None,
            "within_band_now": 0 in inside,
            "band": round(band, 2),
            "direction_matches": plan.mode == "maintain"
            or (intakes[1] - maintenance) * sign > 0,
        }
    )
    return result
