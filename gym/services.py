import math
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import (
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    prefetch_related_objects,
)
from django.utils import timezone
from django.utils.translation import gettext

from accounts.models import User
from tracker.models import WeightEntry

from .forms import mode_fields
from .models import (
    Exercise,
    Routine,
    RoutineExercise,
    Workout,
    WorkoutExercise,
    WorkoutSet,
)

MAX_EXERCISES = 40
MAX_SETS = 30


def exercises_for(user: User) -> QuerySet[Exercise]:
    return Exercise.objects.filter(Q(owner=user) | Q(owner__isnull=True))


def completed_sets(user: User) -> QuerySet[WorkoutSet]:
    return WorkoutSet.objects.filter(
        workout_exercise__workout__user=user,
        workout_exercise__workout__status=Workout.Status.FINISHED,
        workout_exercise__workout__archived_at__isnull=True,
        completed_at__isnull=False,
    ).exclude(kind=WorkoutSet.Kind.WARMUP)


def previous_exercises(
    user: User, ids: list[int], *, exclude: Workout | None = None
) -> dict[int, WorkoutExercise]:
    latest = WorkoutExercise.objects.filter(
        workout__user=user,
        workout__status=Workout.Status.FINISHED,
        workout__archived_at__isnull=True,
        exercise_id=OuterRef("exercise_id"),
        sets__completed_at__isnull=False,
        sets__kind__in=[
            WorkoutSet.Kind.WORKING,
            WorkoutSet.Kind.DROP,
            WorkoutSet.Kind.FAILURE,
        ],
    )
    if exclude:
        latest = latest.exclude(workout=exclude).filter(
            Q(workout__date__lt=exclude.date)
            | Q(workout__date=exclude.date, workout__started_at__lt=exclude.started_at)
            | Q(
                workout__date=exclude.date,
                workout__started_at=exclude.started_at,
                workout__pk__lt=exclude.pk,
            )
        )
    latest = latest.order_by("-workout__date", "-workout__started_at", "-pk")
    rows = WorkoutExercise.objects.filter(
        exercise_id__in=ids, pk=Subquery(latest.values("pk")[:1])
    ).prefetch_related(
        Prefetch(
            "sets",
            queryset=WorkoutSet.objects.filter(completed_at__isnull=False).exclude(
                kind=WorkoutSet.Kind.WARMUP
            ),
        )
    )
    return {row.exercise_id: row for row in rows}


def add_exercise(
    workout: Workout, exercise: Exercise, target: RoutineExercise | None = None
) -> WorkoutExercise:
    if workout.exercises.count() >= MAX_EXERCISES:
        raise ValueError(gettext("A workout can contain up to 40 exercises."))
    row = WorkoutExercise.objects.create(
        workout=workout,
        exercise=exercise,
        name=exercise.name,
        name_fa=exercise.name_fa,
        name_ka=exercise.name_ka,
        muscle=exercise.muscle,
        equipment=exercise.equipment,
        mode=exercise.mode,
        position=workout.exercises.count(),
        rest_seconds=target.rest_seconds if target else 90,
        group=target.group if target else "",
        notes=target.notes if target else "",
    )
    previous = previous_exercises(workout.user, [exercise.pk], exclude=workout).get(
        exercise.pk
    )
    if previous and previous.mode != exercise.mode:
        previous = None
    last_sets = list(previous.sets.all()) if previous else []
    count = target.sets if target else (min(len(last_sets), 10) or 3)
    fields = ("weight", "reps", "seconds", "distance")
    for index in range(count):
        values = {field: getattr(target, field) if target else None for field in fields}
        if index < len(last_sets):
            for field in fields:
                if field != "weight" or target is None or target.weight is None:
                    values[field] = getattr(last_sets[index], field)
        if values["weight"] is None and exercise.mode == Exercise.Mode.BODYWEIGHT:
            values["weight"] = Decimal(0)
        values = {
            field: value if field in mode_fields(exercise.mode) else None
            for field, value in values.items()
        }
        WorkoutSet.objects.create(workout_exercise=row, position=index, **values)
    return row


