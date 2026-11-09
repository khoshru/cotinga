import csv
import io
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from typing import Any, cast
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone, translation

from accounts.models import User
from gym.forms import SecondsField
from gym.models import (
    Exercise,
    GymPreferences,
    Routine,
    RoutineExercise,
    Workout,
    WorkoutExercise,
    WorkoutSet,
)
from gym.services import (
    MAX_EXERCISES,
    MAX_SETS,
    add_exercise,
    exercise_sessions,
    line_chart,
    performance_key,
    previous_exercises,
    rest_seconds,
    training_summary,
    workout_totals,
)
from gym.templatetags.gym_tags import clock, set_result, short_number
from tracker.models import WeightEntry

pytestmark = pytest.mark.django_db
JSON: dict[str, Any] = {"HTTP_ACCEPT": "application/json"}


@pytest.fixture(autouse=True)
def training(client: Client, owner: User) -> None:
    client.force_login(owner)
    if not Exercise.objects.filter(owner__isnull=True).exists():
        data = import_module("gym.migrations.0002_exercise_library").EXERCISES
        Exercise.objects.bulk_create([Exercise(**row) for row in data])


def session_for(
    owner: User, mode: str = "weight_reps", weight: Decimal | None = Decimal(80)
) -> tuple[Workout, WorkoutExercise]:
    session = Workout.objects.create(user=owner, name="Test workout", bodyweight=weight)
    exercise = Exercise.objects.filter(mode=mode).first()
    assert exercise is not None
    return session, add_exercise(session, exercise)


def action(client: Client, session: Workout, action_name: str, **data: Any) -> Any:
    session.refresh_from_db()
    response = client.post(
        f"/gym/workouts/{session.pk}/action/",
        {"action": action_name, "version": session.version, **data},
        **JSON,
    )
    session.refresh_from_db()
    return response


def complete(client: Client, session: Workout, entry: WorkoutSet, **data: Any) -> Any:
    values = {
        "kind": "working",
        "weight": "50",
        "reps": "8",
        "seconds": "02:30",
        "distance": "1.5",
        "rpe": "8",
        "completed": "on",
        **data,
    }
    return action(
        client,
        session,
        "set",
        set=entry.pk,
        **{f"set-{entry.pk}-{key}": value for key, value in values.items()},
    )


def finish(client: Client, session: Workout) -> Any:
    return action(client, session, "finish", confirm_unfinished="on")


def test_catalog_search_is_local_multilingual_and_filterable(client: Client) -> None:
    assert Exercise.objects.filter(owner__isnull=True).count() == 273
    for query in ("Barbell bench", "پرس سینه", "წოლჭიმი"):
        response = client.get("/gym/exercises/", {"q": query})
        assert response.status_code == 200 and response.context["page"].paginator.count
        assert "no-store" in response["Cache-Control"]
    response = client.get(
        "/gym/exercises/",
        {
            "muscle": "chest",
            "equipment": "barbell",
            "mode": "weight_reps",
            "q": "bench press",
            "fragment": "1",
        },
    )
    assert response.status_code == 200
    assert all(
        row.muscle == "chest" and row.equipment == "barbell"
        for row in response.context["page"]
    )
    assert "<!doctype" not in response.content.decode()
    assert (
        client.get("/gym/exercises/?q=nosuchmovement").context["page"].paginator.count
        == 0
    )
    assert client.get("/gym/exercises/?page=99999").status_code == 200


@pytest.mark.parametrize("language", ["en", "fa", "ka"])
def test_every_main_page_renders_and_is_private(
    client: Client, owner: User, language: str
) -> None:
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    session, row = session_for(owner)
    routine = Routine.objects.create(user=owner, name="My plan")
    RoutineExercise.objects.create(routine=routine, exercise=row.exercise)
    urls = [
        "/gym/",
        "/gym/start/",
        "/gym/exercises/",
        "/gym/exercises/new/",
        "/gym/progress/",
        "/gym/history/",
        "/gym/routines/new/",
        f"/gym/routines/{routine.pk}/",
        f"/gym/workouts/{session.pk}/",
        f"/gym/exercises/{row.exercise_id}/",
    ]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
        assert "no-store" in response["Cache-Control"]
        assert f'lang="{language}"' in response.content.decode()
    assert (
        client.get(f"/gym/workouts/{session.pk}/").content.count(b'id="id_bodyweight"')
        == 1
    )
    assert (
        client.get(f"/gym/workouts/{session.pk}/").content.count(
            b'id="fallback_bodyweight"'
        )
        == 1
    )


