import csv
from datetime import timedelta
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, prefetch_related_objects
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.models import User
from tracker.models import WeightEntry

from .forms import (
    BodyweightForm,
    ExerciseForm,
    PreferencesForm,
    RoutineExerciseForm,
    RoutineForm,
    SetForm,
    StartForm,
    WorkoutExerciseForm,
    WorkoutForm,
)
from .models import (
    Exercise,
    GymPreferences,
    Routine,
    RoutineExercise,
    Workout,
    WorkoutExercise,
    WorkoutSet,
)
from .services import (
    MAX_EXERCISES,
    MAX_SETS,
    add_exercise,
    completed_sets,
    exercise_sessions,
    exercises_for,
    line_chart,
    previous_exercises,
    start_workout,
    timer_action,
    training_summary,
    workout_state,
    workout_totals,
)


def json_request(request: HttpRequest) -> bool:
    return "application/json" in request.headers.get("Accept", "")


def integer(value: Any, maximum: int = 2**63 - 1, minimum: int = 1) -> int:
    return int(forms.IntegerField(min_value=minimum, max_value=maximum).clean(value))


def safe_pk(pk: int) -> None:
    if pk >= 2**63:
        raise Http404


def form_error(form: Any) -> str:
    return " ".join(
        f"{form.fields[key].label or key}: {', '.join(errors)}"
        if key in form.fields
        else ", ".join(errors)
        for key, errors in form.errors.items()
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def home(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    preferences, _ = GymPreferences.objects.get_or_create(user=user)
    saved_goal = max(1, preferences.weekly_goal)
    form = PreferencesForm(
        request.POST if request.method == "POST" else None, instance=preferences
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("gym:home")
    today = timezone.localdate()
    week = today - timedelta(days=today.weekday())
    finished = Workout.objects.filter(
        user=user, status="finished", archived_at__isnull=True
    )
    count = finished.filter(date__gte=week, date__lte=today).count()
    archived_routines = request.GET.get("archived") == "1"
    routines_page = Paginator(
        Routine.objects.filter(user=user, archived=archived_routines).prefetch_related(
            "exercises__exercise"
        ),
        12,
    ).get_page(request.GET.get("page"))
    return render(
        request,
        "gym/home.html",
        {
            "active": Workout.objects.filter(
                user=user, status="active", archived_at__isnull=True
            ).first(),
            "routines": routines_page,
            "page": routines_page,
            "archived_routines": archived_routines,
            "params": urlencode({"archived": "1" if archived_routines else "0"}),
            "recent": finished.prefetch_related("exercises__sets")[:4],
            "weekly_count": count,
            "goal": saved_goal,
            "goal_percent": min(100, count / saved_goal * 100),
            "preferences_form": form,
            "summary": training_summary(user),
            "start_form": StartForm(user=user),
        },
    )


@login_required
@never_cache
@require_GET
def library(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    archived = request.GET.get("archived") == "1"
    queryset = (
        exercises_for(user).filter(owner=user, archived=True)
        if archived
        else exercises_for(user).filter(archived=False)
    )
    query = request.GET.get("q", "")[:120].strip()
    for term in query.split()[:8]:
        queryset = queryset.filter(
            Q(name__icontains=term)
            | Q(name_fa__icontains=term)
            | Q(name_ka__icontains=term)
        )
    for field in ("muscle", "equipment", "mode"):
        value = request.GET.get(field, "")
        if value:
            queryset = queryset.filter(**{field: value[:20]})
    if request.GET.get("custom"):
        queryset = queryset.filter(owner=user)
    routine = session = None
    try:
        if request.GET.get("routine"):
            routine = get_object_or_404(
                Routine, pk=integer(request.GET["routine"]), user=user, archived=False
            )
        if request.GET.get("workout"):
            workout_id = UUID(request.GET["workout"])
            session = get_object_or_404(
                Workout, pk=workout_id, user=user, archived_at__isnull=True
            )
    except (forms.ValidationError, ValueError) as exc:
        raise Http404 from exc
    params = request.GET.copy()
    params.pop("page", None)
    params.pop("fragment", None)
    context = {
        "page": Paginator(queryset, 24).get_page(request.GET.get("page")),
        "query": query,
        "filters": request.GET,
        "muscles": Exercise.Muscle.choices,
        "equipment": Exercise.Equipment.choices,
        "modes": Exercise.Mode.choices,
        "routine": routine,
        "session": session,
        "params": params.urlencode(),
        "catalog_count": Exercise.objects.filter(owner__isnull=True).count(),
        "archived_exercises": archived,
    }
    return render(
        request,
        "gym/exercise_cards.html"
        if request.GET.get("fragment")
        else "gym/library.html",
        context,
    )


@login_required
@never_cache
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def exercise_edit(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    user = cast(User, request.user)
    if pk:
        safe_pk(pk)
    exercise = (
        get_object_or_404(Exercise, pk=pk, owner=user) if pk else Exercise(owner=user)
    )
    if (
        request.method == "POST"
        and request.POST.get("action") in ("archive", "restore")
        and exercise.pk
    ):
        exercise.archived = request.POST["action"] == "archive"
        exercise.save(update_fields=["archived"])
        return redirect("gym:exercise", pk=exercise.pk)
    form = ExerciseForm(
        request.POST if request.method == "POST" else None, instance=exercise
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("gym:exercise", pk=exercise.pk)
    return render(
        request, "gym/exercise_form.html", {"form": form, "exercise": exercise}
    )


@login_required
@never_cache
@require_GET
def exercise_progress(request: HttpRequest, pk: int) -> HttpResponse:
    safe_pk(pk)
    exercise = get_object_or_404(exercises_for(cast(User, request.user)), pk=pk)
    sessions = exercise_sessions(cast(User, request.user), exercise)
    metric = str(dict(Exercise.Mode.choices)[exercise.mode])
    if (
        exercise.mode == Exercise.Mode.BODYWEIGHT
        and sessions
        and not any(item["value"] for item in sessions)
    ):
        for item in sessions:
            item["value"] = float(item["best"].reps or 0)
        metric = gettext("Reps")
    elif exercise.mode in (Exercise.Mode.WEIGHT, Exercise.Mode.BODYWEIGHT):
        metric = (
            gettext("Added kg")
            if exercise.mode == Exercise.Mode.BODYWEIGHT
            else gettext("Best weight (kg)")
        )
    elif exercise.mode == Exercise.Mode.ASSISTED:
        metric = gettext("Assistance kg · lower is better")
    elif exercise.mode == Exercise.Mode.DURATION:
        metric = gettext("Best time (seconds)")
    elif exercise.mode == Exercise.Mode.CARDIO:
        metric = gettext("Best distance (km)")
    best = max(sessions, key=lambda item: item["value"]) if sessions else None
    return render(
        request,
        "gym/exercise_progress.html",
        {
            "exercise": exercise,
            "sessions": list(reversed(sessions)),
            "chart": line_chart(sessions),
            "best": best,
            "metric": metric,
        },
    )


def routine_page(
    request: HttpRequest,
    routine: Routine,
    form: RoutineForm | None = None,
    bound_row: RoutineExerciseForm | None = None,
) -> HttpResponse:
    rows = (
        [
            {
                "row": row,
                "form": bound_row
                if bound_row and bound_row.instance.pk == row.pk
                else RoutineExerciseForm(instance=row, prefix=f"row-{row.pk}"),
            }
            for row in routine.exercises.select_related("exercise")
        ]
        if routine.pk
        else []
    )
    return render(
        request,
        "gym/routine.html",
        {
            "routine": routine,
            "form": form
            or RoutineForm(instance=routine, user=cast(User, request.user)),
            "rows": rows,
            "today": timezone.localdate(),
            "choices": exercises_for(cast(User, request.user)).filter(archived=False)
            if not routine.pk
            else [],
            "selection": request.POST.getlist("exercises"),
        },
    )


@login_required
@never_cache
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def routine_edit(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    user = cast(User, request.user)
    if pk:
        safe_pk(pk)
    routine = get_object_or_404(Routine, pk=pk, user=user) if pk else Routine(user=user)
    form = RoutineForm(
        request.POST if request.method == "POST" else None, instance=routine, user=user
    )
    if request.method == "POST" and form.is_valid():
        if (
            not routine.pk
            and Routine.objects.filter(user=user, archived=False).count() >= 100
        ):
            form.add_error(
                None, gettext("Archive a routine before creating more than 100.")
            )
        else:
            with transaction.atomic():
                form.save()
                if pk is None:
                    # Preserve the order the user chose, rather than catalog ordering.
                    chosen = {
                        str(item.pk): item for item in form.cleaned_data["exercises"]
                    }
                    ordered = list(dict.fromkeys(request.POST.getlist("exercises")))
                    RoutineExercise.objects.bulk_create(
                        [
                            RoutineExercise(
                                routine=routine, exercise=chosen[key], position=index
                            )
                            for index, key in enumerate(ordered)
                            if key in chosen
                        ]
                    )
            return redirect("gym:routine", pk=routine.pk)
    return routine_page(request, routine, form)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def routine_delete(request: HttpRequest, pk: int) -> HttpResponse:
    safe_pk(pk)
    routine = get_object_or_404(Routine, pk=pk, user=request.user)
    if request.method == "POST" and request.POST.get("confirm") == "on":
        routine.delete()
        messages.success(
            request, gettext("Routine deleted. Your workout history is kept.")
        )
        return redirect("gym:home")
    return render(request, "gym/routine_delete.html", {"routine": routine})


@login_required
@never_cache
@require_POST
def routine_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    safe_pk(pk)
    user = cast(User, request.user)
    with transaction.atomic():
        User.objects.select_for_update().only("pk").get(pk=user.pk)
        routine = get_object_or_404(Routine, pk=pk, user=user)
        try:
            if action == "add":
                exercise = get_object_or_404(
                    exercises_for(user),
                    pk=integer(request.POST.get("exercise")),
                    archived=False,
                )
                if routine.exercises.count() >= MAX_EXERCISES:
                    raise ValueError(
                        gettext("A routine can contain up to 40 exercises.")
                    )
                if routine.archived:
                    raise ValueError(gettext("Restore this routine before editing it."))
                RoutineExercise.objects.create(
                    routine=routine,
                    exercise=exercise,
                    position=routine.exercises.count(),
                )
            elif action in ("archive", "restore"):
                if (
                    action == "restore"
                    and Routine.objects.filter(user=user, archived=False).count() >= 100
                ):
                    raise ValueError(
                        gettext("Archive a routine before creating more than 100.")
                    )
                routine.archived = action == "archive"
                routine.save(update_fields=["archived", "updated_at"])
            elif action == "duplicate":
                if Routine.objects.filter(user=user, archived=False).count() >= 100:
                    raise ValueError(
                        gettext("Archive a routine before creating more than 100.")
                    )
                duplicate = Routine.objects.create(
                    user=user,
                    name=f"{routine.name[:85]} · {gettext('copy')}",
                    notes=routine.notes,
                )
                fields = (
                    "exercise_id",
                    "position",
                    "sets",
                    "reps",
                    "weight",
                    "seconds",
                    "distance",
                    "rest_seconds",
                    "group",
                    "notes",
                )
                RoutineExercise.objects.bulk_create(
                    [
                        RoutineExercise(
                            routine=duplicate,
                            **{key: getattr(row, key) for key in fields},
                        )
                        for row in routine.exercises.all()
                    ]
                )
                return redirect("gym:routine", pk=duplicate.pk)
            elif action in ("update", "remove", "up", "down"):
                row = get_object_or_404(
                    RoutineExercise,
                    pk=integer(request.POST.get("row")),
                    routine=routine,
                )
                if action == "update":
                    form = RoutineExerciseForm(
                        request.POST, instance=row, prefix=f"row-{row.pk}"
                    )
                    if not form.is_valid():
                        return routine_page(request, routine, bound_row=form)
                    form.save()
                elif action == "remove":
                    row.delete()
                else:
                    reorder(list(routine.exercises.all()), row.pk, action)
            else:
                raise Http404
        except (forms.ValidationError, ValueError) as exc:
            error = (
                str(exc.messages[0])
                if isinstance(exc, forms.ValidationError)
                else str(exc)
            )
            if json_request(request):
                return JsonResponse({"error": error}, status=400)
            messages.error(request, error)
            return redirect("gym:routine", pk=routine.pk)
    if json_request(request):
        return JsonResponse({"ok": True})
    return redirect("gym:routine", pk=routine.pk)


def reorder(rows: Any, pk: int, direction: str) -> None:
    index = next(i for i, row in enumerate(rows) if row.pk == pk)
    other = index - 1 if direction == "up" else index + 1
    if 0 <= other < len(rows):
        rows[index], rows[other] = rows[other], rows[index]
    for position, row in enumerate(rows):
        row.position = position
    if rows:
        type(rows[0]).objects.bulk_update(rows, ["position"])


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def start(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    form = StartForm(
        request.POST if request.method == "POST" else None,
        user=user,
        initial={"routine": request.GET.get("routine")},
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            User.objects.select_for_update().only("pk").get(pk=user.pk)
            session = start_workout(user, form.cleaned_data)
        return redirect("gym:workout", pk=session.pk)
    return render(request, "gym/start.html", {"form": form})


def workout_page(
    request: HttpRequest, session: Workout, bound_form: Any = None
) -> HttpResponse:
    prefetch_related_objects([session], "exercises__exercise", "exercises__sets")
    rows = list(session.exercises.all())
    previous = previous_exercises(
        cast(User, request.user), [row.exercise_id for row in rows], exclude=session
    )
    cards = []
    for row in rows:
        last = previous.get(row.exercise_id)
        last_sets = list(last.sets.all()) if last else []
        sets = []
        for index, entry in enumerate(row.sets.all()):
            sets.append(
                {
                    "entry": entry,
                    "form": bound_form
                    if isinstance(bound_form, SetForm)
                    and bound_form.instance.pk == entry.pk
                    else SetForm(instance=entry, prefix=f"set-{entry.pk}"),
                    "previous": last_sets[index] if index < len(last_sets) else None,
                }
            )
        cards.append(
            {
                "row": row,
                "sets": sets,
                "options": WorkoutExerciseForm(
                    instance=row, prefix=f"exercise-{row.pk}"
                ),
            }
        )
    return render(
        request,
        "gym/workout.html",
        {
            "session": session,
            "cards": cards,
            "state": workout_state(session),
            "totals": workout_totals(session),
            "metadata_form": bound_form
            if isinstance(bound_form, WorkoutForm)
            else WorkoutForm(instance=session),
            "bodyweight_form": bound_form
            if isinstance(bound_form, BodyweightForm)
            else BodyweightForm(instance=session),
            "bodyweight_error": isinstance(bound_form, BodyweightForm),
            "bodyweight_fallback": BodyweightForm(
                instance=session, auto_id="fallback_%s"
            ),
        },
    )


@login_required
@never_cache
@require_GET
def workout(request: HttpRequest, pk: UUID) -> HttpResponse:
    return workout_page(
        request,
        get_object_or_404(Workout, pk=pk, user=request.user, archived_at__isnull=True),
    )


@login_required
@never_cache
@sensitive_post_parameters()
@sensitive_variables()
@require_POST
def workout_action(request: HttpRequest, pk: UUID) -> HttpResponse:
    user = cast(User, request.user)
    destination = ""
    reload_page = False
    bound: SetForm | BodyweightForm | WorkoutForm | None = None
    with transaction.atomic():
        User.objects.select_for_update().only("pk").get(pk=user.pk)
        session = get_object_or_404(Workout, pk=pk, user=user)
        action = request.POST.get("action", "")
        if session.archived_at and action != "restore":
            raise Http404
        try:
            version = integer(request.POST.get("version"), minimum=0)
            if version != session.version:
                if json_request(request):
                    return JsonResponse(
                        {
                            "error": gettext(
                                "This workout changed in another tab. Reload it before saving more changes."
                            ),
                            "conflict": True,
                        },
                        status=409,
                    )
                messages.error(
                    request,
                    gettext(
                        "This workout changed in another tab. Reload it before saving more changes."
                    ),
                )
                return redirect("gym:workout", pk=pk)
            if action == "set":
                entry = get_object_or_404(
                    WorkoutSet.objects.select_related("workout_exercise__workout"),
                    pk=integer(request.POST.get("set")),
                    workout_exercise__workout=session,
                )
                was_done = entry.completed_at is not None
                bound = SetForm(request.POST, instance=entry, prefix=f"set-{entry.pk}")
                if not bound.is_valid():
                    raise ValueError(form_error(bound))
                entry = bound.save(commit=False)
                done = bound.cleaned_data["completed"]
                if (
                    session.status == "finished"
                    and was_done
                    and not done
                    and session.exercises.filter(
                        sets__completed_at__isnull=False
                    ).count()
                    == 1
                ):
                    raise ValueError(
                        gettext(
                            "Keep one completed set, or archive this workout instead."
                        )
                    )
                entry.completed_at = (
                    (entry.completed_at or timezone.now()) if done else None
                )
                entry.save()
                if done and not was_done and session.status == "active":
                    session.rest_ends_at = (
                        timezone.now()
                        + timedelta(seconds=entry.workout_exercise.rest_seconds)
                        if entry.workout_exercise.rest_seconds
                        else None
                    )
                    session.rest_remaining = None
            elif action == "add_exercise":
                exercise = get_object_or_404(
                    exercises_for(user),
                    pk=integer(request.POST.get("exercise")),
                    archived=False,
                )
                add_exercise(session, exercise)
                reload_page = True
            elif action in (
                "add_set",
                "remove_exercise",
                "exercise_options",
                "up",
                "down",
            ):
                row = get_object_or_404(
                    WorkoutExercise,
                    pk=integer(request.POST.get("row")),
                    workout=session,
                )
                if action == "add_set":
                    if row.sets.count() >= MAX_SETS:
                        raise ValueError(
                            gettext("An exercise can contain up to 30 sets.")
                        )
                    last = row.sets.last()
                    position = last.position + 1 if last else 0
                    WorkoutSet.objects.create(
                        workout_exercise=row,
                        position=position,
                        **{
                            field: getattr(last, field) if last else None
                            for field in ("weight", "reps", "seconds", "distance")
                        },
                    )
                elif action == "remove_exercise":
                    if (
                        session.status == "finished"
                        and not WorkoutSet.objects.filter(
                            workout_exercise__workout=session,
                            completed_at__isnull=False,
                        )
                        .exclude(workout_exercise=row)
                        .exists()
                    ):
                        raise ValueError(
                            gettext(
                                "Keep one completed set, or archive this workout instead."
                            )
                        )
                    row.delete()
                elif action == "exercise_options":
                    options = WorkoutExerciseForm(
                        request.POST, instance=row, prefix=f"exercise-{row.pk}"
                    )
                    if not options.is_valid():
                        raise ValueError(form_error(options))
                    options.save()
                else:
                    reorder(list(session.exercises.all()), row.pk, action)
                reload_page = True
            elif action == "remove_set":
                entry = get_object_or_404(
                    WorkoutSet,
                    pk=integer(request.POST.get("set")),
                    workout_exercise__workout=session,
                )
                if (
                    session.status == "finished"
                    and not WorkoutSet.objects.filter(
                        workout_exercise__workout=session, completed_at__isnull=False
                    )
                    .exclude(pk=entry.pk)
                    .exists()
                ):
                    raise ValueError(
                        gettext(
                            "Keep one completed set, or archive this workout instead."
                        )
                    )
                entry.delete()
                reload_page = True
            elif action == "bodyweight":
                bound = BodyweightForm(request.POST, instance=session)
                if not bound.is_valid():
                    raise ValueError(form_error(bound))
                session.bodyweight = bound.cleaned_data["bodyweight"]
                if session.exercises.filter(
                    mode=Exercise.Mode.ASSISTED,
                    sets__completed_at__isnull=False,
                    sets__weight__gt=session.bodyweight,
                ).exists():
                    raise ValueError(
                        gettext(
                            "A completed assisted set has more assistance than this bodyweight. Correct that set first."
                        )
                    )
                session.bodyweight_date = session.date
                if bound.cleaned_data["save_weigh_in"]:
                    WeightEntry.objects.update_or_create(
                        user=user,
                        date=session.date,
                        defaults={"weight": session.bodyweight},
                    )
                reload_page = True
            elif action == "metadata":
                bound = WorkoutForm(request.POST, instance=session)
                if not bound.is_valid():
                    raise ValueError(form_error(bound))
                session = bound.save(commit=False)
                duration = bound.cleaned_data.get("duration")
                if duration and session.finished_at:
                    session.started_at = session.finished_at - timedelta(
                        seconds=duration
                    )
                    session.paused_seconds = 0
                    session.paused_at = None
                reload_page = True
            elif action in ("pause", "resume", "finish", "timer"):
                if session.status != "active":
                    raise ValueError(gettext("This workout is already finished."))
                now = timezone.now()
                if action == "pause" and session.paused_at is None:
                    session.paused_at = now
                elif action == "resume" and session.paused_at:
                    session.paused_seconds += max(
                        0, int((now - session.paused_at).total_seconds())
                    )
                    session.paused_at = None
                elif action == "finish":
                    totals = workout_totals(session)
                    if not totals["completed"]:
                        raise ValueError(
                            gettext(
                                "Complete at least one set before finishing your workout."
                            )
                        )
                    if (
                        totals["remaining"]
                        and request.POST.get("confirm_unfinished") != "on"
                    ):
                        raise ValueError(
                            gettext(
                                "Some sets are unfinished. Complete them, or confirm that you want to finish without them."
                            )
                        )
                    if session.paused_at:
                        session.paused_seconds += max(
                            0, int((now - session.paused_at).total_seconds())
                        )
                    session.paused_at = None
                    session.status = Workout.Status.FINISHED
                    session.finished_at = now
                    session.rest_ends_at = None
                    session.rest_remaining = None
                    reload_page = True
                elif action == "timer":
                    seconds = integer(
                        request.POST.get("seconds", 90), maximum=1800, minimum=-1800
                    )
                    timer_action(session, request.POST.get("command", ""), seconds)
            elif action in ("archive", "restore"):
                if action == "restore":
                    if (
                        session.status == "active"
                        and Workout.objects.filter(
                            user=user, status="active", archived_at__isnull=True
                        )
                        .exclude(pk=session.pk)
                        .exists()
                    ):
                        raise ValueError(
                            gettext(
                                "Finish your current workout before restoring this one."
                            )
                        )
                    session.archived_at = None
                else:
                    session.archived_at = timezone.now()
                    if session.status == "active" and not session.paused_at:
                        session.paused_at = timezone.now()
                    session.rest_ends_at = None
                    session.rest_remaining = None
                destination = (
                    reverse("gym:history")
                    if session.archived_at
                    else reverse("gym:workout", args=[session.pk])
                )
            elif action == "save_routine":
                if Routine.objects.filter(user=user, archived=False).count() >= 100:
                    raise ValueError(
                        gettext("Archive a routine before creating more than 100.")
                    )
                routine = Routine.objects.create(
                    user=user, name=session.name, notes=session.notes[:1000]
                )
                for row in session.exercises.prefetch_related("sets"):
                    all_sets = list(row.sets.all())
                    done = [
                        entry
                        for entry in all_sets
                        if entry.completed_at and entry.kind != "warmup"
                    ]
                    source = done or all_sets
                    last = source[-1] if source else None
                    RoutineExercise.objects.create(
                        routine=routine,
                        exercise=row.exercise,
                        position=row.position,
                        sets=min(20, len(source) or 3),
                        reps=last.reps if last and last.reps else 10,
                        weight=last.weight if last else None,
                        seconds=last.seconds if last and last.seconds else 60,
                        distance=last.distance if last else None,
                        rest_seconds=row.rest_seconds,
                        group=row.group,
                        notes=row.notes,
                    )
                destination = reverse("gym:routine", args=[routine.pk])
            else:
                raise Http404
            session.version += 1
            session.save()
        except (forms.ValidationError, ValueError) as exc:
            error = (
                str(exc.messages[0])
                if isinstance(exc, forms.ValidationError)
                else str(exc)
            )
            if json_request(request):
                return JsonResponse(
                    {
                        "error": error,
                        "fields": dict(bound.errors) if bound is not None else {},
                        "needs_weight": session.bodyweight is None
                        and isinstance(bound, SetForm)
                        and bound.instance.workout_exercise.mode
                        in (Exercise.Mode.BODYWEIGHT, Exercise.Mode.ASSISTED),
                    },
                    status=400,
                )
            messages.error(request, error)
            return workout_page(request, session, bound)
    if json_request(request):
        return JsonResponse(
            {
                "state": workout_state(session),
                "reload": reload_page,
                "redirect": destination,
            }
        )
    return redirect(destination or reverse("gym:workout", args=[pk]))


@login_required
@never_cache
@require_GET
def history(request: HttpRequest) -> HttpResponse:
    archived = request.GET.get("archived") == "1"
    queryset = Workout.objects.filter(
        user=cast(User, request.user), archived_at__isnull=not archived
    ).prefetch_related("exercises__sets")
    if not archived:
        queryset = queryset.filter(status="finished")
    query = request.GET.get("q", "")[:100].strip()
    if query:
        queryset = queryset.filter(name__icontains=query)
    return render(
        request,
        "gym/history.html",
        {
            "page": Paginator(queryset, 20).get_page(request.GET.get("page")),
            "archived": archived,
            "query": query,
            "params": urlencode({"q": query, "archived": "1" if archived else "0"}),
        },
    )


@login_required
@never_cache
@require_GET
def progress(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    days = {"28": 28, "84": 84, "365": 365}.get(request.GET.get("days", "28"), 28)
    ids = (
        completed_sets(user)
        .values_list("workout_exercise__exercise_id", flat=True)
        .distinct()
    )
    exercises = exercises_for(user).filter(pk__in=ids)
    return render(
        request,
        "gym/progress.html",
        {"summary": training_summary(user, days), "exercises": exercises, "days": days},
    )


@login_required
@never_cache
@require_GET
def export(request: HttpRequest) -> StreamingHttpResponse:
    class Echo:
        def write(self, value: str) -> str:
            return value

    writer = csv.writer(Echo())
    queryset = (
        WorkoutSet.objects.filter(
            workout_exercise__workout__user=cast(User, request.user),
            workout_exercise__workout__status="finished",
            workout_exercise__workout__archived_at__isnull=True,
            completed_at__isnull=False,
        )
        .select_related("workout_exercise__workout")
        .order_by(
            "workout_exercise__workout__date",
            "workout_exercise__workout_id",
            "workout_exercise__position",
            "position",
        )
    )

    def clean_cell(value: Any) -> Any:
        return (
            "'" + value
            if isinstance(value, str)
            and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
            else value
        )

    def content() -> Any:
        yield writer.writerow(
            [
                "date",
                "workout",
                "exercise",
                "mode",
                "set_type",
                "weight_kg",
                "reps",
                "time_seconds",
                "distance_km",
                "rpe",
                "bodyweight_kg",
            ]
        )
        for entry in queryset.iterator(chunk_size=1000):
            row = entry.workout_exercise
            values = [
                row.workout.date,
                row.workout.name,
                row.name,
                row.mode,
                entry.kind,
                entry.weight,
                entry.reps,
                entry.seconds,
                entry.distance,
                entry.rpe,
                row.workout.bodyweight,
            ]
            yield writer.writerow([clean_cell(value) for value in values])

    response = StreamingHttpResponse(content(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="cotinga-workouts.csv"'
    return response
