from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from typing import Any

import pytest
from django.apps import apps
from django.db import connection
from django.test import Client
from django.utils import timezone, translation

from accounts.models import User
from gym.models import Exercise, Routine, Workout, WorkoutExercise, WorkoutSet
from gym.records import record_book
from gym.services import previous_exercises, start_workout
from gym.templatetags.gym_tags import record_count

pytestmark = pytest.mark.django_db


@pytest.fixture
def movement(owner: User) -> Exercise:
    return Exercise.objects.create(
        owner=owner,
        name="My bench press",
        muscle="chest",
        equipment="barbell",
        mode="weight_reps",
    )


def logged(
    user: User,
    exercise: Exercise,
    *,
    days: int = 0,
    weight: int = 50,
    reps: int = 8,
    status: str = "finished",
    routine: Routine | None = None,
    seconds: int | None = None,
    distance: str | None = None,
) -> tuple[Workout, WorkoutExercise, WorkoutSet]:
    moment = timezone.now() - timedelta(days=days)
    workout = Workout.objects.create(
        user=user,
        name="Upper body",
        date=moment.date(),
        started_at=moment,
        finished_at=moment + timedelta(minutes=45) if status == "finished" else None,
        status=status,
        routine=routine,
        routine_key=routine.pk if routine else None,
        bodyweight=80,
    )
    row = WorkoutExercise.objects.create(
        workout=workout,
        exercise=exercise,
        name=exercise.name,
        name_fa="پرس سینه",
        name_ka="წოლჭიმი",
        muscle=exercise.muscle,
        equipment=exercise.equipment,
        mode=exercise.mode,
    )
    entry = WorkoutSet.objects.create(
        workout_exercise=row,
        weight=Decimal(weight)
        if exercise.mode in ("weight_reps", "bodyweight_reps", "assisted_reps")
        else None,
        reps=reps
        if exercise.mode in ("weight_reps", "bodyweight_reps", "assisted_reps", "reps")
        else None,
        seconds=seconds,
        distance=Decimal(distance) if distance else None,
        completed_at=moment,
    )
    return workout, row, entry


def metrics(book: Any, workout: Workout) -> dict[str, Any]:
    return {record.current.metric: record for record in book.for_workout(workout)}


def test_first_session_establishes_benchmarks_without_fake_records(
    owner: User, movement: Exercise
) -> None:
    workout, row, _ = logged(owner, movement, weight=40, reps=5)
    WorkoutSet.objects.create(
        workout_exercise=row,
        position=1,
        weight=50,
        reps=10,
        completed_at=timezone.now(),
    )
    book = record_book(owner, workout)
    assert not book.records and book.first_exercises == 1
    assert (
        next(item for item in book.best.values() if item.metric == "set_volume").value
        == 500
    )


def test_weight_reps_set_exercise_workout_and_routine_records(
    owner: User, movement: Exercise
) -> None:
    routine = Routine.objects.create(user=owner, name="Upper body")
    old, _, _ = logged(owner, movement, days=3, routine=routine)
    new, _, entry = logged(owner, movement, reps=10, routine=routine)
    book = record_book(owner, new)
    records = metrics(book, new)
    assert set(records) == {
        "reps_at_load",
        "set_volume",
        "exercise_volume",
        "workout_volume",
        "routine_volume",
    }
    assert records["reps_at_load"].previous.value == 8
    assert records["reps_at_load"].current.value == 10
    assert records["routine_volume"].previous.workout.pk == old.pk
    assert records["routine_volume"].current.value == 500
    assert records["routine_volume"].improvement == "100 kg × reps"
    assert len(book.feedback[entry.pk]["records"]) == 2
    assert book.feedback[entry.pk]["best"].metric == "reps_at_load"
    assert book.feedback[entry.pk]["best"].value == 8
    assert "50 kg" in records["reps_at_load"].current.label


def test_heavier_load_does_not_invent_a_repetition_record_at_that_load(
    owner: User, movement: Exercise
) -> None:
    logged(owner, movement, days=1, weight=50, reps=10)
    new, _, _ = logged(owner, movement, weight=60, reps=5)
    records = metrics(record_book(owner, new), new)
    assert set(records) == {"weight"}
    assert records["weight"].previous.result == "50 kg × 10"
    assert records["weight"].current.result == "60 kg × 5"
    assert records["weight"].improvement == "10 kg"


