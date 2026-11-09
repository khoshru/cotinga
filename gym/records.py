"""Personal records derived from saved sets, including historical corrections."""

from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any

from django.db.models import Prefetch
from django.utils.translation import gettext

from accounts.models import User

from .models import Exercise, Workout, WorkoutExercise, WorkoutSet
from .templatetags.gym_tags import clock, set_result, short_number

Key = tuple[str, str, str, str]


@dataclass(frozen=True)
class Benchmark:
    key: Key
    metric: str
    value: Decimal
    workout: Workout
    row: WorkoutExercise | None = None
    entry: WorkoutSet | None = None
    qualifier: Decimal | None = None
    lower: bool = False

    @property
    def exercise_id(self) -> int | None:
        return self.row.exercise_id if self.row else None

    @property
    def subject(self) -> str:
        return self.row.display_name if self.row else self.workout.name

    @property
    def label(self) -> str:
        labels = {
            "weight": gettext("Heaviest weight"),
            "added_weight": gettext("Heaviest added weight"),
            "assistance": gettext("Least assistance"),
            "set_volume": gettext("Most volume in one set"),
            "exercise_volume": gettext("Exercise volume in one workout"),
            "workout_volume": gettext("Total workout volume"),
            "routine_volume": gettext("Routine volume record"),
            "reps": gettext("Most reps in one set"),
            "total_reps": gettext("Exercise reps in one workout"),
            "duration": gettext("Longest timed set"),
            "distance": gettext("Longest distance in one set"),
            "total_distance": gettext("Exercise distance in one workout"),
        }
        if self.metric == "reps_at_load":
            unit = (
                gettext("assistance kg")
                if self.row and self.row.mode == Exercise.Mode.ASSISTED
                else gettext("added kg")
                if self.row and self.row.mode == Exercise.Mode.BODYWEIGHT
                else gettext("kg")
            )
            return gettext("Most reps at %(load)s %(unit)s") % {
                "load": short_number(self.qualifier),
                "unit": unit,
            }
        if self.metric == "time_at_distance":
            return gettext("Fastest %(distance)s km") % {
                "distance": short_number(self.qualifier)
            }
        return labels[self.metric]

    @property
    def result(self) -> str:
        if (
            self.entry
            and self.row
            and self.metric in ("weight", "added_weight", "assistance")
        ):
            return set_result(self.entry, self.row.mode)
        return self.format_value(self.value)

    def format_value(self, value: Decimal) -> str:
        if "volume" in self.metric:
            return f"{short_number(value)} {gettext('kg × reps')}"
        if self.metric in ("duration", "time_at_distance"):
            return clock(value)
        if self.metric in ("distance", "total_distance"):
            return f"{short_number(value)} {gettext('km')}"
        return f"{short_number(value)} {gettext('reps')}"

    def improves(self, previous: Benchmark) -> bool:
        return (
            self.value < previous.value if self.lower else self.value > previous.value
        )


@dataclass(frozen=True)
class Record:
    current: Benchmark
    previous: Benchmark

    @property
    def improvement(self) -> str:
        difference = abs(self.current.value - self.previous.value)
        if self.current.metric in ("weight", "added_weight", "assistance"):
            return f"{short_number(difference)} {gettext('kg')}"
        return self.current.format_value(difference)


@dataclass
class RecordBook:
    best: dict[Key, Benchmark] = field(default_factory=dict)
    records: list[Record] = field(default_factory=list)
    feedback: dict[int, dict[str, Any]] = field(default_factory=dict)
    first_exercises: int = 0

    def for_workout(self, workout: Workout) -> list[Record]:
        return [
            record for record in self.records if record.current.workout.pk == workout.pk
        ]

    def consider(
        self, candidate: Benchmark, *, established: dict[Key, Benchmark]
    ) -> None:
        previous = self.best.get(candidate.key)
        if previous is not None and not candidate.improves(previous):
            return
        # A first encounter establishes a benchmark for the whole workout.
        if candidate.key in established and previous is not None:
            record = Record(candidate, previous)
            self.records.append(record)
            if candidate.entry and candidate.entry.pk in self.feedback:
                self.feedback[candidate.entry.pk]["records"].append(record)
        self.best[candidate.key] = candidate


