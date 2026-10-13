"""Summaries of logged intake and actual weigh-ins, with SVG chart coordinates."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

from django.db.models import Sum

from accounts.models import User

from .models import NUTRIENTS, DailyTarget, Meal, WeightEntry, WeightGoal


@dataclass(frozen=True)
class DayRecord:
    date: date
    calories: int | None
    protein: int | None
    carbs: int | None
    fat: int | None
    weight: Decimal | None


def daily_records(user: User, start: date, end: date) -> list[DayRecord]:
    nutrients: dict[date, dict[str, Any]] = {
        row["date"]: cast(dict[str, Any], row)
        for row in Meal.objects.filter(user=user, date__range=(start, end))
        .values("date")
        .annotate(**{name: Sum(name) for name in NUTRIENTS})
    }
    weights = dict(
        WeightEntry.objects.filter(user=user, date__range=(start, end)).values_list(
            "date", "weight"
        )
    )
    result = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        row = nutrients.get(day)
        values = {name: int(row[name]) if row else None for name in NUTRIENTS}
        result.append(
            DayRecord(
                day,
                values["calories"],
                values["protein"],
                values["carbs"],
                values["fat"],
                weights.get(day),
            )
        )
    return result


def _intake_chart(records: list[DayRecord], target: DailyTarget) -> dict[str, Any]:
    step = 1 if len(records) <= 45 else 7
    groups = [records[index : index + step] for index in range(0, len(records), step)]
    values = [
        sum(row.calories or 0 for row in group) / len(logged)
        if (logged := [row for row in group if row.calories is not None])
        else None
        for group in groups
    ]
    scale = max([target.calories_max, *(value or 0 for value in values), 1]) * 1.15
    width = 540 / max(len(groups), 1)
    bars = []
    for index, (group, value) in enumerate(zip(groups, values, strict=True)):
        bars.append(
            {
                "x": round(30 + index * width + width * 0.15, 2),
                "width": round(width * 0.7, 2),
                "y": round(190 - (value or 0) / scale * 150, 2),
                "height": round((value or 0) / scale * 150, 2),
                "calories": round(value) if value is not None else None,
                "label": group[0].date.isoformat(),
                "logged": sum(row.calories is not None for row in group),
                "tone": "mint"
                if value is not None
                and target.calories_min <= value <= target.calories_max
                else "muted",
            }
        )
    return {
        "bars": bars,
        "weekly": step == 7,
        "target_y": round(190 - target.calories_max / scale * 150, 2),
        "target_height": round(
            (target.calories_max - target.calories_min) / scale * 150, 2
        ),
        "ticks": [
            {"y": 190 - index * 50, "value": round(scale * index / 3)}
            for index in range(4)
        ],
        "has_data": any(value is not None for value in values),
    }


def _weight_chart(entries: list[WeightEntry], start: date, end: date) -> dict[str, Any]:
    visible = [entry for entry in entries if start <= entry.date <= end]
    if not visible:
        return {"points": [], "path": "", "trend_path": "", "ticks": []}
    values = [float(entry.weight) for entry in visible]
    rolling = {}
    for entry in visible:
        earliest = max(date.min + timedelta(days=6), entry.date) - timedelta(days=6)
        recent = [
            float(previous.weight)
            for previous in entries
            if earliest <= previous.date <= entry.date
        ]
        rolling[entry.date] = sum(recent) / len(recent)
    values.extend(rolling.values())
    low, high = min(values), max(values)
    padding = max((high - low) * 0.25, 0.5)
    low, high = low - padding, high + padding
    span = max((end - start).days, 1)
    points = []
    for entry in visible:
        average = rolling[entry.date]
        points.append(
            {
                "x": round(30 + (entry.date - start).days / span * 540, 2),
                "y": round(190 - (float(entry.weight) - low) / (high - low) * 150, 2),
                "trend_y": round(190 - (average - low) / (high - low) * 150, 2),
                "weight": entry.weight,
                "date": entry.date,
                "average": round(average, 2),
            }
        )
    path = " ".join(
        f"{'M' if index == 0 else 'L'} {point['x']} {point['y']}"
        for index, point in enumerate(points)
    )
    trend = " ".join(
        f"{'M' if index == 0 else 'L'} {point['x']} {point['trend_y']}"
        for index, point in enumerate(points)
    )
    return {
        "points": points,
        "path": path,
        "trend_path": trend,
        "ticks": [
            {"y": 190 - index * 50, "value": round(low + (high - low) * index / 3, 1)}
            for index in range(4)
        ],
    }


def progress_summary(user: User, start: date, end: date) -> dict[str, Any]:
    records = daily_records(user, start, end)
    logged = [row for row in records if row.calories is not None]
    target, _ = DailyTarget.objects.get_or_create(user=user)
    weights = list(
        WeightEntry.objects.filter(
            user=user,
            date__range=(
                max(date.min + timedelta(days=6), start) - timedelta(days=6),
                end,
            ),
        ).order_by("date")
    )
    visible = [entry for entry in weights if entry.date >= start]
    latest = WeightEntry.objects.filter(user=user).first()
    goal = WeightGoal.objects.filter(user=user).first()
    averages = {
        name: round(sum(getattr(row, name) or 0 for row in logged) / len(logged), 1)
        if logged
        else None
        for name in NUTRIENTS
    }
    in_range = sum(
        target.calories_min <= (row.calories or 0) <= target.calories_max
        for row in logged
    )
    return {
        "records": records,
        "recent_days": [
            row
            for row in reversed(records)
            if row.calories is not None or row.weight is not None
        ][:14],
        "start": start,
        "end": end,
        "days": len(records),
        "logged_days": len(logged),
        "meal_count": Meal.objects.filter(user=user, date__range=(start, end)).count(),
        "averages": averages,
        "in_range": in_range,
        "in_range_percent": round(in_range / len(logged) * 100) if logged else None,
        "logging_percent": round(len(logged) / len(records) * 100),
        "latest_weight": latest,
        "weight_change": visible[-1].weight - visible[0].weight
        if len(visible) > 1
        else None,
        "weigh_ins": len(visible),
        "goal": goal,
        "goal_gap": abs(latest.weight - goal.weight)
        if latest and goal and goal.weight is not None
        else None,
        "intake_chart": _intake_chart(records, target),
        "weight_chart": _weight_chart(weights, start, end),
    }