def test_ties_and_warmups_unfinished_active_and_other_users_do_not_count(
    owner: User, movement: Exercise
) -> None:
    old, row, _ = logged(owner, movement, days=2)
    WorkoutSet.objects.create(
        workout_exercise=row,
        position=1,
        kind="warmup",
        weight=200,
        reps=20,
        completed_at=timezone.now(),
    )
    WorkoutSet.objects.create(workout_exercise=row, position=2, weight=250, reps=20)
    new, _, _ = logged(owner, movement, days=1)
    active, _, _ = logged(owner, movement, weight=500, status="active")
    other = User.objects.create_user("other-records@example.com", "test-password")
    logged(other, movement, weight=1000)
    book = record_book(owner)
    assert not book.records
    assert (
        next(item for item in book.best.values() if item.metric == "weight").workout.pk
        == old.pk
    )
    assert record_book(owner, new).for_workout(new) == []
    assert record_book(owner, active).for_workout(active)
    active.status = "cancelled"
    active.save()
    assert not record_book(owner).records


@pytest.mark.parametrize(
    "mode,old,new,expected",
    [
        ("assisted_reps", {"weight": 30}, {"weight": 20}, "assistance"),
        (
            "bodyweight_reps",
            {"weight": 0, "reps": 5},
            {"weight": 0, "reps": 8},
            "reps_at_load",
        ),
        ("reps", {"reps": 8}, {"reps": 12}, "reps"),
        ("duration", {"seconds": 30}, {"seconds": 45}, "duration"),
        (
            "distance_time",
            {"distance": "1.50", "seconds": 600},
            {"distance": "1.50", "seconds": 550},
            "time_at_distance",
        ),
    ],
)
def test_records_match_tracking_mode(
    owner: User,
    movement: Exercise,
    mode: str,
    old: dict[str, Any],
    new: dict[str, Any],
    expected: str,
) -> None:
    movement.mode = mode
    movement.save()
    logged(owner, movement, days=1, **old)
    workout, _, _ = logged(owner, movement, **new)
    records = metrics(record_book(owner, workout), workout)
    assert expected in records and not any("volume" in name for name in records)
    assert records[expected].current.label and records[expected].current.result
    assert records[expected].improvement
    if mode == "assisted_reps":
        assert (
            records[expected].current.lower and records[expected].improvement == "10 kg"
        )


def test_cardio_never_compares_times_for_different_distances(
    owner: User, movement: Exercise
) -> None:
    movement.mode = "distance_time"
    movement.save()
    logged(owner, movement, days=1, distance="1", seconds=100)
    workout, _, _ = logged(owner, movement, distance="2", seconds=90)
    assert "time_at_distance" not in metrics(record_book(owner, workout), workout)
    assert "distance" in metrics(record_book(owner, workout), workout)


def test_repeated_exercises_aggregate_once_and_ignore_bodyweight_volume(
    owner: User, movement: Exercise
) -> None:
    logged(owner, movement, days=1)
    workout, row, _ = logged(owner, movement)
    repeated = WorkoutExercise.objects.create(
        workout=workout,
        exercise=movement,
        name=movement.name,
        muscle=movement.muscle,
        equipment=movement.equipment,
        mode=movement.mode,
        position=1,
    )
    WorkoutSet.objects.create(
        workout_exercise=repeated, weight=50, reps=8, completed_at=timezone.now()
    )
    body = Exercise.objects.create(
        owner=owner,
        name="Pull up",
        mode="bodyweight_reps",
        muscle="back",
        equipment="bodyweight",
    )
    body_row = WorkoutExercise.objects.create(
        workout=workout,
        exercise=body,
        name=body.name,
        mode=body.mode,
        muscle=body.muscle,
        equipment=body.equipment,
        position=2,
    )
    WorkoutSet.objects.create(
        workout_exercise=body_row, weight=10, reps=20, completed_at=timezone.now()
    )
    records = metrics(record_book(owner, workout), workout)
    assert records["exercise_volume"].current.row.exercise_id == row.exercise_id
    assert records["exercise_volume"].current.value == 800
    assert records["workout_volume"].current.value == 800