def set_benchmarks(row: WorkoutExercise, entry: WorkoutSet) -> list[Benchmark]:
    """Compare like tracking modes and like loads; never estimate bodyweight load."""
    result = []

    def add(
        metric: str, value: Any, qualifier: Decimal | None = None, lower: bool = False
    ) -> None:
        if value is None:
            return
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            return
        result.append(
            Benchmark(
                (
                    f"exercise:{row.exercise_id}",
                    row.mode,
                    metric,
                    f"{qualifier:.2f}" if qualifier is not None else "",
                ),
                metric,
                number,
                row.workout,
                row,
                entry,
                qualifier,
                lower,
            )
        )

    if row.mode in (
        Exercise.Mode.WEIGHT,
        Exercise.Mode.BODYWEIGHT,
        Exercise.Mode.ASSISTED,
    ):
        if entry.weight is not None and entry.reps and entry.weight >= 0:
            metric = (
                "weight"
                if row.mode == Exercise.Mode.WEIGHT
                else "added_weight"
                if row.mode == Exercise.Mode.BODYWEIGHT
                else "assistance"
            )
            add(metric, entry.weight, lower=row.mode == Exercise.Mode.ASSISTED)
            add("reps_at_load", entry.reps, entry.weight)
            if row.mode == Exercise.Mode.WEIGHT and entry.weight > 0:
                add("set_volume", entry.weight * entry.reps)
    elif row.mode == Exercise.Mode.REPS and entry.reps:
        add("reps", entry.reps)
    elif row.mode == Exercise.Mode.DURATION and entry.seconds:
        add("duration", entry.seconds)
    elif (
        row.mode == Exercise.Mode.CARDIO
        and entry.distance
        and entry.distance > 0
        and entry.seconds
        and entry.seconds > 0
    ):
        add("distance", entry.distance)
        add("time_at_distance", entry.seconds, entry.distance, lower=True)
    return result


def _order(workout: Workout) -> tuple[Any, ...]:
    return workout.date, workout.started_at, str(workout.pk)


def record_book(user: User, current: Workout | None = None) -> RecordBook:
    if current is not None and current.user_id != user.pk:
        raise ValueError("Workout does not belong to this user.")
    # Three queries, independent of workout count. Completed data remains the
    # source of truth: edits, backdated sessions and archives need no stale cache
    # or duplicated achievement rows to repair.
    history = list(
        Workout.objects.filter(
            user=user, status=Workout.Status.FINISHED, archived_at__isnull=True
        )
        .prefetch_related(
            Prefetch(
                "exercises", queryset=WorkoutExercise.objects.prefetch_related("sets")
            )
        )
        .order_by("date", "started_at", "pk")
    )
    if current is not None:
        history = [
            item
            for item in history
            if item.pk != current.pk and _order(item) < _order(current)
        ]
        if current.archived_at is None and current.user_id == user.pk:
            # Reload its saved sets after an autosave; never use a stale prefetch.
            current = Workout.objects.prefetch_related("exercises__sets").get(
                pk=current.pk, user=user
            )
            history.append(current)
    book = RecordBook()
    for workout in history:
        before = book.best.copy()
        eligible_scopes = {key[:2] for key in before}
        session_groups: dict[tuple[int, str], list[WorkoutSet]] = {}
        session_rows: dict[tuple[int, str], WorkoutExercise] = {}
        total_volume = Decimal(0)

        consider = partial(book.consider, established=before)

        for row in workout.exercises.all():
            group = (row.exercise_id, row.mode)
            session_rows[group] = row
            entries = session_groups.setdefault(group, [])
            for entry in row.sets.all():
                candidates = set_benchmarks(row, entry)
                if current is not None and workout.pk == current.pk:
                    priority = sorted(
                        candidates,
                        key=lambda candidate: candidate.metric != "reps_at_load",
                    )
                    best_before = next(
                        (before[item.key] for item in priority if item.key in before),
                        None,
                    )
                    book.feedback[entry.pk] = {"best": best_before, "records": []}
                if (
                    entry.completed_at is None
                    or entry.kind == WorkoutSet.Kind.WARMUP
                    or not candidates
                ):
                    continue
                entries.append(entry)
                for candidate in candidates:
                    consider(candidate)
        first = 0
        for group, entries in session_groups.items():
            if not entries:
                continue
            row = session_rows[group]
            scope = f"exercise:{row.exercise_id}"
            if (scope, row.mode) not in eligible_scopes:
                first += 1
            if row.mode == Exercise.Mode.WEIGHT:
                volume = sum(
                    (
                        (entry.weight or Decimal(0)) * (entry.reps or 0)
                        for entry in entries
                    ),
                    Decimal(0),
                )
                total_volume += volume
                metric, value = "exercise_volume", volume
            elif row.mode in (
                Exercise.Mode.REPS,
                Exercise.Mode.BODYWEIGHT,
                Exercise.Mode.ASSISTED,
            ):
                metric, value = (
                    "total_reps",
                    Decimal(sum(entry.reps or 0 for entry in entries)),
                )
            elif row.mode == Exercise.Mode.CARDIO:
                metric, value = (
                    "total_distance",
                    sum(
                        (entry.distance or Decimal(0) for entry in entries), Decimal(0)
                    ),
                )
            else:
                continue
            if value > 0:
                consider(
                    Benchmark(
                        (scope, row.mode, metric, ""), metric, value, workout, row
                    )
                )
        if total_volume > 0:
            consider(
                Benchmark(
                    ("workout", "", "workout_volume", ""),
                    "workout_volume",
                    total_volume,
                    workout,
                )
            )
            routine_key = workout.routine_key or workout.routine_id
            if routine_key:
                consider(
                    Benchmark(
                        (f"routine:{routine_key}", "", "routine_volume", ""),
                        "routine_volume",
                        total_volume,
                        workout,
                    )
                )
        if current is not None and workout.pk == current.pk:
            book.first_exercises = first
    return book
