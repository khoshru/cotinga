import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import pytest
from django.http import StreamingHttpResponse
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation

from accounts.models import User
from gym.models import (
    Exercise,
    Routine,
    RoutineExercise,
    Workout,
    WorkoutExercise,
    WorkoutSet,
)
from gym.momentum import training_momentum
from gym.records import record_book
from tracker.models import WeightEntry

pytestmark = pytest.mark.django_db
TODAY = date(2026, 11, 10)  # Tuesday; test the Monday boundary separately.


def saved_workout(user: User, day: date = TODAY, *, archived: bool = False) -> Workout:
    return Workout.objects.create(
        user=user,
        name="Evening strength",
        date=day,
        status=Workout.Status.FINISHED,
        finished_at=timezone.now(),
        archived_at=timezone.now() if archived else None,
    )


def saved_set(workout: Workout, *, weight: int = 50) -> WorkoutSet:
    exercise, _ = Exercise.objects.get_or_create(
        slug="test-momentum-curl",
        defaults={"name": "Curl", "muscle": "biceps", "equipment": "dumbbell"},
    )
    row = WorkoutExercise.objects.create(
        workout=workout,
        exercise=exercise,
        name=exercise.name,
        muscle=exercise.muscle,
        equipment=exercise.equipment,
        mode=exercise.mode,
    )
    return WorkoutSet.objects.create(
        workout_exercise=row,
        weight=Decimal(weight),
        reps=8,
        completed_at=timezone.now(),
    )