def test_corrections_archiving_and_restoring_rebuild_previous_records(
    owner: User, movement: Exercise
) -> None:
    first, _, _ = logged(owner, movement, days=3, weight=50)
    second, _, entry = logged(owner, movement, days=2, weight=60)
    third, _, _ = logged(owner, movement, days=1, weight=55)
    assert "weight" not in metrics(record_book(owner, third), third)
    entry.weight = Decimal(40)
    entry.save()
    assert (
        metrics(record_book(owner, third), third)["weight"].previous.workout.pk
        == first.pk
    )
    first.archived_at = timezone.now()
    first.save()
    assert (
        metrics(record_book(owner, third), third)["weight"].previous.workout.pk
        == second.pk
    )
    first.archived_at = None
    first.save()
    assert (
        metrics(record_book(owner, third), third)["weight"].previous.workout.pk
        == first.pk
    )
    assert (
        len(
            [
                record
                for record in record_book(owner).for_workout(third)
                if record.current.metric == "weight"
            ]
        )
        == 1
    )


def test_backdated_records_follow_workout_dates_not_creation_dates(
    owner: User, movement: Exercise
) -> None:
    first, _, _ = logged(owner, movement, days=3, weight=20)
    newest, _, _ = logged(owner, movement, days=1, weight=40)
    middle, _, _ = logged(owner, movement, days=2, weight=30)
    book = record_book(owner)
    assert metrics(book, middle)["weight"].previous.workout.pk == first.pk
    assert metrics(book, newest)["weight"].previous.workout.pk == middle.pk
    assert (
        metrics(record_book(owner, middle), middle)["weight"].previous.workout.pk
        == first.pk
    )


def test_tracking_mode_changes_establish_separate_benchmarks(
    owner: User, movement: Exercise
) -> None:
    logged(owner, movement, days=1, weight=60)
    movement.mode = "reps"
    movement.save()
    workout, _, _ = logged(owner, movement, reps=50)
    assert not record_book(owner, workout).for_workout(workout)


def test_routine_identity_is_saved_and_survives_deletion(
    owner: User, movement: Exercise
) -> None:
    routine = Routine.objects.create(user=owner, name="Upper body")
    old, _, _ = logged(owner, movement, days=1, routine=routine)
    new, _, _ = logged(owner, movement, reps=10, routine=routine)
    key = routine.pk
    routine.delete()
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.routine_id is None and old.routine_key == new.routine_key == key
    assert "routine_volume" in metrics(record_book(owner, new), new)
    restored = Routine.objects.create(user=owner, name="Another routine")
    assert (
        start_workout(
            owner, {"date": timezone.localdate(), "routine": restored}
        ).routine_key
        == restored.pk
    )


def test_migration_backfills_existing_routine_keys(
    owner: User, movement: Exercise
) -> None:
    routine = Routine.objects.create(user=owner, name="Upper body")
    workout, _, _ = logged(owner, movement, routine=routine)
    Workout.objects.filter(pk=workout.pk).update(routine_key=None)
    migration = import_module("gym.migrations.0005_routine_record_identity")
    migration.preserve_routine_keys(apps, connection.schema_editor())
    workout.refresh_from_db()
    assert workout.routine_key == routine.pk


def test_record_queries_do_not_grow_with_workout_count(
    owner: User, movement: Exercise, django_assert_num_queries: Any
) -> None:
    for day in range(20):
        logged(owner, movement, days=day, reps=day + 1)
    with django_assert_num_queries(3):
        assert record_book(owner).best


def test_live_set_feedback_and_finished_history(
    client: Client, owner: User, movement: Exercise
) -> None:
    logged(owner, movement, days=1)
    workout, _, entry = logged(owner, movement, status="active", reps=10)
    entry.completed_at = None
    entry.save()
    client.force_login(owner)
    path = f"/gym/workouts/{workout.pk}/action/"
    data = {
        "action": "set",
        "version": 0,
        "set": entry.pk,
        f"set-{entry.pk}-weight": "50",
        f"set-{entry.pk}-reps": "10",
        f"set-{entry.pk}-kind": "working",
        f"set-{entry.pk}-completed": "on",
    }
    response = client.post(path, data, HTTP_ACCEPT="application/json")
    assert response.status_code == 200
    feedback = response.json()["records"]
    assert (
        "personal records" in feedback["summary"]
        and "Provisional" in feedback["summary"]
    )
    assert "Most reps at 50 kg" in feedback["sets"][str(entry.pk)]
    assert "Previous" in feedback["sets"][str(entry.pk)]
    assert client.get("/gym/records/").context["record_count"] == 0
    response = client.post(
        path, {"action": "finish", "version": 1}, HTTP_ACCEPT="application/json"
    )
    assert response.status_code == 200
    page = client.get(f"/gym/workouts/{workout.pk}/")
    assert page.context["workout_records"] and b"LOOK WHAT YOU JUST DID" in page.content
    assert client.get("/gym/records/").context["record_count"] > 0
    assert b"personal records" in client.get("/gym/history/").content


