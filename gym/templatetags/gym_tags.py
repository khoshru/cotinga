from decimal import Decimal
from typing import Any

from django import template
from django.utils.translation import gettext

from gym.models import Exercise, WorkoutSet

register = template.Library()


@register.filter
def short_number(value: Any) -> str:
    if value is None:
        return "—"
    return (
        format(Decimal(str(value)), "f").rstrip("0").rstrip(".")
        if "." in str(value)
        else str(value)
    )


@register.filter
def clock(value: Any) -> str:
    seconds = max(0, int(value or 0))
    return (
        f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
        if seconds >= 3600
        else f"{seconds // 60:02}:{seconds % 60:02}"
    )


@register.filter
def set_result(entry: WorkoutSet, mode: str) -> str:
    if mode == Exercise.Mode.DURATION:
        return clock(entry.seconds)
    if mode == Exercise.Mode.CARDIO:
        return (
            f"{short_number(entry.distance)} {gettext('km')} · {clock(entry.seconds)}"
        )
    if mode == Exercise.Mode.REPS:
        return f"{entry.reps or '—'} {gettext('reps')}"
    label = (
        gettext("assistance kg")
        if mode == Exercise.Mode.ASSISTED
        else gettext("added kg")
        if mode == Exercise.Mode.BODYWEIGHT
        else gettext("kg")
    )
    return f"{short_number(entry.weight)} {label} × {entry.reps or '—'}"
