"""Small, rest-friendly milestones calculated from a user's saved workouts."""

from collections import Counter
from datetime import timedelta
from typing import Any

from django.db.models import Count
from django.utils import timezone

from accounts.models import User

from .models import Workout


def training_momentum(user: User) -> dict[str, Any]:
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    days = Counter(
        dict(
            Workout.objects.filter(
                user=user,
                status=Workout.Status.FINISHED,
                archived_at__isnull=True,
                date__lte=today,
            )
            .order_by()
            .values("date")
            .annotate(count=Count("pk"))
            .values_list("date", "count")
        )
    )
    weeks = sorted({day - timedelta(days=day.weekday()) for day in days})
    longest = run = 0
    for index, week in enumerate(weeks):
        run = run + 1 if index and week - weeks[index - 1] == timedelta(days=7) else 1
        longest = max(longest, run)
    # The current week stays open: a rest day on Monday never erases last week.
    streak = run if weeks and weeks[-1] >= monday - timedelta(days=7) else 0
    total = sum(days.values())
    milestones = (1, 5, 10, 25, 50, 100)
    next_milestone = next(
        (number for number in milestones if number > total),
        (total // 50 + 1) * 50,
    )
    week_days = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        week_days.append(
            {
                "date": day,
                "count": days[day],
                "today": day == today,
                "future": day > today,
            }
        )
    return {
        "days": week_days,
        "streak": streak,
        "longest": longest,
        "total": total,
        "next": next_milestone,
        "remaining": next_milestone - total,
        "percent": total / next_milestone * 100,
        "reached": total in milestones or (total > 100 and total % 50 == 0),
    }
