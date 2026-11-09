import json
import uuid
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from accounts.models import User
from tracker import openrouter
from tracker.credentials import CredentialError, decrypt_key
from tracker.localization import localized_text, translation_language

from . import ai
from .ai_forms import (
    RoutineDayForm,
    RoutineRequestForm,
    SuggestedExerciseForm,
    SuggestedExerciseSet,
)
from .models import Exercise, Routine, RoutineExercise, RoutineSuggestion
from .services import exercises_for

ExerciseFormSet: Any = forms.formset_factory(
    SuggestedExerciseForm,
    formset=SuggestedExerciseSet,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
    max_num=10,
    validate_max=True,
    absolute_max=10,
)


def available_exercises(user: User, data: dict[str, Any]) -> list[Exercise]:
    allowed_equipment = [Exercise.Equipment.BODYWEIGHT, *data.get("equipment", [])]
    queryset = exercises_for(user).filter(
        archived=False, equipment__in=allowed_equipment
    )
    if not data.get("include_custom"):
        queryset = queryset.filter(owner__isnull=True)
    return [
        exercise
        for exercise in queryset[:500]
        if ai.equipment_requirements(exercise).issubset(set(allowed_equipment))
    ]


def reserve(
    user: User, data: dict[str, Any], submission: UUID, model: str, language: str
) -> tuple[RoutineSuggestion, bool]:
    with transaction.atomic():
        User.objects.select_for_update().only("pk").get(pk=user.pk)
        existing = RoutineSuggestion.objects.filter(
            user=user, submission=submission
        ).first()
        if existing:
            return existing, False
        now = timezone.now()
        recent = RoutineSuggestion.objects.filter(user=user)
        if (
            recent.filter(created_at__gte=now - timedelta(seconds=20)).exists()
            or recent.filter(
                status="processing", created_at__gte=now - timedelta(minutes=2)
            ).exists()
        ):
            raise openrouter.OpenRouterError(
                gettext(
                    "A plan was just requested. Wait a moment before generating another."
                )
            )
        if recent.filter(created_at__date=timezone.localdate()).count() >= 20:
            raise openrouter.OpenRouterError(
                gettext(
                    "You have reached today's 20-plan limit. You can still build and edit routines manually."
                )
            )
        if (
            Routine.objects.filter(user=user, archived=False).count() + data["days"]
            > 100
        ):
            raise openrouter.OpenRouterError(
                gettext("Archive some routines before adding this weekly plan.")
            )
        return RoutineSuggestion.objects.create(
            user=user,
            submission=submission,
            original_input=data,
            model=model,
            language=language,
        ), True


@login_required
@never_cache
@sensitive_post_parameters()
@sensitive_variables()
@require_http_methods(["GET", "POST"])
def generate(request: HttpRequest) -> HttpResponse:
    from tracker.models import OpenRouterSettings

    user = cast(User, request.user)
    preferences = OpenRouterSettings.objects.filter(user=user).first()
    latest = RoutineSuggestion.objects.filter(user=user).first()
    initial = dict(latest.original_input) if latest else {"equipment": ["dumbbell"]}
    initial["submission"] = uuid.uuid4()
    form = RoutineRequestForm(
        request.POST if request.method == "POST" else None, initial=initial
    )
    connected = bool(preferences and preferences.has_key and preferences.model)
    if request.method == "POST" and form.is_valid():
        batch = None
        try:
            if not connected or preferences is None:
                raise openrouter.OpenRouterError(
                    gettext(
                        "Connect OpenRouter and choose a model in AI settings first."
                    )
                )
            data = {
                name: value
                for name, value in form.cleaned_data.items()
                if name != "submission"
            }
            catalog = available_exercises(user, data)
            if not catalog:
                raise openrouter.OpenRouterError(
                    gettext(
                        "No exercises match this equipment. Choose different equipment or add a custom exercise."
                    )
                )
            batch, created = reserve(
                user,
                data,
                uuid.uuid5(
                    form.cleaned_data["submission"],
                    json.dumps(data, sort_keys=True)
                    + preferences.model
                    + (translation_language() or "en"),
                ),
                preferences.model,
                translation_language() or "en",
            )
            if not created:
                return redirect("gym:ai-review", pk=batch.pk)
            key = decrypt_key(preferences.encrypted_api_key)
            model = openrouter.find_model(preferences.model)
            result = ai.suggest_plan(key, model, data, catalog, batch.language)
            batch.result = result
            batch.status = RoutineSuggestion.Status.READY
            batch.save(update_fields=["result", "status"])
            return redirect("gym:ai-review", pk=batch.pk)
        except (openrouter.OpenRouterError, CredentialError) as exc:
            if batch:
                batch.status = RoutineSuggestion.Status.FAILED
                batch.save(update_fields=["status"])
                updated = request.POST.copy()
                updated["submission"] = str(uuid.uuid4())
                form = RoutineRequestForm(updated)
                form.is_valid()
            form.add_error(None, str(exc))
    return render(
        request,
        "gym/ai_generate.html",
        {
            "form": form,
            "preferences": preferences,
            "connected": connected,
            "latest": latest,
        },
    )


def _localized(row: dict[str, Any], name: str) -> str:
    return localized_text(
        str(row.get(name, "")),
        str(row.get(f"{name}_fa", "")),
        str(row.get(f"{name}_ka", "")),
    )