def test_custom_exercises_are_owned_archivable_and_keep_historical_rules(
    client: Client, owner: User
) -> None:
    values = {
        "name": "My private movement",
        "muscle": "core",
        "equipment": "other",
        "mode": "duration",
        "notes": "Original note فارسی",
    }
    response = client.post("/gym/exercises/new/", values)
    assert response.status_code == 302
    exercise = Exercise.objects.get(owner=owner)
    assert exercise.notes == values["notes"]
    assert client.get("/gym/exercises/?custom=1").context["page"].paginator.count == 1
    session = Workout.objects.create(user=owner, name="Custom workout")
    row = add_exercise(session, exercise)
    response = client.post(
        f"/gym/exercises/{exercise.pk}/edit/", {**values, "mode": "reps"}
    )
    assert response.status_code == 200 and response.context["form"].errors
    exercise.refresh_from_db()
    assert exercise.mode == "duration"
    assert (
        client.post(
            f"/gym/exercises/{exercise.pk}/edit/", {**values, "name": "Renamed"}
        ).status_code
        == 302
    )
    row.refresh_from_db()
    assert row.name == "My private movement"
    assert (
        client.post(
            f"/gym/exercises/{exercise.pk}/edit/", {"action": "archive"}
        ).status_code
        == 302
    )
    assert not client.get("/gym/exercises/?custom=1").context["page"].paginator.count
    assert client.get("/gym/exercises/?archived=1").context["page"].paginator.count == 1
    assert client.get(f"/gym/exercises/{exercise.pk}/").status_code == 200
    assert (
        client.post(
            f"/gym/exercises/{exercise.pk}/edit/", {"action": "restore"}
        ).status_code
        == 302
    )


def test_cross_user_objects_and_builtin_edits_are_inaccessible(
    client: Client, owner: User
) -> None:
    other = User.objects.create_user("other-trainer@example.com", "different-password")
    exercise = Exercise.objects.create(
        owner=other,
        name="Private exercise",
        muscle="back",
        equipment="other",
        mode="reps",
    )
    routine = Routine.objects.create(user=other, name="Private routine")
    session = Workout.objects.create(user=other, name="Private workout")
    for path in [
        f"/gym/exercises/{exercise.pk}/",
        f"/gym/exercises/{exercise.pk}/edit/",
        f"/gym/routines/{routine.pk}/",
        f"/gym/workouts/{session.pk}/",
        f"/gym/exercises/?routine={routine.pk}",
        f"/gym/exercises/?workout={session.pk}",
    ]:
        assert client.get(path).status_code == 404
    assert "Private" not in client.get("/gym/exercises/").content.decode()
    assert (
        client.post(
            f"/gym/workouts/{session.pk}/action/", {"version": 0, "action": "archive"}
        ).status_code
        == 404
    )
    assert client.post(f"/gym/routines/{routine.pk}/archive/").status_code == 404
    built_in = Exercise.objects.filter(owner__isnull=True).first()
    assert built_in is not None
    assert client.get(f"/gym/exercises/{built_in.pk}/edit/").status_code == 404
    assert client.get(f"/gym/exercises/{2**64}/").status_code == 404
    assert client.get(f"/gym/routines/{2**64}/").status_code == 404
    assert client.get("/gym/exercises/?routine=bad").status_code == 404
    assert client.get("/gym/exercises/?workout=bad").status_code == 404
    mine, _ = session_for(owner)
    assert action(client, mine, "add_exercise", exercise=exercise.pk).status_code == 404


def test_login_csrf_and_safe_methods(client: Client, owner: User) -> None:
    session, _ = session_for(owner)
    secure = Client(enforce_csrf_checks=True)
    secure.force_login(owner)
    assert secure.post("/gym/start/", {"date": timezone.localdate()}).status_code == 403
    assert (
        secure.post(
            f"/gym/workouts/{session.pk}/action/", {"action": "finish", "version": 0}
        ).status_code
        == 403
    )
    assert client.get(f"/gym/workouts/{session.pk}/action/").status_code == 405
    assert client.post("/gym/exercises/").status_code == 405
    client.logout()
    for path in [
        "/gym/",
        "/gym/exercises/",
        "/gym/history/",
        "/gym/progress/",
        "/gym/export/",
    ]:
        assert client.get(path).status_code == 302