def test_unchecking_a_set_removes_provisional_records(
    client: Client, owner: User, movement: Exercise
) -> None:
    logged(owner, movement, days=1)
    workout, _, entry = logged(owner, movement, status="active", reps=10)
    client.force_login(owner)
    response = client.post(
        f"/gym/workouts/{workout.pk}/action/",
        {
            "action": "set",
            "version": 0,
            "set": entry.pk,
            f"set-{entry.pk}-weight": "50",
            f"set-{entry.pk}-reps": "10",
            f"set-{entry.pk}-kind": "working",
        },
        HTTP_ACCEPT="application/json",
    )
    assert response.status_code == 200
    assert "personal record" not in response.json()["records"]["summary"]
    assert (
        'class="gym-set-records"'
        not in response.json()["records"]["sets"][str(entry.pk)]
    )


def test_records_are_private_filterable_and_escaped(
    client: Client, owner: User, movement: Exercise
) -> None:
    assert client.get("/gym/records/").status_code == 302
    logged(owner, movement, days=1)
    new, _, _ = logged(owner, movement, reps=10)
    new.name = "<img src=x onerror=alert(1)>"
    new.save()
    stranger = User.objects.create_user("private-records@example.com", "test-password")
    private = Exercise.objects.create(
        owner=stranger,
        name="SECRET RECORD",
        mode="reps",
        muscle="core",
        equipment="bodyweight",
    )
    logged(stranger, private, days=1, reps=5)
    logged(stranger, private, reps=10)
    client.force_login(owner)
    response = client.get("/gym/records/")
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert (
        b"SECRET RECORD" not in response.content
        and b"<img src=x" not in response.content
    )
    assert b"&lt;img src=x" in response.content
    assert (
        client.get("/gym/records/", {"exercise": private.pk}).context["record_count"]
        == 0
    )
    filtered = client.get("/gym/records/", {"exercise": str(movement.pk), "q": "reps"})
    assert filtered.context["record_count"] == 1 and filtered.context["load_bests"]
    assert record_count({}, new.pk) == 0 and record_count(None, new.pk) == 0


@pytest.mark.parametrize("language", ["en", "fa", "ka"])
def test_record_pages_and_workout_cards_render_in_all_languages(
    client: Client, owner: User, movement: Exercise, language: str
) -> None:
    logged(owner, movement, days=1)
    workout, _, _ = logged(owner, movement, reps=10)
    client.force_login(owner)
    client.cookies["django_language"] = language
    with translation.override(language):
        for path in (
            "/gym/records/",
            f"/gym/workouts/{workout.pk}/",
            f"/gym/exercises/{movement.pk}/",
        ):
            assert client.get(path).status_code == 200


def test_previous_session_never_comes_from_a_later_workout_and_warmups_do_not_shift_sets(
    client: Client, owner: User, movement: Exercise
) -> None:
    first, _, first_set = logged(owner, movement, days=3)
    logged(owner, movement, days=1, weight=80)
    middle, row, _ = logged(owner, movement, days=2)
    WorkoutSet.objects.create(
        workout_exercise=row, position=0, kind="warmup", weight=10, reps=5
    )
    WorkoutSet.objects.filter(workout_exercise=row, kind="working").update(position=1)
    assert (
        previous_exercises(owner, [movement.pk], exclude=middle)[movement.pk].workout_id
        == first.pk
    )
    client.force_login(owner)
    cards = client.get(f"/gym/workouts/{middle.pk}/").context["cards"]
    assert cards[0]["sets"][0]["previous"] is None
    assert cards[0]["sets"][1]["previous"].pk == first_set.pk


def test_record_book_refuses_a_workout_from_another_user(
    owner: User, movement: Exercise
) -> None:
    other = User.objects.create_user("foreign-records@example.com", "test-password")
    workout, _, _ = logged(other, movement)
    with pytest.raises(ValueError, match="does not belong"):
        record_book(owner, workout)
