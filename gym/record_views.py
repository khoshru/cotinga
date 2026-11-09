from typing import Any, cast
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.models import User

from .models import Workout
from .records import record_book


def workout_record_context(user: User, workout: Workout) -> dict[str, Any]:
    book = record_book(user, workout)
    return {
        "session": workout,
        "workout_records": book.for_workout(workout),
        "record_feedback": book.feedback,
        "first_exercises": book.first_exercises,
    }


def record_fragments(request: HttpRequest, workout: Workout) -> dict[str, Any]:
    context = workout_record_context(cast(User, request.user), workout)
    return {
        "summary": render_to_string(
            "gym/workout_records.html", context, request=request
        ),
        "sets": {
            str(pk): render_to_string(
                "gym/set_records.html", {"feedback": feedback}, request=request
            )
            for pk, feedback in context["record_feedback"].items()
        },
    }


@login_required
@never_cache
@require_GET
def records(request: HttpRequest) -> HttpResponse:
    book = record_book(cast(User, request.user))
    exercise = request.GET.get("exercise", "")[:20]
    routine = request.GET.get("routine", "")[:20]
    query = request.GET.get("q", "").strip()[:100]
    choices = {
        str(item.exercise_id): item.subject
        for item in book.best.values()
        if item.exercise_id
    }

    def matches(item: Any) -> bool:
        return (
            (not exercise or str(item.exercise_id) == exercise)
            and (not routine or item.key[0] == f"routine:{routine}")
            and (
                not query
                or query.casefold() in f"{item.subject} {item.label}".casefold()
            )
        )

    history = [record for record in reversed(book.records) if matches(record.current)]
    bests = sorted(
        (item for item in book.best.values() if matches(item)),
        key=lambda item: (item.workout.date, item.workout.started_at),
        reverse=True,
    )
    load_bests = [
        item for item in bests if item.metric in ("reps_at_load", "time_at_distance")
    ]
    current_bests = [
        item
        for item in bests
        if item.metric not in ("reps_at_load", "time_at_distance")
    ]
    volume = next(
        (item for item in book.best.values() if item.metric == "workout_volume"), None
    )
    return render(
        request,
        "gym/records.html",
        {
            "page": Paginator(history, 20).get_page(request.GET.get("page")),
            "bests": current_bests if exercise or routine else current_bests[:6],
            "load_bests": load_bests if exercise else [],
            "record_count": len(history),
            "exercise_count": len(choices),
            "best_volume": volume,
            "exercise_choices": sorted(choices.items(), key=lambda choice: choice[1]),
            "exercise_filter": exercise,
            "routine_filter": routine,
            "query": query,
            "params": urlencode({"exercise": exercise, "routine": routine, "q": query}),
        },
    )