def start_workout(user: User, data: dict[str, Any]) -> Workout:
    active = Workout.objects.filter(
        user=user, status=Workout.Status.ACTIVE, archived_at__isnull=True
    ).first()
    if active:
        return active
    routine: Routine | None = data.get("routine")
    latest = WeightEntry.objects.filter(user=user, date__lte=data["date"]).first()
    workout = Workout.objects.create(
        user=user,
        routine=routine,
        routine_key=routine.pk if routine else None,
        name=data.get("name") or (routine.name if routine else gettext("My workout")),
        date=data["date"],
        bodyweight=latest.weight if latest else None,
        bodyweight_date=latest.date if latest else None,
    )
    if routine:
        for target in routine.exercises.select_related("exercise"):
            add_exercise(workout, target.exercise, target)
    return workout


def workout_totals(workout: Workout) -> dict[str, Any]:
    prefetch_related_objects([workout], "exercises__sets")
    volume = Decimal(0)
    completed = working = total = 0
    distance = Decimal(0)
    for row in workout.exercises.all():
        for entry in row.sets.all():
            total += 1
            if entry.completed_at is None:
                continue
            completed += 1
            if entry.kind == WorkoutSet.Kind.WARMUP:
                continue
            working += 1
            if row.mode == Exercise.Mode.WEIGHT:
                volume += (entry.weight or Decimal(0)) * (entry.reps or 0)
            if row.mode == Exercise.Mode.CARDIO:
                distance += entry.distance or Decimal(0)
    return {
        "volume": volume,
        "completed": completed,
        "working": working,
        "total": total,
        "remaining": total - completed,
        "distance": distance,
    }


def workout_state(workout: Workout) -> dict[str, Any]:
    now = timezone.now()
    totals = workout_totals(workout)
    return {
        "version": workout.version,
        "active": workout.status == Workout.Status.ACTIVE,
        "paused": workout.paused_at is not None,
        "elapsed": workout.elapsed_seconds,
        "server_time": now.isoformat(),
        "rest_ends_at": workout.rest_ends_at.isoformat()
        if workout.rest_ends_at
        else None,
        "rest_remaining": workout.rest_remaining,
        "bodyweight": str(workout.bodyweight)
        if workout.bodyweight is not None
        else None,
        "totals": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in totals.items()
        },
    }


def rest_seconds(workout: Workout) -> int:
    if workout.rest_ends_at:
        return max(
            0,
            min(
                1800, math.ceil((workout.rest_ends_at - timezone.now()).total_seconds())
            ),
        )
    return workout.rest_remaining or 0


def timer_action(workout: Workout, command: str, seconds: int) -> None:
    now = timezone.now()
    remaining = rest_seconds(workout)
    if command == "start" and seconds < 1:
        raise ValueError(gettext("Rest time must be between 1 and 1800 seconds."))
    if command == "pause":
        workout.rest_remaining = remaining
        workout.rest_ends_at = None
    elif (
        command == "extend"
        and workout.rest_ends_at is None
        and workout.rest_remaining is not None
    ):
        workout.rest_remaining = max(0, min(remaining + seconds, 1800))
    elif command in ("start", "resume", "extend"):
        amount = (
            seconds
            if command == "start"
            else (remaining if command == "resume" else remaining + seconds)
        )
        amount = max(0, min(amount, 1800))
        workout.rest_ends_at = now + timedelta(seconds=amount) if amount else None
        workout.rest_remaining = None
    elif command == "cancel":
        workout.rest_ends_at = None
        workout.rest_remaining = None
    else:
        raise ValueError(gettext("Choose a valid timer action."))


def performance_key(row: WorkoutExercise, entry: WorkoutSet) -> tuple[Decimal, int]:
    if row.mode in (Exercise.Mode.WEIGHT, Exercise.Mode.BODYWEIGHT):
        return (entry.weight or Decimal(0), entry.reps or 0)
    if row.mode == Exercise.Mode.ASSISTED:
        return (-(entry.weight or Decimal(0)), entry.reps or 0)
    if row.mode == Exercise.Mode.REPS:
        return (Decimal(entry.reps or 0), 0)
    if row.mode == Exercise.Mode.DURATION:
        return (Decimal(entry.seconds or 0), 0)
    return (entry.distance or Decimal(0), 0)


