"""Conditional energy-balance estimates from seven recorded calendar days."""

from datetime import timedelta
from typing import Any

from django.utils import timezone

from accounts.models import User

from .models import BodyProfile, ProgressPlan, WeightEntry
from .outlook import (
    FEEDBACK,
    MAX_RELATIVE_CHANGE,
    current_weight,
    maintenance_energy,
    project_weight,
)
from .statistics import daily_records


def weekly_review(user: User) -> dict[str, Any]:
    end = timezone.localdate() - timedelta(days=1)
    start = end - timedelta(days=6)
    records = daily_records(user, start, end)
    logged = [row for row in records if row.calories is not None]
    result: dict[str, Any] = {
        "start": start,
        "end": end,
        "logged_days": len(logged),
        "ready": False,
        "days": [{"date": row.date, "calories": row.calories} for row in records],
    }
    profile = BodyProfile.objects.filter(user=user).first()
    weight = current_weight(user)
    if profile is None or not profile.complete or weight is None:
        result["needs_profile"] = True
        return result
    if len(logged) != 7:
        return result
    baseline = WeightEntry.objects.filter(
        user=user, date__range=(start - timedelta(days=14), start)
    ).first()
    reference = float(baseline.weight) if baseline else float(weight["value"])
    plan = ProgressPlan.objects.filter(user=user).first()
    energy = maintenance_energy(plan, reference, profile)
    if (
        not 20 <= reference <= 350
        or not 1000 <= energy <= 6000
        or any(not 0 <= int(row.calories or 0) <= 10000 for row in logged)
    ):
        result["unsupported"] = True
        return result

    def simulate(offset: float) -> tuple[float, list[dict[str, Any]]]:
        predicted = reference
        rows = []
        for row in records:
            intake = int(row.calories or 0)
            maintenance = energy * (1 + offset) + FEEDBACK * (predicted - reference)
            rows.append(
                {
                    "date": row.date,
                    "calories": intake,
                    "maintenance": round(maintenance),
                    "balance": round(intake - maintenance),
                }
            )
            predicted = project_weight(predicted, intake, maintenance, 1)
        return predicted - reference, rows

    change, rows = simulate(0)
    sensitivity = [simulate(offset)[0] for offset in (-0.1, 0.1)]
    if (
        abs(change) > reference * MAX_RELATIVE_CHANGE
        or not 20 <= reference + change <= 350
    ):
        result["unsupported"] = True
        return result
    scale = max(row["calories"] for row in rows) or 1
    for row in rows:
        row["bar"] = (
            round(max(6, row["calories"] / scale * 100)) if row["calories"] else 0
        )
    actual = WeightEntry.objects.filter(user=user, date=end + timedelta(days=1)).first()
    result.update(
        {
            "ready": True,
            "average": round(sum(row.calories or 0 for row in records) / 7),
            "maintenance": round(energy),
            "change": round(change, 2),
            "absolute_change": round(abs(change), 2),
            "direction": "lose"
            if change < -0.05
            else "gain"
            if change > 0.05
            else "maintain",
            "low": round(min(sensitivity), 2),
            "high": round(max(sensitivity), 2),
            "reference": round(reference, 1),
            "reference_date": baseline.date if baseline else weight["date"],
            "historical_reference": baseline is not None,
            "days": rows,
            "actual_change": round(float(actual.weight) - reference, 2)
            if actual and baseline and baseline.date == start
            else None,
        }
    )
    return result