def test_delete_only_archived_owned_workout_and_preserve_other_data(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    routine = Routine.objects.create(user=owner, name="Keep plan")
    session = saved_workout(owner, archived=True)
    session.routine = routine
    session.save()
    entry = saved_set(session)
    row_id, exercise_id = entry.workout_exercise_id, entry.workout_exercise.exercise_id
    RoutineExercise.objects.create(routine=routine, exercise_id=exercise_id)
    WeightEntry.objects.create(user=owner, date=TODAY, weight=80)
    keep = saved_workout(owner)
    keep_entry = saved_set(keep)
    url = reverse("gym:workout-delete", args=[session.pk])
    review = client.get(url)
    assert review.status_code == 200 and review.context["totals"]["completed"] == 1
    assert b"cannot be undone" in review.content
    assert b"Keep workout" in review.content
    assert client.post(url, {"version": session.version}).status_code == 200
    assert Workout.objects.filter(pk=session.pk).exists()
    result = client.post(url, {"confirm": "on", "version": session.version})
    assert (
        result.status_code == 302 and result["Location"] == "/gym/history/?archived=1"
    )
    assert not Workout.objects.filter(pk=session.pk).exists()
    assert not WorkoutExercise.objects.filter(pk=row_id).exists()
    assert not WorkoutSet.objects.filter(pk=entry.pk).exists()
    assert WorkoutSet.objects.filter(pk=keep_entry.pk).exists()
    assert RoutineExercise.objects.filter(
        routine=routine, exercise_id=exercise_id
    ).exists()
    assert WeightEntry.objects.filter(user=owner).exists()
    assert record_book(owner).best
    assert client.post(url, {"confirm": "on", "version": 0}).status_code == 404


@pytest.mark.parametrize("version", [None, "invalid", "-1", "999"])
def test_delete_rejects_missing_invalid_and_stale_versions(
    client: Client, owner: User, version: str | None
) -> None:
    client.force_login(owner)
    session = saved_workout(owner, archived=True)
    url = reverse("gym:workout-delete", args=[session.pk])
    data = {"confirm": "on"}
    if version is not None:
        data["version"] = version
    response = client.post(url, data)
    assert response.status_code == 409
    assert b"Review it again" in response.content
    assert Workout.objects.filter(pk=session.pk).exists()


def test_delete_cannot_remove_live_restored_or_another_users_workout(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    session = saved_workout(owner)
    url = reverse("gym:workout-delete", args=[session.pk])
    for status in Workout.Status.values:
        session.status = status
        session.save()
        assert client.get(url).status_code == 404
        assert client.post(url, {"confirm": "on", "version": 0}).status_code == 404
    session.archived_at = timezone.now()
    session.save()
    assert client.get(url).status_code == 200
    assert (
        client.post(
            reverse("gym:workout-action", args=[session.pk]),
            {"action": "restore", "version": 0},
        ).status_code
        == 302
    )
    assert client.post(url, {"confirm": "on", "version": 0}).status_code == 404
    other = User.objects.create_user("private-momentum@example.com", "test-only")
    foreign = saved_workout(other, archived=True)
    foreign_url = reverse("gym:workout-delete", args=[foreign.pk])
    assert client.get(foreign_url).status_code == 404
    assert client.post(foreign_url, {"confirm": "on", "version": 0}).status_code == 404
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    assert (
        csrf_client.post(foreign_url, {"confirm": "on", "version": 0}).status_code
        == 403
    )
    client.logout()
    assert client.get(url).status_code == 302
    assert Workout.objects.count() == 2


def test_history_shows_archive_actions_and_correct_export(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    live = saved_workout(owner)
    saved_set(live, weight=10)
    archived = saved_workout(owner, archived=True)
    saved_set(archived, weight=20)
    # A partially completed session may also be archived and exported.
    archived.status = Workout.Status.ACTIVE
    archived.save()
    other = User.objects.create_user("private-export@example.com", "test-only")
    saved_set(saved_workout(other, archived=True), weight=99)
    response = client.get("/gym/history/?archived=1")
    assert response.context["counts"] == {"finished": 1, "archived": 1}
    assert response.context["page"].paginator.count == 1
    assert (
        reverse("gym:workout-delete", args=[archived.pk]).encode() in response.content
    )
    assert b"Restore" in response.content and b"Delete" in response.content
    for suffix, expected in [("", "10.00"), ("?archived=1", "20.00")]:
        export = cast(StreamingHttpResponse, client.get("/gym/export/" + suffix))
        rows = list(csv.DictReader(io.StringIO(export.getvalue().decode())))
        assert len(rows) == 1 and rows[0]["weight_kg"] == expected
    assert (
        b"No matching workouts"
        in client.get("/gym/history/?archived=1&q=missing").content
    )


def test_momentum_empty_and_excludes_archived_active_future_and_other_user(
    owner: User, django_assert_num_queries: Any
) -> None:
    other = User.objects.create_user("private-streak@example.com", "test-only")
    saved_workout(other)
    saved_workout(owner, archived=True)
    saved_workout(owner, TODAY + timedelta(days=1))
    Workout.objects.create(user=owner, name="In progress", date=TODAY)
    with (
        patch("gym.momentum.timezone.localdate", return_value=TODAY),
        django_assert_num_queries(1),
    ):
        result = training_momentum(owner)
    assert result["total"] == result["streak"] == result["longest"] == 0
    assert result["next"] == result["remaining"] == 1
    assert not result["reached"] and result["percent"] == 0
    assert len(result["days"]) == 7 and result["days"][1]["today"]
    assert result["days"][2]["future"]


def test_weekly_consistency_allows_rest_and_does_not_count_duplicate_days_as_weeks(
    owner: User,
) -> None:
    monday = TODAY - timedelta(days=1)
    for offset in [0, 0, -7, -14, -35, -42, -49, -56]:
        saved_workout(owner, monday + timedelta(days=offset))
    with patch("gym.momentum.timezone.localdate", return_value=TODAY):
        result = training_momentum(owner)
    assert result["streak"] == 3 and result["longest"] == 4
    assert result["total"] == 8 and result["next"] == 10 and result["remaining"] == 2
    assert result["days"][0]["count"] == 2
    # Next Monday is still an open week, even before logging a new session.
    with patch(
        "gym.momentum.timezone.localdate", return_value=monday + timedelta(days=7)
    ):
        assert training_momentum(owner)["streak"] == 3
    with patch(
        "gym.momentum.timezone.localdate", return_value=monday + timedelta(days=14)
    ):
        assert training_momentum(owner)["streak"] == 0


@pytest.mark.parametrize(
    ("total", "next_target", "reached"),
    [
        (1, 5, True),
        (4, 5, False),
        (5, 10, True),
        (25, 50, True),
        (100, 150, True),
        (150, 200, True),
        (151, 200, False),
    ],
)
def test_lifetime_milestones_and_archive_restore(
    owner: User, total: int, next_target: int, reached: bool
) -> None:
    Workout.objects.bulk_create(
        [
            Workout(user=owner, name="Training", date=TODAY, status="finished")
            for _ in range(total)
        ]
    )
    with patch("gym.momentum.timezone.localdate", return_value=TODAY):
        result = training_momentum(owner)
        assert result["next"] == next_target and result["reached"] is reached
        assert result["remaining"] == next_target - total
        assert 0 <= result["percent"] < 100
        session = Workout.objects.filter(user=owner).first()
        assert session is not None
        session.archived_at = timezone.now()
        session.save()
        assert training_momentum(owner)["total"] == total - 1
        session.archived_at = None
        session.save()
        assert training_momentum(owner)["total"] == total


def test_training_home_renders_goal_momentum_and_recent_records(
    client: Client, owner: User
) -> None:
    client.force_login(owner)
    saved_set(saved_workout(owner, TODAY - timedelta(days=7)), weight=40)
    saved_set(saved_workout(owner), weight=50)
    with patch("gym.views.timezone.localdate", return_value=TODAY):
        response = client.get("/gym/")
        assert response.context["weekly_remaining"] == 2
        assert response.context["momentum"]["total"] == 2
        assert response.context["record_counts"]
        assert (
            b"personal records" in response.content
            and b"2 weeks of consistency" in response.content
        )
        assert b"2 more workouts to your weekly goal" in response.content
        for language in ("fa", "ka"):
            client.cookies["django_language"] = language
            with translation.override(language):
                localized = client.get("/gym/")
            assert localized.status_code == 200
            assert b"Every session counts." not in localized.content


def test_goal_reached_and_empty_archive_states(client: Client, owner: User) -> None:
    client.force_login(owner)
    saved_workout(owner)
    client.post("/gym/", {"weekly_goal": 1})
    response = client.get("/gym/")
    assert response.context["weekly_remaining"] == 0
    assert b"Weekly goal reached. Make room for recovery, too." in response.content
    assert b"Your archive is clear." in client.get("/gym/history/?archived=1").content
    archived = saved_workout(owner, archived=True)
    for language in ("fa", "ka"):
        client.cookies["django_language"] = language
        with translation.override(language):
            confirm = client.get(reverse("gym:workout-delete", args=[archived.pk]))
            assert confirm.status_code == 200
            assert b"Delete this workout permanently?" not in confirm.content