def exercise_sessions(user: User, exercise: Exercise) -> list[dict[str, Any]]:
    rows = (
        WorkoutExercise.objects.filter(
            workout__user=user,
            workout__status=Workout.Status.FINISHED,
            workout__archived_at__isnull=True,
            exercise=exercise,
        )
        .select_related("workout")
        .prefetch_related(
            Prefetch(
                "sets",
                queryset=WorkoutSet.objects.filter(completed_at__isnull=False).exclude(
                    kind=WorkoutSet.Kind.WARMUP
                ),
            )
        )
        .order_by("workout__date", "workout__started_at", "position")
    )
    # A repeated exercise in a workout is one session, not another progress point.
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        sets = list(row.sets.all())
        if not sets:
            continue
        key = str(row.workout_id)
        if key not in grouped:
            grouped[key] = {"workout": row.workout, "row": row, "sets": []}
        grouped[key]["sets"].extend(sets)
    sessions = list(grouped.values())
    for session in sessions:
        session["best"] = best_set(session["row"], session["sets"])
        session["value"] = float(performance_key(session["row"], session["best"])[0])
    return sessions


def line_chart(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    visible = sessions[-30:]
    if not visible:
        return {}
    values = [entry["value"] for entry in visible]
    low, high = min(values), max(values)
    spread = max(high - low, abs(high) * 0.1, 1)
    low -= spread * 0.15
    high += spread * 0.15
    dots = []
    for index, entry in enumerate(visible):
        x = 30 + (index / (len(visible) - 1) * 640 if len(visible) > 1 else 320)
        y = 180 - ((entry["value"] - low) / (high - low) * 150)
        dots.append(
            {
                "x": round(x, 2),
                "y": round(y, 2),
                "value": abs(entry["value"]),
                "day": entry["workout"].date,
            }
        )
    return {
        "dots": dots,
        "points": " ".join(f"{dot['x']},{dot['y']}" for dot in dots),
        "first": visible[0]["workout"].date,
        "last": visible[-1]["workout"].date,
    }


def training_summary(user: User, days: int = 28) -> dict[str, Any]:
    today = timezone.localdate()
    since = today - timedelta(days=days - 1)
    workouts = list(
        Workout.objects.filter(
            user=user,
            status=Workout.Status.FINISHED,
            archived_at__isnull=True,
            date__gte=since,
            date__lte=today,
        ).prefetch_related("exercises__sets")
    )
    volume = Decimal(0)
    working = duration = 0
    muscles: dict[str, int] = defaultdict(int)
    weeks: dict[date, int] = defaultdict(int)
    for workout in workouts:
        totals = workout_totals(workout)
        volume += totals["volume"]
        working += totals["working"]
        duration += workout.elapsed_seconds
        weeks[workout.date - timedelta(days=workout.date.weekday())] += 1
        for row in workout.exercises.all():
            muscles[row.muscle] += sum(
                entry.completed_at is not None and entry.kind != WorkoutSet.Kind.WARMUP
                for entry in row.sets.all()
            )
    start = since - timedelta(days=since.weekday())
    bars = []
    while start <= today:
        bars.append(
            {
                "day": start,
                "count": weeks[start],
                "height": max(
                    3, weeks[start] / max(max(weeks.values(), default=1), 1) * 100
                ),
            }
        )
        start += timedelta(days=7)
    muscle_rows = [
        {
            "name": str(dict(Exercise.Muscle.choices).get(key, key)),
            "count": count,
            "width": count / max(max(muscles.values(), default=1), 1) * 100,
        }
        for key, count in sorted(muscles.items(), key=lambda item: -item[1])
        if count
    ]
    return {
        "count": len(workouts),
        "volume": volume,
        "sets": working,
        "minutes": duration // 60,
        "bars": bars,
        "muscles": muscle_rows,
    }


def best_set(row: WorkoutExercise, entries: list[WorkoutSet]) -> WorkoutSet:
    def rank(entry: WorkoutSet) -> tuple[Decimal, int]:
        return performance_key(row, entry)

    return max(entries, key=rank)