def _cards(
    request: HttpRequest, batch: RoutineSuggestion, catalog: list[Exercise]
) -> list[dict[str, Any]]:
    by_id = {exercise.pk: exercise for exercise in catalog}
    days = []
    for index, day in enumerate(batch.result["routines"]):
        meta = RoutineDayForm(
            request.POST if request.method == "POST" else None,
            prefix=f"day-{index}",
            initial={
                "keep": True,
                "name": _localized(day, "name"),
                "notes": _localized(day, "notes"),
            },
        )
        instances = [
            RoutineExercise(exercise=by_id[row["exercise_id"]])
            for row in day["exercises"]
        ]
        formset = ExerciseFormSet(
            request.POST if request.method == "POST" else None,
            prefix=f"moves-{index}",
            initial=[
                {**row, "reps": row["reps"] or 10, "seconds": row["seconds"] or 60}
                for row in day["exercises"]
            ],
            exercises=[instance.exercise for instance in instances],
        )
        days.append(
            {
                "source": day,
                "meta": meta,
                "formset": formset,
                "rows": [
                    {
                        "form": form,
                        "exercise": instances[
                            min(position, len(instances) - 1)
                        ].exercise,
                        "note": _localized(
                            day["exercises"][min(position, len(instances) - 1)], "notes"
                        ),
                    }
                    for position, form in enumerate(formset.forms)
                ],
                "title": _localized(day, "name"),
            }
        )
    return days


def source_context(batch: RoutineSuggestion) -> dict[str, Any]:
    equipment = dict(RoutineRequestForm.base_fields["equipment"].choices)  # type: ignore[attr-defined]
    experience = dict(RoutineRequestForm.base_fields["experience"].choices)  # type: ignore[attr-defined]
    return {
        "source_equipment": [
            equipment.get(key, key) for key in batch.original_input.get("equipment", [])
        ],
        "source_experience": experience.get(batch.original_input.get("experience"), ""),
        "processing_stale": batch.created_at < timezone.now() - timedelta(minutes=2),
        "saved_routines": batch.routines.order_by("pk"),
        "summary": _localized(batch.result, "summary"),
        "schedule": _localized(batch.result, "schedule"),
    }


@login_required
@never_cache
@sensitive_post_parameters()
@sensitive_variables()
@require_http_methods(["GET", "POST"])
def review(request: HttpRequest, pk: UUID) -> HttpResponse:
    user = cast(User, request.user)
    batch = get_object_or_404(RoutineSuggestion, pk=pk, user=user)
    if batch.status != RoutineSuggestion.Status.READY:
        return render(
            request, "gym/ai_status.html", {"batch": batch, **source_context(batch)}
        )
    catalog = available_exercises(user, batch.original_input)
    ids = {exercise.pk for exercise in catalog}
    if any(
        row["exercise_id"] not in ids
        for day in batch.result["routines"]
        for row in day["exercises"]
    ):
        messages.error(
            request,
            gettext(
                "An exercise in this suggestion is no longer available. Generate a fresh plan with the current library."
            ),
        )
        return redirect("gym:ai-generate")
    days = _cards(request, batch, catalog)
    error = ""
    if request.method == "POST":
        valid = True
        kept = []
        for day in days:
            if not day["meta"].is_valid():
                valid = False
            elif day["meta"].cleaned_data["keep"]:
                kept.append(day)
                if not day["formset"].is_valid():
                    valid = False
                if int(
                    day["formset"].management_form.cleaned_data.get("TOTAL_FORMS", 0)
                ) > len(day["source"]["exercises"]):
                    valid = False
                    error = gettext("The suggestion changed. Reload it before saving.")
        if not kept:
            valid = False
            error = gettext("Choose at least one routine to save.")
        if valid:
            with transaction.atomic():
                User.objects.select_for_update().only("pk").get(pk=user.pk)
                current = RoutineSuggestion.objects.select_for_update().get(
                    pk=batch.pk, user=user
                )
                if current.status != RoutineSuggestion.Status.READY:
                    return redirect("gym:ai-review", pk=batch.pk)
                if (
                    Routine.objects.filter(user=user, archived=False).count()
                    + len(kept)
                    > 100
                ):
                    error = gettext(
                        "Archive some routines before adding this weekly plan."
                    )
                else:
                    for day in kept:
                        source = day["source"]
                        names = {
                            field: source.get(field, "")
                            for field in (
                                "name",
                                "name_fa",
                                "name_ka",
                                "notes",
                                "notes_fa",
                                "notes_ka",
                            )
                        }
                        language = translation_language()
                        for field in ("name", "notes"):
                            names[f"{field}_{language}" if language else field] = day[
                                "meta"
                            ].cleaned_data[field]
                        routine = Routine.objects.create(
                            user=user, ai_suggestion=current, **names
                        )
                        for index, form in enumerate(day["formset"].forms):
                            if form.cleaned_data.get("DELETE"):
                                continue
                            movement = form.save(commit=False)
                            movement.routine = routine
                            movement.position = index
                            movement.notes = source["exercises"][index]["notes"]
                            movement.weight = None
                            movement.save()
                    current.status = RoutineSuggestion.Status.SAVED
                    current.saved_at = timezone.now()
                    current.save(update_fields=["status", "saved_at"])
                    messages.success(
                        request,
                        gettext(
                            "Your routines are ready. Open any routine to start training or make changes."
                        ),
                    )
                    return redirect("gym:ai-review", pk=batch.pk)
    for index, day in enumerate(days):
        day["open"] = index == 0 or (
            request.method == "POST"
            and (
                bool(day["meta"].errors)
                or (
                    day["meta"].cleaned_data.get("keep")
                    and (
                        any(day["formset"].errors)
                        or bool(day["formset"].non_form_errors())
                    )
                )
            )
        )
    return render(
        request,
        "gym/ai_review.html",
        {
            "batch": batch,
            "days": days,
            "summary": _localized(batch.result, "summary"),
            "schedule": _localized(batch.result, "schedule"),
            "error": error,
            **source_context(batch),
        },
    )