def test_start_uses_historical_weight_and_is_idempotent(
    client: Client, owner: User
) -> None:
    today = timezone.localdate()
    WeightEntry.objects.create(user=owner, date=today - timedelta(days=10), weight=80)
    WeightEntry.objects.create(user=owner, date=today - timedelta(days=3), weight=78)
    WeightEntry.objects.create(user=owner, date=today, weight=77)
    data = {"date": today - timedelta(days=5), "name": "Original session"}
    first = client.post("/gym/start/", data)
    session = Workout.objects.get(user=owner)
    assert session.bodyweight == 80 and session.bodyweight_date == today - timedelta(
        days=10
    )
    assert client.post("/gym/start/", data)["Location"] == first["Location"]
    assert Workout.objects.filter(user=owner).count() == 1
    WeightEntry.objects.filter(user=owner).update(weight=90)
    session.refresh_from_db()
    assert session.bodyweight == 80
    assert (
        client.post("/gym/start/", {"date": today + timedelta(days=1)}).status_code
        == 200
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        Workout.objects.create(user=owner, name="Duplicate active session")


def test_routine_build_update_reorder_duplicate_archive_and_restore(
    client: Client, owner: User
) -> None:
    response = client.post(
        "/gym/routines/new/", {"name": "Upper A", "notes": "My plan"}
    )
    assert response.status_code == 302
    routine = Routine.objects.get(user=owner)
    exercises = list(Exercise.objects.filter(mode="weight_reps")[:2])
    for exercise in exercises:
        assert (
            client.post(
                f"/gym/routines/{routine.pk}/add/", {"exercise": exercise.pk}, **JSON
            ).status_code
            == 200
        )
    rows = list(routine.exercises.all())
    values = {
        f"row-{rows[0].pk}-{key}": value
        for key, value in {
            "sets": 4,
            "reps": 8,
            "weight": 55,
            "seconds": "01:00",
            "rest_seconds": 120,
            "group": "A",
            "notes": "Keep the bar steady",
        }.items()
    }
    assert (
        client.post(
            f"/gym/routines/{routine.pk}/update/", {"row": rows[0].pk, **values}
        ).status_code
        == 302
    )
    rows[0].refresh_from_db()
    assert rows[0].sets == 4 and rows[0].weight == 55 and rows[0].group == "A"
    bad = client.post(
        f"/gym/routines/{routine.pk}/update/",
        {"row": rows[0].pk, **values, f"row-{rows[0].pk}-sets": 0},
    )
    assert bad.status_code == 200 and b"errorlist" in bad.content
    assert (
        client.post(
            f"/gym/routines/{routine.pk}/down/", {"row": rows[0].pk}
        ).status_code
        == 302
    )
    assert list(routine.exercises.values_list("pk", flat=True)) == [
        rows[1].pk,
        rows[0].pk,
    ]
    assert (
        client.post(f"/gym/routines/{routine.pk}/up/", {"row": rows[0].pk}).status_code
        == 302
    )
    assert client.post(f"/gym/routines/{routine.pk}/duplicate/").status_code == 302
    copy = Routine.objects.filter(user=owner).exclude(pk=routine.pk).get()
    copied_row = copy.exercises.first()
    assert copy.exercises.count() == 2 and copied_row is not None
    assert copied_row.weight == 55
    assert client.post(f"/gym/routines/{routine.pk}/archive/").status_code == 302
    assert routine.pk in [
        row.pk for row in client.get("/gym/?archived=1").context["routines"]
    ]
    assert (
        client.post(
            f"/gym/routines/{routine.pk}/add/", {"exercise": exercises[0].pk}, **JSON
        ).status_code
        == 400
    )
    assert client.post(f"/gym/routines/{routine.pk}/restore/").status_code == 302
    assert (
        client.post(
            "/gym/start/", {"date": timezone.localdate(), "routine": routine.pk}
        ).status_code
        == 302
    )
    session = Workout.objects.get(user=owner)
    assert session.name == "Upper A" and session.exercises.count() == 2
    first = session.exercises.first()
    assert (
        first is not None
        and first.sets.count() == 4
        and first.rest_seconds == 120
        and first.group == "A"
    )
    assert all(
        entry.weight == 55 and entry.completed_at is None and entry.seconds is None
        for entry in first.sets.all()
    )
    assert (
        client.post(
            f"/gym/routines/{routine.pk}/remove/", {"row": rows[1].pk}
        ).status_code
        == 302
    )
    assert session.exercises.count() == 2


@pytest.mark.parametrize("mode", Exercise.Mode.values)
def test_all_tracking_modes_complete_without_counting_unused_fields(
    client: Client, owner: User, mode: str
) -> None:
    session, row = session_for(owner, mode)
    entry = row.sets.first()
    assert entry is not None
    assert complete(client, session, entry).status_code == 200
    entry.refresh_from_db()
    assert entry.completed_at and entry.rpe == 8
    if mode in ("duration", "distance_time"):
        assert entry.seconds == 150 and entry.reps is None and entry.weight is None
    else:
        assert entry.reps == 8 and entry.seconds is None and entry.distance is None
    totals = workout_totals(session)
    assert totals["completed"] == 1 and totals["working"] == 1
    assert totals["volume"] == (400 if mode == "weight_reps" else 0)
    assert finish(client, session).status_code == 200
    response = client.get(f"/gym/exercises/{row.exercise_id}/")
    assert response.status_code == 200 and response.context["best"]
    assert client.get("/gym/progress/").context["summary"]["count"] == 1


@pytest.mark.parametrize(
    "mode,changes",
    [
        ("weight_reps", {"weight": ""}),
        ("weight_reps", {"weight": "-2"}),
        ("weight_reps", {"weight": "2001"}),
        ("weight_reps", {"reps": "0"}),
        ("weight_reps", {"reps": "1001"}),
        ("weight_reps", {"rpe": "11"}),
        ("duration", {"seconds": "02:70"}),
        ("duration", {"seconds": "00:00"}),
        ("distance_time", {"distance": "0"}),
        ("distance_time", {"distance": "1001"}),
        ("assisted_reps", {"weight": "81"}),
    ],
)
def test_invalid_sets_never_count(
    client: Client, owner: User, mode: str, changes: dict[str, str]
) -> None:
    session, row = session_for(owner, mode)
    entry = row.sets.first()
    assert entry is not None
    response = complete(client, session, entry, **changes)
    assert response.status_code == 400 and response.json()["error"]
    entry.refresh_from_db()
    assert entry.completed_at is None and workout_totals(session)["completed"] == 0
    assert session.version == 0


def test_bodyweight_required_shared_only_explicitly_and_frozen(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner, "bodyweight_reps", weight=None)
    entry = row.sets.first()
    assert entry is not None
    response = complete(client, session, entry, weight="")
    assert response.status_code == 400 and response.json()["needs_weight"]
    assert action(client, session, "bodyweight", bodyweight="79.50").status_code == 200
    assert not WeightEntry.objects.filter(user=owner).exists()
    assert complete(client, session, entry, weight="").status_code == 200
    entry.refresh_from_db()
    assert entry.weight == 0
    assert (
        action(
            client, session, "bodyweight", bodyweight="79.25", save_weigh_in="on"
        ).status_code
        == 200
    )
    assert WeightEntry.objects.get(user=owner).weight == Decimal("79.25")
    WeightEntry.objects.filter(user=owner).update(weight=77)
    session.refresh_from_db()
    assert session.bodyweight == Decimal("79.25")
    assert action(client, session, "bodyweight", bodyweight="0").status_code == 400


def test_assistance_checks_weight_changes_against_completed_sets(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner, "assisted_reps")
    entry = row.sets.first()
    assert entry is not None
    assert complete(client, session, entry, weight="50").status_code == 200
    assert (
        action(
            client, session, "bodyweight", bodyweight="40", save_weigh_in="on"
        ).status_code
        == 400
    )
    assert session.bodyweight == 80 and not WeightEntry.objects.exists()


def test_external_weight_does_not_request_unnecessary_bodyweight(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner, weight=None)
    entry = row.sets.first()
    assert entry is not None
    response = complete(client, session, entry, reps=0)
    assert response.status_code == 400 and not response.json()["needs_weight"]
    assert complete(client, session, entry).status_code == 200
    assert session.bodyweight is None


def test_completion_replay_does_not_restart_rest_or_duplicate_sets(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner)
    entry = row.sets.first()
    assert entry is not None
    assert complete(client, session, entry).status_code == 200
    ends = session.rest_ends_at
    entry.refresh_from_db()
    completed_at = entry.completed_at
    assert complete(client, session, entry).status_code == 200
    entry.refresh_from_db()
    assert session.rest_ends_at == ends and entry.completed_at == completed_at
    assert row.sets.count() == 3 and workout_totals(session)["volume"] == 400
    assert complete(client, session, entry, completed="").status_code == 200
    assert workout_totals(session)["volume"] == 0


def test_stale_and_foreign_set_saves_are_rejected(client: Client, owner: User) -> None:
    session, row = session_for(owner)
    entry = row.sets.first()
    assert entry is not None
    assert complete(client, session, entry).status_code == 200
    response = client.post(
        f"/gym/workouts/{session.pk}/action/",
        {"action": "archive", "version": 0},
        **JSON,
    )
    assert response.status_code == 409 and response.json()["conflict"]
    assert action(client, session, "remove_set", set=2**64).status_code == 400
    other = User.objects.create_user("other-set@example.com", "different-password")
    foreign, other_row = session_for(other)
    other_set = other_row.sets.first()
    assert other_set is not None
    assert action(client, session, "remove_set", set=other_set.pk).status_code == 404
    assert (
        action(client, session, "remove_exercise", row=other_row.pk).status_code == 404
    )
    foreign.refresh_from_db()
    assert foreign.version == 0


def test_timers_survive_refresh_pause_resume_and_bounds(
    client: Client, owner: User
) -> None:
    session, _ = session_for(owner)
    assert (
        action(client, session, "timer", command="start", seconds=60).status_code == 200
    )
    assert 59 <= rest_seconds(session) <= 60
    response = client.get(f"/gym/workouts/{session.pk}/")
    assert session.rest_ends_at is not None
    assert response.context["state"]["rest_ends_at"] == session.rest_ends_at.isoformat()
    assert action(client, session, "timer", command="pause").status_code == 200
    assert Workout.objects.get(pk=session.pk).rest_ends_at is None
    assert session.rest_remaining == 60
    assert (
        action(client, session, "timer", command="extend", seconds=30).status_code
        == 200
    )
    assert 89 <= rest_seconds(session) <= 90
    assert Workout.objects.get(pk=session.pk).rest_ends_at is None
    assert session.rest_remaining == 90
    assert action(client, session, "timer", command="pause").status_code == 200
    assert action(client, session, "timer", command="resume").status_code == 200
    assert (
        action(client, session, "timer", command="extend", seconds=1800).status_code
        == 200
    )
    assert rest_seconds(session) <= 1800
    assert action(client, session, "timer", command="cancel").status_code == 200
    assert rest_seconds(session) == 0
    assert (
        action(client, session, "timer", command="start", seconds=0).status_code == 400
    )
    assert action(client, session, "timer", command="bad").status_code == 400
    assert (
        action(client, session, "timer", command="start", seconds=1801).status_code
        == 400
    )


def test_pause_duration_finish_guard_and_history_edit(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner)
    now = timezone.now()
    session.started_at = now - timedelta(seconds=200)
    session.save()
    with patch("gym.views.timezone.now", return_value=now):
        assert action(client, session, "pause").status_code == 200
    with patch("gym.views.timezone.now", return_value=now + timedelta(seconds=50)):
        assert action(client, session, "resume").status_code == 200
    assert session.paused_seconds == 50 and session.paused_at is None
    assert finish(client, session).status_code == 400
    entry = row.sets.first()
    assert entry is not None
    assert complete(client, session, entry).status_code == 200
    assert action(client, session, "finish").status_code == 400
    assert finish(client, session).status_code == 200
    assert session.status == "finished" and session.rest_ends_at is None
    assert (
        action(client, session, "timer", command="start", seconds=30).status_code == 400
    )
    assert action(client, session, "remove_set", set=entry.pk).status_code == 400
    assert action(client, session, "remove_exercise", row=row.pk).status_code == 400
    assert complete(client, session, entry, completed="").status_code == 400
    assert complete(client, session, entry, weight="55").status_code == 200
    assert session.rest_ends_at is None
    assert (
        action(
            client,
            session,
            "metadata",
            name="Corrected",
            date=session.date,
            notes="My note",
            duration="05:00",
        ).status_code
        == 200
    )
    assert session.name == "Corrected" and session.elapsed_seconds == 300
    assert client.get("/gym/history/").context["page"].paginator.count == 1
    assert training_summary(owner)["minutes"] == 5


def test_previous_sets_ignore_warmups_and_never_precomplete_next_workout(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner)
    first, second, third = list(row.sets.all())
    assert complete(client, session, first, kind="warmup", weight=20).status_code == 200
    assert complete(client, session, second, weight=45, reps=12).status_code == 200
    assert complete(client, session, third, weight=47, reps=10).status_code == 200
    assert finish(client, session).status_code == 200
    assert workout_totals(session)["volume"] == 1010
    previous = previous_exercises(owner, [row.exercise_id])
    assert len(previous[row.exercise_id].sets.all()) == 2
    assert client.post("/gym/start/", {"date": timezone.localdate()}).status_code == 302
    next_workout = Workout.objects.get(user=owner, status="active")
    assert (
        action(
            client, next_workout, "add_exercise", exercise=row.exercise_id
        ).status_code
        == 200
    )
    copied = next_workout.exercises.get()
    entries = list(copied.sets.all())
    assert len(entries) == 2 and entries[0].weight == 45 and entries[1].reps == 10
    assert all(entry.completed_at is None for entry in entries)
    assert workout_totals(next_workout)["volume"] == 0
    assert (
        client.get(f"/gym/workouts/{next_workout.pk}/")
        .context["cards"][0]["sets"][0]["previous"]
        .weight
        == 45
    )


def test_workout_rows_sets_options_reordering_and_save_as_routine(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner)
    assert action(client, session, "add_set", row=row.pk).status_code == 200
    assert row.sets.count() == 4
    extra = Exercise.objects.filter(mode="duration").first()
    assert extra is not None
    assert action(client, session, "add_exercise", exercise=extra.pk).status_code == 200
    other = session.exercises.exclude(pk=row.pk).get()
    assert action(client, session, "down", row=row.pk).status_code == 200
    assert list(session.exercises.values_list("pk", flat=True)) == [other.pk, row.pk]
    assert action(client, session, "up", row=row.pk).status_code == 200
    values = {
        f"exercise-{row.pk}-{key}": value
        for key, value in {"rest_seconds": 0, "group": "B", "notes": "Superset"}.items()
    }
    assert (
        action(client, session, "exercise_options", row=row.pk, **values).status_code
        == 200
    )
    row.refresh_from_db()
    assert row.rest_seconds == 0 and row.group == "B"
    entry = row.sets.first()
    assert entry is not None
    assert (
        complete(client, session, entry).status_code == 200
        and session.rest_ends_at is None
    )
    assert action(client, session, "save_routine").status_code == 200
    routine = Routine.objects.get(user=owner)
    planned = routine.exercises.first()
    assert (
        planned is not None
        and planned.sets == 1
        and planned.weight == 50
        and planned.group == "B"
    )
    assert action(client, session, "remove_exercise", row=other.pk).status_code == 200
    assert action(client, session, "remove_set", set=entry.pk).status_code == 200


def test_archive_restore_and_routine_recovery_preserve_history(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner)
    assert action(client, session, "archive").status_code == 200
    assert session.archived_at and session.paused_at
    assert client.get(f"/gym/workouts/{session.pk}/").status_code == 404
    assert client.get("/gym/history/?archived=1").context["page"].paginator.count == 1
    other, _ = session_for(owner, "duration")
    assert action(client, session, "restore").status_code == 400
    assert action(client, other, "archive").status_code == 200
    assert action(client, session, "restore").status_code == 200
    assert Workout.objects.get(pk=session.pk).archived_at is None
    assert row.sets.count() == 3
    entry = row.sets.first()
    assert entry is not None
    assert complete(client, session, entry).status_code == 200
    assert finish(client, session).status_code == 200
    assert action(client, session, "archive").status_code == 200
    assert training_summary(owner)["count"] == 0
    assert action(client, session, "restore").status_code == 200
    assert training_summary(owner)["count"] == 1


def test_progress_groups_repeated_exercise_once_and_filters_warmups(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner)
    second = add_exercise(session, row.exercise)
    first_set, second_set = row.sets.first(), second.sets.first()
    assert first_set is not None and second_set is not None
    assert complete(client, session, first_set, weight=40).status_code == 200
    assert complete(client, session, second_set, weight=55).status_code == 200
    assert finish(client, session).status_code == 200
    sessions = exercise_sessions(owner, row.exercise)
    assert (
        len(sessions) == 1
        and len(sessions[0]["sets"]) == 2
        and sessions[0]["best"].weight == 55
    )
    assert line_chart(sessions)["dots"][0]["x"] == 350
    assert not line_chart([])
    summary = training_summary(owner)
    assert summary["sets"] == 2 and summary["muscles"][0]["count"] == 2
    assert client.get("/gym/progress/?days=84").context["days"] == 84
    assert client.get("/gym/progress/?days=invalid").context["days"] == 28


def test_export_is_private_and_does_not_execute_spreadsheet_formulas(
    client: Client, owner: User
) -> None:
    session, row = session_for(owner)
    session.name = "=dangerous()"
    session.save()
    entry = row.sets.first()
    assert entry is not None
    assert complete(client, session, entry).status_code == 200
    assert finish(client, session).status_code == 200
    other = User.objects.create_user("private-export@example.com", "different-password")
    private = Workout.objects.create(user=other, name="PRIVATE", status="finished")
    hidden = add_exercise(private, row.exercise)
    hidden.sets.update(completed_at=timezone.now(), weight=100, reps=5)
    response = client.get("/gym/export/")
    content = b"".join(cast(Any, response).streaming_content).decode()
    assert "PRIVATE" not in content and "no-store" in response["Cache-Control"]
    rows = list(csv.reader(io.StringIO(content)))
    assert len(rows) == 2 and rows[1][1] == "'=dangerous()"
    assert rows[1][5:7] == ["50.00", "8"]


@pytest.mark.parametrize(
    "raw,expected", [("02:30", 150), ("90", 90), ("1:05", 65), ("1440:00", 86400)]
)
def test_duration_input_supports_minutes_or_seconds(raw: str, expected: int) -> None:
    assert SecondsField().clean(raw) == expected


def test_limits_and_invalid_actions_do_not_corrupt_session(
    client: Client, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_EXERCISES == 40 and MAX_SETS == 30
    session, row = session_for(owner)
    monkeypatch.setattr("gym.services.MAX_EXERCISES", 1)
    assert (
        action(client, session, "add_exercise", exercise=row.exercise_id).status_code
        == 400
    )
    monkeypatch.setattr("gym.views.MAX_SETS", 3)
    assert action(client, session, "add_set", row=row.pk).status_code == 400
    assert action(client, session, "bad").status_code == 404
    assert (
        client.post(
            f"/gym/workouts/{session.pk}/action/",
            {"action": "finish", "version": "bad"},
            **JSON,
        ).status_code
        == 400
    )
    assert (
        action(
            client,
            session,
            "exercise_options",
            row=row.pk,
            **{f"exercise-{row.pk}-rest_seconds": 2000},
        ).status_code
        == 400
    )
    assert action(client, session, "metadata", name="", date="bad").status_code == 400


def test_goal_html_errors_display_and_helper_formatting(
    client: Client, owner: User
) -> None:
    assert client.post("/gym/", {"weekly_goal": 4}).status_code == 302
    assert GymPreferences.objects.get(user=owner).weekly_goal == 4
    assert client.post("/gym/", {"weekly_goal": 0}).status_code == 200
    session, row = session_for(owner)
    response = client.post(
        f"/gym/workouts/{session.pk}/action/",
        {"version": 0, "action": "bodyweight", "bodyweight": 0},
    )
    assert response.status_code == 200 and response.context["bodyweight_form"].errors
    assert short_number(Decimal("10.00")) == "10"
    assert short_number(None) == "—" and clock(3661) == "01:01:01"
    entry = row.sets.first()
    assert entry is not None
    entry.weight, entry.reps, entry.seconds, entry.distance = (
        Decimal(20),
        8,
        90,
        Decimal("1.5"),
    )
    with translation.override("en"):
        for mode in Exercise.Mode.values:
            row.mode = mode
            assert set_result(entry, mode) and performance_key(row, entry)


def test_deleting_owner_removes_private_training_data_without_catalog_loss(
    client: Client, owner: User
) -> None:
    exercise = Exercise.objects.create(
        owner=owner, name="Private", muscle="core", equipment="other", mode="reps"
    )
    session = Workout.objects.create(user=owner, name="Private session")
    add_exercise(session, exercise)
    owner.delete()
    assert (
        not Workout.objects.exists()
        and not Exercise.objects.filter(owner__isnull=False).exists()
    )
    assert Exercise.objects.filter(owner__isnull=True).count() == 273
